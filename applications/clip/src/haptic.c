/*
 * Copyright (c) 2025 Seeed Technology Co., Ltd.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>
#include "haptic.h"
#include "audio.h"
#include "clip_event.h"

LOG_MODULE_REGISTER(haptic, CONFIG_CLIP_LOG_LEVEL);

/* Motor control GPIO - P1.06 controls PMIC GPIO1 which controls BUCK1 (MOTOR_3V3)
 *
 * Hardware connection:
 *   nRF5340 P1.06 → NPM1300 GPIO1 → BUCK1 (VOUT1/MOTOR_3V3) enable
 */
#ifdef CONFIG_CLIP_HAPTIC_MOTOR_ENABLED
#define HAPTIC_MOTOR_GPIO_PIN  CONFIG_CLIP_HAPTIC_MOTOR_GPIO_PIN

static const struct device *gpio1_dev = DEVICE_DT_GET(DT_NODELABEL(gpio1));

/* Current motor state */
static volatile bool motor_is_on = false;

static int execute_pattern(enum haptic_pattern pattern);

/* Motor guard: no legal pattern drives the motor longer than ~1 s
 * (ALERT = 4x150 + 400 = 1000 ms). If the motor is still on ~1.2 s
 * after it was enabled, the haptic thread is stuck or dead (starved,
 * stack overflow, ...) and without this the motor runs away until the
 * device is powered off. The timer callback cuts the GPIO directly —
 * it must not depend on any thread being scheduled. */
#define HAPTIC_MOTOR_GUARD_MS 1200

static void haptic_motor_guard_fn(struct k_timer *tmr)
{
	ARG_UNUSED(tmr);
	if (motor_is_on) {
		/* ISR context: raw GPIO write + printk (log backend not
		 * guaranteed here). */
		gpio_port_set_masked_raw(gpio1_dev,
					 BIT(HAPTIC_MOTOR_GPIO_PIN), 0);
		motor_is_on = false;
		printk("haptic: motor guard tripped\n");
	}
}
static K_TIMER_DEFINE(haptic_motor_guard, haptic_motor_guard_fn, NULL);

/* Pattern execution moved to a dedicated low-priority thread: patterns are
 * on/off timing loops (100-500 ms of k_sleep), and callers (button driver
 * thread, clip event handler) must not be stalled while the motor runs. */
struct haptic_req {
	uint8_t pattern;
	bool feedback;          /* remote feedback: gated on capture state */
};
K_MSGQ_DEFINE(haptic_msgq, sizeof(struct haptic_req), 4, 1);

/* Requests queued or executing. Unlike the queue depth this also covers the
 * motor-off gaps inside a multi-pulse pattern, which audio must wait out. */
static atomic_t haptic_pending;

/* Haptic thread only: the pattern being played is remote feedback */
static bool feedback_gated;

/* Remote feedback must stay out of captures (starting, recording, paused,
 * settling). */
static bool capture_quiet(void)
{
	enum clip_state state = clip_event_get_state();

	return state != CLIP_STATE_RECORDING && state != CLIP_STATE_PAUSED &&
	       !audio_is_recording();
}

static int haptic_enqueue(enum haptic_pattern pattern, bool feedback)
{
	struct haptic_req req = { .pattern = pattern, .feedback = feedback };
	int err;

	atomic_inc(&haptic_pending);
	err = k_msgq_put(&haptic_msgq, &req, K_NO_WAIT);
	if (err) {
		atomic_dec(&haptic_pending);
	}
	return err;
}

/* One motor pulse. Feedback is re-checked before every pulse, at the moment
 * it would actually play; false = abandoned because a capture began. */
static bool pulse(int32_t on_ms, int32_t off_ms)
{
	if (feedback_gated && !capture_quiet()) {
		LOG_INF("haptic feedback dropped");
		return false;
	}
	haptic_set_motor(true);
	k_sleep(K_MSEC(on_ms));
	haptic_set_motor(false);
	k_sleep(K_MSEC(off_ms));
	return true;
}

#define HAPTIC_THREAD_STACK_SIZE 1024
static K_THREAD_STACK_DEFINE(haptic_stack, HAPTIC_THREAD_STACK_SIZE);
static struct k_thread haptic_thread;

static void haptic_thread_fn(void *p1, void *p2, void *p3)
{
	struct haptic_req req;

	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	while (1) {
		k_msgq_get(&haptic_msgq, &req, K_FOREVER);
		feedback_gated = req.feedback;
		(void)execute_pattern(req.pattern);
		atomic_dec(&haptic_pending);
	}
}

#endif /* CONFIG_CLIP_HAPTIC_MOTOR_ENABLED */

