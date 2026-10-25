/*
 * Live Opus stream over BLE: wire format in docs/stream_protocol.md.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef CLIP_STREAM_H
#define CLIP_STREAM_H

#include <stdbool.h>
#include <stdint.h>

/* START "kind" byte */
#define STREAM_KIND_COMMAND     0
#define STREAM_KIND_NOTE        1
#define STREAM_KIND_UNSPECIFIED 0xFF

/* Audio thread: a capture has started (before its first record). The
 * session is streamed only if a subscriber is present now. */
void stream_begin(const char *session_id, uint8_t kind, uint8_t channels);

/* Audio thread: one encoded record, exactly as written to the SD file.
 * Never blocks: if the ring is full the session is marked overflowed. */
void stream_push(const uint8_t *frame, uint16_t len);

/* Audio thread: every captured record has been pushed; queue END. */
void stream_end(void);

/* BLE: stream CCC changed or link dropped (false clears all state). */
void stream_set_subscribed(bool on);

#endif /* CLIP_STREAM_H */
