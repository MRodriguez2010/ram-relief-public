#!/usr/bin/env python3
"""Disposable anonymous-memory fixture controlled through a local Unix socket."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
from pathlib import Path


def checksum(data: bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--mib", type=int, default=256)
    args = parser.parse_args()
    socket_path = Path(args.socket)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    data = bytearray(args.mib * 1024 * 1024)
    for offset in range(0, len(data), 4096):
        data[offset] = (offset // 4096) % 251
    expected = checksum(data)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(1)
    running = True
    while running:
        connection, _address = server.accept()
        with connection:
            command = connection.recv(1024).decode("utf-8").strip()
            if command == "status":
                response = {"pid": os.getpid(), "bytes": len(data), "checksum": checksum(data), "expected": expected}
            elif command == "touch":
                touched = sum(data[offset] for offset in range(0, len(data), 4096))
                response = {"pid": os.getpid(), "checksum": checksum(data), "expected": expected, "page_sum": touched}
            elif command == "exit":
                response = {"pid": os.getpid(), "checksum": checksum(data), "expected": expected}
                running = False
            else:
                response = {"error": "unknown command"}
            connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
    server.close()
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
