"""Independent booking lanes.

A lane is one office/service target with everything it needs to act on its own: its
own portal session, its own grid, and its own watcher. Two lanes can therefore chase
openings at two offices at the same time and each book without disturbing the other.

That isolation is not cosmetic. The wizard is a single stateful flow - each search
replaces the page the previous one left behind - so two lanes sharing a session would
race: lane A finds a slot, lane B searches, and A's reservation now refers to a page
that no longer shows it. One session per lane removes the race entirely.
"""

from __future__ import annotations

import threading

from watcher import Watcher
from wizard_session import WizardSession

MAX_LANES = 2


class LaneLimit(Exception):
    """No room for another lane."""


class UnknownLane(Exception):
    """The lane id does not exist (usually a stale browser tab)."""


class Lane:
    def __init__(self, lane_id, office, service, session):
        self.id = lane_id
        self.office = office
        self.service = service
        self.from_date = None
        self.first_available = False
        self.session = session
        self.watcher = Watcher(session)

    def retarget(self, office=None, service=None, from_date=None, first_available=None):
        """Point the lane at an office, service and date.

        Searching and starting a watch both go through here, so a watch can never
        run against a target the operator has since changed on screen. Omitting a
        field leaves it alone; an empty date clears it.
        """
        if office:
            self.office = office
        if service:
            self.service = service
        if from_date is not None:
            self.from_date = from_date or None
        if first_available is not None:
            self.first_available = bool(first_available)

    def search(self, office=None, service=None, from_date=None, first_available=None):
        self.retarget(office, service, from_date, first_available)
        return self.session.search(
            office=self.office,
            service=self.service,
            from_date=self.from_date,
            first_available=self.first_available,
        )

    def result(self):
        """Whatever this lane last saw - from its watcher if watching, else its search."""
        return self.watcher.result() or self.session.last_search

    def close(self):
        self.watcher.stop()

    def status(self):
        return {
            "id": self.id,
            "office": self.office,
            "service": self.service,
            "from_date": self.from_date,
            "first_available": self.first_available,
            "session": self.session.status(),
            "watch": self.watcher.status(),
        }


class LaneManager:
    """Holds the lanes. `session_factory()` returns a fresh WizardSession per lane."""

    def __init__(self, session_factory, max_lanes=MAX_LANES):
        self._lock = threading.RLock()
        self._lanes = {}
        self._next_id = 1
        self.session_factory = session_factory
        self.max_lanes = max_lanes

    def list(self):
        with self._lock:
            return [self._lanes[key] for key in sorted(self._lanes)]

    def get(self, lane_id):
        with self._lock:
            try:
                return self._lanes[int(lane_id)]
            except (KeyError, ValueError, TypeError):
                raise UnknownLane("No existe el carril {0}".format(lane_id))

    def create(self, office, service):
        with self._lock:
            if len(self._lanes) >= self.max_lanes:
                raise LaneLimit(
                    "Máximo {0} vigilancias a la vez".format(self.max_lanes)
                )
            lane_id = self._next_id
            self._next_id += 1
            lane = Lane(lane_id, office, service, self.session_factory())
            self._lanes[lane_id] = lane
            return lane

    def remove(self, lane_id):
        lane = self.get(lane_id)
        with self._lock:
            self._lanes.pop(lane.id, None)
        lane.close()
        return lane

    def close_all(self):
        """Drop every lane - used when the connection changes under them."""
        for lane in self.list():
            lane.close()
        with self._lock:
            self._lanes.clear()

    def status(self):
        return {
            "max_lanes": self.max_lanes,
            "lanes": [lane.status() for lane in self.list()],
        }
