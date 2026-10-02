"""Explicit trusted adapter execution; bounded observations, not an adapter sandbox."""

from __future__ import annotations

import base64
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import models as m
from .artifacts import canonical_json, preflight_suite
from .contracts import strict_json_loads, to_document, validate_schema
from .loaders import _contained_path

MAX_STDOUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024


def make_task_envelope(case: m.FrozenCase) -> m.TaskEnvelope:
    """Build a gold-free task; logical inputs acquire physical paths only at run time.

    The immutable stable task is retained verbatim. Machine selectors and unit labels
    are additional protocol metadata, independent of confirmation and frozen hashes.
    """
    validated = case.validated
    return m.TaskEnvelope(
        "1",
        validated.manifest.case_id,
        validated.spec.metric_id,
        validated.task,
        validated.manifest.inputs,
        {
            "format": "one strict UTF-8 JSON AgentResult on stdout; no logs or extra fields",
            "result_schema": strict_json_loads(
                files("sheetbenchkit").joinpath("schemas", "result.json").read_bytes()
            ),
            "contract": to_document(validated.spec),
            "unit_labels": to_document(validated.manifest.unit_labels),
        },
        tuple(validated.spec.sources),
    )


def _bind_inputs(envelope: m.TaskEnvelope, ref: m.CaseRef, root: Path) -> m.TaskEnvelope:
    case_root = _contained_path(root, ref.contract_path).parent
    entries = []
    for entry in envelope.inputs:
        absolute = _contained_path(case_root, entry.relative_path).resolve()
        if not absolute.is_relative_to(root):
            raise m.CaseError("PATH_ESCAPE", entry.relative_path)
        entries.append(replace(entry, relative_path=str(absolute)))
    return replace(envelope, inputs=tuple(entries))


def _kill_owned_group(process: subprocess.Popen[bytes]) -> None:
    # start_new_session guarantees pid==pgid. Never signal the caller's group or
    # enumerate unrelated processes; escaped sessions are outside this control.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _text(raw: bytes, stream: str, metadata: dict[str, Any]) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        metadata[stream + "_encoding_error"] = True
        metadata["raw_" + stream + "_base64"] = base64.b64encode(raw).decode("ascii")
        metadata[stream + "_representation"] = "ASCII bytes repr; exact captured bytes in base64"
        # bytes repr begins b' or b", so stdout can never accidentally grade as JSON.
        return repr(raw)


