"""Turning the slots the site offered into the one slot we book.

Precedence, in this order:
    1. --menu          ask the operator, ignoring everything else
    2. --date/--time   an exact match, or an error naming what was actually free
    3. neither         the first slot the calendar showed
"""

from __future__ import annotations

from datetime import datetime

_TIME_FORMATS = ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%H%M")


class NoSlotsAvailable(Exception):
    """The search came back empty."""


class SlotNotAvailable(Exception):
    """The requested date/time was not among the slots on offer."""


def normalise_time(value):
    """'2:30 PM', '9:30', '14:30' -> '14:30' / '09:30' / '14:30'."""
    text = " ".join(value.strip().upper().split())
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M")
        except ValueError:
            continue
    raise SlotNotAvailable("Could not read '{0}' as a time (try 09:30 or 2:30 PM)".format(value))


def describe(slots):
    """Multi-line 'what was actually free', for error messages."""
    lines = []
    for slot in slots:
        lines.append("  {0} {1}".format(slot.date, slot.time))
    return "\n".join(lines)


def choose_slot(slots, date=None, time=None, menu=False, input_fn=input, print_fn=print):
    if not slots:
        raise NoSlotsAvailable("No available time slots were returned for that search")

    if menu:
        return _from_menu(slots, input_fn, print_fn)

    if date or time:
        return _from_filters(slots, date, time)

    return slots[0]


def _from_filters(slots, date, time):
    matches = slots
    if date:
        matches = [slot for slot in matches if slot.date == date]
    if time:
        wanted = normalise_time(time)
        matches = [slot for slot in matches if slot.time == wanted]

    if not matches:
        raise SlotNotAvailable(
            "No slot at {0}. Available:\n{1}".format(
                " ".join(filter(None, [date, time])), describe(slots)
            )
        )
    # Already sorted chronologically, so the first match is the earliest one that fits.
    return matches[0]


def _from_menu(slots, input_fn, print_fn):
    print_fn("\nAvailable appointments:")
    for index, slot in enumerate(slots, start=1):
        print_fn("  {0:>2}. {1}".format(index, slot.label or str(slot)))

    while True:
        answer = input_fn("\nPick one [1-{0}, blank = 1]: ".format(len(slots))).strip()
        if not answer:
            return slots[0]
        try:
            choice = int(answer)
        except ValueError:
            print_fn("Not a number.")
            continue
        if 1 <= choice <= len(slots):
            return slots[choice - 1]
        print_fn("Out of range.")
