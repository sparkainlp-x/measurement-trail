#!/usr/bin/env python3
# Copyright (C) 2026 Jean-François Brisson / Spark AI NLP. SPDX-License-Identifier: AGPL-3.0-only
"""Hash-chained measurement provenance trail prototype (Python standard library only).

The tool only ever appends records, but the trail file itself is not write-protected;
verification detects edits, reordering and broken links, not truncation of trailing records.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_RECORD_KEYS = {"schema_version", "seq", "prev_hash", "event", "record_hash"}
_EVENT_REQUIRED = {"timestamp", "step", "actor"}
_EVENT_OPTIONAL = {"event_id", "metadata", "payload_digest"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# One explicit RFC 3339-style profile, so validity does not depend on the Python version
# (datetime.fromisoformat accepts many more forms on Python 3.11+ than on 3.10).
_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$"
)
# Reject common attempts to place a reading in a field intended for descriptive metadata.
_VALUE_LIKE_METADATA_KEYS = {
    "value",
    "reading",
    "sample",
    "measurement",
    "rawvalue",
    "rawreading",
    "rawsample",
    "rawmeasurement",
    "measurementvalue",
    "readingvalue",
    "samplevalue",
}


class TrailError(ValueError):
    """Raised when a trail or event fails validation."""


@dataclass(frozen=True)
class VerificationResult:
    record_count: int
    last_hash: str | None


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically for hashing and on-disk records."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TrailError(f"value is not canonical-JSON serializable: {exc}") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-standard JSON constant {token}")


def _strict_json_loads(text: str) -> Any:
    """Parse JSON, rejecting duplicate keys and NaN/Infinity (raises ValueError)."""
    return json.loads(
        text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
    )


def _parse_json_line(text: str, line_number: int) -> Any:
    try:
        return _strict_json_loads(text)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError subclass
        raise TrailError(f"line {line_number}: invalid JSON: {exc}") from exc


def _require_nonempty_string(event: dict[str, Any], field: str) -> None:
    value = event.get(field)
    if not isinstance(value, str) or not value.strip():
        raise TrailError(f"event field {field!r} must be a non-empty string")


def _validate_timestamp(timestamp: str) -> None:
    message = (
        "event field 'timestamp' must be ISO-8601 / RFC 3339 with a timezone, "
        "e.g. 2026-09-30T09:00:00Z or 2026-09-30T09:00:00.5-04:00"
    )
    match = _TIMESTAMP_RE.fullmatch(timestamp)
    if match is None:
        raise TrailError(message)
    base, fraction, zone = match.groups()
    normalized = base + ("." + fraction.ljust(6, "0") if fraction else "")
    normalized += "+00:00" if zone == "Z" else zone
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:  # e.g. month 13 or offset hour 25
        raise TrailError(message) from exc
    if parsed.utcoffset() is None:
        raise TrailError(message)


def validate_event(event: Any) -> dict[str, Any]:
    """Validate the deliberately small event schema and return the event unchanged."""
    if not isinstance(event, dict):
        raise TrailError("event must be a JSON object")

    keys = set(event)
    missing = _EVENT_REQUIRED - keys
    unknown = keys - _EVENT_REQUIRED - _EVENT_OPTIONAL
    if missing:
        raise TrailError(f"event is missing required field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise TrailError(f"event has unsupported field(s): {', '.join(sorted(unknown))}")

    for field in ("timestamp", "step", "actor"):
        _require_nonempty_string(event, field)

    _validate_timestamp(event["timestamp"])

    if "event_id" in event:
        _require_nonempty_string(event, "event_id")

    if "metadata" in event:
        metadata = event["metadata"]
        if not isinstance(metadata, dict):
            raise TrailError("event field 'metadata' must be an object of descriptive string values")
        for key, value in metadata.items():
            if not isinstance(key, str) or not key.strip():
                raise TrailError("metadata keys must be non-empty strings")
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized_key in _VALUE_LIKE_METADATA_KEYS:
                raise TrailError(
                    f"metadata key {key!r} is reserved for measurement-value-like data"
                )
            if not isinstance(value, str):
                raise TrailError("metadata values must be strings; do not store measurement values")

    if "payload_digest" in event:
        digest = event["payload_digest"]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise TrailError("event field 'payload_digest' must be 64 lowercase hexadecimal SHA-256 characters")

    # Also ensure any optional values are valid JSON types and contain no NaN/Infinity.
    canonical_json(event)
    return event


def _hash_body(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def make_record(seq: int, prev_hash: str | None, event: Any) -> dict[str, Any]:
    """Build a canonical record envelope for an already-validated event."""
    validated_event = validate_event(event)
    body = {
        "schema_version": SCHEMA_VERSION,
        "seq": seq,
        "prev_hash": prev_hash,
        "event": validated_event,
    }
    return {**body, "record_hash": _hash_body(body)}


def _validate_hash(value: Any, label: str, line_number: int) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise TrailError(f"line {line_number}: {label} must be a 64-character lowercase SHA-256 hex digest")


def verify_trail(path: str | Path) -> VerificationResult:
    """Verify all records and their canonical encoding, sequence, and hash links."""
    trail_path = Path(path)
    expected_seq = 1
    expected_prev_hash: str | None = None
    last_hash: str | None = None

    try:
        stream = trail_path.open("rb")
    except OSError as exc:
        raise TrailError(f"cannot open trail {trail_path}: {exc}") from exc

    with stream:
        line_number = 0
        while True:
            raw_line = stream.readline()
            if raw_line == b"":
                break
            line_number += 1
            if not raw_line.endswith(b"\n"):
                raise TrailError(f"line {line_number}: record is missing its terminating newline")
            try:
                text = raw_line[:-1].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TrailError(f"line {line_number}: record is not valid UTF-8") from exc
            if not text:
                raise TrailError(f"line {line_number}: blank lines are not allowed")

            record = _parse_json_line(text, line_number)
            if not isinstance(record, dict):
                raise TrailError(f"line {line_number}: record must be a JSON object")
            if set(record) != _RECORD_KEYS:
                missing = _RECORD_KEYS - set(record)
                extra = set(record) - _RECORD_KEYS
                detail = []
                if missing:
                    detail.append("missing " + ", ".join(sorted(missing)))
                if extra:
                    detail.append("unsupported " + ", ".join(sorted(extra)))
                raise TrailError(f"line {line_number}: invalid record fields ({'; '.join(detail)})")

            if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
                raise TrailError(f"line {line_number}: unsupported schema_version (expected {SCHEMA_VERSION})")
            if type(record["seq"]) is not int or record["seq"] != expected_seq:
                raise TrailError(f"line {line_number}: sequence mismatch (expected {expected_seq})")
            if record["prev_hash"] != expected_prev_hash:
                raise TrailError(f"line {line_number}: chain break in prev_hash")
            if expected_prev_hash is not None:
                _validate_hash(record["prev_hash"], "prev_hash", line_number)

            try:
                validate_event(record["event"])
            except TrailError as exc:
                raise TrailError(f"line {line_number}: invalid event: {exc}") from exc

            _validate_hash(record["record_hash"], "record_hash", line_number)
            body = {key: record[key] for key in ("schema_version", "seq", "prev_hash", "event")}
            calculated_hash = _hash_body(body)
            if record["record_hash"] != calculated_hash:
                raise TrailError(f"line {line_number}: record_hash mismatch")
            if canonical_json(record) != text:
                raise TrailError(f"line {line_number}: record is not in canonical JSON form")

            last_hash = calculated_hash
            expected_prev_hash = calculated_hash
            expected_seq += 1

    return VerificationResult(record_count=expected_seq - 1, last_hash=last_hash)


def append_event(path: str | Path, event: Any) -> dict[str, Any]:
    """Append one validated event without rewriting existing records."""
    validated_event = validate_event(event)
    trail_path = Path(path)
    if trail_path.exists():
        current = verify_trail(trail_path)
    else:
        current = VerificationResult(record_count=0, last_hash=None)

    record = make_record(current.record_count + 1, current.last_hash, validated_event)
    line = (canonical_json(record) + "\n").encode("utf-8")
    try:
        with trail_path.open("ab") as stream:
            stream.write(line)
            stream.flush()
    except OSError as exc:
        raise TrailError(f"cannot append to trail {trail_path}: {exc}") from exc
    return record


def _load_event_json(text: str) -> dict[str, Any]:
    try:
        event = _strict_json_loads(text)
    except ValueError as exc:
        raise TrailError(f"invalid --event-json: {exc}") from exc
    return validate_event(event)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append and verify an offline hash-chained measurement provenance trail."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    append_parser = subparsers.add_parser("append", help="append one structured event")
    append_parser.add_argument("trail", type=Path, help="JSONL trail file")
    append_parser.add_argument(
        "--event-json",
        required=True,
        help=(
            "event JSON object (timestamp, step, actor; optional event_id, metadata, payload_digest); "
            "never include raw measurement values"
        ),
    )

    verify_parser = subparsers.add_parser("verify", help="verify a trail's records and hash chain")
    verify_parser.add_argument("trail", type=Path, help="JSONL trail file")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "append":
            record = append_event(args.trail, _load_event_json(args.event_json))
            print(f"appended seq={record['seq']} record_hash={record['record_hash']}")
            return 0
        result = verify_trail(args.trail)
        print(f"OK records={result.record_count} last_hash={result.last_hash or '-'}")
        return 0
    except TrailError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
