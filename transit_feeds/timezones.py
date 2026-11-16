"""Time zone rule evidence and local wall-time resolution.

The layer records *which* rules were used (zone name, tzdata version, zone
file digest) and never resolves a wall time by falling back to a process
default. Callers must inspect the returned status and candidates.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from importlib import metadata
from pathlib import Path, PurePosixPath
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


class ZoneRulesError(ValueError):
    """The zone name or the local time zone database is unusable."""


def _checked_zone_name(zone):
    if not isinstance(zone, str) or not zone.strip():
        raise ZoneRulesError("zone must be a non-empty IANA name")
    if zone != zone.strip() or zone.startswith("/") or ".." in zone.split("/"):
        raise ZoneRulesError("invalid IANA zone name: " + repr(zone))
    return zone


@lru_cache(maxsize=1)
def tzdata_version():
    """Return ``(version, source)`` for the tz database this process uses.

    ``version`` is ``None`` when the rules cannot be identified; that is
    reported explicitly instead of pretending the rules are known.
    """
    for root in TZPATH:
        marker = Path(root) / "tzdata.zi"
        try:
            with marker.open("r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline().strip()
        except OSError:
            continue
        fields = first.split()
        if len(fields) >= 3 and fields[0] == "#" and fields[1] == "version":
            return fields[2], "system:" + str(root)
    try:
        return metadata.version("tzdata"), "python-package:tzdata"
    except metadata.PackageNotFoundError:
        return None, "unknown"


@lru_cache(maxsize=None)
def zone_file_digest(zone):
    """SHA-256 of the compiled zone file, or ``None`` for package tzdata."""
    zone = _checked_zone_name(zone)
    for root in TZPATH:
        candidate = Path(root) / zone
        try:
            return hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            continue
    return None


def zone_rules(zone):
    """Describe the rules used to interpret ``zone`` in this environment."""
    zone = _checked_zone_name(zone)
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError as error:
        raise ZoneRulesError("unknown time zone: " + zone) from error
    version, source = tzdata_version()
    return {
        "zone": zone,
        "tzdata_version": version,
        "tzdata_source": source,
        "zone_digest": zone_file_digest(zone),
    }


def _wall_clock(day, seconds, zone):
    return datetime.combine(day, datetime.min.time(), ZoneInfo(zone)) + timedelta(seconds=seconds)


def _valid(day, seconds, zone):
    wall = datetime.combine(day, datetime.min.time()) + timedelta(seconds=seconds)
    local = _wall_clock(day, seconds, zone)
    # Round-tripping through UTC exposes non-existent wall times, which
    # zoneinfo silently maps onto a neighbouring offset otherwise.
    return local.astimezone(UTC).astimezone(ZoneInfo(zone)).replace(tzinfo=None) == wall


@lru_cache(maxsize=8192)
def _transitions(day, zone):
    """Wall-clock transitions that touch ``day`` as ``(before, after)`` seconds."""
    info = ZoneInfo(zone)
    result = []
    probe = datetime.combine(day, datetime.min.time(), UTC) - timedelta(hours=36)
    limit = probe + timedelta(hours=72)
    previous = probe.astimezone(info)
    while probe < limit:
        probe += timedelta(minutes=15)
        current = probe.astimezone(info)
        if current.utcoffset() != previous.utcoffset():
            # Binary-search the 15-minute bucket down to the second.
            low, high = probe - timedelta(minutes=15), probe
            low_offset = previous.utcoffset()
            while (high - low) > timedelta(seconds=1):
                middle = low + (high - low) / 2
                if middle.astimezone(info).utcoffset() == low_offset:
                    low = middle
                else:
                    high = middle
            at = high.astimezone(info)
            before = low.astimezone(info)
            midnight = datetime.combine(day, datetime.min.time())
            wall_before = int((before.replace(tzinfo=None) - midnight).total_seconds())
            wall_after = int((at.replace(tzinfo=None) - midnight).total_seconds())
            if 0 <= wall_before <= 86400 or 0 <= wall_after <= 86400:
                result.append({
                    "at_utc": high.astimezone(UTC).isoformat(),
                    "before_offset": int(low_offset.total_seconds()),
                    "after_offset": int(at.utcoffset().total_seconds()),
                    "wall_before_seconds": wall_before,
                    "wall_after_seconds": wall_after,
                })
        previous = current
    return tuple(result)


def resolve_local(day, seconds, zone, source_offset=None):
    """Interpret a wall-clock time on a service day without guessing.

    ``seconds`` may exceed 86400 for times such as ``26:30``, which is a
    source-side convention for the following civil day inside the same
    service day.

    ``source_offset`` is an optional explicit offset the source attached to
    the time (seconds east of UTC, e.g. ``7200`` for ``+02:00``). When it
    is given it selects the matching candidate, and is refused when it
    matches none -- the source's own statement is never overridden.

    Returns a dict with ``status``:

    * ``unique``       -- exactly one UTC instant;
    * ``repeated``     -- a daylight-saving rollback gives two instants;
    * ``non_existent`` -- a daylight-saving advance skipped this wall time.

    ``candidates`` lists every instant with its offset and ``fold`` role;
    ``chosen`` explains any selection (``only_match`` or ``source_offset``).
    """
    zone = _checked_zone_name(zone)
    rules = zone_rules(zone)
    extended = seconds >= 86400
    civil_day = day + timedelta(days=seconds // 86400)
    local_seconds = seconds % 86400
    candidates = []
    seen = set()
    for fold in (0, 1):
        wall = datetime.combine(civil_day, datetime.min.time()) + timedelta(seconds=local_seconds)
        aware = wall.replace(tzinfo=ZoneInfo(zone), fold=fold)
        if aware.astimezone(UTC).astimezone(ZoneInfo(zone)).replace(tzinfo=None) != wall:
            continue
        instant = aware.astimezone(UTC).isoformat()
        if instant in seen:
            continue
        seen.add(instant)
        candidates.append({
            "utc": instant,
            "offset_seconds": int(aware.utcoffset().total_seconds()),
            "fold": fold,
            "utc_offset": _format_offset(aware.utcoffset()),
            "ambiguous": False,
            "chosen": None,
        })
    if len(candidates) == 2:
        status = "repeated"
        for candidate in candidates:
            candidate["ambiguous"] = True
    elif len(candidates) == 1:
        status = "unique"
        candidates[0]["chosen"] = "only_match"
    else:
        status = "non_existent"
    if source_offset is not None:
        matching = [item for item in candidates if item["offset_seconds"] == source_offset]
        if not matching:
            raise ZoneRulesError(
                "source offset " + _format_offset(timedelta(seconds=source_offset))
                + " matches no instant for " + _format_wall(seconds)
                + " on " + civil_day.isoformat() + " in " + zone
            )
        status = "unique"
        for candidate in candidates:
            candidate["ambiguous"] = False
            candidate["chosen"] = "source_offset" if candidate is matching[0] else None
    return {
        "requested": {"day": day.isoformat(), "seconds": seconds, "wall": _format_wall(seconds)},
        "extended_day": extended,
        "civil_day": civil_day.isoformat(),
        "civil_seconds": local_seconds,
        "status": status,
        "candidates": candidates,
        "source_offset": source_offset,
        "rules": rules,
        "transitions": _transitions(civil_day, zone),
    }


def _format_offset(offset):
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "{}{:02d}:{:02d}".format(sign, total // 3600, (total % 3600) // 60)


def _format_wall(seconds):
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return "{}{:02d}:{:02d}:{:02d}".format(sign, seconds // 3600, (seconds % 3600) // 60, seconds % 60)
