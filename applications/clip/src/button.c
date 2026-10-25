/*
 * Copyright (c) 2025 Seeed Technology Co., Ltd.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>
#include <zephyr/drivers/input/button.h>

#include "button.h"
#include "clip_event.h"

LOG_MODULE_REGISTER(button, CONFIG_CLIP_LOG_LEVEL);

/* Button device from device tree */
static const struct device *button_dev = DEVICE_DT_GET(DT_NODELABEL(usr_btn));

/* Callback function and user data */
static button_callback_t button_cb = NULL;
static void *button_user_data = NULL;

/* Shutdown in progress (set when shutdown is requested): ignore ALL input.
 * The shutdown sequence runs hundreds of ms; presses during it must not
 * race a START before ship mode cuts power. Cleared only by reboot (or a
 * failed shutdown, see button_shutdown_unlock). */
static atomic_t shutdown_lockout = ATOMIC_INIT(0);

/*
 * Controls (thresholds in the board DTS: long-press-ms = <1000 5000>):
 *   tap (< 1 s)   toggle recording, decided by the event thread from the
 *                 audio lifecycle (CLIP_EVENT_TOGGLE)
 *   hold (>= 1 s) never toggles; releasing it shows the status bar
 *   5 s held      graceful shutdown at once, all further input ignored
 * The driver never reports a tap for a press that reached 1 s.
 */
static void button_event_callback(const struct device *dev, enum button_action action)
{
    ARG_UNUSED(dev);

    /* Firmware upgrade in progress: ignore ALL button input. NOTE: the
     * state machine does NOT enter CLIP_STATE_OTA during upload, so this
     * must check the DFU flag. Blocks the power-off path (would abort the
     * update mid-upload/swap) and UI switches away from the progress
     * screen. Shutdown committed: ignore everything too. */
    if (clip_event_ota_in_progress() || atomic_get(&shutdown_lockout)) {
        return;
    }

    switch (action) {
    case BUTTON_SINGLE_CLICK:
        clip_post_event(CLIP_EVENT_TOGGLE);
        break;

    case BUTTON_LONG_PRESS:
        LOG_INF("hold");
        clip_post_event(CLIP_EVENT_HOLD);
        break;

    case BUTTON_LONG_PRESS_LEVEL_1:
        if (clip_get_context()->status.battery_charging) {
            LOG_INF("USB charging, ignore power off");
            break;
        }
        /* Straight to the dedicated shutdown thread via its semaphore,
         * never through the 8-deep event queue (a full queue would drop
         * it); it stops any recording through the settled lifecycle. */
        atomic_set(&shutdown_lockout, 1);
        clip_request_shutdown();
        break;

    case BUTTON_RELEASE:
        /* End of a hold that did not power off */
        clip_post_event(CLIP_EVENT_STATUS_SHOW);
        break;

    default:
        break;
    }

    if (button_cb) {
        button_cb(action, button_user_data);
    }
}

void button_shutdown_lockout(void)
{
    atomic_set(&shutdown_lockout, 1);
}

void button_shutdown_unlock(void)
{
    atomic_clear(&shutdown_lockout);
}

int button_init(void)
{
    int err;

    if (!device_is_ready(button_dev)) {
        LOG_ERR("Button device not ready");
        return -ENODEV;
    }

    err = button_callback_register(button_dev, button_event_callback);
    if (err < 0) {
        LOG_ERR("Failed to register button callback: %d", err);
        return err;
    }

    LOG_INF("Button handler initialized");
    return 0;
}

bool button_is_ready(void)
{
    return device_is_ready(button_dev);
}

int button_register_callback(button_callback_t callback, void *user_data)
{
    if (!callback) {
        return -EINVAL;
    }

    button_cb = callback;
    button_user_data = user_data;

    return 0;
}
