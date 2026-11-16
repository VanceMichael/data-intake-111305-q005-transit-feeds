import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .decoding import records
from .origin import serve


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


def sync(base_url, runtime, source, timetable, calendar, attempts):
    from .intake import Intake
    with Intake(base_url, Path(runtime) / "raw.sqlite3", Path(runtime) / "state.sqlite3",
                source, attempts=attempts) as intake:
        return intake.sync(timetable, calendar)


def demo_flow():
    """End-to-end local example of the service-day workflow."""
    from .intake import Intake
    with tempfile.TemporaryDirectory() as temporary:
        runtime = Path(temporary)
        report = {}
        with serve() as origin:
            with Intake(origin.base_url, runtime / "raw.sqlite3", runtime / "state.sqlite3", "demo") as intake:
                report["first_sync"] = _brief(intake.sync())
                for name, operator, trip, stop, day in (
                    ("SHIFT-N12", "border-west", "N12", "central", "2026-10-31"),
                    ("SHIFT-N13", "border-west", "N13", "central", "2026-10-25"),
                    ("SHIFT-C1", "channel", "C1", "terminal", "2026-10-25"),
                    ("SHIFT-S9", "south", "S9", "quay", "2026-10-04"),
                    ("SHIFT-S7", "border-west", "S7", "harbour", "2026-10-26"),
                    ("SHIFT-D30", "border-west", "D30", "central", "2026-10-31"),
                ):
                    intake.store.assign_shift(name, operator, trip, stop, day)
                report["ambiguous_shifts"] = intake.store.ambiguous_shifts()
                report["second_sync"] = _brief(intake.sync())
                report["review_queue"] = intake.store.review_queue()
                report["trace_D30"] = _brief(intake.store.shift_trace("SHIFT-D30"), drop=("transitions", "rules"))
                report["ambiguous_after_update"] = intake.store.ambiguous_shifts()
        return report


def _brief(value, drop=("interpretation", "transitions", "record", "rules", "detail")):
    if isinstance(value, dict):
        return {key: _brief(item, drop) for key, item in value.items() if key not in drop}
    if isinstance(value, list):
        return [_brief(item, drop) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description="来源响应接收、服务日解释与派班复核")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)

    sync_parser = subcommands.add_parser("sync", help="抓取时刻表与日历两半并发布相容版本")
    sync_parser.add_argument("--base-url", required=True)
    sync_parser.add_argument("--runtime", type=Path, required=True)
    sync_parser.add_argument("--source", default="local-source")
    sync_parser.add_argument("--timetable", default="/timetable")
    sync_parser.add_argument("--calendar", default="/calendar")
    sync_parser.add_argument("--attempts", type=int, default=3)

    assign = subcommands.add_parser("assign", help="把派班记录绑定到当前版本并保留解释依据")
    assign.add_argument("--runtime", type=Path, required=True)
    assign.add_argument("--id", required=True)
    assign.add_argument("--operator", required=True)
    assign.add_argument("--trip", required=True)
    assign.add_argument("--stop", default="")
    assign.add_argument("--service-day", required=True)

    trace = subcommands.add_parser("trace", help="从派班记录查回来源时刻及解释依据")
    trace.add_argument("--runtime", type=Path, required=True)
    trace.add_argument("--id", required=True)

    review = subcommands.add_parser("review", help="查看待调度复核的班次及仍有歧义的班次")
    review.add_argument("--runtime", type=Path, required=True)
    acknowledge = subcommands.add_parser("acknowledge", help="标记复核项已由调度处理（不更改派班）")
    acknowledge.add_argument("--runtime", type=Path, required=True)
    acknowledge.add_argument("--item", type=int, required=True)

    pending = subcommands.add_parser("pending", help="查看尚未配成相容版本的半份文档")
    pending.add_argument("--runtime", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    elif args.command == "sync":
        print(json.dumps(sync(args.base_url, args.runtime, args.source, args.timetable, args.calendar, args.attempts),
                         ensure_ascii=False, indent=2))
    elif args.command in ("assign", "trace", "review", "acknowledge", "pending"):
        from .publishing import Store
        store = Store(Path(args.runtime) / "state.sqlite3")
        try:
            if args.command == "assign":
                result = store.assign_shift(args.id, args.operator, args.trip, args.stop, args.service_day)
            elif args.command == "trace":
                result = store.shift_trace(args.id)
            elif args.command == "review":
                result = {"review_items": store.review_queue(), "ambiguous_shifts": store.ambiguous_shifts()}
            elif args.command == "acknowledge":
                store.acknowledge(args.item)
                result = {"acknowledged": args.item}
            else:
                result = store.pending_halves()
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
