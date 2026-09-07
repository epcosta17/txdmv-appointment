"""Session recovery. A dead residential proxy is the normal failure here, not an edge case."""

import pytest
import requests

import wizard_session as ws
from nemoq_booking import BookingError


class FakeBooking:
    def __init__(self, fail_times=0, error=None):
        self.fail_times = fail_times
        self.error = error or requests.exceptions.ProxyError("tunnel failed")
        self.calls = 0
        self.html = ""

    def offices(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return [("0", "Select office..."), ("189", "Houston North")]

    def find_slots(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return []


class FakePool:
    def __init__(self):
        self.discarded = []
        self.proxies = ["p1", "p2"]

    def pick(self, verify=None):
        return self.proxies[0]

    def discard(self, proxy):
        self.discarded.append(proxy)
        self.proxies = self.proxies[1:] or ["p3"]


@pytest.fixture
def session(monkeypatch):
    opened = []

    def fake_open(proxy):
        booking = FakeBooking(fail_times=opened.count("boom"))
        opened.append("open")
        return booking

    monkeypatch.setattr(ws, "open_wizard", fake_open)
    s = ws.WizardSession(pool=FakePool())
    s._opened = opened
    return s


def test_a_healthy_read_opens_once(session):
    assert [o["name"] for o in session.offices()] == ["Houston North"]
    assert len(session._opened) == 1


def test_a_dead_proxy_is_discarded_and_the_flow_restarts(session, monkeypatch):
    calls = {"n": 0}

    def flaky_open(proxy):
        calls["n"] += 1
        return FakeBooking(fail_times=1 if calls["n"] == 1 else 0)

    monkeypatch.setattr(ws, "open_wizard", flaky_open)
    s = ws.WizardSession(pool=FakePool())

    assert [o["name"] for o in s.offices()] == ["Houston North"]
    assert s._pool.discarded == ["p1"]
    assert calls["n"] == 2


def test_a_lost_wizard_session_also_recovers(session, monkeypatch):
    calls = {"n": 0}

    def flaky_open(proxy):
        calls["n"] += 1
        return FakeBooking(fail_times=1 if calls["n"] == 1 else 0,
                           error=BookingError("Expected 'Select time'"))

    monkeypatch.setattr(ws, "open_wizard", flaky_open)
    s = ws.WizardSession(pool=None)

    assert s.offices()
    assert calls["n"] == 2


def test_a_second_failure_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(ws, "open_wizard", lambda proxy: FakeBooking(fail_times=99))
    s = ws.WizardSession(pool=FakePool())
    with pytest.raises(requests.RequestException):
        s.offices()


def test_booking_is_never_retried_blindly(monkeypatch):
    """A half-finished reservation may already exist; retrying could double-book."""
    booking = FakeBooking()
    booking.html = ""
    monkeypatch.setattr(ws, "open_wizard", lambda proxy: booking)
    s = ws.WizardSession(pool=None)
    s.ensure()

    with pytest.raises(ws.SessionExpired):
        s.book("9/8/2026 9:00:00 AM", {"first_name": "a", "last_name": "b",
                                       "email": "e", "phone": "p"})


def test_status_reports_the_configured_connection():
    assert ws.WizardSession(pool=None).status()["direct"] is True
    assert ws.WizardSession(pool=FakePool()).status()["direct"] is False
