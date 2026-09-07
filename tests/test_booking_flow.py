"""Wizard mechanics against the captured pages, with a fake session instead of network."""

from datetime import date
from pathlib import Path

import pytest

from nemoq_booking import BookingError, NemoQBooking
from nemoq_parsing import parse_slots
from webshare import Proxy

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.url = "https://nqa3.nemoqappointment.com/Booking/Booking/Index/uh5d75sjtv4"

    def raise_for_status(self):
        pass


class FakeSession:
    """Serves canned pages and records what was sent."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.sent = []
        self.proxies = {}

    def _next_page(self):
        # Keep serving the last page once the queue drains, so a test only has to
        # queue the pages it actually asserts on.
        page = self.pages.pop(0) if len(self.pages) > 1 else (self.pages[0] if self.pages else "")
        return FakeResponse(page)

    def get(self, url, **kwargs):
        self.sent.append({"method": "GET", "url": url, **kwargs})
        return self._next_page()

    def post(self, url, **kwargs):
        self.sent.append({"method": "POST", "url": url, **kwargs})
        return self._next_page()


@pytest.fixture
def results_html():
    return fixture("search_results.html")


def posted(session):
    return dict(session.sent[-1]["data"])


# ------------------------------------------------------- anti-forgery scoping


def test_reserve_uses_the_reservation_forms_token(results_html):
    # The results page carries two tokens; posting the search form's one is rejected.
    session = FakeSession([fixture("personal_data.html")])
    booking = NemoQBooking(session=session)
    booking.html = results_html

    booking.reserve(parse_slots(results_html)[0])

    assert posted(session)["__RequestVerificationToken"].startswith("ZgZi-EE8v-ZQcRufQTkY")


def test_search_uses_the_search_forms_token(results_html):
    session = FakeSession([results_html])
    booking = NemoQBooking(session=session)
    booking.html = results_html

    booking.search(section_id="189", service_type_id="3784", from_date="2026-09-08")

    assert posted(session)["__RequestVerificationToken"].startswith("u7ctO2gZHHjyKb08b1k4")


# ------------------------------------------------------------ reserve payload


def test_reserve_posts_the_sites_own_datetime_string(results_html):
    session = FakeSession([fixture("personal_data.html")])
    booking = NemoQBooking(session=session)
    booking.html = results_html

    booking.reserve(parse_slots(results_html)[0])

    fields = posted(session)
    assert fields["ReservedDateTime"] == "9/8/2026 9:00:00 AM"
    assert fields["FormId"] == "2"


def test_reserve_takes_the_ids_from_the_slot_not_from_us(results_html):
    # Booking an office we were never offered is the failure mode worth preventing.
    session = FakeSession([fixture("personal_data.html")])
    booking = NemoQBooking(session=session)
    booking.html = results_html

    booking.reserve(parse_slots(results_html)[0])

    fields = posted(session)
    assert fields["ReservedSectionId"] == "448"
    assert fields["ReservedServiceTypeId"] == "3784"


# ---------------------------------------------------------------- searching


def test_first_available_presses_the_sites_own_button(results_html):
    session = FakeSession([results_html])
    booking = NemoQBooking(session=session)
    booking.html = results_html

    booking.search(section_id="189", service_type_id="3784", first_available=True)

    fields = posted(session)
    assert "TimeSearchFirstAvailableButton" in fields
    assert "TimeSearchButton" not in fields


def test_a_date_search_presses_search(results_html):
    session = FakeSession([results_html])
    booking = NemoQBooking(session=session)
    booking.html = results_html

    booking.search(section_id="189", service_type_id="3784", from_date="2026-09-08")

    fields = posted(session)
    assert fields["TimeSearchButton"] == "Search"
    assert fields["FromDateString"] == "2026-09-08"


def test_first_available_is_preceded_by_a_plain_search(results_html):
    # Pressed cold, the site's First Available button renders no grid at all - a
    # normal search has to commit the office/type to the session state first.
    session = FakeSession([results_html, results_html, results_html])
    booking = NemoQBooking(session=session)
    booking.html = results_html

    booking.find_slots(office="Houston South", service="Title Companies and Runners",
                       first_available=True)

    searches = [dict(call["data"]) for call in session.sent if call["method"] == "POST"]
    assert "TimeSearchButton" in searches[-2]
    assert "TimeSearchFirstAvailableButton" in searches[-1]


def test_the_default_search_never_presses_first_available(results_html):
    # The visible 3-day window is what the no-flags default wants; jumping past it
    # would silently book a date the operator never saw.
    session = FakeSession([results_html])
    booking = NemoQBooking(session=session)
    booking.html = results_html

    booking.find_slots(office="Houston South", service="Title Companies and Runners")

    searches = [dict(call["data"]) for call in session.sent if call["method"] == "POST"]
    assert all("TimeSearchFirstAvailableButton" not in fields for fields in searches)


def test_the_default_search_starts_from_today(results_html):
    session = FakeSession([results_html])
    booking = NemoQBooking(session=session)
    booking.html = results_html

    booking.find_slots(office="Houston South", service="Title Companies and Runners")

    searches = [dict(call["data"]) for call in session.sent if call["method"] == "POST"]
    assert searches[-1]["FromDateString"] == date.today().isoformat()


def test_first_available_always_carries_a_concrete_date(results_html):
    # An empty FromDateString makes the button render a page with no grid at all.
    session = FakeSession([results_html])
    booking = NemoQBooking(session=session)
    booking.html = results_html

    booking.find_slots(office="Houston South", service="Title Companies and Runners",
                       first_available=True)

    searches = [dict(call["data"]) for call in session.sent if call["method"] == "POST"]
    assert searches[-1]["FromDateString"] == date.today().isoformat()


# ------------------------------------------------------------ office lookup


def test_resolves_houston_north_from_the_live_dropdown(results_html):
    booking = NemoQBooking(session=FakeSession([]))
    booking.html = results_html
    assert booking.resolve_office("Houston North") == "189"


def test_unknown_office_is_reported_with_the_real_list(results_html):
    booking = NemoQBooking(session=FakeSession([]))
    booking.html = results_html
    with pytest.raises(BookingError) as excinfo:
        booking.resolve_office("Guadalajara")
    assert "Houston North" in str(excinfo.value)


# --------------------------------------------------------------- step guards


def test_a_step_that_did_not_advance_raises(results_html):
    # Posting personal data but landing back on 'Select time' means the step failed.
    session = FakeSession([results_html])
    booking = NemoQBooking(session=session)
    booking.html = fixture("personal_data.html")

    with pytest.raises(BookingError) as excinfo:
        booking.submit_booking_fields(description="d", first_name="a", last_name="b")
    assert "Select time" in str(excinfo.value)


# --------------------------------------------------------------------- proxy


def test_the_proxy_is_pinned_onto_the_session():
    proxy = Proxy(host="1.2.3.4", port=8080, username="u", password="p", country_code="US")
    session = FakeSession([])
    NemoQBooking(session=session, proxy=proxy)
    assert session.proxies["https"] == "http://u:p@1.2.3.4:8080"


def test_without_a_proxy_the_session_is_left_alone():
    session = FakeSession([])
    NemoQBooking(session=session, proxy=None)
    assert session.proxies == {}
