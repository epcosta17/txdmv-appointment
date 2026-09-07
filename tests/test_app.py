"""API layer over lanes, with portal sessions stubbed out. No network."""

import pytest
from fastapi.testclient import TestClient

import app as web
from lanes import LaneManager

APPLICANT = {
    "first_name": "Marisol",
    "last_name": "Trevino",
    "email": "m@lonestar.com",
    "phone": "7135550148",
}


class StubSession:
    def __init__(self):
        self.searches = []
        self.booked = []
        self.last_search = None

    def status(self):
        return {"open": True, "age_seconds": 12, "expires_in": 2400, "proxy": None, "direct": True}

    def offices(self):
        return [{"id": "189", "name": "Houston North"}, {"id": "448", "name": "Houston South"}]

    def services(self, office):
        return [{"id": "1275", "name": "Title Companies and Runners"}]

    def search(self, office, service, from_date=None, first_available=False):
        self.searches.append((office, service, from_date, first_available))
        self.last_search = {"slots": [], "timetable": [], "latency": 0.4, "at": "11:00:00",
                            "office": office}
        return self.last_search

    def book(self, raw, applicant):
        self.booked.append((raw, applicant))
        return {"appointment_number": "1881183439", "time": raw, "office": "Houston North"}

    def keepalive(self):
        return True


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web, "lanes", LaneManager(StubSession, max_lanes=2))
    monkeypatch.setattr(web, "_picker_session", StubSession())
    web.build_lanes()
    with TestClient(web.app) as c:
        yield c
    web.lanes.close_all()


def lane_ids(client):
    return [lane["id"] for lane in client.get("/api/lanes").json()["lanes"]]


# --------------------------------------------------------------------- state


def test_the_two_lanes_exist_from_the_start(client):
    body = client.get("/api/state").json()
    assert [lane["office"] for lane in body["lanes"]] == ["Houston North", "Houston South"]
    assert body["max_lanes"] == 2
    assert body["settings"]["use_proxy"] is False


def test_offices_come_from_the_picker_session(client):
    names = [o["name"] for o in client.get("/api/offices").json()["offices"]]
    assert "Houston North" in names


def test_services_are_scoped_to_the_office(client):
    body = client.get("/api/services", params={"office": "Houston North"}).json()
    assert body["services"][0]["id"] == "1275"


# --------------------------------------------------------------------- lanes


def test_lanes_start_idle(client):
    assert all(l["watch"]["state"] == "idle" for l in client.get("/api/lanes").json()["lanes"])


def test_lanes_cannot_be_created_over_the_api(client):
    # The pair is fixed, so the endpoint is gone rather than merely guarded.
    assert client.post("/api/lanes", json={"office": "Austin", "service": "X"}).status_code == 405


def test_lanes_cannot_be_deleted_over_the_api(client):
    # 404 rather than 405: the route is gone entirely, not merely method-guarded.
    assert client.delete(f"/api/lanes/{lane_ids(client)[0]}").status_code == 404


def test_rebuilding_restores_exactly_the_two_lanes(client):
    web.build_lanes()
    assert [l["office"] for l in client.get("/api/lanes").json()["lanes"]] == [
        "Houston North", "Houston South"
    ]


def test_acting_on_an_unknown_lane_is_a_404(client):
    assert client.post("/api/lanes/99/search", json={}).status_code == 404


def test_searching_one_lane_does_not_touch_the_other(client):
    a, b = lane_ids(client)

    client.post(f"/api/lanes/{a}/search", json={})

    assert web.lanes.get(a).session.searches and not web.lanes.get(b).session.searches


def test_search_result_carries_the_lanes_office(client):
    body = client.post(f"/api/lanes/{lane_ids(client)[1]}/search", json={}).json()
    assert body["office"] == "Houston South"


def test_a_lane_remembers_a_changed_target(client):
    lane = lane_ids(client)[0]
    client.post(f"/api/lanes/{lane}/search",
                json={"office": "Austin", "service": "Y", "from_date": "2026-09-08"})
    assert web.lanes.get(lane).office == "Austin"


def test_result_endpoint_is_empty_before_searching(client):
    body = client.get(f"/api/lanes/{lane_ids(client)[0]}/result").json()
    assert body["slots"] == []


# ------------------------------------------------------------------- booking


def test_booking_without_personal_data_is_refused(client):
    lane = lane_ids(client)[0]
    response = client.post(f"/api/lanes/{lane}/book",
                           json={"raw": "9/8/2026 9:00:00 AM", "applicant": {}})
    assert response.status_code == 400
    assert web.lanes.get(lane).session.booked == []


def test_each_lane_books_on_its_own_session(client):
    a, b = lane_ids(client)

    client.post(f"/api/lanes/{a}/book",
                json={"raw": "9/8/2026 9:00:00 AM", "applicant": APPLICANT})

    assert web.lanes.get(a).session.booked
    assert not web.lanes.get(b).session.booked


def test_both_lanes_can_book_in_parallel(client):
    a, b = lane_ids(client)

    for lane, raw in ((a, "9/8/2026 9:00:00 AM"), (b, "9/9/2026 2:30:00 PM")):
        assert client.post(f"/api/lanes/{lane}/book",
                           json={"raw": raw, "applicant": APPLICANT}).status_code == 200

    assert web.lanes.get(a).session.booked[0][0] == "9/8/2026 9:00:00 AM"
    assert web.lanes.get(b).session.booked[0][0] == "9/9/2026 2:30:00 PM"


