#!/usr/bin/env python3
"""RAM Relief v2 command line and native application launcher."""

from __future__ import annotations

import argparse
import json
import sys

from ram_relief_cleanup import run_cleanup, scan_json
from ram_relief_core import (
    POLICY_LABELS,
    SettingsStore,
    append_aggregate_history,
    assess_cgroups,
    format_bytes,
    run_memory_relief,
    status_document,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ram-relief",
        description="Transparent, pressure-aware memory and storage care for Ubuntu.",
    )
    parser.add_argument("--status", action="store_true", help="show current pressure and safety capability")
    parser.add_argument("--dry-run", action="store_true", help="preview Optimize without reclaiming or deleting")
    parser.add_argument("--configure", action="store_true", help="open Settings")
    parser.add_argument("--list-candidates", action="store_true", help="list application-scope park eligibility")
    parser.add_argument("--auto-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scan-json", action="store_true", help="print aggregate cleanup scan JSON without filenames")
    parser.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--memory", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cleanup", action="store_true", help=argparse.SUPPRESS)
    return parser


def cli_status(settings: dict[str, object]) -> int:
    document = status_document(settings)
    pressure = document["pressure"]
    reclaim = document["memory_reclaim"]
    print("RAM Relief — Memory & Storage Care")
    print(f"Setup complete: {'yes' if document['setup_complete'] else 'no'}")
    print(f"Memory mode: {document['memory_mode']}")
    print(
        "Available RAM: "
        f"{format_bytes(int(pressure['available_bytes']))} "
        f"({float(pressure['available_percent']):.1f}%)"
    )
    print(f"Memory PSI some avg10: {float(pressure['psi_some_avg10']):.2f}%")
    print(f"Swap free: {format_bytes(int(pressure['swap_free_bytes']))}")
    print(f"Pressure trigger: {'active' if pressure['triggered'] else 'comfortable'}")
    print(f"Transparent reclaim: {'available' if reclaim['available'] else 'unavailable'} — {reclaim['reason']}")
    print("Cleanup categories: " + (", ".join(document["cleanup_enabled"]) or "none"))
    return 0


def cli_candidates(settings: dict[str, object]) -> int:
    _apps, decisions, _activity = assess_cgroups(settings, automatic=True, short_sample_seconds=0)
    if not decisions:
        print("No graphical application scopes are currently visible.")
        return 0
    for item in decisions:
        policy = POLICY_LABELS.get(item.policy, item.policy)
        app_id = item.app_id or "unknown"
        print(
            f"{item.label:<24} {policy:<16} {item.status:<18} "
            f"{format_bytes(item.memory_current):>10}  {item.reason} [{app_id}]"
        )
    return 0


def _print_memory_result(result: object) -> None:
    data = result.to_aggregate_dict()
    print(f"Memory pressure: {'triggered' if data['pressure_triggered'] else 'comfortable'}")
    if data["error"]:
        print(f"Memory: safe no-op — {data['error']}")
    elif data["stopped_reason"] and not data["parked"]:
        print(f"Memory: {data['stopped_reason']}")
    for item in data["parked"]:
        if data["dry_run"]:
            print(f"Would request anonymous reclaim from {item['label']}: {format_bytes(item['requested_bytes'])}")
        else:
            print(
                f"{item['label']}: requested {format_bytes(item['requested_bytes'])}; "
                f"measured cgroup reduction {format_bytes(item['memory_reduction_bytes'])}; "
                f"cgroup swap growth {format_bytes(item['cgroup_swap_growth_bytes'])}"
            )
    if not data["dry_run"]:
        available_change = int(data["available_change_bytes"])
        swap_growth = int(data["swap_growth_bytes"])
        print(f"Available-memory change: {available_change / (1024 * 1024):+.1f} MiB")
        print(f"System swap change: {swap_growth / (1024 * 1024):+.1f} MiB")
    if data["skipped"]:
        print(f"Skipped application scopes: {len(data['skipped'])}")


def cli_dry_run(settings: dict[str, object]) -> int:
    if not settings.get("setup_complete"):
        print("Initial setup is not complete. Open RAM Relief and review the migration setup first.")
        return 0
    memory = run_memory_relief(settings, automatic=False, dry_run=True)
    cleanup = run_cleanup(settings, dry_run=True)
    _print_memory_result(memory)
    print(
        f"Cleanup preview: {cleanup.scan.total_count} files, "
        f"{format_bytes(cleanup.scan.total_bytes)} across approved roots"
    )
    for category, count in sorted(cleanup.scan.category_counts.items()):
        print(f"  {category}: {count} files, {format_bytes(cleanup.scan.category_bytes.get(category, 0))}")
    print("Dry run only — no page reclaim or deletion was performed.")
    return 0


def cli_auto_run(settings: dict[str, object]) -> int:
    result = run_memory_relief(settings, automatic=True)
    aggregate = {"kind": "memory", **result.to_aggregate_dict()}
    append_aggregate_history(aggregate)
    print(json.dumps(aggregate, sort_keys=True))
    return 0


def run_gui(argv: list[str]) -> int:
    from ram_relief_ui import RamReliefApplication

    return RamReliefApplication().run(argv)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv if argv is None else argv)
    arguments = _parser().parse_args(raw[1:])
    store = SettingsStore()
    settings = store.load()
    if arguments.status:
        return cli_status(settings)
    if arguments.list_candidates:
        return cli_candidates(settings)
    if arguments.scan_json:
        print(scan_json(settings))
        return 0
    if arguments.dry_run:
        return cli_dry_run(settings)
    if arguments.auto_run:
        return cli_auto_run(settings)
    try:
        return run_gui(raw)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
