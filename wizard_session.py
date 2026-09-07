"""One long-lived wizard session, shared by the web UI - exactly what the CLI does.

The NemoQ wizard is stateful: a slot can only be reserved from the same session that
listed it. So the browser does not get a fresh flow per request; it gets a handle on
one live NemoQBooking that stays parked on the "Select time" step between requests.

Searching again is a single POST on that page, so changing office, service or date
costs one request instead of reopening the whole wizard.
"""

from __future__ import annotations

import threading
import time
from datetime import date

import requests

import webshare
from nemoq_booking import BASE, BASE_HEADERS, SEQ, BookingError, NemoQBooking, open_wizard
from nemoq_parsing import find_option_value, parse_slots, parse_timetable

# The wizard idles out after 60 minutes; refresh well before that and treat anything
# older as gone rather than discovering it mid-reservation.
SESSION_MAX_IDLE = 45 * 60
KEEPALIVE_EVERY = 10 * 60


class SessionExpired(Exception):
    """The wizard session was lost; the caller should search again."""


class WizardSession:
    """Thread-safe holder for the live wizard.

    Every public method takes the lock: the watcher thread and the browser's requests
    both drive the same requests.Session, and interleaving them would scramble the
    anti-forgery token exchange.
    """

    def __init__(self, pool=None, attempts=3):
        self._lock = threading.RLock()
        self._pool = pool
        self._attempts = attempts
        self._booking = None
        self._proxy = None
        self._opened_at = 0.0
        self._touched_at = 0.0
        self._last_keepalive = 0.0
        self.last_search = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def proxy(self):
        return self._proxy

    def _verifier(self):
        if self._pool is None:
            return None
        return webshare.make_site_verifier(
            "{0}/Booking/Booking/Index/{1}".format(BASE, SEQ), headers=BASE_HEADERS
        )

    def _stale(self):
        return self._booking is None or (time.time() - self._touched_at) > SESSION_MAX_IDLE

    def _open(self):
        """Open a wizard, replacing a dead proxy if needed. Nothing is held yet."""
        verifier = self._verifier()
        last_error = None
        for attempt in range(1, self._attempts + 1):
            proxy = self._pool.pick(verify=verifier) if self._pool is not None else None
            try:
                self._booking = open_wizard(proxy)
                self._proxy = proxy
                self._opened_at = self._touched_at = time.time()
                return self._booking
            except Exception as error:
                last_error = error
                if self._pool is None or attempt == self._attempts:
                    raise
                self._pool.discard(proxy)
        raise BookingError("Could not open the wizard: {0}".format(last_error))

    # A lost session and a dead proxy both surface as a failed read; both are fixed
    # the same way - throw the connection away and open a new one.
    RETRYABLE = (BookingError, ValueError, requests.RequestException)

    def _with_retry(self, operation):
        """Run an idempotent read, recovering once from a dead proxy or lost session.

        Only for reads. Booking is never retried this way: a reservation that failed
        halfway may still have gone through, and a blind retry could double-book.
        """
        try:
            return operation()
        except self.RETRYABLE as error:
            if isinstance(error, requests.RequestException) and self._pool and self._proxy:
                self._pool.discard(self._proxy)
            self._open()
            return operation()

    def ensure(self):
        with self._lock:
            if self._stale():
                self._open()
            return self._booking

    def reset(self):
        with self._lock:
            self._booking = None
            self.last_search = None

    def status(self):
        with self._lock:
            age = time.time() - self._opened_at if self._booking else 0
            return {
                "open": self._booking is not None and not self._stale(),
                "age_seconds": int(age),
                "expires_in": max(0, int(SESSION_MAX_IDLE - (time.time() - self._touched_at)))
                if self._booking
                else 0,
                "proxy": repr(self._proxy) if self._proxy else None,
                # Reflects how this run is configured, not just which proxy is live
                # yet: the proxy is only picked when the wizard actually opens.
                "direct": self._pool is None,
            }

    def keepalive(self):
        """Called on a timer by the page; cheap and idempotent."""
        with self._lock:
            if self._booking is None or self._stale():
                return False
            if time.time() - self._last_keepalive < KEEPALIVE_EVERY:
                return True
            try:
                ok = self._booking.keepalive()
            except Exception:
                return False
            if ok:
                self._last_keepalive = self._touched_at = time.time()
            return ok

    # -------------------------------------------------------------------- reading

    def offices(self):
        def read():
            booking = self.ensure()
            return [
                {"id": value, "name": label}
                for value, label in booking.offices()
                if value != "0"
            ]

        with self._lock:
            return self._with_retry(read)

    def services(self, office):
        with self._lock:
            return self._with_retry(lambda: self._services(office))

    def _services(self, office):
        booking = self.ensure()
        section_id = booking.resolve_office(office)
        options = booking._fetch_service_types(section_id)
        if not options:
            booking.search(
                section_id=section_id,
                service_type_id=(booking.service_types() or [("0", "")])[0][0],
                from_date=date.today().isoformat(),
            )
            options = booking.service_types()
        self._touched_at = time.time()
        return [
            {"id": value, "name": label}
            for value, label in options
            if "please select" not in label.lower()
        ]

    def search(self, office, service, from_date=None, first_available=False):
        """Search on the live session, reopening once if it had expired or died."""
        with self._lock:
            result = self._with_retry(
                lambda: self._search(office, service, from_date, first_available)
            )
            self.last_search = result
            self._touched_at = time.time()
            return result

    def _search(self, office, service, from_date, first_available):
        booking = self.ensure()
        started = time.time()
        slots = booking.find_slots(
            office=office, service=service, from_date=from_date, first_available=first_available
        )
        return {
            "office": office,
            "service": service,
            "from_date": from_date or date.today().isoformat(),
            "slots": [
                {
                    "raw": slot.raw,
                    "date": slot.date,
                    "time": slot.time,
                    "label": slot.label,
                    "section_id": slot.section_id,
                    "service_type_id": slot.service_type_id,
                }
                for slot in slots
            ],
            "timetable": parse_timetable(booking.html),
            "latency": round(time.time() - started, 2),
            "at": time.strftime("%H:%M:%S"),
        }

    # -------------------------------------------------------------------- booking

    def book(self, raw_datetime, applicant):
        """Reserve and confirm, on the session that listed the slot.

        The slot is matched against what the live page currently offers, so a stale
        click from the browser is refused instead of booking something else.
        """
        with self._lock:
            booking = self.ensure()
            slot = next((s for s in parse_slots(booking.html) if s.raw == raw_datetime), None)
            if slot is None:
                raise SessionExpired(
                    "That time is no longer offered on the current results page. Search again."
                )

            booking.reserve(slot)
            booking.submit_booking_fields(
                description=applicant.get("description") or "Title Service",
                first_name=applicant["first_name"],
                last_name=applicant["last_name"],
            )
            booking.submit_contact_info(
                email=applicant["email"],
                phone=applicant["phone"],
                free_text=applicant.get("details", ""),
            )
            details = booking.confirm()
            # The wizard is finished; the next search needs a fresh one.
            self.reset()
            return details
