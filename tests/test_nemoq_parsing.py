"""Parser tests against real markup captured from the live wizard (see fixtures/)."""

from datetime import datetime
from pathlib import Path

import pytest

from nemoq_parsing import (
    Slot,
    extract_token,
    find_option_value,
    parse_confirmation,
    parse_select_options,
    parse_slots,
    parse_timetable,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def results_html():
    return fixture("search_results.html")


@pytest.fixture(scope="module")
def no_office_html():
    return fixture("search_page_no_office.html")


# ------------------------------------------------------------------- parse_slots


def test_parses_only_available_cells(results_html):
    slots = parse_slots(results_html)
    # The page advertises "11 Available Times"; the booked cells must not appear.
    assert len(slots) == 11


def test_ignores_booked_cells(results_html):
    slots = parse_slots(results_html)
    # 9:30 AM on Sep 8 is rendered as tt-unavailable ("Booked").
    assert all(not (s.date == "2026-09-08" and s.time == "09:30") for s in slots)


def test_slot_carries_the_value_the_post_expects(results_html):
    first = parse_slots(results_html)[0]
    assert first.raw == "9/8/2026 9:00:00 AM"
    assert first.section_id == "448"
    assert first.service_type_id == "3784"


def test_slot_exposes_normalised_date_and_time(results_html):
    first = parse_slots(results_html)[0]
    assert first.date == "2026-09-08"
    assert first.time == "09:00"
    assert first.when == datetime(2026, 9, 8, 9, 0)


def test_afternoon_slots_parse_as_pm(results_html):
    slots = parse_slots(results_html)
    afternoon = [s for s in slots if s.date == "2026-09-08" and s.time == "14:30"]
    assert len(afternoon) == 1
    assert afternoon[0].raw == "9/8/2026 2:30:00 PM"


def test_slots_are_sorted_chronologically(results_html):
    slots = parse_slots(results_html)
    assert [s.when for s in slots] == sorted(s.when for s in slots)


def test_first_slot_is_the_calendar_default(results_html):
    # The no-flags path books slots[0]; pin what that resolves to.
    assert parse_slots(results_html)[0].raw == "9/8/2026 9:00:00 AM"


def test_page_without_results_yields_no_slots(no_office_html):
    assert parse_slots(no_office_html) == []


# -------------------------------------------------------------- parse_timetable
# The UI renders booked and closed cells too, so the whole grid has to be read -
# not just the bookable slots.


def test_reads_every_day_column(results_html):
    assert len(parse_timetable(results_html)) == 7


def test_column_carries_its_day_and_count(results_html):
    tuesday = parse_timetable(results_html)[2]
    assert "8 September" in tuesday["title"]
    assert tuesday["available"] == 4


def test_closed_days_are_marked(results_html):
    closed = [column["closed"] for column in parse_timetable(results_html)]
    # Mon 7 (today), Thu 10, Fri 11 and Sat 12 render as solid closed blocks.
    assert closed == [False, True, False, False, True, True, True]


def test_cells_keep_the_order_the_site_rendered(results_html):
    tuesday = parse_timetable(results_html)[2]
    assert [cell["state"] for cell in tuesday["cells"]] == [
        "free", "booked", "free", "booked", "booked", "free", "free"
    ]


def test_booked_cells_expose_their_time_but_no_slot(results_html):
    booked = parse_timetable(results_html)[2]["cells"][1]
    assert booked["time"] == "9:30 AM"
    assert booked["raw"] is None


def test_free_cells_carry_the_bookable_value(results_html):
    free = parse_timetable(results_html)[2]["cells"][0]
    assert free["time"] == "9:00 AM"
    assert free["raw"] == "9/8/2026 9:00:00 AM"


def test_closed_column_has_no_cells(results_html):
    assert parse_timetable(results_html)[1]["cells"] == []


def test_timetable_of_a_page_without_results_is_empty(no_office_html):
    assert parse_timetable(no_office_html) == []


# --------------------------------------------------------- dropdown resolution


def test_reads_the_office_dropdown(results_html):
    options = parse_select_options(results_html, "SectionId")
    assert ("189", "Houston North") in options
    assert ("448", "Houston South") in options


def test_resolves_office_by_name(results_html):
    options = parse_select_options(results_html, "SectionId")
    assert find_option_value(options, "Houston North") == "189"


def test_office_lookup_is_case_insensitive(results_html):
    options = parse_select_options(results_html, "SectionId")
    assert find_option_value(options, "houston north") == "189"


def test_service_types_are_scoped_to_the_selected_office(results_html, no_office_html):
    # Before an office is picked the site only offers generic placeholders...
    before = parse_select_options(no_office_html, "ServiceTypeId")
    assert find_option_value(before, "Title Companies and Runners") is None
    # ...and only re-renders the real ones once SectionId is set.
    after = parse_select_options(results_html, "ServiceTypeId")
    assert find_option_value(after, "Title Companies and Runners") == "3784"


def test_placeholder_option_is_reported(no_office_html):
    options = parse_select_options(no_office_html, "ServiceTypeId")
    assert options[0] == ("1231", "Please Select...")


def test_unknown_option_returns_none(results_html):
    options = parse_select_options(results_html, "SectionId")
    assert find_option_value(options, "Guadalajara") is None


# ------------------------------------------------------------------ misc pages


def test_extracts_anti_forgery_token(results_html):
    token = extract_token(results_html)
    assert token.startswith("u7ctO2gZHHjyKb08b1k4jDKxkCh9LNlWridPhkjCT277")


def test_parses_the_confirmation_page():
    info = parse_confirmation(fixture("confirmation.html"))
    assert info["appointment_number"] == "1881183439"
    assert info["time"] == "09/08/2026 9:00 AM"
    assert info["office"] == "Houston South"


def test_non_confirmation_page_is_not_mistaken_for_success(results_html):
    assert parse_confirmation(results_html) is None
