"""服务日解释的持久化层。

与只追加原始字节的 :mod:`transit_feeds.archive` 分工：archive 保存供方原始响应，
本库保存解释后的业务版本。设计要点：

* 部件（part）只追加，新修订不覆盖旧修订；
* 解释结果按依据哈希跨版本缓存，依据不变（含时区数据库版本）则复用；
* 每个运营方同时只有一组有效 bundle，单到的部件不进入有效视图；
* 派班记录保存当时解释快照，可从派班反查来源时刻与解释依据；
* 日历更新只对受影响的已派班次生成复核条目，绝不自动取消任务。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import Bundle, FeedPart
from .serviceday import (
    STATUS_AMBIGUOUS,
    STATUS_INVALID,
    STATUS_NONEXISTENT,
    DepartureInterpretation,
    cache_key_for,
)

REVIEW_OPEN = "open"
REVIEW_RESOLVED = "resolved"

RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_FAILED = "failed"

REVIEW_CALENDAR_CHANGE = "calendar_change"
REVIEW_AMBIGUOUS_DISPATCH = "ambiguous_dispatch"
REVIEW_UNRESOLVABLE_DISPATCH = "unresolvable_dispatch"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    id INTEGER PRIMARY KEY,
    part TEXT NOT NULL,
    operator TEXT NOT NULL,
    calendar_version TEXT,
    digest TEXT NOT NULL UNIQUE,
    observed_at TEXT NOT NULL,
    capture_id INTEGER,
    source_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interpretations (
    cache_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    interpretation_json TEXT NOT NULL,
    computed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bundles (
    id INTEGER PRIMARY KEY,
    operator TEXT NOT NULL,
    calendar_version TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    timetable_part_id INTEGER NOT NULL REFERENCES parts(id),
    calendar_part_id INTEGER NOT NULL REFERENCES parts(id),
    is_effective INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bundle_identity
    ON bundles(operator, calendar_version);
CREATE TABLE IF NOT EXISTS trip_instances (
    id INTEGER PRIMARY KEY,
    bundle_id INTEGER NOT NULL REFERENCES bundles(id),
    operator TEXT NOT NULL,
    trip TEXT NOT NULL,
    service_day TEXT NOT NULL,
    operates INTEGER NOT NULL,
    action TEXT NOT NULL,
    departure TEXT NOT NULL,
    zone TEXT NOT NULL,
    source_json TEXT NOT NULL,
    interp_cache_key TEXT,
    interp_json TEXT,
    interp_status TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_instance_identity
    ON trip_instances(operator, trip, service_day);
CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id TEXT PRIMARY KEY,
    operator TEXT NOT NULL,
    trip TEXT NOT NULL,
    service_day TEXT NOT NULL,
    bundle_id INTEGER REFERENCES bundles(id),
    part_capture_id INTEGER,
    source_departure TEXT NOT NULL,
    interp_cache_key TEXT,
    interp_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY,
    dispatch_id TEXT REFERENCES dispatches(dispatch_id),
    operator TEXT NOT NULL,
    trip TEXT NOT NULL,
    service_day TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT,
    dedupe_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS source_watermarks (
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    etag TEXT,
    last_capture_id INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, path)
);
CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ServiceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(_SCHEMA)

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self.connection:
            yield self.connection

    # -- 部件（只追加，按内容去重，重复补抓返回同一行） -----------------------
    def add_part(self, part: FeedPart) -> tuple[int, bool]:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM parts WHERE digest=?", (part.digest,)
            ).fetchone()
            if existing is not None:
                return existing["id"], False
            cursor = connection.execute(
                "INSERT INTO parts(part,operator,calendar_version,digest,observed_at,capture_id,source_text)"
                " VALUES (?,?,?,?,?,?,?)",
                (part.part, part.operator, part.calendar_version, part.digest,
                 part.observed_at, part.capture_id, part.source_text),
            )
            return cursor.lastrowid, True

    def list_parts(self, operator: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM parts"
        params: tuple = ()
        if operator is not None:
            query += " WHERE operator=?"
            params = (operator,)
        rows = self.connection.execute(query + " ORDER BY id", params).fetchall()
        return [dict(row) for row in rows]

    # -- 解释缓存（跨版本，依据含 tzdata 版本） -------------------------------
    def get_interpretation(self, cache_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT interpretation_json FROM interpretations WHERE cache_key=?", (cache_key,)
        ).fetchone()
        return None if row is None else json.loads(row["interpretation_json"])

    def store_interpretation(self, interpretation: DepartureInterpretation) -> tuple[str, bool]:
        key = cache_key_for(interpretation)
        payload = interpretation.to_dict()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT cache_key FROM interpretations WHERE cache_key=?", (key,)
            ).fetchone()
            if existing is not None:
                return key, False
            connection.execute(
                "INSERT INTO interpretations(cache_key,status,interpretation_json,computed_at)"
                " VALUES (?,?,?,?)",
                (key, interpretation.status, json.dumps(payload, ensure_ascii=False), _utc_now()),
            )
        return key, True

    # -- bundle 与有效版本 ----------------------------------------------------
    def record_bundle(self, bundle: Bundle) -> tuple[int, bool]:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM bundles WHERE digest=?", (bundle.digest,)
            ).fetchone()
            if existing is not None:
                return existing["id"], False
            timetable_row = connection.execute(
                "SELECT id FROM parts WHERE digest=?", (bundle.timetable.digest,)
            ).fetchone()
            calendar_row = connection.execute(
                "SELECT id FROM parts WHERE digest=?", (bundle.calendar.digest,)
            ).fetchone()
            if timetable_row is None or calendar_row is None:
                raise sqlite3.IntegrityError("bundle 引用的部件尚未入库")
            cursor = connection.execute(
                "INSERT INTO bundles(operator,calendar_version,digest,timetable_part_id,"
                "calendar_part_id,created_at) VALUES (?,?,?,?,?,?)",
                (bundle.operator, bundle.calendar_version, bundle.digest,
                 timetable_row["id"], calendar_row["id"], _utc_now()),
            )
            return cursor.lastrowid, True

    def promote_bundle(self, bundle_id: int) -> None:
        """把一组 bundle 提升为该运营方的唯一有效版本，旧版本保留可追溯。"""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT operator FROM bundles WHERE id=?", (bundle_id,)
            ).fetchone()
            if row is None:
                raise KeyError(bundle_id)
            connection.execute(
                "UPDATE bundles SET is_effective=0 WHERE operator=?", (row["operator"],)
            )
            connection.execute(
                "UPDATE bundles SET is_effective=1 WHERE id=?", (bundle_id,)
            )

    def effective_bundle(self, operator: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM bundles WHERE operator=? AND is_effective=1", (operator,)
        ).fetchone()
        return None if row is None else dict(row)

    def list_bundles(self, operator: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM bundles"
        params: tuple = ()
        if operator is not None:
            query += " WHERE operator=?"
            params = (operator,)
        return [dict(row) for row in self.connection.execute(query + " ORDER BY id", params).fetchall()]

    def replace_instances(self, bundle_id: int, materialized: list[dict[str, Any]]) -> None:
        """在事务内整体替换某 bundle 的班次实例（失败回滚，不残留半成品）。"""
        with self.transaction() as connection:
            connection.execute("DELETE FROM trip_instances WHERE bundle_id=?", (bundle_id,))
            connection.executemany(
                "INSERT INTO trip_instances(bundle_id,operator,trip,service_day,operates,action,"
                "departure,zone,source_json,interp_cache_key,interp_json,interp_status,warnings_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        bundle_id, item["operator"], item["trip"], item["service_day"],
                        1 if item["operates"] else 0, item["action"], item["departure"],
                        item["zone"], json.dumps(item["source"], ensure_ascii=False),
                        item.get("interp_cache_key"),
                        json.dumps(item["interpretation"], ensure_ascii=False)
                        if item.get("interpretation") is not None else None,
                        item.get("interp_status"),
                        json.dumps(item.get("warnings", []), ensure_ascii=False),
                    )
                    for item in materialized
                ],
            )

    def effective_instances(self, operator: str) -> list[dict[str, Any]]:
        bundle = self.effective_bundle(operator)
        if bundle is None:
            return []
        return self._instances_of(bundle["id"])

    def _instances_of(self, bundle_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM trip_instances WHERE bundle_id=? ORDER BY service_day,trip", (bundle_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["operates"] = bool(row["operates"])
            item["source"] = json.loads(row["source_json"])
            item["warnings"] = json.loads(row["warnings_json"])
            item["interpretation"] = json.loads(row["interp_json"]) if row["interp_json"] else None
            result.append(item)
        return result

    def get_effective_instance(self, operator: str, trip: str, service_day: str) -> dict[str, Any] | None:
        bundle = self.effective_bundle(operator)
        if bundle is None:
            return None
        row = self.connection.execute(
            "SELECT * FROM trip_instances WHERE bundle_id=? AND trip=? AND service_day=?",
            (bundle["id"], trip, service_day),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["operates"] = bool(row["operates"])
        item["source"] = json.loads(row["source_json"])
        item["warnings"] = json.loads(row["warnings_json"])
        item["interpretation"] = json.loads(row["interp_json"]) if row["interp_json"] else None
        return item

    def instance_history(self, operator: str, trip: str, service_day: str) -> list[dict[str, Any]]:
        """同一业务班次在所有 bundle 版本上的解释历史。"""
        rows = self.connection.execute(
            "SELECT ti.*, b.calendar_version, b.is_effective FROM trip_instances ti"
            " JOIN bundles b ON b.id=ti.bundle_id"
            " WHERE ti.operator=? AND ti.trip=? AND ti.service_day=?"
            " ORDER BY b.id",
            (operator, trip, service_day),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["operates"] = bool(row["operates"])
            item["source"] = json.loads(row["source_json"])
            item["interpretation"] = json.loads(row["interp_json"]) if row["interp_json"] else None
            result.append(item)
        return result

    # -- 派班与追溯 -----------------------------------------------------------
    def record_dispatch(self, dispatch_id: str, instance: dict[str, Any],
                        part_capture_id: int | None = None) -> dict[str, Any]:
        """记录派班并冻结当时解释快照；有歧义/不可解释时自动开复核，不阻断派班。"""
        interpretation = instance.get("interpretation")
        interp_json = json.dumps(interpretation, ensure_ascii=False) if interpretation else "null"
        cache_key = instance.get("interp_cache_key")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT dispatch_id FROM dispatches WHERE dispatch_id=?", (dispatch_id,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"派班记录已存在: {dispatch_id}")
            connection.execute(
                "INSERT INTO dispatches(dispatch_id,operator,trip,service_day,bundle_id,"
                "part_capture_id,source_departure,interp_cache_key,interp_json,recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (dispatch_id, instance["operator"], instance["trip"], instance["service_day"],
                 instance["bundle_id"] if "bundle_id" in instance else None,
                 part_capture_id, instance["departure"], cache_key, interp_json, _utc_now()),
            )
            status = instance.get("interp_status")
            if status in (STATUS_AMBIGUOUS, STATUS_NONEXISTENT, STATUS_INVALID):
                reason, detail = {
                    STATUS_AMBIGUOUS: (
                        REVIEW_AMBIGUOUS_DISPATCH,
                        "派班时刻存在夏令时回拨歧义，候选 UTC 未由调度确认",
                    ),
                    STATUS_NONEXISTENT: (
                        REVIEW_UNRESOLVABLE_DISPATCH,
                        "派班时刻是不存在的当地时间（夏令时前跳空档），需要人工给出来源依据",
                    ),
                    STATUS_INVALID: (
                        REVIEW_UNRESOLVABLE_DISPATCH,
                        "派班时刻无法解释，已保留原文等待人工处理",
                    ),
                }[status]
                self._insert_review(
                    connection, dispatch_id, instance["operator"], instance["trip"],
                    instance["service_day"], reason, detail,
                )
        return self.trace_dispatch(dispatch_id)

    def trace_dispatch(self, dispatch_id: str) -> dict[str, Any]:
        """从派班记录查回当时的来源时刻、解释依据与版本链路。"""
        row = self.connection.execute(
            "SELECT * FROM dispatches WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise KeyError(dispatch_id)
        dispatch = dict(row)
        dispatch["interpretation"] = json.loads(row["interp_json"])
        bundle = None
        if row["bundle_id"] is not None:
            bundle = dict(self.connection.execute(
                "SELECT * FROM bundles WHERE id=?", (row["bundle_id"],)
            ).fetchone())
        return {
            "dispatch": dispatch,
            "bundle": bundle,
            "history": self.instance_history(row["operator"], row["trip"], row["service_day"]),
            "reviews": self.list_reviews(dispatch_id=dispatch_id),
        }

    # -- 复核队列（只登记，不替调度取消） -------------------------------------
    @staticmethod
    def _dedupe_key(dispatch_id, operator, trip, service_day, reason):
        return "|".join((dispatch_id or "-", operator, trip, service_day, reason))

    def _insert_review(self, connection, dispatch_id, operator, trip, service_day, reason, detail):
        connection.execute(
            "INSERT OR IGNORE INTO review_queue(dispatch_id,operator,trip,service_day,reason,"
            "detail,created_at,dedupe_key) VALUES (?,?,?,?,?,?,?,?)",
            (dispatch_id, operator, trip, service_day, reason, detail, _utc_now(),
             self._dedupe_key(dispatch_id, operator, trip, service_day, reason)),
        )

    def open_review(self, dispatch_id, operator, trip, service_day, reason, detail) -> bool:
        """打开一条复核；去重键已存在时返回 False，不重复建条。"""
        dedupe_key = self._dedupe_key(dispatch_id, operator, trip, service_day, reason)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM review_queue WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            if existing is not None:
                return False
            self._insert_review(connection, dispatch_id, operator, trip, service_day, reason, detail)
            return True

    def resolve_review(self, review_id: int, resolution: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE review_queue SET status=?, resolved_at=?, resolution=? WHERE id=? AND status=?",
                (REVIEW_RESOLVED, _utc_now(), resolution, review_id, REVIEW_OPEN),
            )
            if cursor.rowcount == 0:
                raise KeyError(review_id)

    def list_reviews(self, status: str | None = None, dispatch_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if dispatch_id is not None:
            clauses.append("dispatch_id=?")
            params.append(dispatch_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM review_queue" + where + " ORDER BY id", params
        ).fetchall()]

    def list_ambiguous_dispatches(self) -> list[dict[str, Any]]:
        """明确列出仍有时间歧义的已派班次（含未解决的不存在/无效时刻）。"""
        rows = self.connection.execute(
            "SELECT rq.* FROM review_queue rq WHERE rq.status=? AND rq.reason IN (?, ?, ?)"
            " ORDER BY rq.id",
            (REVIEW_OPEN, REVIEW_AMBIGUOUS_DISPATCH,
             REVIEW_UNRESOLVABLE_DISPATCH, REVIEW_CALENDAR_CHANGE),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- 日历更新影响分析 -----------------------------------------------------
    def diff_bundles(self, old_bundle_id: int, new_bundle_id: int) -> dict[str, list[dict[str, Any]]]:
        """比较两个 bundle，返回受影响班次（取消、新增、时刻/解释变化）。"""
        old_rows = {
            (row["trip"], row["service_day"]): row for row in self._instances_of(old_bundle_id)
        }
        new_rows = {
            (row["trip"], row["service_day"]): row for row in self._instances_of(new_bundle_id)
        }
        removed, added, changed = [], [], []
        for identity in sorted(set(old_rows) | set(new_rows)):
            old, new = old_rows.get(identity), new_rows.get(identity)
            if new is None:
                removed.append({"trip": identity[0], "service_day": identity[1], "before": old, "after": None})
            elif old is None:
                added.append({"trip": identity[0], "service_day": identity[1], "before": None, "after": new})
            elif self._material_signature(old) != self._material_signature(new):
                changed.append({"trip": identity[0], "service_day": identity[1], "before": old, "after": new})
        return {"removed": removed, "added": added, "changed": changed}

    @staticmethod
    def _material_signature(instance: dict[str, Any]) -> tuple:
        interp = instance.get("interpretation")
        candidate_sig = None
        if interp:
            candidate_sig = tuple(
                (candidate["fold"], candidate["utc_iso"]) for candidate in interp.get("candidates", [])
            )
        return (
            bool(instance["operates"]),
            instance["departure"],
            instance["zone"],
            instance.get("interp_status"),
            candidate_sig,
        )

    def open_reviews_for_calendar_change(self, old_bundle_id: int, new_bundle_id: int) -> list[dict[str, Any]]:
        """对受影响且已有派班的班次开复核；没有派班的只在差异报告里体现。"""
        diff = self.diff_bundles(old_bundle_id, new_bundle_id)
        opened = []
        with self.transaction() as connection:
            for group in ("removed", "added", "changed"):
                for item in diff[group]:
                    trip, service_day = item["trip"], item["service_day"]
                    after = item["after"] or item["before"]
                    dispatch_rows = connection.execute(
                        "SELECT dispatch_id FROM dispatches WHERE operator=? AND trip=? AND service_day=?",
                        (after["operator"], trip, service_day),
                    ).fetchall()
                    for dispatch_row in dispatch_rows:
                        dispatch_id = dispatch_row["dispatch_id"]
                        detail = (
                            f"日历升级到 bundle {new_bundle_id} 后该已派班次属于 {group}："
                            f"运营状态/时刻/时区解释发生变化，需调度复核；系统未自动取消派班"
                        )
                        self._insert_review(
                            connection, dispatch_id, after["operator"], trip, service_day,
                            REVIEW_CALENDAR_CHANGE, detail,
                        )
                        opened.append({"dispatch_id": dispatch_id, "trip": trip,
                                       "service_day": service_day, "group": group})
        return opened

    # -- 摄入运行状态与水位（失败恢复/补抓） ----------------------------------
    def start_run(self, source: str) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO ingestion_runs(source,state,started_at) VALUES (?,?,?)",
                (source, RUN_RUNNING, _utc_now()),
            )
            return cursor.lastrowid

    def finish_run(self, run_id: int, detail: dict[str, Any] | None = None, state: str = RUN_DONE) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE ingestion_runs SET state=?, finished_at=?, detail=? WHERE id=?",
                (state, _utc_now(), json.dumps(detail or {}, ensure_ascii=False), run_id),
            )

    def fail_run(self, run_id: int, error: str) -> None:
        self.finish_run(run_id, {"error": error}, state=RUN_FAILED)

    def running_runs(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ingestion_runs WHERE state=? ORDER BY id", (RUN_RUNNING,)
        ).fetchall()]

    def list_runs(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ingestion_runs ORDER BY id"
        ).fetchall()]

    def get_watermark(self, source: str, path: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM source_watermarks WHERE source=? AND path=?", (source, path)
        ).fetchone()
        return None if row is None else dict(row)

    def set_watermark(self, source: str, path: str, etag: str | None, capture_id: int | None) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO source_watermarks(source,path,etag,last_capture_id,updated_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(source,path) DO UPDATE SET"
                " etag=excluded.etag, last_capture_id=excluded.last_capture_id, updated_at=excluded.updated_at",
                (source, path, etag, capture_id, _utc_now()),
            )

    # -- 隔离区：无法装配/解释的原始记录不丢弃 --------------------------------
    def quarantine(self, source: str, reason: str, raw: Any) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO quarantine(source,reason,raw_json,observed_at) VALUES (?,?,?,?)",
                (source, reason, json.dumps(raw, ensure_ascii=False, default=str), _utc_now()),
            )

    def list_quarantine(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM quarantine ORDER BY id"
        ).fetchall()]
