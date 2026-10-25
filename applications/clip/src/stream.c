/*
 * Live Opus stream over BLE (docs/stream_protocol.md).
 *
 * The audio thread copies each encoded record into a bounded ring and never
 * waits for Bluetooth. One sender thread turns session state plus the ring
 * into START, DATA and END notifications, so END cannot overtake DATA. Any
 * loss leaves the session to the SD file, which is always written.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/ring_buffer.h>

#include "audio.h"
#include "ble.h"
#include "stream.h"

LOG_MODULE_REGISTER(stream, CONFIG_CLIP_LOG_LEVEL);

#define STREAM_VERSION    1
#define STREAM_CODEC_OPUS 1    /* SD .opus layout: u16 LE length + packet */
#define STREAM_RING_SIZE  8192 /* ~2 s of 32 kbps Opus: BLE stall tolerance */
#define PKT_MAX           244  /* one LE data PDU with DLE (251 - 4 - 3) */
#define HDR               5    /* type u8 + seq u32 LE */
#define START_LEN         20   /* fits ATT MTU 23 */
#define END_LEN           18

enum { PKT_START = 1, PKT_DATA = 2, PKT_END = 3 };
enum { ST_IDLE, ST_CAPTURE, ST_DRAIN };

RING_BUF_DECLARE(stream_ring, STREAM_RING_SIZE);
static struct k_spinlock lock;
static K_SEM_DEFINE(kick, 0, 1);

static struct {
	uint8_t state;
	bool subscribed;
	bool started;  /* START handed to the sender */
	bool overflow; /* a record did not fit: stream incomplete */
	uint8_t kind;
	uint8_t channels;
	uint32_t gen;  /* bumped per streamed session */
	uint32_t seq;
	uint32_t frames;
	uint32_t bytes;
	uint32_t crc;
	uint64_t session;
} s;

void stream_set_subscribed(bool on)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	s.subscribed = on;
	if (!on) {
		s.state = ST_IDLE;
		ring_buf_reset(&stream_ring);
	}
	k_spin_unlock(&lock, key);
}

void stream_begin(const char *session_id, uint8_t kind, uint8_t channels)
{
	uint64_t id = 0;
	bool busy, live;

	/* Session IDs are 14 decimal digits; sent as an integer so START fits
	 * a 23-byte MTU. */
	for (const char *c = session_id; *c >= '0' && *c <= '9'; c++) {
		id = id * 10U + (uint64_t)(*c - '0');
	}

	k_spinlock_key_t key = k_spin_lock(&lock);

	busy = s.subscribed && s.state != ST_IDLE;
	live = s.subscribed && !busy;
	if (live) {
		ring_buf_reset(&stream_ring);
		s.state = ST_CAPTURE;
		s.started = false;
		s.overflow = false;
		s.kind = kind;
		s.channels = channels;
		s.gen++;
		s.seq = 0;
		s.frames = 0;
		s.bytes = 0;
		s.crc = 0;
		s.session = id;
	}
	k_spin_unlock(&lock, key);

	if (busy) {
		/* Previous session still draining: this one goes by file. */
		LOG_WRN("stream busy, %s by file", session_id);
	}
	if (live) {
		k_sem_give(&kick);
	}
}

void stream_push(const uint8_t *frame, uint16_t len)
{
	uint8_t prefix[2];
	uint32_t crc;
	bool live;

	sys_put_le16(len, prefix);
	/* s.crc is written only by the audio thread (here, stream_begin), so
	 * the CRC runs outside the interrupt lock. */
	crc = crc32_ieee_update(crc32_ieee_update(s.crc, prefix, 2), frame, len);

	k_spinlock_key_t key = k_spin_lock(&lock);

	live = s.state == ST_CAPTURE;
	if (live) {
		s.frames++;
		s.bytes += 2U + len;
		s.crc = crc;
		if (!s.overflow && ring_buf_space_get(&stream_ring) >= 2U + len) {
			ring_buf_put(&stream_ring, prefix, 2);
			ring_buf_put(&stream_ring, frame, len);
		} else {
			s.overflow = true;
		}
	}
	k_spin_unlock(&lock, key);
	if (live) {
		k_sem_give(&kick);
	}
}

void stream_end(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	bool live = s.state == ST_CAPTURE;

	if (live) {
		s.state = ST_DRAIN;
	}
	k_spin_unlock(&lock, key);
	if (live) {
		k_sem_give(&kick);
	}
}

/* Builds the next packet in session order into p (at most max bytes);
 * returns its length, or 0 when nothing is due. */
static size_t next_packet(uint8_t *p, size_t max, uint32_t *gen)
{
	size_t n = 0;
	k_spinlock_key_t key = k_spin_lock(&lock);

	if (s.state == ST_IDLE) {
		/* nothing streaming */
	} else if (!s.started) {
		p[0] = PKT_START;
		p[5] = STREAM_VERSION;
		p[6] = s.kind;
		p[7] = STREAM_CODEC_OPUS;
		p[8] = s.channels;
		p[9] = AUDIO_FRAME_MS;
		sys_put_le16(AUDIO_SAMPLE_RATE, &p[10]);
		sys_put_le64(s.session, &p[12]);
		n = START_LEN;
		s.started = true;
	} else if (!ring_buf_is_empty(&stream_ring)) {
		p[0] = PKT_DATA;
		n = HDR + ring_buf_get(&stream_ring, &p[HDR], max - HDR);
	} else if (s.state == ST_DRAIN) {
		p[0] = PKT_END;
		p[5] = s.overflow ? 1 : 0;
		sys_put_le32(s.frames, &p[6]);
		sys_put_le32(s.bytes, &p[10]);
		sys_put_le32(s.crc, &p[14]);
		n = END_LEN;
		s.state = ST_IDLE;
	}
	if (n) {
		sys_put_le32(s.seq++, &p[1]);
		*gen = s.gen;
	}
	k_spin_unlock(&lock, key);
	return n;
}

/* Sends every packet that is due, in order; returns when none is. */
static void stream_drain(void)
{
	static uint8_t pkt[PKT_MAX];

	while (true) {
		struct bt_conn *conn = ble_conn_ref();
		uint32_t gen = 0;
		size_t n = 0;
		int err = 0;

		if (conn) {
			n = next_packet(pkt, MIN(bt_gatt_get_mtu(conn) - 3U,
						 sizeof(pkt)), &gen);
			if (n) {
				/* Blocks only this thread, until an ATT
				 * buffer frees or the link drops. */
				err = ble_stream_notify(conn, pkt, n);
			}
			bt_conn_unref(conn);
		}
		if (!n) {
			return;
		}
		if (err) {
			/* No END follows: the relay uses the file. */
			k_spinlock_key_t key = k_spin_lock(&lock);

			if (s.gen == gen) {
				s.state = ST_IDLE;
				ring_buf_reset(&stream_ring);
			}
			k_spin_unlock(&lock, key);
			LOG_WRN("stream notify %d, by file", err);
		}
	}
}

static void stream_sender(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	while (true) {
		k_sem_take(&kick, K_FOREVER);
		stream_drain();
	}
}

K_THREAD_DEFINE(stream_tid, 1536, stream_sender, NULL, NULL, NULL, 4, 0, 0);
