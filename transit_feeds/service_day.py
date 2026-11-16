"""Service-day interpretation for timetable documents.

A source service day is an operating day, not a civil date: ``26:30`` on
service day 2026-10-31 runs in the early morning of 2026-11-01 in the
operator's zone. Night services therefore keep their source text and are
interpreted against the operator's calendar day and rules.

Nothing here guesses. A wall time that occurs twice is reported with both
instants; a wall time skipped by a daylight-saving advance is reported as
``non_existent`` with no instants at all. Callers decide what to do next.
"""

import re
from datetime import date

from .timezones import ZoneRulesError, resolve_local, zone_rules

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLOCK = re.compile(r"^(\d{1,3}):([0-5]\d)(?::([0-5]\d))?$")


class DocumentError(ValueError):
    """A source document does not satisfy the declared schema."""


def parse_date(value, field="date"):
    if not isinstance(value, str) or not _DATE.match(value):
        raise DocumentError(field + " must be a YYYY-MM-DD string")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise DocumentError(field + " is not a valid date: " + value) from error


def parse_clock(text, field="departure"):
    """Return seconds after the service day's local midnight.

    The source text is not rewritten; ``26:30`` stays ``26:30`` and becomes
    95400 seconds, i.e. 02:30 on the following civil day.
    """
    if not isinstance(text, str):
        raise DocumentError(field + " must be a string")
    match = _CLOCK.match(text)
    if match is None:
        raise DocumentError(field + " is not an HH:MM time: " + repr(text))
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds or 0)


def parse_offset(value, field="utc_offset"):
    """Parse ``+HH:MM``/``-HH:MM``/``Z`` into seconds east of UTC."""
    if not isinstance(value, str):
        raise DocumentError(field + " must be a string offset")
    if value == "Z":
        return 0
    match = re.match(r"^([+-])(\d{2}):(\d{2})$", value)
    if match is None:
        raise DocumentError(field + " is not an HH:MM offset: " + repr(value))
    sign, hours, minutes = match.groups()
    if int(hours) > 14 or int(minutes) > 59:
        raise DocumentError(field + " is out of range: " + repr(value))
    total = int(hours) * 3600 + int(minutes) * 60
    return total if sign == "+" else -total


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise DocumentError(field + " must be a non-empty string")
    return value.strip()


def _mapping(value, field):
    if not isinstance(value, dict):
        raise DocumentError(field + " must be an object")
    return value


def load_timetable(payload):
    """Validate a timetable document and normalize it without losing text."""
    payload = _mapping(payload, "timetable")
    if payload.get("kind") != "timetable":
        raise DocumentError("timetable document must declare kind=timetable")
    version = _text(payload.get("version"), "version")
    calendar_version = _text(payload.get("calendar_version"), "calendar_version")
    operators = _mapping(payload.get("operators"), "operators")
    if not operators:
        raise DocumentError("operators must not be empty")
    zones = {}
    for name, detail in operators.items():
        detail = _mapping(detail, "operators." + str(name))
        zone = _text(detail.get("zone"), "operators." + str(name) + ".zone")
        try:
            zone_rules(zone)
        except ZoneRulesError as error:
            raise DocumentError(str(error)) from error
        zones[name] = zone

    valid_from = payload.get("valid_from")
    valid_to = payload.get("valid_to")
    valid_from = parse_date(valid_from, "valid_from") if valid_from is not None else None
    valid_to = parse_date(valid_to, "valid_to") if valid_to is not None else None
    if valid_from and valid_to and valid_to < valid_from:
        raise DocumentError("valid_to precedes valid_from")

    departures = payload.get("departures")
    if not isinstance(departures, list) or not departures:
        raise DocumentError("departures must be a non-empty array")
    normalized = []
    seen = set()
    for index, raw in enumerate(departures):
        raw = _mapping(raw, "departures[" + str(index) + "]")
        operator = _text(raw.get("operator"), "departures[].operator")
        if operator not in zones:
            raise DocumentError("departures[].operator is not declared: " + operator)
        trip = _text(raw.get("trip"), "departures[].trip")
        stop = raw.get("stop", "")
        if not isinstance(stop, str):
            raise DocumentError("departures[].stop must be a string")
        service_day = parse_date(raw.get("service_day"), "departures[].service_day")
        departure = raw.get("departure")
        seconds = parse_clock(departure, "departures[].departure")
        zone = raw.get("zone", zones[operator])
        zone = _text(zone, "departures[].zone")
        if zone != zones[operator]:
            raise DocumentError(
                "departures[].zone conflicts with operator zone: " + zone + " != " + zones[operator]
            )
        if valid_from and service_day < valid_from or valid_to and service_day > valid_to:
            raise DocumentError(
                "service day outside timetable validity: " + service_day.isoformat()
            )
        source_offset = raw.get("utc_offset")
        if source_offset is not None:
            source_offset = parse_offset(source_offset, "departures[].utc_offset")
        key = (operator, trip, stop, service_day)
        if key in seen:
            raise DocumentError(
                "duplicate departure for " + "/".join((operator, trip, stop or "-", service_day.isoformat()))
            )
        seen.add(key)
        normalized.append({
            "operator": operator,
            "trip": trip,
            "stop": stop,
            "service_day": service_day,
            "source_time": departure,
            "seconds": seconds,
            "source_offset": source_offset,
            "zone": zone,
            "calendar_version": calendar_version,
            "timetable_version": version,
            "record": dict(raw),
        })
    return {
        "kind": "timetable",
        "version": version,
        "calendar_version": calendar_version,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "operators": zones,
        "departures": normalized,
    }