# -------------------------------------------------------------------- watch


def test_auto_book_without_personal_data_is_refused(client):
    lane = lane_ids(client)[0]
    response = client.post(f"/api/lanes/{lane}/watch",
                           json={"auto_book": True, "applicant": {}})
    assert response.status_code == 400
    assert web.lanes.get(lane).watcher.status()["state"] == "idle"


def test_notify_mode_starts_without_personal_data(client):
    body = client.post(f"/api/lanes/{lane_ids(client)[0]}/watch",
                       json={"auto_book": False, "applicant": {}}).json()
    assert body["watch"]["state"] == "watching"
    assert body["watch"]["auto_book"] is False


def test_a_watch_inherits_its_lanes_target(client):
    lane = lane_ids(client)[1]
    body = client.post(f"/api/lanes/{lane}/watch", json={"applicant": {}}).json()
    assert body["watch"]["office"] == "Houston South"
    assert body["watch"]["service"] == "Title Companies and Runners"


def test_a_watch_without_filters_accepts_any_slot(client):
    # No floor sent means no floor applied; the watcher takes whatever shows up.
    lane = lane_ids(client)[0]
    client.post(f"/api/lanes/{lane}/watch", json={"applicant": {}})
    config = web.lanes.get(lane).watcher.config
    assert config["time_from"] is None and config["date_to"] is None


def test_a_watch_carries_the_time_window(client):
    lane = lane_ids(client)[0]
    body = client.post(f"/api/lanes/{lane}/watch",
                       json={"applicant": {}, "time_from": "10:30", "time_to": "15:00"}).json()
    config = web.lanes.get(lane).watcher.config
    assert (config["time_from"], config["time_to"]) == ("10:30", "15:00")
    assert (body["watch"]["time_from"], body["watch"]["time_to"]) == ("10:30", "15:00")


def test_a_watch_uses_the_target_sent_with_it_not_the_last_search(client):
    # The dangerous version of this bug: change the office, press watch without
    # searching, and auto-booking would reserve at the office you left behind.
    lane = lane_ids(client)[0]
    body = client.post(f"/api/lanes/{lane}/watch",
                       json={"applicant": {}, "office": "Austin",
                             "service": "All other transactions"}).json()

    assert body["watch"]["office"] == "Austin"
    assert body["watch"]["service"] == "All other transactions"
    assert web.lanes.get(lane).office == "Austin"


def test_a_watch_uses_the_date_sent_with_it(client):
    lane = lane_ids(client)[0]
    client.post(f"/api/lanes/{lane}/watch",
                json={"applicant": {}, "date_from": "2026-09-11"})
    assert web.lanes.get(lane).watcher.config["date_from"] == "2026-09-11"


def test_an_empty_date_clears_the_lanes_date(client):
    lane = lane_ids(client)[0]
    client.post(f"/api/lanes/{lane}/search", json={"from_date": "2026-09-08"})
    client.post(f"/api/lanes/{lane}/watch", json={"applicant": {}, "date_from": ""})
    assert web.lanes.get(lane).watcher.config["date_from"] is None


def test_a_watch_without_a_target_keeps_the_lanes_own(client):
    lane = lane_ids(client)[0]
    client.post(f"/api/lanes/{lane}/search", json={"office": "Waco", "service": "Y"})
    body = client.post(f"/api/lanes/{lane}/watch", json={"applicant": {}}).json()
    assert body["watch"]["office"] == "Waco"


def test_either_end_of_the_window_may_be_omitted(client):
    lane = lane_ids(client)[0]
    client.post(f"/api/lanes/{lane}/watch", json={"applicant": {}, "time_to": "12:00"})
    config = web.lanes.get(lane).watcher.config
    assert config["time_from"] is None and config["time_to"] == "12:00"


def test_watching_one_lane_leaves_the_other_idle(client):
    a, b = lane_ids(client)

    client.post(f"/api/lanes/{a}/watch", json={"applicant": {}})

    states = {l["id"]: l["watch"]["state"] for l in client.get("/api/lanes").json()["lanes"]}
    assert states[a] == "watching" and states[b] == "idle"


def test_both_lanes_can_watch_at_once(client):
    for lane in lane_ids(client):
        client.post(f"/api/lanes/{lane}/watch", json={"applicant": {}})

    states = [l["watch"]["state"] for l in client.get("/api/lanes").json()["lanes"]]
    assert states == ["watching", "watching"]


def test_a_watch_can_be_stopped(client):
    lane = lane_ids(client)[0]
    client.post(f"/api/lanes/{lane}/watch", json={"applicant": {}})
    assert client.delete(f"/api/lanes/{lane}/watch").json()["watch"]["state"] == "idle"


# ------------------------------------------------------------------ settings


def test_the_proxy_is_off_by_default(client):
    assert client.get("/api/settings").json()["use_proxy"] is False


def test_turning_the_proxy_on_without_credentials_is_refused(client, monkeypatch):
    monkeypatch.setattr(web.webshare, "is_configured", lambda *a, **k: False)
    assert client.post("/api/settings", json={"use_proxy": True}).status_code == 400
    assert web.USE_PROXY is False


def test_the_applicant_default_service_is_title_services(client):
    assert web.Applicant().description == "Title Services"


# --------------------------------------------------------------------- page


def test_the_page_is_served(client):
    assert "APPOINTMENT DESK" in client.get("/").text


def test_the_script_is_served(client):
    assert client.get("/static/app.js").status_code == 200
