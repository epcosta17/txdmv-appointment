"""API layer, with the wizard session stubbed out. No network."""

import pytest
from fastapi.testclient import TestClient

import app as web

APPLICANT = {
    "first_name": "Marisol",
    "last_name": "Trevino",
    "email": "m@lonestar.com",
    "phone": "7135550148",
}


class StubSession:
    def __init__(self):
        self.booked = []
        self.searches = []

    def status(self):
        return {"open": True, "age_seconds": 12, "expires_in": 2400, "proxy": None, "direct": True}

    def offices(self):
        return [{"id": "189", "name": "Houston North"}, {"id": "448", "name": "Houston South"}]

    def services(self, office):
        return [{"id": "1275", "name": "Title Companies and Runners"}]

    def search(self, office, service, from_date=None, first_available=False):
        self.searches.append((office, service, from_date, first_available))
        return {"slots": [], "timetable": [], "latency": 0.4, "at": "11:00:00"}

    def book(self, raw, applicant):
        self.booked.append((raw, applicant))
        return {"appointment_number": "1881183439", "time": raw, "office": "Houston North"}

    def keepalive(self):
        return True


@pytest.fixture
def client(monkeypatch):
    stub = StubSession()
    monkeypatch.setattr(web, "session", stub)
    monkeypatch.setattr(web.watcher, "session", stub)
    web.watcher.stop()
    with TestClient(web.app) as c:
        c.stub = stub
        yield c
    web.watcher.stop()


def test_state_reports_a_direct_connection(client):
    body = client.get("/api/state").json()
    assert body["session"]["direct"] is True
    assert body["watch"]["state"] == "idle"


def test_offices_are_listed(client):
    names = [o["name"] for o in client.get("/api/offices").json()["offices"]]
    assert "Houston North" in names


def test_services_are_scoped_to_the_office(client):
    body = client.get("/api/services", params={"office": "Houston North"}).json()
    assert body["services"][0]["id"] == "1275"


def test_search_passes_the_form_through(client):
    client.post("/api/search", json={"office": "Houston South", "service": "X", "from_date": "2026-09-08"})
    assert client.stub.searches[-1] == ("Houston South", "X", "2026-09-08", False)


# --------------------------------------------------------------- book guards


def test_booking_without_personal_data_is_refused(client):
    response = client.post("/api/book", json={"raw": "9/8/2026 9:00:00 AM", "applicant": {}})
    assert response.status_code == 400
    assert client.stub.booked == []


def test_booking_with_data_goes_through(client):
    response = client.post(
        "/api/book", json={"raw": "9/8/2026 9:00:00 AM", "applicant": APPLICANT}
    )
    assert response.status_code == 200
    assert response.json()["appointment_number"] == "1881183439"
    assert client.stub.booked[0][0] == "9/8/2026 9:00:00 AM"


def test_a_stale_slot_is_reported_as_a_conflict(client, monkeypatch):
    def gone(raw, applicant):
        raise web.SessionExpired("ya no está")

    monkeypatch.setattr(client.stub, "book", gone)
    response = client.post("/api/book", json={"raw": "x", "applicant": APPLICANT})
    assert response.status_code == 409


# -------------------------------------------------------------- watch guards


def test_auto_book_without_personal_data_is_refused(client):
    response = client.post(
        "/api/watch/start",
        json={"office": "Houston North", "service": "X", "auto_book": True, "applicant": {}},
    )
    assert response.status_code == 400
    assert web.watcher.status()["state"] == "idle"


def test_notify_mode_starts_without_personal_data(client):
    response = client.post(
        "/api/watch/start",
        json={"office": "Houston North", "service": "X", "auto_book": False, "applicant": {}},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "watching"
    assert response.json()["auto_book"] is False


def test_auto_book_with_data_arms_the_watcher(client):
    response = client.post(
        "/api/watch/start",
        json={"office": "Houston North", "service": "X", "auto_book": True,
              "interval": 60, "applicant": APPLICANT},
    )
    assert response.json()["auto_book"] is True


def test_stopping_returns_to_idle(client):
    client.post("/api/watch/start", json={"office": "A", "service": "B", "applicant": {}})
    assert client.post("/api/watch/stop").json()["state"] == "idle"


# ------------------------------------------------------------------ settings


def test_the_proxy_is_off_by_default(client):
    body = client.get("/api/settings").json()
    assert body["use_proxy"] is False


def test_settings_report_whether_a_proxy_is_even_configured(client):
    assert "proxy_configured" in client.get("/api/settings").json()


def test_state_carries_the_settings(client):
    assert client.get("/api/state").json()["settings"]["use_proxy"] is False


def test_turning_the_proxy_on_without_credentials_is_refused(client, monkeypatch):
    monkeypatch.setattr(web.webshare, "is_configured", lambda *a, **k: False)
    response = client.post("/api/settings", json={"use_proxy": True})
    assert response.status_code == 400
    assert web.USE_PROXY is False


def test_the_applicant_default_service_is_title_services(client):
    assert web.Applicant().description == "Title Services"


# ------------------------------------------------------- watcher grid publish


def test_watch_result_is_empty_before_any_poll(client):
    body = client.get("/api/watch/result").json()
    assert body["slots"] == [] and body["timetable"] == []


def test_watch_status_exposes_a_result_sequence(client):
    assert isinstance(client.get("/api/watch/status").json()["result_seq"], int)


def test_the_grid_the_watcher_saw_is_published(client):
    # The sequence is deliberately monotonic across runs, so the UI never sees the
    # same number twice and can tell "new grid" from "same grid".
    before = client.get("/api/watch/status").json()["result_seq"]
    web.watcher.arm({"office": "Houston North", "service": "X", "applicant": {}})
    web.watcher.tick()

    assert client.get("/api/watch/status").json()["result_seq"] == before + 1
    assert client.get("/api/watch/result").json()["at"] == "11:00:00"


# --------------------------------------------------------------------- page


def test_the_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "APPOINTMENT DESK" in response.text


def test_the_script_is_served(client):
    assert client.get("/static/app.js").status_code == 200
