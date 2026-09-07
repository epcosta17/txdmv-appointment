"""Pure HTML parsing for the NemoQ wizard. No network, no state - just str in, data out.

Everything here was written against real captured markup; see fixtures/ for the pages
these regexes are pinned to.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from datetime import datetime

# The site posts and renders slot timestamps in US format: '9/8/2026 9:00:00 AM'.
SITE_DATETIME_FORMAT = "%m/%d/%Y %I:%M:%S %p"

_TOKEN_RE = re.compile(
    r'<input[^>]*\bname="__RequestVerificationToken"[^>]*\bvalue="([^"]*)"'
    r'|<input[^>]*\bvalue="([^"]*)"[^>]*\bname="__RequestVerificationToken"',
    re.IGNORECASE,
)

# An available slot is a <div> carrying data-function="timeTableCell". Booked cells
# (.tt-unavailable) and closed days (.tt-closed) render without those data attributes,
# so they drop out on their own - but we double-check the class to stay honest.
_CELL_RE = re.compile(r"<div\b[^>]*\bdata-function=\"timeTableCell\"[^>]*>", re.IGNORECASE)

_SELECT_RE_TEMPLATE = r'<select\b[^>]*\bname="{0}"[^>]*>(.*?)</select>'
_OPTION_RE = re.compile(r'<option\b[^>]*\bvalue="([^"]*)"[^>]*>(.*?)</option>', re.IGNORECASE | re.DOTALL)

_FREETEXT_TEMPLATE = r'{0}.*?class="control-freetext">\s*([^<]+?)\s*<'


@dataclass(frozen=True)
class Slot:
    """One bookable time cell.

    `raw` is the site's own string and the only value that may be posted back as
    ReservedDateTime - reformatting it is how you book the wrong appointment.
    """

    raw: str
    when: datetime
    section_id: str
    service_type_id: str
    label: str

    @property
    def date(self):
        """ISO date, e.g. '2026-09-08'."""
        return self.when.strftime("%Y-%m-%d")

    @property
    def time(self):
        """24-hour time, e.g. '14:30'."""
        return self.when.strftime("%H:%M")

    def __str__(self):
        return "{0} {1}".format(self.date, self.when.strftime("%I:%M %p").lstrip("0"))


def _attr(tag, name):
    match = re.search(r'\b{0}="([^"]*)"'.format(re.escape(name)), tag, re.IGNORECASE)
    return match.group(1) if match else ""


def _text(raw):
    """Unescape entities and collapse whitespace."""
    return re.sub(r"\s+", " ", html_module.unescape(raw)).strip()


def parse_slots(html):
    """Every available slot on a search-results page, sorted chronologically.

    Returns [] for any page that is not a results page (no cells, no exception) so
    callers can distinguish "searched, nothing free" from a parse failure.
    """
    slots = []
    for tag in _CELL_RE.findall(html):
        if "tt-unavailable" in _attr(tag, "class"):
            continue
        raw = _attr(tag, "data-fromdatetime")
        if not raw:
            continue
        try:
            when = datetime.strptime(raw, SITE_DATETIME_FORMAT)
        except ValueError:
            # A slot we cannot parse is a slot we must not book.
            continue
        slots.append(
            Slot(
                raw=raw,
                when=when,
                section_id=_attr(tag, "data-sectionid"),
                service_type_id=_attr(tag, "data-servicetypeid"),
                label=_text(_attr(tag, "aria-label")),
            )
        )
    return sorted(slots, key=lambda slot: slot.when)


_GRID_TAG_RE = re.compile(r"<div\b[^>]*\bclass=\"[^\"]*\btt-(header|cell|closed)\b[^\"]*\"[^>]*>", re.IGNORECASE)
_HEADER_LABEL_RE = re.compile(r"^(.*?),\s*(\d+)\s+Available", re.IGNORECASE | re.DOTALL)


def _column_of(tag):
    """Which day column a grid element sits in."""
    explicit = _attr(tag, "data-col")
    if explicit.isdigit():
        return int(explicit)
    match = re.search(r"grid-column:\s*(\d+)", _attr(tag, "style"))
    return int(match.group(1)) if match else None


def parse_timetable(html):
    """The whole week grid: one entry per day column, cells in rendered order.

    parse_slots() answers "what can I book"; this answers "what does the page look
    like", which is what a calendar UI needs - booked and closed cells included.
    Returns [] for a page with no results grid.
    """
    columns = {}

    for match in _GRID_TAG_RE.finditer(html):
        tag = match.group(0)
        kind = match.group(1).lower()
        classes = _attr(tag, "class")
        column = _column_of(tag)
        if column is None:
            continue
        entry = columns.setdefault(
            column, {"column": column, "title": "", "available": 0, "closed": False, "cells": []}
        )
        label = _text(_attr(tag, "aria-label"))

        if kind == "header":
            heading = _HEADER_LABEL_RE.match(label)
            entry["title"] = heading.group(1).strip() if heading else label
            entry["available"] = int(heading.group(2)) if heading else 0
        elif kind == "closed":
            entry["closed"] = True
        elif "tt-cell-empty" not in classes:
            booked = "tt-unavailable" in classes
            entry["cells"].append({
                "time": label.split(",")[0].strip(),
                "state": "booked" if booked else "free",
                "raw": None if booked else (_attr(tag, "data-fromdatetime") or None),
            })

    return [columns[key] for key in sorted(columns)]


def parse_select_options(html, name):
    """[(value, label)] for a <select name="...">, in document order.

    Empty list when the dropdown is absent.
    """
    match = re.search(_SELECT_RE_TEMPLATE.format(re.escape(name)), html, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    return [(value, _text(label)) for value, label in _OPTION_RE.findall(match.group(1))]


def find_option_value(options, label):
    """Resolve a dropdown label to its value, or None.

    Exact (case-insensitive) match first, then an unambiguous substring match so
    '--office houston north' works without matching Houston South too.
    """
    wanted = label.strip().lower()
    for value, text in options:
        if text.lower() == wanted:
            return value
    partial = [value for value, text in options if wanted in text.lower()]
    return partial[0] if len(partial) == 1 else None


def extract_token(html, form_id=None):
    """The anti-forgery token that must be echoed with the next POST.

    A results page renders two forms with *different* tokens - the search form
    (FormId=1) and the reservation form (FormId=2) - and posting the wrong one is
    rejected. Pass form_id to scope the lookup; the default takes the first token.
    """
    haystack = html
    if form_id is not None:
        haystack = _form_containing(html, form_id) or html
    match = _TOKEN_RE.search(haystack)
    if not match:
        raise ValueError("__RequestVerificationToken not found in response HTML")
    return match.group(1) or match.group(2)


def _form_containing(html, form_id):
    needle = re.compile(r'\bname="FormId"[^>]*\bvalue="{0}"'.format(re.escape(str(form_id))))
    for chunk in html.split("<form")[1:]:
        if needle.search(chunk):
            return chunk
    return None


def parse_heading(html):
    """The <h1> of the current wizard step - handy for error messages."""
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    return _text(match.group(1)) if match else ""


def parse_validation_errors(html):
    """Validation messages ASP.NET renders when a step is rejected."""
    pattern = re.compile(
        r'<span[^>]*class="[^"]*field-validation-error[^"]*"[^>]*>(.*?)</span>'
        r'|<li[^>]*class="[^"]*validation-summary[^"]*"[^>]*>(.*?)</li>',
        re.IGNORECASE | re.DOTALL,
    )
    messages = []
    for match in pattern.finditer(html):
        message = _text(match.group(1) or match.group(2) or "")
        if message and message not in messages:
            messages.append(message)
    return messages


def parse_confirmation(html):
    """Booking details from the final page, or None if this is not that page."""
    number = re.search(_FREETEXT_TEMPLATE.format("Appointment number"), html, re.DOTALL)
    if not number:
        return None

    def field(label):
        match = re.search(_FREETEXT_TEMPLATE.format(">{0}:</strong>".format(label)), html, re.DOTALL)
        return _text(match.group(1)) if match else None

    return {
        "appointment_number": _text(number.group(1)),
        "time": field("Time"),
        "office": field("Office"),
    }
