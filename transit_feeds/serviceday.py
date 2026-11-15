"""服务日解释层。

把供方保留的时刻写法（例如 ``26:30``）结合运营方时区与服务日，解释为带依据的
UTC 候选。核心约束：

* 来源写法逐字保留，不做就地覆盖；
* 夏令时回拨产生的重复当地时间保留两个候选，不任选一个；
* 夏令时前跳产生的不存在当地时间显式标记为 ``nonexistent``，不静默取系统默认值；
* 无法解释的输入显式标记为 ``invalid`` 并给出机器可读的原因码。

本模块只做纯解释，不接触数据库与网络，便于跨版本缓存与独立测试。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError

# 计划班次不可能跨两个以上自然日，>=48 小时视为录入错误而非合法时刻。
MAX_CLOCK_HOUR = 47

STATUS_DEFINITE = "definite"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NONEXISTENT = "nonexistent"
STATUS_INVALID = "invalid"

ISSUE_BAD_SERVICE_DAY = "bad_service_day"
ISSUE_BAD_CLOCK_FORMAT = "bad_clock_format"
ISSUE_CLOCK_OUT_OF_RANGE = "clock_out_of_range"
ISSUE_UNKNOWN_ZONE = "unknown_zone"
ISSUE_AMBIGUOUS_FOLD = "ambiguous_fold"
ISSUE_NONEXISTENT_GAP = "nonexistent_gap"

_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_SERVICE_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ClockParseError(ValueError):
    """时刻写法无法解析，issue_code 给出机器可读原因。"""

    def __init__(self, issue_code: str, detail: str):
        super().__init__(detail)
        self.issue_code = issue_code
        self.detail = detail


@dataclass(frozen=True)
class ClockTime:
    """解析后的供方时刻；verbatim 保留原始写法。"""

    hour: int
    minute: int
    second: int
    elapsed_minutes: int
    verbatim: str


@dataclass(frozen=True)
class InstantCandidate:
    """一个可成立的 UTC 瞬间；fold 对应 PEP 495 的 fold 取值。"""

    fold: int
    utc_iso: str
    utc_offset_minutes: int
    dst_minutes: int


@dataclass(frozen=True)
class GapBracket:
    """不存在当地时间两侧的 UTC 边界，作为解释依据而非替代值。"""

    earliest_utc_iso: str
    latest_utc_iso: str


@dataclass(frozen=True)
class DepartureInterpretation:
    source_departure: str
    service_day: str
    zone_name: str
    calendar_version: str | None
    wall_local_iso: str | None
    elapsed_minutes: int | None
    crosses_midnight: bool
    status: str
    candidates: tuple[InstantCandidate, ...]
    gap_bracket: GapBracket | None
    issue_code: str | None
    detail: str | None
    zone_rule: str

    def to_dict(self) -> dict:
        data = {
            "source_departure": self.source_departure,
            "service_day": self.service_day,
            "zone_name": self.zone_name,
            "calendar_version": self.calendar_version,
            "wall_local_iso": self.wall_local_iso,
            "elapsed_minutes": self.elapsed_minutes,
            "crosses_midnight": self.crosses_midnight,
            "status": self.status,
            "candidates": [candidate.__dict__ for candidate in self.candidates],
            "gap_bracket": None if self.gap_bracket is None else self.gap_bracket.__dict__,
            "issue_code": self.issue_code,
            "detail": self.detail,
            "zone_rule": self.zone_rule,
        }
        return data


@lru_cache(maxsize=1)
def tzdata_version() -> str:
    """读取运行环境时区数据库版本；找不到时显式返回 unknown，不猜测。"""
    for root in TZPATH:
        marker = Path(root) / "tzdata.zi"
        if marker.is_file():
            for line in marker.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0] == "#" and parts[1] == "version":
                    return parts[2]
    return "unknown"


def zone_rule_id(zone_name: str) -> str:
    """生成解释所用时区规则标识：时区名 + 时区数据库版本。"""
    return f"{zone_name}@tzdata{tzdata_version()}"


def parse_service_day(value) -> date:
    if not isinstance(value, str) or not _SERVICE_DAY_RE.match(value.strip()):
        raise ClockParseError(ISSUE_BAD_SERVICE_DAY, f"service_day 必须是 YYYY-MM-DD 字符串: {value!r}")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise ClockParseError(ISSUE_BAD_SERVICE_DAY, f"service_day 不是有效日期: {value!r}") from error


def parse_clock(value) -> ClockTime:
    """解析供方时刻写法，允许 24:00 以上的跨日小时数。"""
    if not isinstance(value, str):
        raise ClockParseError(ISSUE_BAD_CLOCK_FORMAT, f"时刻必须是字符串: {value!r}")
    verbatim = value
    matched = _CLOCK_RE.match(value.strip())
    if matched is None:
        raise ClockParseError(ISSUE_BAD_CLOCK_FORMAT, f"无法识别的时刻写法: {value!r}")
    hour, minute = int(matched.group(1)), int(matched.group(2))
    second = int(matched.group(3) or 0)
    if hour > MAX_CLOCK_HOUR:
        raise ClockParseError(
            ISSUE_CLOCK_OUT_OF_RANGE,
            f"小时数 {hour} 超过 {MAX_CLOCK_HOUR}，疑似录入错误: {value!r}",
        )
    if minute >= 60 or second >= 60:
        raise ClockParseError(ISSUE_CLOCK_OUT_OF_RANGE, f"分秒超出范围: {value!r}")
    elapsed_minutes = hour * 60 + minute
    return ClockTime(hour, minute, second, elapsed_minutes, verbatim)


def _invalid(service_day, departure, zone_name, calendar_version, issue_code, detail) -> DepartureInterpretation:
    zone_rule = None
    if isinstance(zone_name, str) and zone_name.strip():
        try:
            ZoneInfo(zone_name)
            zone_rule = zone_rule_id(zone_name)
        except ZoneInfoNotFoundError:
            zone_rule = f"{zone_name}@unresolved"
    return DepartureInterpretation(
        source_departure=departure if isinstance(departure, str) else str(departure),
        service_day=service_day if isinstance(service_day, str) else str(service_day),
        zone_name=zone_name if isinstance(zone_name, str) else str(zone_name),
        calendar_version=calendar_version,
        wall_local_iso=None,
        elapsed_minutes=None,
        crosses_midnight=False,
        status=STATUS_INVALID,
        candidates=(),
        gap_bracket=None,
        issue_code=issue_code,
        detail=detail,
        zone_rule=zone_rule,
    )


def _candidate(fold: int, aware: datetime) -> InstantCandidate:
    utc_value = aware.astimezone(timezone.utc)
    offset = aware.utcoffset()
    dst = aware.dst() or timedelta(0)
    return InstantCandidate(
        fold=fold,
        utc_iso=utc_value.strftime("%Y-%m-%dT%H:%M:%SZ"),
        utc_offset_minutes=int(offset.total_seconds() // 60),
        dst_minutes=int(dst.total_seconds() // 60),
    )


def interpret_departure(service_day, departure, zone_name, calendar_version=None) -> DepartureInterpretation:
    """解释单条班线时刻，任何数据问题都返回 invalid 状态而非抛默认值。"""
    try:
        day = parse_service_day(service_day)
    except ClockParseError as error:
        return _invalid(service_day, departure, zone_name, calendar_version, error.issue_code, error.detail)
    try:
        clock = parse_clock(departure)
    except ClockParseError as error:
        return _invalid(service_day, departure, zone_name, calendar_version, error.issue_code, error.detail)
    try:
        zone = ZoneInfo(zone_name)
    except Exception as error:  # noqa: BLE001 - 时区名缺失/非法/库异常都显式标记，不静默回退
        return _invalid(
            service_day, departure, zone_name, calendar_version,
            ISSUE_UNKNOWN_ZONE, f"时区无法解析: {zone_name!r} ({error})",
        )

    midnight = datetime.combine(day, time.min)
    wall_naive = midnight + timedelta(minutes=clock.elapsed_minutes)
    aware_fold0 = wall_naive.replace(tzinfo=zone, fold=0)
    aware_fold1 = wall_naive.replace(tzinfo=zone, fold=1)
    offset0, offset1 = aware_fold0.utcoffset(), aware_fold1.utcoffset()
    crosses_midnight = clock.elapsed_minutes >= 24 * 60
    common = dict(
        source_departure=clock.verbatim,
        service_day=day.isoformat(),
        zone_name=zone_name,
        calendar_version=calendar_version,
        wall_local_iso=wall_naive.isoformat(),
        elapsed_minutes=clock.elapsed_minutes,
        crosses_midnight=crosses_midnight,
        zone_rule=zone_rule_id(zone_name),
    )

    if offset0 == offset1:
        return DepartureInterpretation(
            **common, status=STATUS_DEFINITE,
            candidates=(_candidate(0, aware_fold0),),
            gap_bracket=None, issue_code=None, detail=None,
        )

    roundtrip0 = aware_fold0.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == wall_naive
    roundtrip1 = aware_fold1.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == wall_naive

    if roundtrip0 and roundtrip1:
        candidates = sorted(
            (_candidate(0, aware_fold0), _candidate(1, aware_fold1)),
            key=lambda item: item.utc_iso,
        )
        return DepartureInterpretation(
            **common, status=STATUS_AMBIGUOUS, candidates=tuple(candidates), gap_bracket=None,
            issue_code=ISSUE_AMBIGUOUS_FOLD,
            detail=(
                f"当地时间 {wall_naive.isoformat()} 在夏令时回拨中出现两次，"
                f"保留 fold=0/fold=1 两个 UTC 候选，需要调度确认"
            ),
        )

    bracket = sorted(
        (
            _candidate(0, aware_fold0).utc_iso,
            _candidate(1, aware_fold1).utc_iso,
        )
    )
    return DepartureInterpretation(
        **common, status=STATUS_NONEXISTENT, candidates=(),
        gap_bracket=GapBracket(earliest_utc_iso=bracket[0], latest_utc_iso=bracket[1]),
        issue_code=ISSUE_NONEXISTENT_GAP,
        detail=(
            f"当地时间 {wall_naive.isoformat()} 落在夏令时前跳空档内，时钟直接跳过该时刻，"
            f"不存在对应 UTC，禁止以系统默认值顶替"
        ),
    )


def cache_key_for(interpretation: DepartureInterpretation) -> str:
    """解释依据的内容哈希；依据不变则跨时刻表版本复用同一缓存条目。"""
    basis = {
        "service_day": interpretation.service_day,
        "departure": interpretation.source_departure,
        "zone": interpretation.zone_name,
        "calendar_version": interpretation.calendar_version,
        "zone_rule": interpretation.zone_rule,
    }
    return hashlib.sha256(
        json.dumps(basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
