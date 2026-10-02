"""Trusted local process controls; no network, provider or package implementation."""

import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path


def heartbeat(path):
    while True:
        Path(path).write_text(str(time.monotonic()))
        time.sleep(0.02)


def main():
    mode, directory = sys.argv[1:3]
    root = Path(directory)
    if mode == "heartbeat":
        heartbeat(root)
        return
    if mode == "child_worker":
        grandchild = subprocess.Popen(
            [sys.executable, __file__, "heartbeat", str(root / "grandchild")]
        )
        (root / "grandchild.pid").write_text(str(grandchild.pid))
        heartbeat(root / "child")
        return
    with (root / "starts").open("a") as handle:
        handle.write(str(os.getpid()) + "\n")
    if mode == "no_read":
        time.sleep(10)
        return
    envelope = json.load(sys.stdin)
    (root / f"envelope-{envelope['case_id']}.json").write_text(json.dumps(envelope))
    if mode in ("tree", "exit_with_tree", "closed_tree"):
        options = (
            {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if (mode == "closed_tree")
            else {}
        )
        child = subprocess.Popen([sys.executable, __file__, "child_worker", str(root)], **options)
        (root / "owned.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "pgid": os.getpgrp(),
                    "child_pid": child.pid,
                }
            )
        )
        if mode == "tree":
            heartbeat(root / "parent")
        else:
            deadline = time.monotonic() + 2
            while not (root / "grandchild").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if mode == "exit_with_tree":
                print("{}", flush=True)
                return
    if mode == "echo":
        print(json.dumps(envelope))
        return
    if mode == "nonzero":
        print('{"not":"an answer"}')
        print("adapter diagnostic", file=sys.stderr)
        sys.exit(7)
    if mode == "empty":
        return
    if mode == "logs":
        print("not JSON: some logs")
        return
    if mode == "usage_extra":
        print('{"usage":{"tokens":99}}')
        return
    contract = envelope["output_protocol"]["contract"]
    source = contract["sources"]["target"]
    entry = next(item for item in envelope["inputs"] if item["file_id"] == source["file_id"])
    path = Path(entry["relative_path"])
    assert path.is_absolute(), "runtime input paths must be absolute"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
    rows = list(csv.reader(raw.decode("utf-8").splitlines()))
    field = contract["metric"]["field"]
    column = rows[source["header_row"] - 1].index(field)
    value = sum((Decimal(row[column]) for row in rows[1:]), Decimal(0))
    result = {
        "schema_version": "1",
        "case_id": envelope["case_id"],
        "metric_id": envelope["metric_id"],
        "status": "VALUE",
        "value": f"{value:.2f}",
        "unit": "CNY",
        "reason": None,
        "bindings": [
            {
                "source": "target",
                "file_id": source["file_id"],
                "sheet": source["sheet"],
                "header_row": source["header_row"],
                "rows": source["rows"],
                "fields": [field],
            }
        ],
    }
    output = json.dumps(result).encode()
    if mode == "flood_stdout":
        os.write(sys.stdout.fileno(), output)
        while True:
            os.write(sys.stdout.fileno(), b" " * 8192)
    if mode == "flood_stderr":
        os.write(sys.stdout.fileno(), output)
        while True:
            os.write(sys.stderr.fileno(), b"x" * 8192)
    if mode == "invalid_utf8":
        os.write(sys.stdout.fileno(), b'"\xff"')
        return
    if mode == "invalid_stderr":
        os.write(sys.stderr.fileno(), b'"\xff"')
    if mode == "exact_stdout":
        output += b" " * (1024 * 1024 - len(output))
    if mode == "exact_stderr":
        os.write(sys.stderr.fileno(), b"x" * (64 * 1024))
    os.write(sys.stdout.fileno(), output)


if __name__ == "__main__":
    main()
