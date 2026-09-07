"""Selection precedence: --menu wins, then --date/--time, then the calendar default."""

from pathlib import Path

import pytest

from nemoq_parsing import parse_slots
from slot_picker import NoSlotsAvailable, SlotNotAvailable, choose_slot

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def slots():
    return parse_slots((FIXTURES / "search_results.html").read_text(encoding="utf-8"))


# --------------------------------------------------------------- default path


def test_no_arguments_takes_the_first_slot_in_the_calendar(slots):
    assert choose_slot(slots).raw == "9/8/2026 9:00:00 AM"


def test_empty_slot_list_is_an_error(slots):
    with pytest.raises(NoSlotsAvailable):
        choose_slot([])


# ------------------------------------------------------------------ flag path


def test_date_and_time_select_an_exact_slot(slots):
    picked = choose_slot(slots, date="2026-09-09", time="14:30")
    assert picked.raw == "9/9/2026 2:30:00 PM"


def test_date_alone_takes_the_earliest_slot_that_day(slots):
    assert choose_slot(slots, date="2026-09-09").raw == "9/9/2026 9:00:00 AM"


def test_time_alone_takes_the_earliest_day_offering_it(slots):
    assert choose_slot(slots, time="09:30").raw == "9/9/2026 9:30:00 AM"


def test_twelve_hour_time_is_accepted(slots):
    assert choose_slot(slots, date="2026-09-09", time="2:30 PM").raw == "9/9/2026 2:30:00 PM"


def test_time_without_leading_zero_is_accepted(slots):
    assert choose_slot(slots, date="2026-09-09", time="9:30").raw == "9/9/2026 9:30:00 AM"


def test_unavailable_slot_is_refused_rather_than_approximated(slots):
    # 9:30 AM on Sep 8 is booked; picking the neighbouring 10:00 would book the wrong slot.
    with pytest.raises(SlotNotAvailable):
        choose_slot(slots, date="2026-09-08", time="09:30")


def test_the_error_lists_what_is_actually_available(slots):
    with pytest.raises(SlotNotAvailable) as excinfo:
        choose_slot(slots, date="2026-09-08", time="09:30")
    assert "09:00" in str(excinfo.value)


def test_unknown_date_is_refused(slots):
    with pytest.raises(SlotNotAvailable):
        choose_slot(slots, date="2026-12-25")


# ------------------------------------------------------------------ menu path


def test_menu_takes_precedence_over_flags(slots):
    # --menu is checked first, so the date/time flags must be ignored when both are given.
    picked = choose_slot(slots, date="2026-09-08", time="09:00", menu=True, input_fn=lambda _: "3")
    assert picked.raw == "9/8/2026 2:30:00 PM"


def test_menu_selection_is_one_based(slots):
    assert choose_slot(slots, menu=True, input_fn=lambda _: "1").raw == "9/8/2026 9:00:00 AM"


def test_menu_reprompts_on_invalid_input(slots):
    answers = iter(["cero", "99", "2"])
    picked = choose_slot(slots, menu=True, input_fn=lambda _: next(answers))
    assert picked.raw == "9/8/2026 10:00:00 AM"


def test_menu_empty_answer_takes_the_first_slot(slots):
    assert choose_slot(slots, menu=True, input_fn=lambda _: "").raw == "9/8/2026 9:00:00 AM"
