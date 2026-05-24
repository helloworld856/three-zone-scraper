from __future__ import annotations

import random
import time


def should_stop(stop_event=None) -> bool:
    return bool(stop_event and stop_event.is_set())


def interruptible_sleep(seconds: float, stop_event=None, step: float = 0.2) -> bool:
    end_time = time.time() + max(0, seconds)
    while time.time() < end_time:
        if should_stop(stop_event):
            return True
        time.sleep(min(step, max(0, end_time - time.time())))
    return should_stop(stop_event)


def random_cooldown(log_callback=None, stop_event=None, min_seconds: float = 3.0, max_seconds: float = 8.0, reason: str = "降低访问频率"):
    seconds = random.uniform(min_seconds, max_seconds)
    if log_callback:
        log_callback(f"  随机等待 {seconds:.1f} 秒，{reason}。")
    return interruptible_sleep(seconds, stop_event)


def wait_if_paused(pause_event=None, stop_event=None) -> bool:
    while pause_event and pause_event.is_set():
        if should_stop(stop_event):
            return True
        time.sleep(0.1)
    return should_stop(stop_event)
