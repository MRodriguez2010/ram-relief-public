#!/usr/bin/env python3
"""Live disposable cgroup reclaim verification; never targets a real app."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT / "tests" / "memory_fixture.py"
UID = os.getuid()
UNIT_BASE = f"ram-relief-fixture-{os.getpid()}"
UNIT = f"{UNIT_BASE}.scope"
SOCKET = Path(f"/run/user/{UID}/{UNIT_BASE}.sock")
CGROUP = Path(f"/sys/fs/cgroup/user.slice/user-{UID}.slice/user@{UID}.service/app.slice/{UNIT}")
MIB = 1024 * 1024


def request(command: str) -> dict[str, object]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(SOCKET))
    client.sendall((command + "\n").encode("utf-8"))
    chunks: list[bytes] = []
    while True:
        chunk = client.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    client.close()
    return json.loads(b"".join(chunks).decode("utf-8"))


def read_int(name: str) -> int:
    return int((CGROUP / name).read_text(encoding="utf-8").strip())


def main() -> int:
    command = [
        "systemd-run",
        "--user",
        "--scope",
        f"--unit={UNIT_BASE}",
        "--slice=app.slice",
        "--collect",
        "--quiet",
        sys.executable,
        str(FIXTURE),
        "--socket",
        str(SOCKET),
        "--mib",
        "256",
    ]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (not SOCKET.exists() or not CGROUP.exists()):
            time.sleep(0.1)
        if not SOCKET.exists() or not CGROUP.exists():
            stderr = process.stderr.read() if process.poll() is not None and process.stderr else ""
            raise RuntimeError(f"fixture scope did not start: {stderr}")
        status_before = request("status")
        memory_before = read_int("memory.current")
        swap_before = read_int("memory.swap.current")
        reclaim = CGROUP / "memory.reclaim"
        if not os.access(reclaim, os.W_OK):
            raise RuntimeError("fixture memory.reclaim is not user-writable")
        try:
            reclaim.write_text(f"{128 * MIB} swappiness=max\n", encoding="utf-8")
            reclaim_error = None
        except OSError as error:
            reclaim_error = f"{error.__class__.__name__}: {error}"
        time.sleep(0.5)
        memory_after = read_int("memory.current")
        swap_after = read_int("memory.swap.current")
        status_after = request("status")
        restored = request("touch")
        memory_restored = read_int("memory.current")
        swap_restored = read_int("memory.swap.current")
        exited = request("exit")
        process.wait(timeout=10)
        report = {
            "unit": UNIT,
            "reclaim_error": reclaim_error,
            "memory_before": memory_before,
            "memory_after": memory_after,
            "memory_restored": memory_restored,
            "swap_before": swap_before,
            "swap_after": swap_after,
            "swap_restored": swap_restored,
            "process_alive_after_reclaim": status_before["pid"] == status_after["pid"] == restored["pid"],
            "checksum_before": status_before["checksum"],
            "checksum_after": status_after["checksum"],
            "checksum_restored": restored["checksum"],
            "checksum_expected": restored["expected"],
            "checksum_on_exit": exited["checksum"],
        }
        report["checksum_verified"] = len(
            {
                report["checksum_before"],
                report["checksum_after"],
                report["checksum_restored"],
                report["checksum_expected"],
                report["checksum_on_exit"],
            }
        ) == 1
        print(json.dumps(report, indent=2, sort_keys=True))
        if not report["process_alive_after_reclaim"] or not report["checksum_verified"]:
            return 1
        return 0
    finally:
        if process.poll() is None and SOCKET.exists():
            try:
                request("exit")
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            SOCKET.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
