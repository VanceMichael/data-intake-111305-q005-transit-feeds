import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .decoding import records
from .ingestion import ingest_records
from .origin import load_scenario, serve
from .repository import REVIEW_OPEN, ServiceStore
from .serviceday import interpret_departure


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def _load_records(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "items" in payload:
        payload = payload["items"]
    if not isinstance(payload, list):
        raise SystemExit("输入文件必须是记录数组或带 items 的对象")
    return payload


def run_interpret(records_path: Path) -> dict:
    payload = _load_records(records_path)
    results = []
    for record in payload:
        results.append(interpret_departure(
            service_day=record.get("service_day"),
            departure=record.get("departure"),
            zone_name=record.get("zone"),
            calendar_version=record.get("calendar_version"),
        ).to_dict())
    ambiguous = [item for item in results if item["status"] != "definite"]
    return {"interpreted": len(results), "needs_attention": ambiguous, "results": results}


def run_ingest(store_path: Path, source: str, records_path: Path | None,
               base_url: str | None, archive_path: Path | None, feed_path: str) -> dict:
    capture_id = None
    if base_url is not None:
        if archive_path is None:
            raise SystemExit("从来源摄入时必须同时给出 --archive 原始归档库")
        archive = Archive(archive_path)
        try:
            capture_id, response = collect(Client(base_url), archive, source, feed_path)
            if response.status != 200:
                raise SystemExit(f"来源返回非成功状态 {response.status}，本次不摄入（保留原始归档）")
            payload = records(response.body, response.headers.get("content-type", ""))
        finally:
            archive.close()
    else:
        if records_path is None:
            raise SystemExit("需要 --records 文件或 --base-url 来源地址")
        payload = _load_records(records_path)

    store = ServiceStore(store_path)
    try:
        if base_url is not None:
            store.set_watermark(source, feed_path, None, capture_id)
        summary = ingest_records(store, source, payload, capture_id=capture_id)
        summary["ambiguous_reviews_open"] = len(store.list_reviews(status=REVIEW_OPEN))
        return summary
    finally:
        store.close()


def run_trace(store_path: Path, dispatch_id: str) -> dict:
    store = ServiceStore(store_path)
    try:
        return store.trace_dispatch(dispatch_id)
    finally:
        store.close()


def run_reviews(store_path: Path) -> dict:
    store = ServiceStore(store_path)
    try:
        open_reviews = store.list_reviews(status=REVIEW_OPEN)
        return {"open": len(open_reviews), "items": open_reviews}
    finally:
        store.close()


def _scenario_path(name: str) -> Path:
    # 兼容源码布局（顶层 fixtures/）与 wheel 安装布局（包内 transit_feeds/fixtures/）
    candidates = (
        Path(__file__).parent / "fixtures" / name,
        Path(__file__).parents[1] / "fixtures" / name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"找不到场景文件 {name}，已查找: {[str(c) for c in candidates]}")


def run_demo() -> dict:
    scenario = load_scenario(_scenario_path("winter_switch.json"))
    with tempfile.TemporaryDirectory() as temporary, serve(scenario) as origin:
        temporary = Path(temporary)
        archive = Archive(temporary / "raw.sqlite3")
        store = ServiceStore(temporary / "service.sqlite3")
        try:
            # 第一次到达：只有时刻表，业务尚看不到有效版本
            tt_capture_id, tt_response = collect(Client(origin.base_url), archive, "local-source", "/feed")
            tt_records = records(tt_response.body, tt_response.headers["content-type"])
            first = ingest_records(store, "local-source", tt_records, capture_id=tt_capture_id)

            # 第二次到达：例外日历，装配成一组相容版本后才提升为有效
            cal_capture_id, cal_response = collect(Client(origin.base_url), archive, "local-source", "/calendar")
            cal_records = records(cal_response.body, cal_response.headers["content-type"])
            second = ingest_records(store, "local-source", cal_records, capture_id=cal_capture_id)

            # 对有效版本中每个运营班次派班，冻结当时解释
            for instance in store.effective_instances("border-west"):
                if instance["operates"]:
                    store.record_dispatch(
                        f"DISP-{instance['trip']}", instance, part_capture_id=tt_capture_id
                    )

            rendered = []
            for item in store.effective_instances("border-west"):
                interpretation = item["interpretation"]
                rendered.append({
                    "trip": item["trip"],
                    "service_day": item["service_day"],
                    "operates": item["operates"],
                    "source_departure": item["departure"],
                    "status": item["interp_status"],
                    "wall_local": interpretation["wall_local_iso"] if interpretation else None,
                    "candidates": interpretation["candidates"] if interpretation else [],
                    "gap_bracket": interpretation["gap_bracket"] if interpretation else None,
                    "zone_rule": interpretation["zone_rule"] if interpretation else None,
                })
            return {
                "operation": "service_day_demo",
                "raw_captures": {"timetable": tt_capture_id, "calendar": cal_capture_id},
                "first_arrival_timetable_only": first,
                "second_arrival_calendar": second,
                "effective_instances": rendered,
                "open_time_reviews": store.list_ambiguous_dispatches(),
                "trace_n12": store.trace_dispatch("DISP-N12"),
            }
        finally:
            archive.close()
            store.close()


def main():
    parser = argparse.ArgumentParser(description="来源响应接收、归档与服务日解释")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)

    interpreter = subcommands.add_parser("interpret", help="对记录文件做服务日解释（不落库）")
    interpreter.add_argument("--records", type=Path, required=True)

    ingester = subcommands.add_parser("ingest", help="摄入来源记录并装配有效业务版本")
    ingester.add_argument("--db", type=Path, required=True)
    ingester.add_argument("--source", required=True)
    ingester.add_argument("--records", type=Path)
    ingester.add_argument("--base-url")
    ingester.add_argument("--archive", type=Path)
    ingester.add_argument("--path", default="/feed")

    tracer = subcommands.add_parser("trace", help="从派班记录查回来源时刻与解释依据")
    tracer.add_argument("--db", type=Path, required=True)
    tracer.add_argument("--dispatch-id", required=True)

    reviewer = subcommands.add_parser("reviews", help="列出仍待调度处理的歧义/复核班次")
    reviewer.add_argument("--db", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "demo":
        print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path),
                         ensure_ascii=False, indent=2))
    elif args.command == "serve":
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
    elif args.command == "interpret":
        print(json.dumps(run_interpret(args.records), ensure_ascii=False, indent=2))
    elif args.command == "ingest":
        print(json.dumps(run_ingest(args.db, args.source, args.records,
                                    args.base_url, args.archive, args.path),
                         ensure_ascii=False, indent=2))
    elif args.command == "trace":
        print(json.dumps(run_trace(args.db, args.dispatch_id), ensure_ascii=False, indent=2))
    elif args.command == "reviews":
        print(json.dumps(run_reviews(args.db), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
