"""Polls for openings and, only when explicitly armed to, books one.

Auto-booking creates a real appointment with nobody watching, so the guardrails are
part of the design rather than an afterthought:

  * notify-only is the default; auto_book must be set explicitly by the caller
  * the watch stops the moment it books, or the moment it finds something in
    notify mode - it never books twice and never keeps hammering after a hit
  * only slots inside the configured date range and time window can be taken
  * a run of failures stops the watch instead of retrying forever
"""

from __future__ import annotations

import threading
import time
from collections import deque

MIN_INTERVAL = 30
MAX_FAILURES = 5
LOG_SIZE = 200


class Watcher:
    def __init__(self, session, log_size=LOG_SIZE):
        self.session = session
        self._lock = threading.RLock()
        self._log = deque(maxlen=log_size)
        self._thread = None
        self._wake = threading.Event()
        self.state = "idle"
        self.config = {}
        self.found = []
        self.booking = None
        self.failures = 0
        self.started_at = None
        self.last_tick = None
        # The grid the watcher itself saw. Publishing it lets the UI show those
        # openings without spending a second search that could miss them.
        self.last_result = None
        self.result_seq = 0

    # ------------------------------------------------------------------ logging

    def log(self, text, level="info"):
        with self._lock:
            self._log.append({"at": time.strftime("%H:%M:%S"), "text": text, "level": level})

    # ------------------------------------------------------------------- control

    def arm(self, config):
        """Load a config and move to 'watching'. Does not start the thread."""
        with self._lock:
            self.config = dict(config)
            self.config["interval"] = max(MIN_INTERVAL, int(config.get("interval") or 60))
            self.config["auto_book"] = bool(config.get("auto_book"))
            self.state = "watching"
            self.found = []
            self.booking = None
            self.failures = 0
            self.last_result = None
            self.started_at = time.time()
            self._log.clear()
            self.log(
                "vigilancia iniciada · {0} · cada {1} s · {2}".format(
                    self.config.get("office", "?"),
                    self.config["interval"],
                    "reserva automática" if self.config["auto_book"] else "solo avisar",
                ),
                "warn" if self.config["auto_book"] else "info",
            )

    def start(self, config):
        self.stop()
        self.arm(config)
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name="watcher", daemon=True)
        self._thread.start()

    def stop(self):
        with self._lock:
            was_running = self.state == "watching"
            if was_running:
                self.state = "idle"
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None
        if was_running:
            self.log("vigilancia detenida")

    def _loop(self):
        while True:
            with self._lock:
                if self.state != "watching":
                    return
                interval = self.config["interval"]

            self.tick()

            with self._lock:
                if self.state != "watching":
                    return
            if self._wake.wait(timeout=interval):
                return

    # ---------------------------------------------------------------------- poll

    def tick(self):
        """One poll. Public so the loop and the tests drive the same code path."""
        with self._lock:
            if self.state != "watching":
                return
            config = dict(self.config)

        self.log("buscando {0}…".format(config.get("office", "?")))
        try:
            result = self.session.search(
                office=config["office"],
                service=config["service"],
                from_date=config.get("date_from"),
                first_available=bool(config.get("first_available")),
            )
        except Exception as error:
            with self._lock:
                self.failures += 1
                failures = self.failures
            self.log("error: {0}".format(error), "error")
            if failures >= MAX_FAILURES:
                with self._lock:
                    self.state = "error"
                self.log("demasiados fallos seguidos, vigilancia detenida", "error")
            return

        with self._lock:
            self.failures = 0
            self.last_tick = time.time()
            self.last_result = result
            self.result_seq += 1

        matches = [s for s in result.get("slots", []) if self._matches(s, config)]
        if not matches:
            self.log("{0} libres".format(len(result.get("slots", []))))
            return

        target = matches[0]
        self.log(
            "{0} libre(s) — {1}".format(len(matches), target.get("label") or target["raw"]), "ok"
        )

        if not config["auto_book"]:
            with self._lock:
                self.found = matches
                self.state = "found"
            self.log("esperando que confirmes la reserva", "ok")
            return

        self._book(target, config)

    def _book(self, target, config):
        self.log("reservando {0}…".format(target.get("label") or target["raw"]), "warn")
        try:
            details = self.session.book(target["raw"], config["applicant"])
        except Exception as error:
            with self._lock:
                self.failures += 1
            self.log("no se pudo reservar: {0}".format(error), "error")
            return

        with self._lock:
            self.booking = details
            self.found = [target]
            self.state = "booked"
        self.log("cita {0} confirmada".format(details.get("appointment_number", "?")), "ok")

    @staticmethod
    def _matches(slot, config):
        """Inclusive on both ends - a 09:00-15:00 window includes 09:00 and 15:00."""
        for key, bound, compare in (
            ("date", config.get("date_from"), lambda v, b: v >= b),
            ("date", config.get("date_to"), lambda v, b: v <= b),
            ("time", config.get("time_from"), lambda v, b: v >= b),
            ("time", config.get("time_to"), lambda v, b: v <= b),
        ):
            if bound and not compare(slot.get(key, ""), bound):
                return False
        return True

    def result(self):
        """The last grid the watcher pulled, for the UI to render as-is."""
        with self._lock:
            return self.last_result

    # -------------------------------------------------------------------- status

    def status(self):
        with self._lock:
            elapsed = int(time.time() - self.started_at) if self.started_at else 0
            next_in = 0
            if self.state == "watching" and self.last_tick:
                next_in = max(0, int(self.config["interval"] - (time.time() - self.last_tick)))
            return {
                "state": self.state,
                "auto_book": bool(self.config.get("auto_book")),
                "interval": self.config.get("interval", 60),
                "office": self.config.get("office"),
                "service": self.config.get("service"),
                # Exposed so the UI reports the floor the watch is actually running
                # with, rather than whatever the form happens to show now.
                "time_from": self.config.get("time_from"),
                "elapsed": elapsed,
                "next_in": next_in,
                "found": list(self.found),
                "result_seq": self.result_seq,
                "booking": self.booking,
                "log": list(self._log),
            }
