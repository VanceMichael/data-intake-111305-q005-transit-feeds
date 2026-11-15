"""版本目录：把分两次到达的时刻表与例外日历装配成一组相容版本。

供方资料分两类部件（part）：

* ``timetable``：线路时刻表，声明其依据的 ``calendar_version``；
* ``calendar``：运营日历，按 ``calendar_version`` 给出停开/增补例外。

同一 (operator, calendar_version) 的两个部件齐备时才构成一组 *bundle*；
业务只能看到装配完成的 bundle，单到的部件保持 pending，不与其他版本混搭。
部件只追加，新版本不会覆盖旧版本；装配结果可重放，失败后重跑幂等。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

ACTION_CANCEL = "cancel"
ACTION_ADDED = "added"
VALID_ACTIONS = frozenset((ACTION_CANCEL, ACTION_ADDED))

PART_TIMETABLE = "timetable"
PART_CALENDAR = "calendar"
VALID_PARTS = frozenset((PART_TIMETABLE, PART_CALENDAR))


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class FeedPart:
    part: str
    operator: str
    calendar_version: str | None
    trips: tuple[dict[str, Any], ...]
    exceptions: tuple[dict[str, Any], ...]
    digest: str
    observed_at: str
    capture_id: int | None
    source_text: str

    @property
    def key(self) -> tuple[str, str | None]:
        return self.operator, self.calendar_version


@dataclass(frozen=True)
class Bundle:
    operator: str
    calendar_version: str
    timetable: FeedPart
    calendar: FeedPart
    digest: str


@dataclass(frozen=True)
class TripInstance:
    operator: str
    trip: str
    service_day: str
    departure: str
    zone: str
    calendar_version: str
    operates: bool
    action: str
    source: dict[str, Any]
    warnings: tuple[str, ...] = field(default=())


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def normalize_part(raw: dict[str, Any], observed_at: str, capture_id: int | None = None) -> FeedPart:
    """规范化一次到达的部件；字段缺失或动作非法时抛 CatalogError，由上层隔离。"""
    part = raw.get("part", PART_TIMETABLE)
    if part not in VALID_PARTS:
        raise CatalogError(f"未知部件类型 part={part!r}")
    operator = str(raw.get("operator", "")).strip()
    if not operator:
        raise CatalogError("部件缺少 operator")
    calendar_version = raw.get("calendar_version")
    if part == PART_TIMETABLE and not (isinstance(calendar_version, str) and calendar_version.strip()):
        raise CatalogError("时刻表部件必须声明非空 calendar_version")
    if part == PART_CALENDAR and not (isinstance(calendar_version, str) and calendar_version.strip()):
        raise CatalogError("日历部件必须声明非空 calendar_version")
    calendar_version = calendar_version.strip() if isinstance(calendar_version, str) else calendar_version

    trips = raw.get("trips")
    exceptions = raw.get("exceptions", [])
    if part == PART_TIMETABLE:
        if not isinstance(trips, list) or any(not isinstance(item, dict) for item in trips):
            raise CatalogError("时刻表部件的 trips 必须是对象数组")
        exceptions = []
    else:
        if not isinstance(exceptions, list) or any(not isinstance(item, dict) for item in exceptions):
            raise CatalogError("日历部件的 exceptions 必须是对象数组")
        for item in exceptions:
            action = item.get("action", ACTION_CANCEL)
            if action not in VALID_ACTIONS:
                raise CatalogError(f"例外动作非法: {action!r}")
        trips = []

    source_text = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return FeedPart(
        part=part,
        operator=operator,
        calendar_version=calendar_version,
        trips=tuple(trips),
        exceptions=tuple(exceptions),
        digest=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        observed_at=observed_at,
        capture_id=capture_id,
        source_text=source_text,
    )


def assemble_bundles(parts: list[FeedPart]) -> tuple[list[Bundle], list[FeedPart]]:
    """按 (operator, calendar_version) 配对；返回相容 bundle 与仍单到的 pending 部件。"""
    latest_timetable: dict[tuple[str, str], FeedPart] = {}
    latest_calendar: dict[tuple[str, str], FeedPart] = {}
    pending: list[FeedPart] = []
    for part in parts:
        if part.calendar_version is None:
            pending.append(part)
            continue
        target = latest_timetable if part.part == PART_TIMETABLE else latest_calendar
        target[part.key] = part

    bundles: list[Bundle] = []
    paired_keys = set(latest_timetable) & set(latest_calendar)
    for key in sorted(paired_keys):
        timetable = latest_timetable[key]
        calendar = latest_calendar[key]
        bundle_digest = _digest({"timetable": timetable.digest, "calendar": calendar.digest})
        bundles.append(
            Bundle(
                operator=key[0],
                calendar_version=key[1],
                timetable=timetable,
                calendar=calendar,
                digest=bundle_digest,
            )
        )
    for part in parts:
        if part.calendar_version is None or part.key not in paired_keys:
            pending.append(part)
    return bundles, pending


def _trip_identity(trip: dict[str, Any]) -> tuple[str, str]:
    return str(trip["trip"]), str(trip["service_day"])


def apply_calendar(bundle: Bundle) -> tuple[list[TripInstance], list[str]]:
    """在一组相容版本上展开运营日：基础班次叠加停开/增补例外，保留来源写法。

    返回 (班次实例, 装配级警告)。警告不改变任何实例，只提示例外未命中基础班次等情况。
    """
    exceptions_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    warnings: list[str] = []
    for exception in bundle.calendar.exceptions:
        identity = (str(exception.get("trip", "")), str(exception.get("service_day", "")))
        if not identity[0] or not identity[1]:
            raise CatalogError(f"例外缺少 trip/service_day: {exception!r}")
        exceptions_by_identity[identity] = exception

    instances: list[TripInstance] = []
    base_identities = set()
    for trip in bundle.timetable.trips:
        for required in ("trip", "service_day", "departure", "zone"):
            if required not in trip:
                raise CatalogError(f"班次缺少字段 {required}: {trip!r}")
        identity = _trip_identity(trip)
        base_identities.add(identity)
        exception = exceptions_by_identity.get(identity)
        operates = True
        action = "scheduled"
        if exception is not None and exception.get("action", ACTION_CANCEL) == ACTION_CANCEL:
            operates = False
            action = ACTION_CANCEL
        instances.append(
            TripInstance(
                operator=bundle.operator,
                trip=identity[0],
                service_day=identity[1],
                departure=str(trip["departure"]),
                zone=str(trip["zone"]),
                calendar_version=bundle.calendar_version,
                operates=operates,
                action=action,
                source=dict(trip),
            )
        )

    for identity, exception in sorted(exceptions_by_identity.items()):
        if identity in base_identities:
            continue
        action = exception.get("action", ACTION_CANCEL)
        if action == ACTION_CANCEL:
            warnings.append(
                f"停开例外 {identity} 在时刻表 {bundle.calendar_version} 中没有对应基础班次，已忽略"
            )
            continue
        instance_warnings = () if exception.get("departure") and exception.get("zone") else ("增补班次缺少完整时刻字段",)
        instances.append(
            TripInstance(
                operator=bundle.operator,
                trip=identity[0],
                service_day=identity[1],
                departure=str(exception.get("departure", "")),
                zone=str(exception.get("zone", "")),
                calendar_version=bundle.calendar_version,
                operates=True,
                action=ACTION_ADDED,
                source=dict(exception),
                warnings=instance_warnings,
            )
        )
    instances.sort(key=lambda item: (item.service_day, item.trip))
    return instances, warnings
