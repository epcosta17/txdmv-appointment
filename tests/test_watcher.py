"""Watcher behaviour, with a fake session. The safety tests are the point here."""

import pytest

from watcher import MIN_INTERVAL, Watcher

APPLICANT = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": "1"}


def slot(date, time, raw=None):
    return {"raw": raw or "{0} {1}".format(date, time), "date": date, "time": time,
            "label": "{0} {1}".format(time, date)}


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.searches = 0
        self.booked = []

    def search(self, office, service, from_date=None, first_available=False):
        self.searches += 1
        found = self.results.pop(0) if len(self.results) > 1 else (self.results[0] if self.results else [])
        return {"slots": found, "timetable": [], "latency": 0.1, "at": "11:00:00"}

    def book(self, raw, applicant):
        self.booked.append(raw)
        return {"appointment_number": "1881183439", "time": raw, "office": "Houston North"}


def config(**overrides):
    base = {
        "office": "Houston North",
        "service": "Title Companies and Runners",
        "auto_book": False,
        "interval": 60,
        "applicant": APPLICANT,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- safety


def test_notify_mode_never_books():
    session = FakeSession([[slot("2026-09-09", "09:30")]])
    watcher = Watcher(session)
    watcher.arm(config(auto_book=False))
    watcher.tick()

    assert session.booked == []
    assert watcher.status()["state"] == "found"


def test_auto_book_requires_explicit_arming():
    # A config without auto_book must never book, whatever the slots look like.
    session = FakeSession([[slot("2026-09-09", "09:30")]])
    watcher = Watcher(session)
    watcher.arm(config())
    watcher.tick()
    assert session.booked == []


def test_auto_book_books_the_first_match():
    session = FakeSession([[slot("2026-09-09", "09:30"), slot("2026-09-09", "10:00")]])
    watcher = Watcher(session)
    watcher.arm(config(auto_book=True))
    watcher.tick()

    assert session.booked == ["2026-09-09 09:30"]
    assert watcher.status()["state"] == "booked"


def test_watcher_stops_after_booking():
    session = FakeSession([[slot("2026-09-09", "09:30")]])
    watcher = Watcher(session)
    watcher.arm(config(auto_book=True))
    watcher.tick()
    watcher.tick()

    assert len(session.booked) == 1
    assert session.searches == 1


def test_watcher_stops_after_finding_in_notify_mode():
    session = FakeSession([[slot("2026-09-09", "09:30")]])
    watcher = Watcher(session)
    watcher.arm(config())
    watcher.tick()
    watcher.tick()

    assert session.searches == 1


def test_interval_has_a_floor():
    watcher = Watcher(FakeSession([[]]))
    watcher.arm(config(interval=2))
    assert watcher.status()["interval"] == MIN_INTERVAL


# -------------------------------------------------------------------- filters


def test_slots_outside_the_time_window_are_ignored():
    session = FakeSession([[slot("2026-09-09", "08:00"), slot("2026-09-09", "16:00")]])
    watcher = Watcher(session)
    watcher.arm(config(auto_book=True, time_from="09:00", time_to="15:00"))
    watcher.tick()

    assert session.booked == []
    assert watcher.status()["state"] == "watching"


def test_a_slot_inside_the_window_is_taken():
    session = FakeSession([[slot("2026-09-09", "08:00"), slot("2026-09-09", "14:30")]])
    watcher = Watcher(session)
    watcher.arm(config(auto_book=True, time_from="09:00", time_to="15:00"))
    watcher.tick()

    assert session.booked == ["2026-09-09 14:30"]


def test_slots_outside_the_date_range_are_ignored():
    session = FakeSession([[slot("2026-09-20", "10:00")]])
    watcher = Watcher(session)
    watcher.arm(config(auto_book=True, date_from="2026-09-08", date_to="2026-09-12"))
    watcher.tick()

    assert session.booked == []


def test_window_bounds_are_inclusive():
    session = FakeSession([[slot("2026-09-08", "09:00")]])
    watcher = Watcher(session)
    watcher.arm(config(auto_book=True, date_from="2026-09-08", date_to="2026-09-12",
                       time_from="09:00", time_to="15:00"))
    watcher.tick()

    assert session.booked == ["2026-09-08 09:00"]


# ----------------------------------------------------------------- log & state


def test_empty_result_keeps_watching_and_logs():
    watcher = Watcher(FakeSession([[]]))
    watcher.arm(config())
    watcher.tick()

    status = watcher.status()
    assert status["state"] == "watching"
    assert any("0" in entry["text"] for entry in status["log"])


def test_a_failing_search_is_logged_without_killing_the_watch():
    class Failing(FakeSession):
        def search(self, *args, **kwargs):
            raise RuntimeError("proxy died")

    watcher = Watcher(Failing([[]]))
    watcher.arm(config())
    watcher.tick()

    status = watcher.status()
    assert status["state"] == "watching"
    assert any("proxy died" in entry["text"] for entry in status["log"])


def test_repeated_failures_stop_the_watch():
    class Failing(FakeSession):
        def search(self, *args, **kwargs):
            raise RuntimeError("nope")

    watcher = Watcher(Failing([[]]))
    watcher.arm(config())
    for _ in range(6):
        watcher.tick()

    assert watcher.status()["state"] == "error"


def test_stop_returns_it_to_idle():
    watcher = Watcher(FakeSession([[]]))
    watcher.arm(config())
    watcher.stop()
    assert watcher.status()["state"] == "idle"


def test_status_reports_the_floor_it_is_running_with():
    watcher = Watcher(FakeSession([[]]))
    watcher.arm(config(time_from="10:00"))
    assert watcher.status()["time_from"] == "10:00"


def test_status_reports_no_floor_when_none_was_set():
    watcher = Watcher(FakeSession([[]]))
    watcher.arm(config())
    assert watcher.status()["time_from"] is None


def test_a_floor_alone_still_filters():
    session = FakeSession([[slot("2026-09-09", "08:00"), slot("2026-09-09", "10:15")]])
    watcher = Watcher(session)
    watcher.arm(config(auto_book=True, time_from="10:00"))
    watcher.tick()
    assert session.booked == ["2026-09-09 10:15"]


def test_status_is_json_safe():
    import json

    watcher = Watcher(FakeSession([[slot("2026-09-09", "09:30")]]))
    watcher.arm(config(auto_book=True))
    watcher.tick()
    json.dumps(watcher.status())
