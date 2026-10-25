"""Host check of the open-segment sync in applications/clip/src/storage.c.

Compiles the real flush_write_buffer() and storage_write_frame() (cut out of
storage.c with their buffer statics) against an in-memory fs_write/fs_sync on
a virtual clock, writes 300 s of VBR Opus-sized records and checks:
- every sync commits a size that ends on a whole [len16][opus] record, i.e. the
  file a reset leaves behind passes the relay's clip_stream.split_records;
- a sync follows every 250th record (~5 s at 20 ms frames);
- `sync max` is the longest whole storage_write_frame() call that synced
  (data writes + sync), the audio-thread stall the DMIC slack must absorb.
"""
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / 'applications/clip/src/storage.c').read_text()
FRAMES, WRITE_MS, SYNC_MS = 15000, 30, 7

HARNESS = r'''
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#define CONFIG_CLIP_STORAGE_CHUNK_SIZE 4096
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#define LOG_ERR(...) do { } while (0)
#define LOG_WRN(...) do { } while (0)
struct fs_file_t { int unused; };
struct storage_file { uint32_t bytes_written, frames_written; bool is_open; struct fs_file_t internal_file; };
bool sd_mounted = true, sd_full;
uint64_t total_bytes;
static uint32_t now_ms, syncs;
static uint8_t disk[1 << 21];
static size_t disk_len;
static uint32_t k_uptime_get_32(void) { return now_ms; }
static ssize_t fs_write(struct fs_file_t *f, const void *d, size_t n)
{
	(void)f;
	memcpy(disk + disk_len, d, n);
	disk_len += n;
	now_ms += %(write_ms)d;
	return n;
}
static int fs_sync(struct fs_file_t *f)
{
	(void)f;
	printf("C %%zu\n", disk_len);
	syncs++;
	now_ms += %(sync_ms)d;
	return 0;
}
%(statics)s
%(flush)s
%(write)s
int main(int argc, char **argv)
{
	struct storage_file file = { .is_open = true };
	static uint8_t frame[128];
	uint32_t rng = (uint32_t)atoi(argv[1]);
	FILE *out;

	current_file_ptr = &file.internal_file;
	for (int i = 0; i < %(frames)d; i++) {
		uint32_t before = syncs, t0 = now_ms, n;

		rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
		n = 60 + rng %% 41;  /* 32 kbps VBR Opus frames */
		memset(frame, i, n);
		if (storage_write_frame(&file, frame, n) != 0) return 1;
		if (syncs != before) printf("T %%u\n", now_ms - t0);
	}
	printf("M %%u\n", sync_max_ms);
	if (flush_write_buffer() != 0) return 1;  /* what storage_close_file() does */
	out = fopen(argv[2], "wb");
	fwrite(disk, 1, disk_len, out);
	return fclose(out);
}
'''


def cut(start, end):
    i = SRC.index(start)
    return SRC[i:SRC.index(end, i) + len(end)]


def boundaries(data):
    """Offsets where a prefix of data passes clip_stream.split_records."""
    out, off = [], 0
    while off < len(data):
        off += 2 + struct.unpack_from('<H', data, off)[0]
        out.append(off)
    assert off == len(data), 'last record runs past end'
    return out


@unittest.skipUnless(shutil.which('cc'), 'no host C compiler')
class StorageSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = []
        with tempfile.TemporaryDirectory() as tmp:
            src, exe, disk = (os.path.join(tmp, n) for n in ('h.c', 'h', 'disk'))
            Path(src).write_text(HARNESS % {
                'write_ms': WRITE_MS, 'sync_ms': SYNC_MS, 'frames': FRAMES,
                'statics': cut('static uint8_t write_buffer', '\n/* Statistics */'),
                'flush': cut('static int flush_write_buffer(void)\n{', '\n}\n'),
                'write': cut('int storage_write_frame(struct storage_file *file,', '\n}\n'),
            })
            subprocess.run(['cc', '-std=gnu11', '-Wall', '-Werror', src, '-o', exe], check=True)
            for seed in (1, 7, 2026):
                out = subprocess.run([exe, str(seed), disk], check=True,
                                     capture_output=True, text=True).stdout.split('\n')
                cls.runs.append(({k: [int(l[2:]) for l in out if l[:1] == k] for k in 'CTM'},
                                 Path(disk).read_bytes()))

    def test_every_sync_ends_on_a_record_every_250_records(self):
        for got, data in self.runs:
            ends = boundaries(data)
            self.assertEqual(len(ends), FRAMES)
            self.assertEqual(got['C'], ends[249::250])

    def test_sync_max_times_the_whole_frame_write(self):
        for got, _ in self.runs:
            self.assertEqual(got['M'], [max(got['T'])])
            self.assertGreaterEqual(got['M'][0], WRITE_MS + SYNC_MS)


if __name__ == '__main__':
    unittest.main()
