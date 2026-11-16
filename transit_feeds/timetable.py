"""Normalization of a single captured source observation.

This module deliberately does not interpret service days. It only checks
the envelope a captured record carries so the rest of the pipeline can
rely on it. Service-day interpretation lives in ``service_day.py`` and
``timezones.py``; the ``event`` field was removed because labelling every
record ``service_day`` asserted a business meaning the envelope does not
establish.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Observation:
    source: str
    key: str
    revision: int
    value: Any


def inspect_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one source observation, keeping its source."""
    required = ("source", "key", "revision", "value")
    missing = [name for name in required if name not in raw]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    source, key = str(raw["source"]).strip(), str(raw["key"]).strip()
    if not source or not key:
        raise ValueError("source and key must be non-empty")
    revision = raw["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("revision must be a non-negative integer")
    item = Observation(source, key, revision, raw["value"])
    return {"source": item.source, "key": item.key, "revision": item.revision, "value": item.value}
