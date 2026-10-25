# Live Opus Stream Protocol (version 1)

Firmware 0.1.4+4 and later stream each recording to a subscribed BLE central
while it is captured. The central can then submit the utterance as soon as
the recording stops, without the post-stop file download. The SD recording
is written exactly as before and stays the recovery path. If anything goes
wrong with the stream, the central downloads the settled file instead
(`AT+DOWNLOAD`, [protocol.md](protocol.md) section 4).

## 1. Characteristic

| Item | Value |
|------|-------|
| Service | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` (the Clip service) |
| Characteristic | `6E400006-B5A3-F393-E0A9-E50E24DCCA9E`, Notify only |
| Security | The CCC write needs an encrypted link, and the stack never notifies the value over an unencrypted one (`BT_GATT_PERM_READ_ENCRYPT`). Reads are refused. |
| Position | Appended after Audio Visualization, so the existing Clip characteristics keep their handles; services placed after the Clip service may shift. The GATT database hash changes, so bonded centrals get a Service Changed indication and rediscover. |
| Absent | Older firmware has no such characteristic: use the file path. |

A session is streamed only if the central is subscribed when the capture
starts. Subscribing during a recording takes effect at the next one.
Unsubscribing or disconnecting ends the session's stream immediately: no
END is sent and the device keeps no stream state.

## 2. Packets

Every notification is one packet. All integers are little-endian.

```
offset 0  type  u8   1 = START, 2 = DATA, 3 = END
offset 1  seq   u32  0 for START, then +1 per packet of the session
offset 5  body
```

A packet is at most `min(ATT_MTU - 3, 244)` bytes. 244 is one LE data PDU
with data length extension. START (20 bytes) and END (18 bytes) fit the
minimum ATT MTU of 23, so the stream works at any MTU. DATA fills the rest,
and its size follows the MTU in force when it is sent.

### START (type 1), 20 bytes

| Offset | Field | Type | Value |
|--------|-------|------|-------|
| 5 | version | u8 | `1` |
| 6 | kind | u8 | `0` command, `1` note, `0xFF` not reported |
| 7 | codec | u8 | `1` = Opus records in the SD `.opus` layout (below) |
| 8 | channels | u8 | Opus channel count (1 for the shipped modes) |
| 9 | frame_ms | u8 | `20` |
| 10 | sample_rate | u16 | `16000` |
| 12 | session | u64 | the 14-digit session ID as an integer; format with `%014d` to get the `AT+LIST` ID back |

Firmware 0.1.4+4 always sends kind `0xFF`, because it has no capture-kind
source yet; the central keeps its own command/note rule. Builds that persist
the capture kind report `0` or `1`.

### DATA (type 2), 6 to 244 bytes

The body is the next slice of the session's **record stream**. A record is
written to the stream exactly as it is written to the SD file:

```
[len u16 LE][Opus packet, len bytes]
```

Slices ignore record boundaries: one record can span packets. Joining the
DATA bodies in `seq` order gives the session's `.opus` files (`0001.opus`,
`0002.opus`, and so on) concatenated byte for byte. Pause/resume starts a new
file but continues the same stream.

### END (type 3), 18 bytes

| Offset | Field | Type | Meaning |
|--------|-------|------|---------|
| 5 | flags | u8 | bit 0 = overflow: some records were not streamed |
| 6 | frames | u32 | records captured in the session |
| 10 | bytes | u32 | record-stream bytes captured, length prefixes included |
| 14 | crc32 | u32 | CRC-32/IEEE (`zlib.crc32`, initial value 0) of those bytes |

`frames`, `bytes` and `crc32` describe every record the session captured,
not only what was streamed, so they also describe the SD files, unless an
SD write failed (card full). END is queued only after the audio thread has
pushed its last record, and the single sender sends it after all DATA.

**END does not mean the SD file is settled.** The device may send END before
it closes the file. The existing `state` event
(`{"event":"state","state":"IDLE",...}` on the Response characteristic)
still marks the session as closed and downloadable. END and IDLE travel on
different characteristics and can arrive in either order, and IDLE itself
can precede the final file close when the SD card is slow (the "IDLE async"
path). Never delete a session because END arrived. While the session is
still closing, `AT+DELETE` answers `cannot delete active session` (retry
later) and `AT+DOWNLOAD` runs in continuous mode, waiting for the files to
settle.

## 3. Receiver rules

A session's stream is **complete** only if all of these hold:

1. The first packet is a well-formed START with version 1 and codec 1.
2. `seq` runs 0, 1, 2, ... with no gap, repeat or reordering, and nothing
   follows END.
3. A well-formed END arrives, with the overflow flag clear.
4. The joined DATA length equals `bytes` and its CRC-32 equals `crc32`.
5. The data splits into exactly `frames` records with no dangling bytes.

Otherwise use the file: wait for IDLE for that session ID (taken from START
when known), then `AT+DOWNLOAD` it. The fallback covers a missing or
malformed START, sequence gaps, overflow, CRC or count mismatches, a
truncated or missing END, low-MTU trouble, a disconnect or unsubscribe
mid-session, and an absent characteristic. Discard packets that arrive
before a START. A START always begins a new session; any unfinished
previous session falls back to its file.

The reference decoder is `clip_stream.py` in the relay
(`respeaker_clip_python/`). It provides `parse_packet`, `assemble`,
`split_records` and a firmware-equivalent `packetize`, with a self-test.
`assemble()` returns either the verified record stream or the reason the
file is needed.

## 4. Device behavior

- The audio thread copies each record into an 8 KiB ring (about 2 s at
  32 kbit/s) and never waits for Bluetooth. If a record does not fit, the
  session is marked overflowed, nothing more is queued for it, and END
  reports the overflow. Recording and SD writes continue unchanged.
- One sender thread (priority 4, below audio) emits START, DATA and END in
  order. It blocks only itself while the stack has no free ATT buffer. A
  failed notification ends the session's stream without an END.
- Stream traffic refreshes the 5-minute BLE inactivity timer, so a long
  streamed note keeps the link.
- If a new capture starts while the previous session is still draining,
  the new session is not streamed (file path).
