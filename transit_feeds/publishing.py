"""Publication of compatible timetable/calendar versions and dispatch review.

Storage model (one SQLite file, same style as ``archive.py``):

* ``documents`` -- every accepted source document, keyed by SHA-256 of its
  canonical JSON, with the capture that first delivered it. Identical
  bodies are reused across versions instead of being copied.
* ``observations`` -- every fetch attempt per route, including 304 and
  failures, with status, error detail and retry metadata. A failed fetch
  is never interpreted as an empty document.
* ``versions`` -- an immutable (timetable, calendar) pair. A pair is only
  published when both halves are present and compatible; a half that
  arrives alone stays pending and the previous version stays in force.
* ``shifts`` -- business dispatch records. This module never deletes,
  cancels or edits them.
* ``shift_bindings`` -- append-only snapshot of how a shift's departure was
  interpreted (source text, service day, zone rules, candidate instants)
  under one published version.
* ``review_items`` -- shifts affected by a newly published version, queued
  for dispatch review. Acknowledgement only closes the review item.

When a version is replaced, every bound shift is re-interpreted and any
change in source text, candidate instants or operating state is flagged.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .service_day import (
    DocumentError, coverage_gap, exception_for, interpret_departure,
    load_calendar, load_timetable,
)


class PublishError(ValueError):
    """The two source halves cannot be published as one compatible version."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def sleep_for(headers, default=1.0):
    """Delay requested by the source via Retry-After, in seconds.

    Accepts both delay-seconds and an HTTP date. Invalid values fall back
    to ``default``; the result is bounded so a hostile header cannot stall
    a local run indefinitely.
    """
    value = (headers or {}).get("retry-after")
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return default
    return min(max(delay, 0.0), 30.0)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                digest TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                source_version TEXT NOT NULL,
                content TEXT NOT NULL,
                capture_id INTEGER,
                first_seen TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY,
                route TEXT NOT NULL,
                status INTEGER NOT NULL,
                ok INTEGER NOT NULL,
                detail TEXT NOT NULL,
                capture_id INTEGER,
                digest TEXT REFERENCES documents(digest),
                observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS observations_route ON observations(route, id);
            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY,
                state TEXT NOT NULL CHECK (state IN ('published','superseded')),
                timetable_version TEXT NOT NULL,
                calendar_version TEXT NOT NULL,
                timetable_digest TEXT NOT NULL REFERENCES documents(digest),
                calendar_digest TEXT NOT NULL REFERENCES documents(digest),
                coverage_from TEXT NOT NULL,
                coverage_to TEXT NOT NULL,
                superseded_by INTEGER REFERENCES versions(id),
                published_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE (timetable_digest, calendar_digest)
            );
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY,
                external_id TEXT NOT NULL UNIQUE,
                operator TEXT NOT NULL,
                trip TEXT NOT NULL,
                stop TEXT NOT NULL,
                service_day TEXT NOT NULL,
                assigned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS shift_bindings (
                id INTEGER PRIMARY KEY,
                shift_id INTEGER NOT NULL REFERENCES shifts(id),
                version_id INTEGER NOT NULL REFERENCES versions(id),
                departure_key TEXT NOT NULL,
                service_state TEXT NOT NULL
                    CHECK (service_state IN ('operating','closed_by_source','departure_missing')),
                source_time TEXT NOT NULL,
                zone TEXT NOT NULL,
                interpretation TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS shift_bindings_shift
                ON shift_bindings(shift_id, id DESC);
            CREATE TABLE IF NOT EXISTS review_items (
                id INTEGER PRIMARY KEY,
                shift_id INTEGER NOT NULL REFERENCES shifts(id),
                binding_id INTEGER NOT NULL REFERENCES shift_bindings(id),
                previous_version_id INTEGER NOT NULL REFERENCES versions(id),
                current_version_id INTEGER NOT NULL REFERENCES versions(id),
                reason TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','acknowledged')),
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE (binding_id, reason)
            );
        """)

    def close(self):
        self.connection.close()

    # -- documents and observations -------------------------------------

    def _store_document(self, kind, payload):
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT OR IGNORE INTO documents(digest,kind,source_version,content) VALUES (?,?,?,?)",
            (digest, kind, str(payload.get("version")), content),
        )
        return digest

    def record_observation(self, route, status, detail, capture_id=None, digest=None):
        if not isinstance(route, str) or not route:
            raise ValueError("route is required")
        if isinstance(status, bool) or not isinstance(status, int) or not 0 <= status <= 599:
            raise ValueError("invalid HTTP status")
        if not isinstance(detail, dict):
            raise TypeError("detail must be an object")
        with self.connection:
            self.connection.execute(
                "INSERT INTO observations(route,status,ok,detail,capture_id,digest) VALUES (?,?,?,?,?,?)",
                (route, status, int(digest is not None), json.dumps(detail, ensure_ascii=False, sort_keys=True),
                 capture_id, digest),
            )

    def record_document(self, route, status, payload, capture_id=None, etag=None):
        """Validate and record a fetched document; ``None`` if unusable."""
        try:
            kind = _document_kind(payload)
        except DocumentError as error:
            self.record_observation(route, status, {"error": str(error), "etag": etag}, capture_id)
            return None
        digest = self._store_document(kind, payload)
        with self.connection:
            self.connection.execute(
                "UPDATE documents SET capture_id=COALESCE(capture_id,?) WHERE digest=?", (capture_id, digest)
            )
        self.record_observation(route, status, {"kind": kind, "etag": etag}, capture_id, digest)
        return digest, kind

    def documents(self, kind=None):
        query = "SELECT * FROM documents"
        params = ()
        if kind is not None:
            query += " WHERE kind=?"
            params = (kind,)
        return [dict(row) for row in self.connection.execute(query + " ORDER BY rowid", params)]

    def observations(self, route=None):
        query, params = "SELECT * FROM observations", ()
        if route is not None:
            query, params = query + " WHERE route=?", (route,)
        return [dict(row) for row in self.connection.execute(query + " ORDER BY id", params)]

    def document(self, digest):
        row = self.connection.execute("SELECT * FROM documents WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise KeyError(digest)
        return {**dict(row), "payload": json.loads(row["content"])}

    # -- publication ----------------------------------------------------

    def plans(self, timetable_digest, calendar_digest):
        """Load references for a prospective pair without changing anything."""
        timetable = load_timetable(self.document(timetable_digest)["payload"])
        calendar = load_calendar(self.document(calendar_digest)["payload"])
        return timetable, calendar

    def check_pair(self, timetable, calendar):
        """Raise ``PublishError`` unless the halves form one coherent set.

        Coverage and operator zones are hard requirements. The timetable's
        declared ``calendar_version`` is checked by the intake layer, which
        knows whether the timetable is new or already in force.
        """
        gaps = coverage_gap(timetable, calendar)
        if gaps:
            raise PublishError(
                "coverage_gap",
                "calendar " + calendar["version"] + " does not cover timetable service days: "
                + ", ".join(day.isoformat() for day in gaps),
            )
        conflicts = []
        for operator, zone in calendar["operators"].items():
            if operator in timetable["operators"] and timetable["operators"][operator] != zone:
                conflicts.append(operator)
        if conflicts:
            raise PublishError(
                "zone_conflict",
                "operator zone differs between timetable and calendar: " + ", ".join(conflicts),
            )

    def publish(self, timetable_digest, calendar_digest):
        """Publish one compatible pair; returns the now-current version id.

        An incompatible pair raises ``PublishError`` and leaves the current
        version, its shifts and their bindings untouched.
        """
        timetable = load_timetable(self.document(timetable_digest)["payload"])
        calendar = load_calendar(self.document(calendar_digest)["payload"])
        self.check_pair(timetable, calendar)
        existing = self.connection.execute(
            "SELECT id,state FROM versions WHERE timetable_digest=? AND calendar_digest=?",
            (timetable_digest, calendar_digest),
        ).fetchone()
        with self.connection:
            current = self.connection.execute(
                "SELECT id FROM versions WHERE state='published'"
            ).fetchone()
            if existing is not None and current is not None and existing["id"] == current["id"]:
                return existing["id"]
            if existing is not None:
                # A previously superseded pair is current again (source
                # rollback); re-activate it rather than duplicating history.
                version_id = existing["id"]
                self.connection.execute(
                    "UPDATE versions SET state='published', superseded_by=NULL WHERE id=?", (version_id,)
                )
            else:
                cursor = self.connection.execute(
                    """INSERT INTO versions(state,timetable_version,calendar_version,
                           timetable_digest,calendar_digest,coverage_from,coverage_to)
                       VALUES ('published',?,?,?,?,?,?)""",
                    (
                        timetable["version"], calendar["version"], timetable_digest, calendar_digest,
                        calendar["valid_from"].isoformat(), calendar["valid_to"].isoformat(),
                    ),
                )
                version_id = cursor.lastrowid
            if current is not None and current["id"] != version_id:
                self.connection.execute(
                    "UPDATE versions SET state='superseded', superseded_by=? WHERE id=?",
                    (version_id, current["id"]),
                )
                self._bind_new_version(version_id, current["id"], timetable, calendar)
        return version_id

    def current_version(self):
        row = self.connection.execute("SELECT id FROM versions WHERE state='published'").fetchone()
        return row["id"] if row else None

    def version(self, version_id):
        row = self.connection.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise KeyError(version_id)
        return dict(row)

    def versions(self):
        return [dict(row) for row in self.connection.execute("SELECT * FROM versions ORDER BY id")]

    def current_halves(self):
        """Latest successfully accepted digest per document kind.

        This is the source's current statement for each half, including a
        rollback to a document published before.
        """
        result = {}
        for row in self.connection.execute(
            """SELECT o.digest, d.kind FROM observations o
               JOIN documents d ON d.digest=o.digest
               WHERE o.digest IS NOT NULL ORDER BY o.id"""
        ):
            result[row["kind"]] = row["digest"]
        return result

    def calendar_digest_for(self, source_version):
        """Newest accepted calendar document declaring ``source_version``."""
        row = self.connection.execute(
            """SELECT o.digest FROM observations o
               JOIN documents d ON d.digest=o.digest
               WHERE d.kind='calendar' AND d.source_version=? AND o.digest IS NOT NULL
               ORDER BY o.id DESC LIMIT 1""",
            (source_version,),
        ).fetchone()
        return row["digest"] if row else None

    def pending_halves(self):
        """Accepted documents not part of any published pair.

        Used for reporting a half that arrived without its counterpart.
        """
        used_timetables = {row["timetable_digest"] for row in self.versions()}
        used_calendars = {row["calendar_digest"] for row in self.versions()}
        current = self.current_halves()
        return {
            "timetable": [current[kind] for kind in ("timetable",) if kind in current
                          and current[kind] not in used_timetables],
            "calendar": [current[kind] for kind in ("calendar",) if kind in current
                         and current[kind] not in used_calendars],
            "published_pairs": len(self.versions()),
        }

    # -- shifts and review ----------------------------------------------

    def assign_shift(self, external_id, operator, trip, stop, service_day):
        """Create a shift or rebind it to the current version.

        The binding snapshots the interpretation dispatch is working from,
        so a later calendar/timetable change can be compared against it.
        """
        version_id = self.current_version()
        if version_id is None:
            raise PublishError("no_version", "no published version to bind the shift to")
        version = self.version(version_id)
        timetable = load_timetable(self.document(version["timetable_digest"])["payload"])
        calendar = load_calendar(self.document(version["calendar_digest"])["payload"])
        departure = self._find_departure(timetable, operator, trip, stop, service_day)
        if departure is None:
            raise LookupError(
                "no departure " + "/".join((operator, trip, service_day)) + " in version " + str(version_id)
            )
        evidence = interpret_departure(departure, exception_for(calendar, departure))
        with self.connection:
            row = self.connection.execute("SELECT id FROM shifts WHERE external_id=?", (external_id,)).fetchone()
            if row is None:
                cursor = self.connection.execute(
                    "INSERT INTO shifts(external_id,operator,trip,stop,service_day) VALUES (?,?,?,?,?)",
                    (external_id, operator, trip, stop, service_day),
                )
                shift_id = cursor.lastrowid
            else:
                shift_id = row["id"]
            cursor = self._insert_binding(shift_id, version_id, departure, evidence)
        return {"shift_id": shift_id, "binding_id": cursor.lastrowid, "status": evidence["status"]}

    def _insert_binding(self, shift_id, version_id, departure, evidence):
        return self.connection.execute(
            """INSERT INTO shift_bindings(shift_id,version_id,departure_key,service_state,
                   source_time,zone,interpretation) VALUES (?,?,?,?,?,?,?)""",
            (
                shift_id, version_id, self._departure_key(departure), _service_state(evidence),
                departure["source_time"], departure["zone"],
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            ),
        )

    @staticmethod
    def _departure_key(departure):
        return "|".join((
            departure["operator"], departure["trip"], departure["stop"],
            departure["service_day"].isoformat(),
        ))

    @staticmethod
    def _find_departure(timetable, operator, trip, stop, service_day):
        for departure in timetable["departures"]:
            if (
                departure["operator"] == operator and departure["trip"] == trip
                and departure["stop"] == stop and departure["service_day"].isoformat() == service_day
            ):
                return departure
        return None

    def _bind_new_version(self, version_id, previous_version_id, timetable, calendar):
        """Re-evaluate bound shifts and flag changes for dispatch review.

        The baseline is the shift's latest binding, because that is what
        dispatch last saw. Shifts whose departure disappeared keep their
        record and gain ``departure_missing``; nothing is cancelled here.
        """
        rows = self.connection.execute(
            """SELECT b.*, s.external_id
               FROM shifts s JOIN shift_bindings b ON b.id = (
                   SELECT id FROM shift_bindings WHERE shift_id=s.id ORDER BY id DESC LIMIT 1)"""
        ).fetchall()
        departures = {self._departure_key(item): item for item in timetable["departures"]}
        for row in rows:
            departure = departures.get(row["departure_key"])
            old_evidence = json.loads(row["interpretation"])
            if departure is None:
                # Only flag the transition once. A binding that is already
                # ``departure_missing`` gains no second review item.
                if old_evidence["status"] != "departure_missing":
                    self._flag(row, version_id, previous_version_id, "departure_removed", {
                        "previous_source_time": row["source_time"],
                        "previous_candidates": [item["utc"] for item in old_evidence["candidates"]],
                    })
                evidence = dict(old_evidence)
                evidence.update({
                    "status": "departure_missing",
                    "previous_status": old_evidence.get("previous_status", old_evidence["status"]),
                    "candidates": [],
                    "civil_day": None,
                })
                self.connection.execute(
                    """INSERT INTO shift_bindings(shift_id,version_id,departure_key,service_state,
                           source_time,zone,interpretation) VALUES (?,?,?,?,?,?,?)""",
                    (
                        row["shift_id"], version_id, row["departure_key"], "departure_missing",
                        row["source_time"], row["zone"],
                        json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    ),
                )
                continue
            evidence = interpret_departure(departure, exception_for(calendar, departure))
            for reason, detail in _compare(old_evidence, evidence):
                self._flag(row, version_id, previous_version_id, reason, detail)
            self._insert_binding(row["shift_id"], version_id, departure, evidence)

    def _flag(self, row, version_id, previous_version_id, reason, detail):
        self.connection.execute(
            """INSERT OR IGNORE INTO review_items(shift_id,binding_id,previous_version_id,
                   current_version_id,reason,detail) VALUES (?,?,?,?,?,?)""",
            (
                row["shift_id"], row["id"], previous_version_id, version_id, reason,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
            ),
        )

    def review_queue(self, open_only=True):
        query = """SELECT r.*, s.external_id, s.operator, s.trip, s.service_day
                   FROM review_items r JOIN shifts s ON s.id=r.shift_id"""
        if open_only:
            query += " WHERE r.state='open'"
        return [
            {**dict(row), "detail": json.loads(row["detail"])}
            for row in self.connection.execute(query + " ORDER BY r.id")
        ]

    def acknowledge(self, review_id):
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE review_items SET state='acknowledged' WHERE id=? AND state='open'", (review_id,)
            )
        if cursor.rowcount == 0:
            raise LookupError("no open review item: " + str(review_id))

    def shift_trace(self, external_id):
        """Return every binding of a shift, oldest first, with its version."""
        shift = self.connection.execute("SELECT * FROM shifts WHERE external_id=?", (external_id,)).fetchone()
        if shift is None:
            raise KeyError(external_id)
        bindings = []
        for row in self.connection.execute(
            "SELECT * FROM shift_bindings WHERE shift_id=? ORDER BY id", (shift["id"],)
        ):
            version = self.version(row["version_id"])
            timetable_document = self.document(version["timetable_digest"])
            calendar_document = self.document(version["calendar_digest"])
            bindings.append({
                **dict(row),
                "version": {
                    "id": version["id"],
                    "state": version["state"],
                    "timetable_version": version["timetable_version"],
                    "calendar_version": version["calendar_version"],
                    "timetable_digest": version["timetable_digest"],
                    "calendar_digest": version["calendar_digest"],
                    "timetable_capture_id": timetable_document["capture_id"],
                    "calendar_capture_id": calendar_document["capture_id"],
                },
                "interpretation": json.loads(row["interpretation"]),
            })
        return {"shift": dict(shift), "bindings": bindings}

    def ambiguous_shifts(self):
        """Shifts whose latest binding still contains a time ambiguity."""
        rows = self.connection.execute(
            """SELECT s.external_id, b.id AS binding_id, b.service_state, b.source_time,
                      b.zone, b.interpretation
               FROM shifts s JOIN shift_bindings b ON b.id = (
                   SELECT id FROM shift_bindings WHERE shift_id=s.id ORDER BY id DESC LIMIT 1)
               ORDER BY s.external_id"""
        ).fetchall()
        result = []
        for row in rows:
            evidence = json.loads(row["interpretation"])
            if evidence["status"] in ("repeated", "non_existent"):
                result.append({
                    "external_id": row["external_id"],
                    "binding_id": row["binding_id"],
                    "service_state": row["service_state"],
                    "status": evidence["status"],
                    "source_time": row["source_time"],
                    "service_day": evidence["service_day"],
                    "zone": row["zone"],
                    "candidates": evidence["candidates"],
                })
        return result


def _document_kind(payload):
    if not isinstance(payload, dict):
        raise DocumentError("source document must be an object")
    if payload.get("kind") == "timetable":
        load_timetable(payload)
        return "timetable"
    if payload.get("kind") == "calendar":
        load_calendar(payload)
        return "calendar"
    raise DocumentError("document must declare kind=timetable or kind=calendar")


def _service_state(evidence):
    if evidence["status"] == "service_closed":
        return "closed_by_source"
    return "operating"


def _compare(old_evidence, new_evidence):
    """Reasons a bound shift is affected by a newly published version."""
    changes = []
    if old_evidence["status"] == "departure_missing":
        changes.append(("departure_restored", {
            "previous_status": old_evidence.get("previous_status"),
            "status": new_evidence["status"],
            "source_time": new_evidence["source_time"],
        }))
        return changes
    if old_evidence.get("source_offset") != new_evidence.get("source_offset"):
        changes.append(("source_offset_changed", {
            "previous_source_offset": old_evidence.get("source_offset"),
            "source_offset": new_evidence.get("source_offset"),
        }))
    if old_evidence["source_time"] != new_evidence["source_time"]:
        changes.append(("time_changed", {
            "previous_source_time": old_evidence["source_time"],
            "source_time": new_evidence["source_time"],
        }))
    if old_evidence["status"] != new_evidence["status"]:
        changes.append(("interpretation_changed", {
            "previous_status": old_evidence["status"],
            "status": new_evidence["status"],
        }))
    old_instants = sorted(candidate["utc"] for candidate in old_evidence["candidates"])
    new_instants = sorted(candidate["utc"] for candidate in new_evidence["candidates"])
    if old_instants != new_instants:
        changes.append(("instants_changed", {
            "previous_candidates": old_instants,
            "candidates": new_instants,
        }))
    if new_evidence["status"] == "service_closed" and old_evidence["status"] != "service_closed":
        changes.append(("service_closed", {
            "reason": new_evidence["closure"]["reason"],
            "service_day": new_evidence["closure"]["service_day"],
        }))
    if old_evidence["status"] == "service_closed" and new_evidence["status"] != "service_closed":
        changes.append(("service_reopened", {
            "previous_reason": old_evidence.get("closure", {}).get("reason"),
            "status": new_evidence["status"],
        }))
    return changes
