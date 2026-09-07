"""Lane isolation. Two lanes must never share a portal session."""

import pytest

from lanes import Lane, LaneLimit, LaneManager, UnknownLane


class FakeSession:
    counter = 0

    def __init__(self):
        FakeSession.counter += 1
        self.name = "session-{0}".format(FakeSession.counter)
        self.searches = []
        self.last_search = None
        self.booked = []

    def search(self, office, service, from_date=None, first_available=False):
        self.searches.append((office, service, from_date, first_available))
        self.last_search = {"slots": [], "timetable": [], "office": office}
        return self.last_search

    def book(self, raw, applicant):
        self.booked.append(raw)
        return {"appointment_number": "1", "time": raw, "office": "x"}

    def status(self):
        return {"open": True, "direct": True, "expires_in": 100, "age_seconds": 1, "proxy": None}


@pytest.fixture
def manager():
    return LaneManager(FakeSession, max_lanes=2)


def test_each_lane_gets_its_own_session(manager):
    a = manager.create("Houston North", "Title Companies and Runners")
    b = manager.create("Houston South", "All other transactions")
    assert a.session is not b.session
    assert a.session.name != b.session.name


def test_each_lane_gets_its_own_watcher(manager):
    a = manager.create("A", "X")
    b = manager.create("B", "Y")
    assert a.watcher is not b.watcher
    assert a.watcher.session is a.session


def test_searching_one_lane_leaves_the_other_untouched(manager):
    a = manager.create("Houston North", "X")
    b = manager.create("Houston South", "Y")
    a.search()
    assert a.session.searches == [("Houston North", "X", None, False)]
    assert b.session.searches == []


def test_the_lane_limit_is_enforced(manager):
    manager.create("A", "X")
    manager.create("B", "Y")
    with pytest.raises(LaneLimit):
        manager.create("C", "Z")


def test_removing_a_lane_frees_a_slot(manager):
    a = manager.create("A", "X")
    manager.create("B", "Y")
    manager.remove(a.id)
    assert manager.create("C", "Z").office == "C"


def test_removing_a_lane_stops_its_watcher(manager):
    lane = manager.create("A", "X")
    lane.watcher.arm({"office": "A", "service": "X", "applicant": {}})
    manager.remove(lane.id)
    assert lane.watcher.status()["state"] == "idle"


def test_an_unknown_lane_is_reported(manager):
    with pytest.raises(UnknownLane):
        manager.get(99)


def test_lane_ids_are_not_reused_after_removal(manager):
    a = manager.create("A", "X")
    manager.remove(a.id)
    assert manager.create("B", "Y").id != a.id


# ---------------------------------------------------------------- lane state


def test_search_remembers_the_target_for_the_watcher(manager):
    lane = manager.create("Houston North", "X")
    lane.search(office="Houston South", service="Y", from_date="2026-09-08")
    assert (lane.office, lane.service, lane.from_date) == ("Houston South", "Y", "2026-09-08")


def test_an_empty_date_clears_the_stored_one(manager):
    lane = manager.create("A", "X")
    lane.search(from_date="2026-09-08")
    lane.search(from_date="")
    assert lane.from_date is None


def test_result_prefers_what_the_watcher_saw(manager):
    lane = manager.create("A", "X")
    lane.search()
    assert lane.result()["office"] == "A"
    lane.watcher.last_result = {"slots": [], "timetable": [], "office": "from-watcher"}
    assert lane.result()["office"] == "from-watcher"


def test_retarget_changes_the_lane_without_searching(manager):
    lane = manager.create("Houston North", "X")
    lane.retarget(office="Houston South", service="Y", from_date="2026-09-08")

    assert (lane.office, lane.service, lane.from_date) == ("Houston South", "Y", "2026-09-08")
    assert lane.session.searches == []


def test_retarget_leaves_omitted_fields_alone(manager):
    lane = manager.create("Houston North", "X")
    lane.retarget(from_date="2026-09-08")
    assert lane.office == "Houston North" and lane.service == "X"


def test_status_is_json_safe(manager):
    import json

    manager.create("A", "X")
    json.dumps(manager.status())


def test_closing_all_lanes_empties_the_manager(manager):
    manager.create("A", "X")
    manager.create("B", "Y")
    manager.close_all()
    assert manager.status()["lanes"] == []
