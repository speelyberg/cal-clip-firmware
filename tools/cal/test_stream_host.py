"""Host check of the live-stream framing in applications/clip/src/stream.c.

Compiles stream.c against tiny Zephyr/BLE stubs, drives sessions through
stream_begin/push/end and stream_drain() (the sender thread's loop body),
and checks the packets against docs/stream_protocol.md: START first, seq
without gaps, DATA joining to the SD record stream, END counts/CRC/overflow,
sizing for small MTUs, and no END after a fault.
"""
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib

APP = os.path.join(os.path.dirname(__file__), "..", "..", "applications", "clip")

STUB = r"""
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define ARG_UNUSED(x) (void)(x)
#define K_FOREVER 0
#define K_SEM_DEFINE(n, i, m) struct k_sem n = { i }
#define K_THREAD_DEFINE(name, size, entry, ...) void (*name)(void *, void *, void *) = entry
#define LOG_MODULE_REGISTER(...)
#define LOG_WRN(...) do { } while (0)
struct k_spinlock { int unused; };
typedef int k_spinlock_key_t;
struct k_sem { int count; };
struct bt_conn;
static inline k_spinlock_key_t k_spin_lock(struct k_spinlock *l) { (void)l; return 0; }
static inline void k_spin_unlock(struct k_spinlock *l, k_spinlock_key_t k) { (void)l; (void)k; }
static inline void k_sem_give(struct k_sem *s) { s->count = 1; }
static inline int k_sem_take(struct k_sem *s, int t) { (void)t; s->count = 0; return 0; }
static inline void sys_put_le16(uint16_t v, uint8_t *d) { d[0] = v; d[1] = v >> 8; }
static inline void sys_put_le32(uint32_t v, uint8_t *d) { sys_put_le16(v, d); sys_put_le16(v >> 16, d + 2); }
static inline void sys_put_le64(uint64_t v, uint8_t *d) { sys_put_le32(v, d); sys_put_le32(v >> 32, d + 4); }
static inline uint32_t crc32_ieee_update(uint32_t crc, const uint8_t *d, size_t n)
{
	crc = ~crc;
	while (n--) {
		crc ^= *d++;
		for (int b = 0; b < 8; b++) crc = (crc >> 1) ^ (0xEDB88320u & -(crc & 1u));
	}
	return ~crc;
}
struct ring_buf { uint8_t *buf; uint32_t size, head, len; };
#define RING_BUF_DECLARE(name, sz) static uint8_t name##_d[sz]; struct ring_buf name = { name##_d, sz, 0, 0 }
static inline void ring_buf_reset(struct ring_buf *r) { r->head = r->len = 0; }
static inline bool ring_buf_is_empty(const struct ring_buf *r) { return r->len == 0; }
static inline uint32_t ring_buf_space_get(const struct ring_buf *r) { return r->size - r->len; }
static inline uint32_t ring_buf_put(struct ring_buf *r, const uint8_t *d, uint32_t n)
{
	for (uint32_t i = 0; i < n; i++) r->buf[(r->head + r->len++) % r->size] = d[i];
	return n;
}
static inline uint32_t ring_buf_get(struct ring_buf *r, uint8_t *d, uint32_t n)
{
	n = MIN(n, r->len);
	for (uint32_t i = 0; i < n; i++) d[i] = r->buf[(r->head + i) % r->size];
	r->head = (r->head + n) % r->size;
	r->len -= n;
	return n;
}
uint16_t bt_gatt_get_mtu(struct bt_conn *c);
void bt_conn_unref(struct bt_conn *c);
"""

HARNESS = r"""
#include <stdio.h>
#include "stream.c"

static uint16_t mtu = 247;
static int sent, fail_at = -1, conn_up = 1;
static int dummy;

struct bt_conn *ble_conn_ref(void) { return conn_up ? (struct bt_conn *)&dummy : NULL; }
void bt_conn_unref(struct bt_conn *c) { (void)c; }
uint16_t bt_gatt_get_mtu(struct bt_conn *c) { (void)c; return mtu; }
int ble_stream_notify(struct bt_conn *c, const uint8_t *d, uint16_t n)
{
	(void)c;
	if (sent++ == fail_at) return -128;
	printf("P ");
	for (int i = 0; i < n; i++) printf("%02x", d[i]);
	printf("\n");
	return 0;
}

static void rec(int i)
{
	static uint8_t b[400];
	int n = (i * 37) % 301 + 1;

	for (int j = 0; j < n; j++) b[j] = (uint8_t)(i * 7 + j);
	stream_push(b, (uint16_t)n);
}

static void session(const char *id, int count, int drain_every)
{
	stream_begin(id, STREAM_KIND_UNSPECIFIED, 1);
	for (int i = 0; i < count; i++) {
		rec(i);
		if (drain_every && i % drain_every == 0) stream_drain();
	}
	stream_end();
	stream_drain();
}

int main(void)
{
	const uint16_t mtus[] = { 23, 24, 247, 517 };

	stream_set_subscribed(true);
	for (int k = 0; k < 4; k++) {
		mtu = mtus[k];
		printf("S complete-%u\n", mtu);
		session("20261024123456", 150, 7);
	}
	mtu = 247;
	printf("S empty\n");
	session("00000000012345", 0, 0);
	printf("S overflow\n");
	session("20261024123457", 200, 0);       /* nothing drained during capture */
	printf("S notify-fail\n");
	sent = 0;
	fail_at = 3;
	session("20261024123458", 20, 1);
	fail_at = -1;
	printf("S after-fail\n");
	session("20261024123459", 5, 1);
	printf("S unsub-mid\n");
	stream_begin("20261024123500", 0, 1);
	rec(0);
	stream_drain();
	stream_set_subscribed(false);              /* CCC off or disconnect */
	rec(1);
	stream_end();
	stream_drain();
	printf("S unsubscribed\n");
	session("20261024123501", 5, 1);
	stream_set_subscribed(true);
	printf("S busy\n");
	stream_begin("20261024123502", 0, 1);
	rec(0);
	stream_end();                              /* still draining when B starts */
	session("20261024123503", 3, 0);
	printf("S no-conn\n");
	conn_up = 0;
	session("20261024123504", 3, 1);           /* sender sees no link */
	conn_up = 1;
	stream_set_subscribed(false);              /* what disconnected() does */
	stream_drain();
	printf("S done\n");
	return 0;
}
"""