def _execute(
    envelope: m.TaskEnvelope,
    adapter_argv: tuple[str, ...],
    timeout_s: int,
    attempt: int,
    config_id: str,
) -> m.Observation:
    payload = canonical_json(envelope) + b"\n"
    started_at = time.monotonic()
    metadata: dict[str, Any] = {
        "adapter_argv": adapter_argv,
        "input_paths": {entry.file_id: entry.relative_path for entry in envelope.inputs},
        "timeout_s": timeout_s,
        "started": False,
        "usage_status": "unknown",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    selector: selectors.BaseSelector | None = None
    try:
        # Acquire the IO control before owning any child, including resource failures.
        selector = selectors.DefaultSelector()
        process = subprocess.Popen(
            adapter_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        if selector is not None:
            selector.close()
        metadata.update(
            {
                "elapsed_s": time.monotonic() - started_at,
                "start_error": str(exc),
                "returncode": None,
            }
        )
        return m.Observation(
            envelope.case_id, attempt, config_id, "START_ERROR", "", "", None, metadata
        )

    metadata.update({"started": True, "pid": process.pid, "pgid": process.pid})
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": MAX_STDOUT_BYTES, "stderr": MAX_STDERR_BYTES}
    status = "SUCCESS"
    assert selector is not None
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    pipes = (process.stdin, process.stdout, process.stderr)
    offset = 0
    try:
        for pipe, label, event in (
            (process.stdin, "stdin", selectors.EVENT_WRITE),
            (process.stdout, "stdout", selectors.EVENT_READ),
            (process.stderr, "stderr", selectors.EVENT_READ),
        ):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, event, label)
        deadline = started_at + timeout_s
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "TIMEOUT"
                break
            for key, _ in selector.select(min(remaining, 0.05)):
                stream = key.fileobj
                fd = key.fd
                label = key.data
                if label == "stdin":
                    try:
                        offset += os.write(fd, payload[offset : offset + 16384])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        offset = len(payload)
                    if offset == len(payload):
                        selector.unregister(stream)
                        stream.close()  # type: ignore[union-attr]
                else:
                    # Read only the remaining budget plus a one-byte overflow sentinel.
                    capacity = limits[label] - len(captured[label])
                    try:
                        raw = os.read(fd, min(65536, capacity + 1))
                    except BlockingIOError:
                        continue
                    if not raw:
                        selector.unregister(stream)
                        stream.close()  # type: ignore[union-attr]
                        continue
                    captured[label].extend(raw[:capacity])
                    if len(raw) > capacity:
                        status = "OUTPUT_LIMIT"
                        metadata[label + "_truncated"] = True
                        metadata["output_limit_stream"] = label
                        break
            if status != "SUCCESS":
                break
    finally:
        # Also remove descendants that closed their pipes before a successful parent exit.
        # Closing our descriptors prevents escaped pipe holders from blocking collection.
        try:
            _kill_owned_group(process)
        finally:
            selector.close()
            for pipe in pipes:
                pipe.close()
            process.wait()
    if status == "SUCCESS" and process.returncode != 0:
        status = "NONZERO_EXIT"
    metadata.update(
        {
            "returncode": process.returncode,
            "elapsed_s": time.monotonic() - started_at,
            "stdout_captured_bytes": len(captured["stdout"]),
            "stderr_captured_bytes": len(captured["stderr"]),
            "capture_complete": status in {"SUCCESS", "NONZERO_EXIT"},
        }
    )
    stdout = _text(bytes(captured["stdout"]), "stdout", metadata)
    stderr = _text(bytes(captured["stderr"]), "stderr", metadata)
    # AgentResult permits no usage field or wrapper. No provider telemetry is inferred
    # from stdout, stderr, argv labels, elapsed time or inherited environment variables.
    return m.Observation(
        envelope.case_id, attempt, config_id, status, stdout, stderr, None, metadata
    )


def run_suite(
    suite: m.Suite,
    root: Path,
    adapter_argv: tuple[str, ...],
    repeats: int = 1,
    timeout_s: int = 60,
    config_id: str = "adapter",
) -> m.ObservationBatch:
    """Preflight every case, then execute exactly the complete declared config/round plan.

    Only macOS/Linux POSIX process groups are controlled. This executes trusted argv
    without a shell; it does not restrict file/network access or adapter-internal bills.
    """
    if sys.platform == "win32" or os.name != "posix":
        raise m.CaseError("UNSUPPORTED_PLATFORM", "run requires POSIX owned process groups")
    if (
        not isinstance(adapter_argv, tuple)
        or not adapter_argv
        or any(not isinstance(arg, str) or "\x00" in arg for arg in adapter_argv)
        or not adapter_argv[0]
    ):
        raise m.CaseError("INVALID_ADAPTER_ARGV", "nonempty trusted argv tuple required")
    if type(timeout_s) is not int or timeout_s <= 0:
        raise m.CaseError("INVALID_TIMEOUT", "positive integer seconds required")
    run = m.RunSpec(config_id, repeats)
    validate_schema("observations", m.ObservationBatch("1", (run,), ()))
    root = Path(root).resolve()
    cases = preflight_suite(suite, root)
    # Prepare all envelopes and contained paths before starting even the first process.
    envelopes = tuple(
        _bind_inputs(make_task_envelope(case), ref, root)
        for case, ref in zip(cases, suite.cases, strict=True)
    )
    observations = tuple(
        _execute(envelope, adapter_argv, timeout_s, attempt, config_id)
        for attempt in range(1, run.repeats + 1)
        for envelope in envelopes
    )
    return m.ObservationBatch("1", (run,), observations)
