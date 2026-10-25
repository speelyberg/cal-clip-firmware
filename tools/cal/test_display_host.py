"""Compile the real display state transitions with a virtual clock and fake hardware.

Boot notifications must not hide the selected logo. Recording and OTA must preempt
it, and a stale splash timeout must not replace the recording screen.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / 'applications/clip/src/display.c').read_text()
HEADER = (ROOT / 'applications/clip/include/display.h').read_text()

def function(signature):
    start = SRC.index(signature + '\n{')
    return SRC[start:SRC.index('\n}', start) + 2]

HARNESS = r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#define LOG_DBG(...) ((void)0)
#define K_MSEC(x) (x)
#define K_SECONDS(x) ((x)*1000)
#define DISPLAY_STATUS_TIMEOUT_MS 3000
#define DISPLAY_PAIRING_GUIDE_TIMEOUT_MS 3000
#define DISPLAY_REC_WAVE_TIMEOUT_MS 5000
#define REC_ANIM_NORMAL 0
#define REC_ANIM_FAST 1
#define HAPTIC_DOUBLE 1
#define PM_DEVICE_ACTION_SUSPEND 0
#define PM_DEVICE_ACTION_RESUME 1
%(enums)s
static enum ui_state g_ui_state;
static bool g_recording, g_enhanced_mode, g_has_untransferred, g_dot_animation_played;
static bool g_ota_active, g_error_active, bonded;
static int g_current_anim_type, g_mark_frame, g_ota_percent;
static int64_t now, g_rec_wave_start_ms, g_status_bar_start_ms;
static struct display_status g_status;
static uint8_t display_buffer[528];
static int display_timeout_work, display_anim_work;
static void *display_dev;
static struct clip_context {struct {int oled_brightness;} config;} context;
#define clip_get_context() (&context)
#define clip_display_set_brightness(x) ((void)(x))
#define pm_device_action_run(...) ((void)0)
static int64_t k_uptime_get(void) {return now;}
static void k_work_cancel_delayable(int *w) {*w=0;}
static void k_work_schedule(int *w, int ms) {if (!*w) *w=ms;}
static void k_work_reschedule(int *w, int ms) {*w=ms;}
static bool ble_is_bonded(void) {return bonded;}
static bool ble_is_connected(void) {return bonded;}
static void battery_poll(void) {}
static void fast_anim_init(void) {}
static void render_status_bar(uint8_t *b) {(void)b;}
static void render_pairing_guide(uint8_t *b) {(void)b;}
static void flush_display(void) {}
static void haptic_play_feedback(int pattern) {(void)pattern;}
static void display_start_ui(void);
%(state)s
%(event)s
%(start)s
int main(void) {
  /* A charging notification may already have shown status when boot is queued. */
  g_ui_state=UI_STATE_STATUS_BAR;
  handle_event(UI_EVENT_BOOT_LOGO);
  assert(g_ui_state==UI_STATE_BOOT_LOGO);
  handle_event(UI_EVENT_STATUS_SHOW);
  handle_event(UI_EVENT_PAIRING_SHOW);
  handle_event(UI_EVENT_USB_CONNECTED);
  assert(g_ui_state==UI_STATE_BOOT_LOGO);
  assert(g_status.battery_charging);
  assert(display_timeout_work==2000);
  bonded=true;
  handle_event(UI_EVENT_TIMEOUT);
  assert(g_ui_state==UI_STATE_STATUS_BAR);

  g_ui_state=UI_STATE_OFF;
  handle_event(UI_EVENT_BOOT_LOGO);
  now=100;
  handle_event(UI_EVENT_REC_START);
  assert(g_ui_state==UI_STATE_REC_WAVE);
  assert(display_timeout_work==5000);
  handle_event(UI_EVENT_TIMEOUT); /* stale splash timeout */
  handle_event(UI_EVENT_BOOT_LOGO); /* delayed duplicate */
  assert(g_ui_state==UI_STATE_REC_WAVE);
  now=5100;
  handle_event(UI_EVENT_TIMEOUT);
  assert(g_ui_state==UI_STATE_REC_DOT);
  handle_event(UI_EVENT_REC_STOP);
  g_ui_state=UI_STATE_OFF;
  handle_event(UI_EVENT_BOOT_LOGO);
  handle_event(UI_EVENT_OTA_START);
  handle_event(UI_EVENT_BOOT_LOGO);
  assert(g_ui_state==UI_STATE_OTA_PROGRESS);
  return 0;
}
'''

@unittest.skipUnless(shutil.which('cc'), 'no host C compiler')
class DisplayStateTest(unittest.TestCase):
    def test_splash_notifications_and_recording_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, exe = Path(tmp)/'display.c', Path(tmp)/'display'
            src.write_text(HARNESS % {
                'enums': HEADER[HEADER.index('enum ui_state {'):HEADER.index('/* ===', HEADER.index('struct display_status {'))],
                'state': function('static void set_ui_state(enum ui_state new_state)'),
                'event': function('static void handle_event(enum ui_event event)'),
                'start': function('static void display_start_ui(void)'),
            })
            subprocess.run(['cc', '-std=c11', '-Wall', '-Werror', str(src), '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True)

if __name__ == '__main__':
    unittest.main()