def record(i):
    return bytes((i * 7 + j) & 0xFF for j in range((i * 37) % 301 + 1))


def stream_bytes(records):
    return b"".join(struct.pack("<H", len(r)) + r for r in records)


@unittest.skipUnless(shutil.which("cc"), "no host C compiler")
class StreamHostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            for sub in ("zephyr/logging", "zephyr/sys", "zephyr/bluetooth"):
                os.makedirs(os.path.join(tmp, sub), exist_ok=True)
            for name in ("kernel", "logging/log", "sys/byteorder", "sys/crc",
                         "sys/ring_buffer", "bluetooth/bluetooth", "bluetooth/conn",
                         "bluetooth/gatt", "bluetooth/uuid"):
                with open(os.path.join(tmp, "zephyr", name + ".h"), "w") as f:
                    f.write(STUB if name == "kernel" else '#include <zephyr/kernel.h>\n')
            src = os.path.join(tmp, "harness.c")
            with open(src, "w") as f:
                f.write(HARNESS)
            exe = os.path.join(tmp, "harness")
            subprocess.run(["cc", "-std=gnu11", "-Wall", "-Werror", "-I", tmp,
                            "-I", os.path.join(APP, "include"),
                            "-I", os.path.join(APP, "src"), src, "-o", exe], check=True)
            out = subprocess.run([exe], check=True, capture_output=True, text=True).stdout
        cls.sessions, name = {}, None
        for line in out.splitlines():
            kind, val = line.split(" ", 1)
            if kind == "S":
                name = val
                cls.sessions[name] = []
            else:
                cls.sessions[name].append(bytes.fromhex(val))

    def decode(self, pkts, mtu=247):
        self.assertTrue(pkts, "no packets")
        for i, p in enumerate(pkts):
            self.assertLessEqual(len(p), min(mtu - 3, 244))
            self.assertEqual(struct.unpack_from("<I", p, 1)[0], i, "seq gap")
        self.assertEqual(pkts[0][0], 1, "START first")
        self.assertEqual(len(pkts[0]), 20)
        start = struct.unpack("<BBBBBHQ", pkts[0][5:])
        data = b"".join(p[5:] for p in pkts[1:] if p[0] == 2)
        self.assertTrue(all(p[0] == 2 for p in pkts[1:-1]), "only DATA between")
        end = struct.unpack("<BIII", pkts[-1][5:]) if pkts[-1][0] == 3 else None
        return start, data, end

    def test_complete_sessions_match_sd_bytes_at_every_mtu(self):
        want = stream_bytes([record(i) for i in range(150)])
        for mtu in (23, 24, 247, 517):
            start, data, end = self.decode(self.sessions[f"complete-{mtu}"], mtu)
            self.assertEqual(start, (1, 0xFF, 1, 1, 20, 16000, 20261024123456))
            self.assertEqual(data, want)
            self.assertEqual(end, (0, 150, len(want), zlib.crc32(want)))

    def test_empty_session_and_uptime_id(self):
        start, data, end = self.decode(self.sessions["empty"])
        self.assertEqual(f"{start[6]:014d}", "00000000012345")
        self.assertEqual((data, end), (b"", (0, 0, 0, 0)))

    def test_overflow_is_flagged_with_full_counts(self):
        want = stream_bytes([record(i) for i in range(200)])
        _, data, end = self.decode(self.sessions["overflow"])
        self.assertEqual(end, (1, 200, len(want), zlib.crc32(want)))
        self.assertLessEqual(len(data), 8192)
        self.assertEqual(data, want[:len(data)])  # a clean prefix, never holes

    def test_faults_never_send_end(self):
        fail = self.sessions["notify-fail"]
        self.assertEqual(len(fail), 3)  # 4th notify failed: nothing after it
        self.assertNotIn(3, [p[0] for p in fail])
        _, data, end = self.decode(self.sessions["after-fail"])
        self.assertEqual(end[1:3], (5, len(data)))
        mid = self.sessions["unsub-mid"]
        self.assertEqual([p[0] for p in mid], [1, 2])
        for name in ("unsubscribed", "no-conn", "done"):
            self.assertEqual(self.sessions[name], [], name)

    def test_capture_during_drain_is_not_streamed(self):
        start, _, end = self.decode(self.sessions["busy"])
        self.assertEqual(start[6], 20261024123502)
        self.assertEqual(end[1], 1)


if __name__ == "__main__":
    unittest.main()