int haptic_init(void)
{
#ifdef CONFIG_CLIP_HAPTIC_MOTOR_ENABLED

	if (!device_is_ready(gpio1_dev)) {
		LOG_ERR("GPIO1 not ready (haptic)");
		return -ENODEV;
	}

	/* Configure motor control GPIO as output, initial state LOW (motor off) */
	int ret = gpio_pin_configure(gpio1_dev, HAPTIC_MOTOR_GPIO_PIN,
				     GPIO_OUTPUT_LOW);
	if (ret != 0 && ret != -EEXIST) {
		LOG_ERR("haptic GPIO config %d", ret);
		return ret;
	}


	k_thread_create(&haptic_thread, haptic_stack,
			K_THREAD_STACK_SIZEOF(haptic_stack),
			haptic_thread_fn, NULL, NULL, NULL,
			K_PRIO_PREEMPT(4), 0, K_NO_WAIT);
	k_thread_name_set(&haptic_thread, "haptic");
	return 0;
#else
	LOG_WRN("Haptic motor disabled via Kconfig");
	return -ENOTSUP;
#endif
}

int haptic_set_motor(bool enable)
{
#ifdef CONFIG_CLIP_HAPTIC_MOTOR_ENABLED
	int ret = gpio_port_set_masked_raw(gpio1_dev, BIT(HAPTIC_MOTOR_GPIO_PIN),
					   enable ? BIT(HAPTIC_MOTOR_GPIO_PIN) : 0);
	if (ret == 0) {
		motor_is_on = enable;
		if (enable) {
			/* (Re)arm the runaway guard; a harmless one-shot
			 * expiry when the pattern turns the motor off in
			 * time. */
			k_timer_start(&haptic_motor_guard,
				      K_MSEC(HAPTIC_MOTOR_GUARD_MS),
				      K_NO_WAIT);
		}
		LOG_DBG("Motor %s via GPIO1.%d", enable ? "ON" : "OFF", HAPTIC_MOTOR_GPIO_PIN);
	} else {
		LOG_ERR("Failed to set motor state: %d", ret);
	}

	return ret;
#else
	return -ENOTSUP;
#endif
}

bool haptic_is_busy(void)
{
#ifdef CONFIG_CLIP_HAPTIC_MOTOR_ENABLED
	/* A queued or running pattern counts: the motor's mechanical noise
	 * couples into the PDM mics, so audio capture must wait it out. */
	return atomic_get(&haptic_pending) > 0 || motor_is_on;
#else
	return false;
#endif
}

bool haptic_is_running(void)
{
#ifdef CONFIG_CLIP_HAPTIC_MOTOR_ENABLED
	return motor_is_on;
#else
	return false;
#endif
}

/* Execute haptic pattern (blocking) */
static int execute_pattern(enum haptic_pattern pattern)
{
#ifdef CONFIG_CLIP_HAPTIC_MOTOR_ENABLED
	LOG_DBG("Haptic: %d", pattern);
	switch (pattern) {
	case HAPTIC_SHORT:      /* Short tap: 100ms */
		(void)pulse(100, 0);
		break;

	case HAPTIC_DOUBLE:     /* Double tap: 100ms on, 100ms off, 100ms on */
		(void)(pulse(100, 100) && pulse(100, 0));
		break;

	case HAPTIC_TRIPLE:     /* Triple tap: three short taps */
		(void)(pulse(100, 100) && pulse(100, 100) && pulse(100, 0));
		break;

	case HAPTIC_LONG:       /* Long: 500ms on */
		(void)pulse(500, 0);
		break;

	case HAPTIC_ALERT:      /* Alert: 2 short, 1 long */
		(void)(pulse(150, 150) && pulse(150, 150) && pulse(400, 0));
		break;

	default:
		LOG_WRN("Unknown haptic pattern: %d", pattern);
		return -EINVAL;
	}

	return 0;
#else
	return -ENOTSUP;
#endif
}

int haptic_play_pattern(enum haptic_pattern pattern)
{
#ifdef CONFIG_CLIP_HAPTIC_MOTOR_ENABLED
	/* Non-blocking: queue for the haptic thread (drops if one is already
	 * queued — feedback patterns are idempotent-ish, a dropped duplicate
	 * is fine). */
	return haptic_enqueue(pattern, false);
#else
	return -ENOTSUP;
#endif
}

int haptic_play_feedback(enum haptic_pattern pattern)
{
#ifdef CONFIG_CLIP_HAPTIC_MOTOR_ENABLED
	if (!capture_quiet()) {
		return -EBUSY;
	}
	return haptic_enqueue(pattern, true);
#else
	return -ENOTSUP;
#endif
}
