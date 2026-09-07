"""NemoQ (nqa3.nemoqappointment.com) booking flow for TxDMV appointments.

The site is a classic ASP.NET MVC wizard using Post/Redirect/Get:

    GET  /Booking/Booking/Index/{seq}   -> renders the current wizard step
    POST /Booking/Booking/Next/{seq}    -> validates + advances, redirects back to Index

Three things carry the state:
  * cookies  - ASP.NET_VentusBooking_SessionId, ASP.NET_VentusBooking_SeqGUID,
               __RequestVerificationToken_L0Jvb2tpbmc1 (a requests.Session handles all three)
  * the anti-forgery token - a fresh __RequestVerificationToken hidden input is rendered
               on every page and must be echoed back with the next POST. A results page
               renders TWO forms with different tokens, so the token is looked up per form.
  * the source IP - the session is pinned to it, which is why the proxy is resolved once
               up front and never rotated mid-flow (see webshare.py).

Nothing is sent at import time. Booking only happens with an explicit --run flag.

    python3 nemoq_booking.py --list-slots
    python3 nemoq_booking.py --menu --run
    python3 nemoq_booking.py --date 2026-09-08 --time 09:00 --run
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import requests

import webshare
from nemoq_parsing import (
    extract_token,
    find_option_value,
    parse_confirmation,
    parse_heading,
    parse_select_options,
    parse_slots,
    parse_validation_errors,
)
from slot_picker import NoSlotsAvailable, SlotNotAvailable, choose_slot

BASE = "https://nqa3.nemoqappointment.com"
SEQ = "uh5d75sjtv4"  # ASP.NET_VentusBooking_SeqGUID, also the URL segment
SERVICE_GROUP_ID = 229

DEFAULT_OFFICE = "Houston North"
DEFAULT_SERVICE = "Title Companies and Runners"

# Residential proxies are slow to first byte; without a cap a stalled connection
# would hang the run holding a reserved slot.
REQUEST_TIMEOUT = 60

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-language": "en-MX,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "priority": "u=0, i",
    "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": USER_AGENT,
}

# Every step passes its fields as a list of (name, value) pairs rather than a dict:
# ASP.NET model binding is index/order sensitive, and several fields are intentionally
# sent twice (checkbox + hidden fallback, e.g. AcceptInformationStorage=true&...=false).


class BookingError(Exception):
    """The site rejected a step, or rendered something we did not expect."""


class NemoQBooking:
    def __init__(self, seq=SEQ, session=None, proxy=None):
        self.seq = seq
        self.index_url = "{0}/Booking/Booking/Index/{1}".format(BASE, seq)
        self.next_url = "{0}/Booking/Booking/Next/{1}".format(BASE, seq)
        self.session = session or requests.Session()
        self.proxy = proxy
        if proxy is not None:
            # Fixed for the lifetime of the session: the wizard is IP-bound.
            self.session.proxies.update(proxy.as_requests_proxies())
        self.html = ""  # HTML of the page currently displayed
        self._referer = self.index_url

    # ---------------------------------------------------------------- plumbing

    def _remember(self, response):
        response.raise_for_status()
        self.html = response.text
        # After a POST the browser lands back on Index; mirror whatever URL we ended
        # up on as the referer for the next request.
        self._referer = response.url
        return response

    def token(self, form_id=None):
        return extract_token(self.html, form_id=form_id)

    def get_index(self, first_visit=False):
        """GET the wizard page. first_visit mirrors the browser's sec-fetch-site: none."""
        headers = dict(BASE_HEADERS)
        if first_visit:
            headers["sec-fetch-site"] = "none"
        else:
            headers["sec-fetch-site"] = "same-origin"
            headers["referer"] = self._referer
        return self._remember(
            self.session.get(self.index_url, headers=headers, timeout=REQUEST_TIMEOUT)
        )

    def post_next(self, fields, form_id=None):
        """POST one wizard step, prefixing that form's anti-forgery token."""
        headers = dict(BASE_HEADERS)
        headers.update({
            "content-type": "application/x-www-form-urlencoded",
            "origin": BASE,
            "referer": self._referer,
            "sec-fetch-site": "same-origin",
        })
        data = [("__RequestVerificationToken", self.token(form_id))] + list(fields)
        # allow_redirects (default True) reproduces the POST -> 302 -> GET /Index pair
        # that shows up as two entries in the cURL trace.
        return self._remember(
            self.session.post(self.next_url, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
        )

    def keepalive(self):
        """Push back the wizard's 60-minute idle timeout.

        The site's own page does this from JavaScript; we call the same endpoint so a
        session can stay parked on the results page while someone decides.
        """
        headers = dict(BASE_HEADERS)
        headers.update({
            "accept": "*/*",
            "x-requested-with": "XMLHttpRequest",
            "origin": BASE,
            "referer": self._referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        })
        response = self.session.post(
            "{0}/Booking/Booking/RestartSessionTimeout".format(BASE),
            headers=headers,
            data="",
            timeout=REQUEST_TIMEOUT,
        )
        return response.status_code == 200

    def _require_step(self, expected):
        """Fail loudly when a POST did not advance to the step we expected."""
        heading = parse_heading(self.html)
        if expected.lower() not in heading.lower():
            errors = parse_validation_errors(self.html)
            raise BookingError(
                "Expected the '{0}' step but the site rendered '{1}'{2}".format(
                    expected, heading, ": " + "; ".join(errors) if errors else ""
                )
            )

    # ------------------------------------------------------------ wizard steps

    def start(self, service_group_id=SERVICE_GROUP_ID):
        """Step 1: pick a service group and press 'Make an appointment'."""
        response = self.post_next([
            ("FormId", "1"),
            ("ServiceGroupId", str(service_group_id)),
            ("StartNextButton", "Make an appointment"),
        ])
        self._require_step("Approval of terms")
        return response

    def accept_storage(self, number_of_people=1):
        """Step 2: privacy consent + party size.

        AcceptInformationStorage is sent twice on purpose: the checked checkbox posts
        'true' and its hidden companion posts 'false' (ASP.NET keeps the first).
        """
        response = self.post_next([
            ("AcceptInformationStorage", "true"),
            ("AcceptInformationStorage", "false"),
            ("NumberOfPeople", str(number_of_people)),
            ("Next", "Next"),
        ])
        self._require_step("Select time")
        return response

    # ------------------------------------------------------- office / service

    def offices(self):
        """[(section_id, name)] from the office dropdown on the current page."""
        return parse_select_options(self.html, "SectionId")

    def service_types(self):
        """[(service_type_id, name)] currently rendered in the appointment-type dropdown."""
        return parse_select_options(self.html, "ServiceTypeId")

    def resolve_office(self, name):
        section_id = find_option_value(self.offices(), name)
        if not section_id or section_id == "0":
            raise BookingError(
                "Office {0!r} not found. Available: {1}".format(
                    name, ", ".join(label for _, label in self.offices() if label != "Select office...")
                )
            )
        return section_id

    def resolve_service_type(self, section_id, name):
        """Service-type ids are scoped to the office, so they must be looked up per office.

        The dropdown is populated client-side from an AJAX endpoint; we call the same
        endpoint, and fall back to a server round-trip if its shape ever changes.
        """
        options = self._fetch_service_types(section_id)
        if options:
            service_type_id = find_option_value(options, name)
            if service_type_id:
                return service_type_id

        # Fallback: post a search for this office and read the re-rendered dropdown.
        placeholder = self.service_types()[0][0] if self.service_types() else "0"
        self.search(section_id=section_id, service_type_id=placeholder, from_date=None)
        options = self.service_types()
        service_type_id = find_option_value(options, name)
        if not service_type_id:
            raise BookingError(
                "Appointment type {0!r} not offered by this office. Available: {1}".format(
                    name, ", ".join(label for _, label in options)
                )
            )
        return service_type_id

    def _fetch_service_types(self, section_id):
        """The dropdown's own AJAX source. Returns [] if it is unavailable."""
        url = "{0}/Booking/Ajax/GetServiceTypes".format(BASE)
        headers = dict(BASE_HEADERS)
        headers.update({
            "accept": "application/json, text/javascript, */*; q=0.01",
            "x-requested-with": "XMLHttpRequest",
            "referer": self._referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        })
        try:
            response = self.session.get(
                url,
                headers=headers,
                params={"serviceGroupId": SERVICE_GROUP_ID, "sectionId": section_id},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return []
        return _options_from_json(payload)

    # -------------------------------------------------------------- searching

    def search(self, section_id, service_type_id, from_date=None, region_id=0, first_available=False):
        """Step 3: search open slots.

        from_date is ISO ('2026-09-08'). With first_available the site's own
        'See First Available' button is pressed instead, which jumps to the next day
        that actually has openings rather than guessing a date.
        """
        button = (
            ("TimeSearchFirstAvailableButton", "See First Available (Recommended)")
            if first_available
            else ("TimeSearchButton", "Search")
        )
        return self.post_next(
            [
                ("FormId", "1"),
                ("RegionId", str(region_id)),
                ("SectionId", str(section_id)),
                ("ServiceTypeId", str(service_type_id)),
                ("FromDateString", from_date or ""),
                button,
            ],
            form_id="1",
        )

    def available_slots(self):
        """Available slots on the current results page, sorted chronologically."""
        return parse_slots(self.html)

    def find_slots(self, office=DEFAULT_OFFICE, service=DEFAULT_SERVICE, from_date=None,
                   first_available=False):
        """Resolve office + appointment type by name, search, and return the slots.

        The site only ever renders "Current Day and Next 2 Days", and the current day
        itself is never bookable - so searching from today shows the whole bookable
        window, which is what the no-flags default wants.

        first_available presses the site's own button to jump past that window to the
        next day that has openings. It needs a concrete date and a preceding plain
        search: pressed cold, or with an empty FromDateString, it renders no grid.
        """
        section_id = self.resolve_office(office)
        service_type_id = self.resolve_service_type(section_id, service)
        from_date = from_date or date.today().isoformat()

        self.search(section_id=section_id, service_type_id=service_type_id, from_date=from_date)
        if first_available:
            self.search(
                section_id=section_id,
                service_type_id=service_type_id,
                from_date=from_date,
                first_available=True,
            )
        return self.available_slots()

    # ----------------------------------------------------------- booking steps

    def reserve(self, slot):
        """Step 4: hold a slot.

        The ids come from the slot's own data attributes rather than from our request,
        so we can only ever reserve something the page actually offered. The token for
        this POST belongs to the results page's second form (FormId=2).
        """
        response = self.post_next(
            [
                ("FormId", "2"),
                ("ReservedSectionId", slot.section_id),
                ("ReservedServiceTypeId", slot.service_type_id),
                ("ReservedDateTime", slot.raw),
                ("Next", "Next"),
            ],
            form_id="2",
        )
        self._require_step("Personal data")
        return response

    def submit_booking_fields(self, description, first_name, last_name):
        """Step 5: the booking form.

        The BookingFieldId / BookingFieldTextName values (BF_38_*) belong to this
        booking definition and are rendered as hidden inputs on the page.
        """
        response = self.post_next([
            ("BookingFieldValues[0].Value", description),
            ("BookingFieldValues[0].BookingFieldId", "1152"),
            ("BookingFieldValues[0].BookingFieldTextName", "BF_38_DESCRIPTION"),
            ("BookingFieldValues[0].FieldTypeId", "1"),
            ("Customers[0].BookingCustomerId", "0"),
            ("Customers[0].BookingFieldValues[0].Value", first_name),
            ("Customers[0].BookingFieldValues[0].BookingFieldId", "1117"),
            ("Customers[0].BookingFieldValues[0].BookingFieldTextName", "BF_38_FIRSTNAME"),
            ("Customers[0].BookingFieldValues[0].FieldTypeId", "1"),
            ("Customers[0].BookingFieldValues[1].Value", last_name),
            ("Customers[0].BookingFieldValues[1].BookingFieldId", "1118"),
            ("Customers[0].BookingFieldValues[1].BookingFieldTextName", "BF_38_LASTNAME"),
            ("Customers[0].BookingFieldValues[1].FieldTypeId", "1"),
            ("Next", "Next"),
        ])
        self._require_step("Contact information")
        return response

    def submit_contact_info(self, email, phone, reminder_option=1, free_text=""):
        """Step 6: notification preferences.

        SelectedContacts is a 4-row grid: MessageKindId 1 = confirmation, 2 = reminder;
        MessageTypeId 2 = email, 1 = SMS. The email rows post true+false (checked
        checkbox plus hidden fallback), the SMS rows post only the hidden 'False'.
        """
        response = self.post_next([
            ("EmailAddress", email),
            ("ConfirmEmailAddress", email),
            ("PhoneNumber", phone),
            ("NoSelectedConfirmation", ""),
            # confirmation by email (checked)
            ("SelectedContacts[0].IsSelected", "true"),
            ("SelectedContacts[0].IsSelected", "false"),
            ("SelectedContacts[0].MessageTypeId", "2"),
            ("SelectedContacts[0].MessageKindId", "1"),
            ("SelectedContacts[0].TextName", "MESSAGETYPE_EMAIL"),
            # confirmation by SMS (unchecked)
            ("SelectedContacts[1].IsSelected", "False"),
            ("SelectedContacts[1].MessageTypeId", "1"),
            ("SelectedContacts[1].MessageKindId", "1"),
            ("SelectedContacts[1].TextName", "MESSAGETYPE_SMS"),
            # reminder by email (checked)
            ("SelectedContacts[2].IsSelected", "true"),
            ("SelectedContacts[2].IsSelected", "false"),
            ("SelectedContacts[2].MessageTypeId", "2"),
            ("SelectedContacts[2].MessageKindId", "2"),
            ("SelectedContacts[2].TextName", "MESSAGETYPE_EMAIL"),
            # reminder by SMS (unchecked)
            ("SelectedContacts[3].IsSelected", "False"),
            ("SelectedContacts[3].MessageTypeId", "1"),
            ("SelectedContacts[3].MessageKindId", "2"),
            ("SelectedContacts[3].TextName", "MESSAGETYPE_SMS"),
            ("NoSelectedReminder", ""),
            ("ReminderOption", str(reminder_option)),
            ("FreeText", free_text),
            ("Next", "Next"),
        ])
        self._require_step("Review Information")
        return response

    def confirm(self):
        """Step 7: final 'Schedule Appointment' - this is what actually books the slot."""
        self.post_next([("Next", "Schedule Appointment")])
        details = parse_confirmation(self.html)
        if details is None:
            raise BookingError(
                "No confirmation number on the final page (step: {0})".format(parse_heading(self.html))
            )
        return details


def _options_from_json(payload):
    """Normalise the AJAX dropdown payload, whose key casing we have not pinned."""
    if isinstance(payload, dict):
        for key in ("serviceTypes", "ServiceTypes", "results", "data", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            return []
    if not isinstance(payload, list):
        return []

    options = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        value = next(
            (entry[k] for k in ("Id", "id", "Value", "value", "ServiceTypeId") if k in entry), None
        )
        label = next(
            (entry[k] for k in ("Name", "name", "Text", "text", "Label", "label") if k in entry), None
        )
        if value is not None and label is not None:
            options.append((str(value), str(label).strip()))
    return options


# ------------------------------------------------------------------------ CLI


def build_parser():
    parser = argparse.ArgumentParser(
        description="Book a TxDMV appointment through the NemoQ wizard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Nothing is sent")[-1].strip(),
    )

    what = parser.add_argument_group("what to book")
    what.add_argument("--office", default=DEFAULT_OFFICE, help="office name (default: %(default)s)")
    what.add_argument("--service", default=DEFAULT_SERVICE, help="appointment type (default: %(default)s)")
    what.add_argument("--from-date", help="ISO date to search from (default: today)")
    what.add_argument(
        "--first-available",
        action="store_true",
        help="jump past the visible 3-day window to the next day with openings",
    )

    when = parser.add_argument_group("which slot (menu wins, then date/time, else the first offered)")
    when.add_argument("--menu", action="store_true", help="list the slots and pick one interactively")
    when.add_argument("--date", help="ISO date of the wanted slot, e.g. 2026-09-08")
    when.add_argument("--time", help="time of the wanted slot, e.g. 09:30 or 2:30 PM")

    who = parser.add_argument_group("personal data")
    who.add_argument("--first-name", default="")
    who.add_argument("--last-name", default="")
    who.add_argument("--email", default="")
    who.add_argument("--phone", default="")
    who.add_argument("--description", default="Title Service", help="the 'Service Needed' field")
    who.add_argument("--details", default="", help="the required free-text field (VIN / plate)")

    proxy = parser.add_argument_group("proxy")
    proxy.add_argument("--proxy", help="use this proxy: host:port:user:pass")
    proxy.add_argument("--proxy-file", default="proxies.txt", help="(default: %(default)s)")
    proxy.add_argument("--country", help="required exit country (default: US)")
    proxy.add_argument("--no-proxy", action="store_true", help="connect directly")

    mode = parser.add_argument_group("mode")
    mode.add_argument("--list-offices", action="store_true", help="print the offices and exit")
    mode.add_argument("--list-slots", action="store_true", help="search and print the slots, book nothing")
    mode.add_argument("--dry-run", action="store_true", help="pick a slot and stop before reserving it")
    mode.add_argument("--run", action="store_true", help="actually book the appointment")
    return parser


def resolve_pool(args):
    if args.no_proxy:
        print("proxy: disabled (direct connection)")
        return None

    pool = webshare.load_pool(
        proxy_file=args.proxy_file, proxy_spec=args.proxy, country=args.country
    )
    if pool is None:
        print("proxy: none configured (direct connection) - see .env.example")
        return None

    print("proxy: {0} candidate(s) available".format(len(pool.candidates())))
    return pool


def open_wizard(proxy):
    """Steps 1-2, up to the 'Select time' page."""
    booking = NemoQBooking(proxy=proxy)
    booking.get_index(first_visit=True)
    booking.start()
    booking.accept_storage()
    return booking


def open_and_search(pool, args, attempts=3):
    """Open the wizard and search, swapping the proxy if one dies on the way.

    Retrying is only safe here: no slot is held yet, so starting the flow over on a
    fresh IP costs nothing. Once reserve() has run, a dead proxy is a hard failure -
    the session cannot move to another IP without losing the reservation.
    """
    verifier = None
    if pool is not None:
        verifier = webshare.make_site_verifier(
            "{0}/Booking/Booking/Index/{1}".format(BASE, SEQ), headers=BASE_HEADERS
        )

    for attempt in range(1, attempts + 1):
        proxy = None
        if pool is not None:
            print("proxy: verifying candidates...")
            proxy = pool.pick(verify=verifier)
            print("proxy: using {0!r}".format(proxy))
        try:
            booking = open_wizard(proxy)
            if args.list_offices:
                return booking, []
            slots = booking.find_slots(
                office=args.office,
                service=args.service,
                from_date=args.from_date or args.date,
                first_available=args.first_available,
            )
            return booking, slots
        except requests.RequestException as error:
            if pool is None or attempt == attempts:
                raise
            print("proxy: {0!r} died ({1}); starting over on another".format(
                proxy, type(error).__name__
            ))
            pool.discard(proxy)

    raise BookingError("Could not complete a search through any proxy")


def main(argv=None):
    args = build_parser().parse_args(argv)

    if not (args.run or args.list_slots or args.list_offices or args.dry_run):
        print(
            "Nothing sent. Pass --list-slots to search, --dry-run to stop before "
            "reserving, or --run to book a real appointment."
        )
        return 0

    if args.run:
        missing = [
            name
            for name in ("first_name", "last_name", "email", "phone")
            if not getattr(args, name)
        ]
        if missing:
            print("--run needs: {0}".format(", ".join("--" + m.replace("_", "-") for m in missing)))
            return 2

    booking, slots = open_and_search(resolve_pool(args), args)

    if args.list_offices:
        for value, label in booking.offices():
            if value != "0":
                print("{0:>5}  {1}".format(value, label))
        return 0

    print("\n{0} - {1}: {2} slot(s) available".format(args.office, args.service, len(slots)))
    for slot in slots:
        print("  {0}".format(slot.label or slot))

    if args.list_slots:
        return 0

    slot = choose_slot(slots, date=args.date, time=args.time, menu=args.menu)
    print("\nselected: {0}  ({1})".format(slot, slot.raw))

    if not args.run:
        print("dry run - stopping before the slot is reserved.")
        return 0

    booking.reserve(slot)
    booking.submit_booking_fields(
        description=args.description, first_name=args.first_name, last_name=args.last_name
    )
    booking.submit_contact_info(email=args.email, phone=args.phone, free_text=args.details)
    details = booking.confirm()

    print("\nbooked.")
    print("  appointment number: {0}".format(details["appointment_number"]))
    print("  time:               {0}".format(details["time"]))
    print("  office:             {0}".format(details["office"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BookingError, NoSlotsAvailable, SlotNotAvailable, webshare.NoUsableProxy) as error:
        print("\nerror: {0}".format(error), file=sys.stderr)
        sys.exit(1)
