"""Host check of the button driver against the board's thresholds.

Compiles the real drivers/input/gpio_button.c with stub Zephyr headers, replays
pin timelines on a virtual clock (k_sleep advances it; each GPIO edge wakes the
polling loop, edges during a run re-wake it once, like the driver's drained
semaphore) and checks the events the app gets: a tap toggles, a hold never
does, 8 s powers off. Thresholds come from the board DTS.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DTS = ROOT / 'boards/seeed/clip/clip_nrf5340_cpuapp.dts'
LONG, LEVEL_1, CLICK, DOUBLE, RELEASE = 0, 1, 4, 5, 6

STUBS = {
    'zephyr/kernel.h': '''#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <errno.h>
typedef struct { int32_t ms; } k_timeout_t;
#define K_MSEC(x) ((k_timeout_t){ (x) })
int64_t k_uptime_get(void);
void k_sleep(k_timeout_t t);
struct k_work { void (*handler)(struct k_work *); };
static inline int k_work_submit(struct k_work *w) { (void)w; return 0; }
''',
    'zephyr/device.h': '#pragma once\nstruct device { const void *config; void *data; };\n',
    'zephyr/drivers/gpio.h': '''#pragma once
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#define BIT(n) (1UL << (n))
#define GPIO_INPUT 0
#define GPIO_INT_EDGE_BOTH 0
struct gpio_dt_spec { const struct device *port; int pin; int dt_flags; };
struct gpio_callback { int unused; };
typedef void (*gpio_callback_handler_t)(const struct device *, struct gpio_callback *, uint32_t);
int gpio_pin_get_dt(const struct gpio_dt_spec *spec);
static inline int gpio_pin_configure_dt(const struct gpio_dt_spec *s, int f) { return 0; }
static inline int gpio_pin_interrupt_configure_dt(const struct gpio_dt_spec *s, int f) { return 0; }
static inline void gpio_init_callback(struct gpio_callback *c, gpio_callback_handler_t h, uint32_t p) {}
static inline int gpio_add_callback(const struct device *d, struct gpio_callback *c) { return 0; }
static inline bool gpio_is_ready_dt(const struct gpio_dt_spec *s) { return true; }
''',
    'zephyr/sys/util.h': '''#pragma once
#include <stddef.h>
#define CONTAINER_OF(p, t, f) ((t *)(((char *)(p)) - offsetof(t, f)))
#define ARG_UNUSED(x) (void)(x)
#define MIN(a, b) ((a) < (b) ? (a) : (b))
''',
    'zephyr/logging/log.h': '''#pragma once
#define LOG_MODULE_REGISTER(...)
#define LOG_DBG(...) do { } while (0)
#define LOG_ERR(...) do { } while (0)
''',
}

HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#define CONFIG_INPUT_GPIO_BUTTON_GLOBAL_THREAD 1
#define DT_INST_FOREACH_STATUS_OKAY(fn)
#include "gpio_button.c"

static int64_t now_ms;
static int n;
static int64_t et[512];
static int ev[512];

int64_t k_uptime_get(void) { return now_ms; }
void k_sleep(k_timeout_t t) { now_ms += t.ms; }
int gpio_pin_get_dt(const struct gpio_dt_spec *s)
{
	int v = 0;
	for (int i = 0; i < n && et[i] <= now_ms; i++) v = ev[i];
	return v;
}
static void record(const struct device *d, enum button_action a)
{
	printf("%lld %d\n", (long long)now_ms, (int)a);
}

int main(int argc, char **argv)
{
	static struct gpio_button_config cfg;
	static struct gpio_button_data data;
	static struct device dev = { &cfg, &data };

	cfg.debounce_ms = atoi(argv[1]);
	cfg.long_press_ms[0] = atoi(argv[2]);
	cfg.long_press_ms[1] = atoi(argv[3]);
	cfg.long_press_count = 2;
	cfg.double_click_ms = atoi(argv[4]);
	data.dev = &dev;
	data.cfg = &cfg;
	for (int i = 0; i < BUTTON_EVENT_NUM; i++) data.event_cb[i] = record;
	for (int i = 5; i + 1 < argc; i += 2) { et[n] = atoll(argv[i]); ev[n++] = atoi(argv[i + 1]); }

	int i = 0, pending = 0;
	while (pending || i < n) {
		if (!pending && et[i] > now_ms) now_ms = et[i];
		while (i < n && et[i] <= now_ms) i++;   /* drained */
		gpio_button_thread_cb(&dev);
		pending = i < n && et[i] <= now_ms;     /* edge during the run */
	}
	return 0;
}
'''


def dts_button():
    node = re.search(r'usr_btn: button \{(.*?)\};', DTS.read_text(), re.S).group(1)
    get = lambda prop: [int(x) for x in re.search(prop + r' = <([\d ]+)>;', node).group(1).split()]
    return get('debounce-ms') + get('long-press-ms') + get('double-click-ms')


@unittest.skipUnless(shutil.which('cc'), 'no host C compiler')
class ButtonDriver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        for name, text in STUBS.items():
            (cls.tmp / name).parent.mkdir(parents=True, exist_ok=True)
            (cls.tmp / name).write_text(text)
        (cls.tmp / 'harness.c').write_text(HARNESS)
        cls.exe = cls.tmp / 'harness'
        subprocess.run(['cc', '-w', '-std=gnu11', '-I', str(cls.tmp), '-I', str(ROOT / 'include'),
                        '-I', str(ROOT / 'drivers/input'), str(cls.tmp / 'harness.c'), '-o', str(cls.exe)],
                       check=True)
        cls.cfg = dts_button()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def events(self, *presses):
        """presses: (down_ms, up_ms) pairs; returns [(time, action)]."""
        edges = [str(x) for d, u in presses for x in (d, 1, u, 0)]
        out = subprocess.run([str(self.exe), *map(str, self.cfg), *edges], capture_output=True, text=True,
                             check=True).stdout
        return [tuple(map(int, line.split())) for line in out.split('\n') if line]

    def actions(self, *presses):
        return [a for _, a in self.events(*presses)]

    def test_thresholds(self):
        self.assertEqual(self.cfg, [50, 1000, 8000, 0])

    def test_tap_toggles_once_at_release(self):
        ev = self.events((1000, 1120))
        self.assertEqual([a for _, a in ev], [CLICK])
        self.assertLess(ev[0][0] - 1120, 100)

    def test_two_quick_taps_are_two_toggles(self):
        self.assertEqual(self.actions((1000, 1100), (1300, 1400)), [CLICK, CLICK])

    def test_glitch_is_ignored(self):
        self.assertEqual(self.actions((1000, 1010)), [])

    def test_effective_tap_threshold_is_two_polls(self):
        # debounce-ms is compared on 30 ms polls: a press still down at the
        # second poll measures 60 ms and toggles (docs/protocol.md E.1)
        for ms, want in ((20, []), (29, []), (31, [CLICK]), (35, [CLICK]), (45, [CLICK]),
                         (49, [CLICK])):
            with self.subTest(press_ms=ms):
                self.assertEqual(self.actions((1000, 1000 + ms)), want)

    def test_glitch_does_not_poison_the_next_press(self):
        # Stock left the glitch armed: the next tap was timed from it and read
        # as an instant long press (LONG_PRESS + RELEASE, no toggle).
        self.assertEqual(self.actions((1000, 1010), (6000, 6120)), [CLICK])

    def test_bouncy_tap_toggles_once(self):
        self.assertEqual(self.actions((1000, 1004), (1008, 1130), (1140, 1143)), [CLICK])

    def test_hold_never_toggles(self):
        for up in (1100 + 1000, 1000 + 3000, 1000 + 7900):
            with self.subTest(hold=up - 1000):
                self.assertEqual(self.actions((1000, up)), [LONG, RELEASE])

    def test_eight_second_hold_powers_off_while_held(self):
        ev = self.events((1000, 1000 + 9500))
        self.assertEqual([a for _, a in ev], [LONG, LEVEL_1, RELEASE])
        self.assertTrue(1000 + 8000 <= ev[1][0] < 1000 + 8000 + 60)   # no release needed
        self.assertEqual(ev[2][0], 1000 + 9500)

    def test_tap_after_power_off_hold(self):
        self.assertEqual(self.actions((1000, 10500), (12000, 12100)), [LONG, LEVEL_1, RELEASE, CLICK])


if __name__ == '__main__':
    unittest.main()
