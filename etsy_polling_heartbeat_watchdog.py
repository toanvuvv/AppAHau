"""Heartbeat + watchdog for the background Etsy polling thread.

Failure mode this covers: a poll HANGS (network stall inside a Google client)
instead of crashing. The thread then never returns, keeps the poll mutex, and
polling silently stops while the web UI still looks alive.

The watchdog checks the heartbeat every minute. If a poll runs too long or the
polling loop stopped ticking, it exits the gunicorn worker with code 1; the
gunicorn master respawns a fresh worker, which restarts polling.
Docker's healthcheck alone cannot do this: Docker never restarts "unhealthy"
containers.
"""
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

WATCHDOG_INTERVAL_SECONDS = 60
STUCK_POLL_SECONDS = 30 * 60   # one poll cycle / manual job running longer than this = hung
STALE_TICK_SECONDS = 20 * 60   # polling loop ticks every 30s; silence this long = loop dead

# Written by the polling thread / job runner, read by watchdog and /healthz.
heartbeat = {
    "last_tick": None,         # epoch of last polling-loop iteration
    "poll_started_at": None,   # epoch when the current poll/job started, None when idle
    "last_poll_finished_at": None,
}

_watchdog_started = False
_watchdog_lock = threading.Lock()


def mark_tick():
    heartbeat["last_tick"] = time.time()


def mark_poll_started():
    heartbeat["poll_started_at"] = time.time()


def mark_poll_finished():
    heartbeat["poll_started_at"] = None
    heartbeat["last_poll_finished_at"] = time.time()


def find_stuck_reason(now):
    """Return a human-readable reason if polling looks hung, else None."""
    started = heartbeat["poll_started_at"]
    if started and now - started > STUCK_POLL_SECONDS:
        return f"poll dang chay {int((now - started) / 60)} phut (nghi bi treo)"
    tick = heartbeat["last_tick"]
    if tick and not started and now - tick > STALE_TICK_SECONDS:
        return f"vong lap polling khong chay {int((now - tick) / 60)} phut"
    return None


def _shift_for_suspend(gap):
    """Windows sleep / WSL VM pause freezes the clock-based checks: move the
    heartbeat forward by the suspended time so waking up is not seen as a hang."""
    for key in ("last_tick", "poll_started_at"):
        if heartbeat[key]:
            heartbeat[key] += gap


def _watchdog_loop(exit_fn):
    last_check = time.time()
    while True:
        time.sleep(WATCHDOG_INTERVAL_SECONDS)
        now = time.time()
        overslept = now - last_check - WATCHDOG_INTERVAL_SECONDS
        last_check = now
        if overslept > 2 * WATCHDOG_INTERVAL_SECONDS:
            logger.info(f"Watchdog: may vua thuc day sau ~{int(overslept / 60)} phut, bo qua lan kiem tra nay")
            _shift_for_suspend(overslept)
            continue
        reason = find_stuck_reason(now)
        if reason:
            logger.critical(f"Watchdog: {reason} -- khoi dong lai worker")
            for handler in logging.getLogger().handlers:
                try:
                    handler.flush()
                except Exception:
                    pass
            exit_fn(1)


def start_watchdog(exit_fn=os._exit):
    """Start the watchdog daemon thread once per process."""
    global _watchdog_started
    with _watchdog_lock:
        if _watchdog_started:
            return
        _watchdog_started = True
    threading.Thread(target=_watchdog_loop, args=(exit_fn,), daemon=True, name="etsy-watchdog").start()
    logger.info("Watchdog polling da khoi dong")