def load_calendar(payload):
    """Validate an operating-calendar document (exception closure days)."""
    payload = _mapping(payload, "calendar")
    if payload.get("kind") != "calendar":
        raise DocumentError("calendar document must declare kind=calendar")
    version = _text(payload.get("version"), "version")
    valid_from = parse_date(payload.get("valid_from"), "valid_from")
    valid_to = parse_date(payload.get("valid_to"), "valid_to")
    if valid_to < valid_from:
        raise DocumentError("valid_to precedes valid_from")
    raw_exceptions = payload.get("exceptions", [])
    if not isinstance(raw_exceptions, list):
        raise DocumentError("exceptions must be an array")
    raw_operators = payload.get("operators", {})
    if not isinstance(raw_operators, dict):
        raise DocumentError("operators must be an object")
    operators = {}
    for name, detail in raw_operators.items():
        detail = _mapping(detail, "operators." + str(name))
        operators[name] = _text(detail.get("zone"), "operators." + str(name) + ".zone")
    exceptions = []
    for index, raw in enumerate(raw_exceptions):
        raw = _mapping(raw, "exceptions[" + str(index) + "]")
        service_day = parse_date(raw.get("date"), "exceptions[].date")
        if not valid_from <= service_day <= valid_to:
            raise DocumentError("exception outside calendar validity: " + service_day.isoformat())
        scope = raw.get("scope", {})
        if not isinstance(scope, dict):
            raise DocumentError("exceptions[].scope must be an object")
        operator = scope.get("operator")
        trip = scope.get("trip")
        for name, value in (("operator", operator), ("trip", trip)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise DocumentError("exceptions[].scope." + name + " must be a non-empty string")
        if operator is not None:
            operator = operator.strip()
            if operators and operator not in operators:
                raise DocumentError(
                    "exceptions[].scope.operator conflicts with calendar operators: " + operator
                )
        exceptions.append({
            "service_day": service_day,
            "operator": operator,
            "trip": trip.strip() if trip else None,
            "reason": _text(raw.get("reason"), "exceptions[].reason"),
        })
    return {
        "kind": "calendar",
        "version": version,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "operators": operators,
        "exceptions": exceptions,
    }


def coverage_gap(timetable, calendar):
    """Service days of the timetable the calendar does not cover."""
    days = sorted({item["service_day"] for item in timetable["departures"]})
    return [day for day in days if not calendar["valid_from"] <= day <= calendar["valid_to"]]


def exception_for(calendar, departure):
    for exception in calendar["exceptions"]:
        if exception["service_day"] != departure["service_day"]:
            continue
        if exception["operator"] not in (None, departure["operator"]):
            continue
        if exception["trip"] not in (None, departure["trip"]):
            continue
        return exception
    return None


def interpret_departure(departure, exception=None):
    """Build the stored interpretation evidence for one departure.

    ``status`` is one of ``unique``, ``repeated``, ``non_existent`` or
    ``service_closed``. Only ``unique`` carries a single instant; this
    function never chooses between ``repeated`` candidates.
    """
    rules = zone_rules(departure["zone"])
    evidence = {
        "service_day": departure["service_day"].isoformat(),
        "source_time": departure["source_time"],
        "zone": departure["zone"],
        "calendar_version": departure["calendar_version"],
        "timetable_version": departure["timetable_version"],
        "rules": rules,
    }
    if exception is not None:
        evidence.update({
            "status": "service_closed",
            "candidates": [],
            "civil_day": None,
            "extended_day": departure["seconds"] >= 86400,
            "closure": {
                "reason": exception["reason"],
                "operator": exception["operator"],
                "trip": exception["trip"],
                "service_day": exception["service_day"].isoformat(),
            },
        })
        return evidence
    resolved = resolve_local(
        departure["service_day"], departure["seconds"], departure["zone"],
        departure.get("source_offset"),
    )
    evidence.update({
        "status": resolved["status"],
        "candidates": resolved["candidates"],
        "civil_day": resolved["civil_day"],
        "civil_seconds": resolved["civil_seconds"],
        "extended_day": resolved["extended_day"],
        "source_offset": resolved["source_offset"],
        "transitions": resolved["transitions"],
    })
    return evidence
