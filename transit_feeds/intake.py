"""Intake: fetch timetable/calendar halves, archive them, record outcomes.

The halves are fetched independently. A half that fails (rate limit,
unavailable, truncated, undecodable) is recorded as a failed observation
and never becomes an empty document. A half that already succeeded is
kept by digest and reused, so a retry only re-captures what is missing.

``sync`` is safe to run repeatedly: unchanged documents are detected by
ETag/digest and an already published pair is not re-published.
"""

import json
import time
from dataclasses import dataclass

from .archive import Archive
from .client import Client
from .decoding import document
from .publishing import PublishError, Store, sleep_for

RETRYABLE = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Fetched:
    """Outcome of one route fetch. ``payload`` is ``None`` unless usable."""

    status: int
    payload: object
    capture_id: object
    etag: object = None


class Intake:
    def __init__(self, base_url, archive_path, store_path, source, attempts=3,
                 sleep=time.sleep, timeout=3):
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        self.client = Client(base_url, timeout=timeout)
        self.archive = Archive(archive_path)
        self.store = Store(store_path)
        self.source = source
        self.attempts = attempts
        self.sleep = sleep

    def close(self):
        self.store.close()
        self.archive.close()

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()

    def _conditional_etag(self, route):
        """ETag last recorded for this route, so a resumed run stays conditional."""
        for row in reversed(self.store.observations(route)):
            etag = json.loads(row["detail"]).get("etag")
            if etag:
                return etag
        return None

    def fetch_once(self, route, conditional=True):
        """Fetch one route with bounded retries, recording every attempt."""
        headers = {}
        etag = self._conditional_etag(route)
        if conditional and etag:
            headers["If-None-Match"] = etag
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.client.fetch(route, headers)
            except Exception as error:
                if attempt == self.attempts:
                    self.store.record_observation(
                        route, 0, {"error": type(error).__name__, "message": str(error)}
                    )
                    return Fetched(0, None, None)
                self.sleep(float(attempt))
                continue
            capture_id = self.archive.append(
                self.source, response.url, response.status, response.headers, response.body
            )
            response_etag = response.headers.get("etag") or etag
            status = response.status
            if status == 304:
                self.store.record_observation(
                    route, 304, {"not_modified": True, "etag": response_etag}, capture_id
                )
                return Fetched(304, None, capture_id, response_etag)
            if status != 200:
                if status in RETRYABLE and attempt < self.attempts:
                    self.sleep(sleep_for(response.headers))
                    continue
                self.store.record_observation(route, status, {
                    "error": "source status",
                    "retry_after": response.headers.get("retry-after"),
                    "etag": response_etag,
                }, capture_id)
                return Fetched(status, None, capture_id, response_etag)
            try:
                payload = document(response.body, response.headers.get("content-type", ""))
            except (ValueError, UnicodeDecodeError) as error:
                self.store.record_observation(
                    route, 200, {"error": str(error), "etag": response_etag}, capture_id
                )
                return Fetched(200, None, capture_id, response_etag)
            return Fetched(200, payload, capture_id, response_etag)
        return Fetched(0, None, None)

    def sync(self, timetable_route="/timetable", calendar_route="/calendar"):
        """Fetch both halves, then publish the pair they currently describe."""
        outcomes = {}
        for role, route in (("timetable", timetable_route), ("calendar", calendar_route)):
            fetched = self.fetch_once(route)
            outcome = {"route": route, "status": fetched.status, "capture_id": fetched.capture_id}
            if fetched.payload is not None:
                recorded = self.store.record_document(
                    route, fetched.status, fetched.payload, fetched.capture_id, etag=fetched.etag
                )
                if recorded is not None:
                    outcome["digest"], outcome["document"] = recorded
            if "digest" not in outcome and fetched.status != 304:
                outcome["error"] = _last_error(self.store, route)
            outcomes[role] = outcome
        outcomes["version_id"] = self.publish_available()
        outcomes["version"] = (self.store.version(outcomes["version_id"])
                               if outcomes["version_id"] is not None else None)
        outcomes["pending"] = self.store.pending_halves()
        outcomes["ambiguous"] = self.store.ambiguous_shifts()
        return outcomes

    def publish_available(self):
        """Publish the pair formed by the source's current statement per half.

        A half that did not change (304 or absent) falls back to the half
        in the current version, so a calendar-only delivery is evaluated
        against the timetable already in force. A *new* timetable that
        declares a calendar version not yet delivered is held back rather
        than paired with the old calendar; the previous version stays
        live until the matching calendar arrives. Any incompatibility
        leaves the current version in force and is recorded.
        """
        halves = self.store.current_halves()
        current = self.store.current_version()
        current_version = self.version_or_none(current)
        timetable_digest = halves.get("timetable")
        calendar_digest = halves.get("calendar")
        if current_version is not None:
            timetable_digest = timetable_digest or current_version["timetable_digest"]
            calendar_digest = calendar_digest or current_version["calendar_digest"]
        if timetable_digest is None or calendar_digest is None:
            self.store.record_observation("publish", 0, {
                "error": "waiting for the other half",
                "missing": "calendar" if timetable_digest else "timetable",
                "current_version_id": current,
            })
            return current
        timetable, calendar = self.store.plans(timetable_digest, calendar_digest)
        # A timetable already in force may be paired with a newer calendar:
        # calendar revisions supersede exceptions independently. A *new*
        # timetable is paired only with the calendar version it declares;
        # otherwise it waits instead of silently mixing two revisions.
        in_force = (current_version is not None
                    and timetable_digest == current_version["timetable_digest"])
        if not in_force and timetable["calendar_version"] != calendar["version"]:
            declared = self.store.calendar_digest_for(timetable["calendar_version"])
            if declared is None:
                self.store.record_observation("publish", 0, {
                    "error": "waiting for the declared calendar version",
                    "code": "calendar_version_pending",
                    "declared": timetable["calendar_version"],
                    "available": calendar["version"],
                    "timetable_digest": timetable_digest,
                    "calendar_digest": calendar_digest,
                    "current_version_id": current,
                })
                return current
            timetable, calendar = self.store.plans(timetable_digest, declared)
            calendar_digest = declared
        try:
            return self.store.publish(timetable_digest, calendar_digest)
        except PublishError as error:
            self.store.record_observation("publish", 0, {
                "error": str(error), "code": error.code,
                "timetable_digest": timetable_digest, "calendar_digest": calendar_digest,
                "current_version_id": current,
            })
            return current

    def version_or_none(self, version_id):
        return self.store.version(version_id) if version_id is not None else None


def _last_error(store, route):
    rows = store.observations(route)
    return json.loads(rows[-1]["detail"]) if rows else None
