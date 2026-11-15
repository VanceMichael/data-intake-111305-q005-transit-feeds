"""摄入流水线：从已归档的原始响应到有效业务版本。

一次摄入在单个运行记录内完成，保证失败可恢复：

1. 部件按内容去重追加，重复补抓不产生新版本；
2. 单到部件保持 pending，只有时刻表+日历齐备才装配 bundle；
3. 解释结果按依据哈希跨版本缓存；
4. 每个运营方仅提升一组有效 bundle，业务视图始终相容；
5. 升级时比较新旧 bundle，对受影响的 *已派* 班次开复核，不自动取消；
6. 无法装配或解释的记录进隔离区，不静默丢弃。
"""
from __future__ import annotations

import json
from typing import Any

from .catalog import (
    Bundle,
    apply_calendar,
    assemble_bundles,
    normalize_part,
)
from .repository import ServiceStore
from .serviceday import cache_key_for, interpret_departure


def classify_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """兼容裸班次记录：无 part 字段的记录包成 timetable 部件，日历记录保持原样。"""
    normalized = []
    for record in records:
        if "part" in record:
            normalized.append(record)
            continue
        if not {"operator", "trip", "service_day", "departure", "zone"} <= set(record):
            raise ValueError("裸班次记录缺少 operator/trip/service_day/departure/zone 字段")
        normalized.append({
            "part": "timetable",
            "operator": record["operator"],
            "calendar_version": record.get("calendar_version"),
            "trips": [record],
        })
    return normalized


def _interpret_with_cache(store: ServiceStore, instance) -> dict[str, Any]:
    interpretation = interpret_departure(
        service_day=instance.service_day,
        departure=instance.departure,
        zone_name=instance.zone,
        calendar_version=instance.calendar_version,
    )
    key = cache_key_for(interpretation)
    cached = store.get_interpretation(key)
    if cached is not None:
        return cached, key, False
    stored_key, inserted = store.store_interpretation(interpretation)
    return interpretation.to_dict(), stored_key, inserted


def ingest_records(store: ServiceStore, source: str,
                   records: list[dict[str, Any]], capture_id: int | None = None) -> dict[str, Any]:
    """把一次到达的来源记录摄入服务日库；返回结构化摄入报告。"""
    run_id = store.start_run(source)
    summary: dict[str, Any] = {
        "run_id": run_id,
        "parts_seen": 0,
        "parts_new": 0,
        "quarantined": 0,
        "bundles_assembled": 0,
        "promotions": [],
        "reviews_opened": 0,
        "pending_operators": [],
        "cache_hits": 0,
        "cache_misses": 0,
    }
    try:
        prepared = classify_records(records)
        observed_at = store.connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now') AS now"
        ).fetchone()["now"]

        for raw in prepared:
            summary["parts_seen"] += 1
            try:
                part = normalize_part(raw, observed_at=observed_at, capture_id=capture_id)
            except Exception as error:  # noqa: BLE001 - 隔离坏数据而非中断整批
                store.quarantine(source, f"部件规范化失败: {error}", raw)
                summary["quarantined"] += 1
                continue
            _, is_new = store.add_part(part)
            summary["parts_new"] += int(is_new)

        all_parts = _reload_parts(store)
        bundles, pending = assemble_bundles(all_parts)
        summary["pending_operators"] = sorted({
            f"{part.operator}:{part.calendar_version or '无版本'}" for part in pending
        })

        # 每个运营方只提升本批次中 id 最大（部件最新到达）的新 bundle，
        # 其余新 bundle 已实例化留档，可追溯但不进入有效业务视图。
        recorded: list[tuple[Bundle, int]] = []
        for bundle in bundles:
            bundle_id, is_new_bundle = store.record_bundle(bundle)
            if not is_new_bundle:
                continue
            summary["bundles_assembled"] += 1
            instances, warnings = apply_calendar(bundle)
            materialized = []
            for instance in instances:
                item = {
                    "operator": instance.operator,
                    "trip": instance.trip,
                    "service_day": instance.service_day,
                    "operates": instance.operates,
                    "action": instance.action,
                    "departure": instance.departure,
                    "zone": instance.zone,
                    "source": instance.source,
                    "warnings": list(instance.warnings) + list(warnings),
                }
                if not instance.operates or not instance.departure or not instance.zone:
                    item.update(interpretation=None, interp_cache_key=None, interp_status=None)
                else:
                    interpretation, cache_key, inserted = _interpret_with_cache(store, instance)
                    item.update(
                        interpretation=interpretation,
                        interp_cache_key=cache_key,
                        interp_status=interpretation["status"],
                    )
                    summary["cache_hits"] += 0 if inserted else 1
                    summary["cache_misses"] += 1 if inserted else 0
                materialized.append(item)
            store.replace_instances(bundle_id, materialized)
            recorded.append((bundle, bundle_id))

        latest_new_by_operator: dict[str, int] = {}
        for bundle, bundle_id in recorded:
            if bundle_id > latest_new_by_operator.get(bundle.operator, -1):
                latest_new_by_operator[bundle.operator] = bundle_id

        for bundle, bundle_id in recorded:
            if latest_new_by_operator.get(bundle.operator) != bundle_id:
                continue
            previous = store.effective_bundle(bundle.operator)
            store.promote_bundle(bundle_id)
            promotion = {
                "operator": bundle.operator,
                "calendar_version": bundle.calendar_version,
                "bundle_id": bundle_id,
            }
            if previous is not None:
                opened = store.open_reviews_for_calendar_change(previous["id"], bundle_id)
                promotion["replaced_bundle_id"] = previous["id"]
                promotion["reviews_opened"] = opened
                summary["reviews_opened"] += len(opened)
            summary["promotions"].append(promotion)

        store.finish_run(run_id, summary)
    except Exception as error:
        store.fail_run(run_id, repr(error))
        raise
    return summary


def _reload_parts(store: ServiceStore):
    parts = []
    for row in store.list_parts():
        raw = json.loads(row["source_text"])
        parts.append(normalize_part(raw, observed_at=row["observed_at"], capture_id=row["capture_id"]))
    return parts
