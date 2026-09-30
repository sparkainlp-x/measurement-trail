# Copyright (C) 2026 Jean-François Brisson / Spark AI NLP. SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import measurement_trail  # noqa: E402
from measurement_trail import (  # noqa: E402
    TrailError,
    append_event,
    canonical_json,
    make_record,
    verify_trail,
)

# Pinned outputs: the record format is meant to be deterministic across platforms and
# Python versions, so any change to canonicalisation or hashing must show up here.
PINNED_README_RECORD_HASH = "746799031f6f935aeeae3e844f794512b7b4b0b2cfe78bab296f3fc5cc4fd4e3"
PINNED_SAMPLE_LAST_HASH = "be830a5e1dd30038635ec6e05c388ba79e5bfa2f115619a7e11c0a22360215e1"
README_EVENT = {
    "timestamp": "2026-09-30T09:00:00Z",
    "step": "filter-v2",
    "actor": "edge-gateway-7",
    "metadata": {"software_version": "2.4.1", "status": "processed"},
    "payload_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
}


class MeasurementTrailTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "trail.jsonl"

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def event(step="sensor-capture"):
        return {
            "timestamp": "2026-09-30T09:00:00Z",
            "step": step,
            "actor": "device-17",
            "event_id": f"event-{step}",
            "metadata": {"software_version": "1.2.0", "status": "complete"},
            "payload_digest": "a" * 64,
        }

    def _records(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_valid_append_and_verification(self):
        first = append_event(self.path, self.event())
        second = append_event(self.path, self.event("unit-conversion"))

        result = verify_trail(self.path)
        records = self._records()
        self.assertEqual(result.record_count, 2)
        self.assertEqual(first["seq"], 1)
        self.assertIsNone(first["prev_hash"])
        self.assertEqual(second["seq"], 2)
        self.assertEqual(second["prev_hash"], first["record_hash"])
        self.assertEqual(result.last_hash, second["record_hash"])
        self.assertEqual(records[0]["event"]["step"], "sensor-capture")
        self.assertNotIn("measurement_value", records[0]["event"])

    def test_edit_is_detected(self):
        append_event(self.path, self.event())
        record = self._records()[0]
        record["event"]["metadata"]["status"] = "edited"
        self.path.write_text(canonical_json(record) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(TrailError, "record_hash mismatch"):
            verify_trail(self.path)

    def test_reordering_is_detected(self):
        append_event(self.path, self.event())
        append_event(self.path, self.event("unit-conversion"))
        lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        self.path.write_text(lines[1] + lines[0], encoding="utf-8")

        with self.assertRaises(TrailError):
            verify_trail(self.path)

    def test_recomputed_record_with_broken_link_is_detected(self):
        append_event(self.path, self.event())
        append_event(self.path, self.event("unit-conversion"))
        records = self._records()
        records[1]["prev_hash"] = "0" * 64
        records[1] = make_record(records[1]["seq"], records[1]["prev_hash"], records[1]["event"])
        self.path.write_text(
            "".join(canonical_json(record) + "\n" for record in records), encoding="utf-8"
        )

        with self.assertRaisesRegex(TrailError, "chain break"):
            verify_trail(self.path)

    def test_invalid_events_are_rejected_without_appending(self):
        invalid_events = [
            {"timestamp": "2026-09-30T09:00:00Z", "step": "x"},  # missing actor
            {**self.event(), "measurement_value": "23.1"},  # unsupported raw-value field
            {**self.event(), "timestamp": "not-a-timestamp"},
            {**self.event(), "payload_digest": "not-a-digest"},
            {**self.event(), "metadata": {"status": 3}},  # metadata is descriptive text only
            {**self.event(), "metadata": {"reading": "23.1"}},
            {**self.event(), "step": "   "},
        ]
        for event in invalid_events:
            with self.subTest(event=event):
                with self.assertRaises(TrailError):
                    append_event(self.path, event)
                self.assertFalse(self.path.exists())

    def test_schema_version_is_checked_before_hash(self):
        append_event(self.path, self.event())
        record = self._records()[0]
        record["schema_version"] = 99
        # The schema check must fail before hash validation.
        self.path.write_text(canonical_json(record) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(TrailError, "unsupported schema_version"):
            verify_trail(self.path)

    def _write_records(self, records):
        self.path.write_text("".join(canonical_json(r) + "\n" for r in records), encoding="utf-8")

    def test_malformed_record_hash_and_prev_hash_are_rejected(self):
        append_event(self.path, self.event())
        append_event(self.path, self.event("unit-conversion"))
        records = self._records()
        bad = dict(records[0], record_hash=records[0]["record_hash"].upper())
        self._write_records([bad])
        with self.assertRaisesRegex(TrailError, "record_hash must be"):
            verify_trail(self.path)
        bad_first = dict(records[0], prev_hash="0" * 64)
        self._write_records([bad_first])
        with self.assertRaisesRegex(TrailError, "chain break"):
            verify_trail(self.path)

    def test_boolean_seq_and_schema_version_are_rejected(self):
        body = make_record(1, None, self.event())
        for field, value in (("seq", True), ("schema_version", True)):
            with self.subTest(field=field):
                self._write_records([dict(body, **{field: value})])
                with self.assertRaises(TrailError):
                    verify_trail(self.path)

    def test_extra_or_missing_record_fields_are_rejected(self):
        record = make_record(1, None, self.event())
        for mutated in ({**record, "note": "x"}, {k: v for k, v in record.items() if k != "seq"}):
            with self.subTest(keys=sorted(mutated)):
                self._write_records([mutated])
                with self.assertRaisesRegex(TrailError, "invalid record fields"):
                    verify_trail(self.path)

    def test_pinned_record_hash_matches_readme_example(self):
        record = make_record(1, None, README_EVENT)
        self.assertEqual(record["record_hash"], PINNED_README_RECORD_HASH)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(canonical_json(record), readme)

    def test_bundled_sample_trail_verifies_with_pinned_head(self):
        result = verify_trail(ROOT / "samples" / "example.jsonl")
        self.assertEqual(result.record_count, 3)
        self.assertEqual(result.last_hash, PINNED_SAMPLE_LAST_HASH)

    def test_timestamp_profile_is_python_version_independent(self):
        accepted = [
            "2026-09-30T09:00:00Z",
            "2026-09-30T09:00:00.5Z",
            "2026-09-30T09:00:00.123456-04:00",
            "2026-09-30T09:00:00+05:30",
        ]
        rejected = [
            "2026-09-30T09:00:00",  # no timezone
            "2026-09-30 09:00:00Z",  # no 'T'
            "20260930T090000Z",  # basic format: accepted by fromisoformat only on 3.11+
            "2026-W40-3T09:00:00Z",  # ISO week date: accepted only on 3.11+
            "2026-09-30T09:00Z",  # seconds required
            "2026-13-01T09:00:00Z",
            "2026-09-30T09:00:00+25:00",
            "2026-09-30T09:00:00.1234567Z",
            "2026-09-30T09:00:00+00:00Z",
        ]
        for ts in accepted:
            with self.subTest(ts=ts):
                measurement_trail.validate_event({**self.event(), "timestamp": ts})
        for ts in rejected:
            with self.subTest(ts=ts):
                with self.assertRaises(TrailError):
                    measurement_trail.validate_event({**self.event(), "timestamp": ts})

    def test_duplicate_keys_nan_blank_lines_and_bad_utf8_are_rejected(self):
        line = canonical_json(make_record(1, None, self.event()))
        duplicate = line[:-1] + ',"seq":1}'
        cases = {
            "duplicate": (duplicate + "\n").encode(),
            "nan": line.replace('"seq":1', '"seq":NaN').encode() + b"\n",
            "blank": b"\n" + line.encode() + b"\n",
            "utf8": b"\xff\n",
            "not-object": b"[1]\n",
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.path.write_bytes(data)
                with self.assertRaises(TrailError):
                    verify_trail(self.path)

    def test_append_refuses_invalid_existing_trail(self):
        append_event(self.path, self.event())
        record = self._records()[0]
        record["event"]["step"] = "tampered"
        self._write_records([record])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(TrailError, "record_hash mismatch"):
            append_event(self.path, self.event("next"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_empty_file_is_a_valid_empty_trail(self):
        self.path.write_bytes(b"")
        result = verify_trail(self.path)
        self.assertEqual((result.record_count, result.last_hash), (0, None))
        self.assertEqual(append_event(self.path, self.event())["seq"], 1)

    def test_non_ascii_text_round_trips(self):
        event = {**self.event(), "actor": "capteur-é-7", "metadata": {"site": "Montréal"}}
        append_event(self.path, event)
        self.assertEqual(verify_trail(self.path).record_count, 1)
        self.assertIn("Montréal", self.path.read_text(encoding="utf-8"))

    def _cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = measurement_trail.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_append_and_verify(self):
        code, out, _ = self._cli("append", str(self.path), "--event-json", json.dumps(README_EVENT))
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"appended seq=1 record_hash={PINNED_README_RECORD_HASH}")
        code, out, _ = self._cli("verify", str(self.path))
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"OK records=1 last_hash={PINNED_README_RECORD_HASH}")

    def test_cli_reports_errors_with_exit_code_2(self):
        bad_inputs = ['{"timestamp":"2026-09-30T09:00:00Z"}', "not json", '{"a":1,"a":2}', "[]"]
        for text in bad_inputs:
            with self.subTest(text=text):
                code, out, err = self._cli("append", str(self.path), "--event-json", text)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertTrue(err.startswith("error: "))
                self.assertFalse(self.path.exists())
        code, _, err = self._cli("verify", str(self.path))
        self.assertEqual(code, 2)
        self.assertIn("cannot open trail", err)

    def test_unanchored_valid_prefix_does_not_reveal_tail_truncation(self):
        append_event(self.path, self.event("capture"))
        append_event(self.path, self.event("normalize"))
        append_event(self.path, self.event("publish"))
        lines = self.path.read_bytes().splitlines(keepends=True)
        self.path.write_bytes(b"".join(lines[:2]))

        # Deliberately succeeds: without an independently stored checkpoint, the verifier
        # cannot know that the previously valid third record was removed.
        result = verify_trail(self.path)
        self.assertEqual(result.record_count, 2)

    def test_incomplete_final_line_is_rejected(self):
        append_event(self.path, self.event())
        self.path.write_bytes(self.path.read_bytes().rstrip(b"\n"))

        with self.assertRaisesRegex(TrailError, "terminating newline"):
            verify_trail(self.path)

    def test_noncanonical_json_is_rejected(self):
        record = make_record(1, None, self.event())
        self.path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(TrailError, "canonical JSON"):
            verify_trail(self.path)

    def test_deeply_nested_json_is_reported_as_trail_error(self):
        deep = "[" * 100_000
        self.path.write_text(deep + "\n", encoding="utf-8")
        with self.assertRaisesRegex(TrailError, "nesting is too deep"):
            verify_trail(self.path)
        code, out, err = self._cli("verify", str(self.path))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("nesting is too deep", err)

        other = Path(self.temp_dir.name) / "other.jsonl"
        nested_event = (
            '{"timestamp":"2026-09-30T09:00:00Z","step":"x","actor":"a","metadata":{"k":'
            + deep
        )
        code, out, err = self._cli("append", str(other), "--event-json", nested_event)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("nesting is too deep", err)
        self.assertFalse(other.exists())

    @unittest.skipIf(measurement_trail.fcntl is None, "advisory locking is POSIX-only")
    def test_concurrent_cli_appends_do_not_corrupt_the_trail(self):
        script = str(ROOT / "measurement_trail.py")
        count = 24
        processes = [
            subprocess.Popen(
                [
                    sys.executable, script, "append", str(self.path), "--event-json",
                    json.dumps({**self.event(f"step-{index}"), "event_id": f"evt-{index}"}),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            for index in range(count)
        ]
        for process in processes:
            _, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, err)
        result = verify_trail(self.path)
        self.assertEqual(result.record_count, count)
        self.assertEqual(
            sorted(record["seq"] for record in self._records()), list(range(1, count + 1))
        )

    def test_duplicate_event_id_is_rejected_on_append_and_verify(self):
        append_event(self.path, self.event())
        before = self.path.read_bytes()
        with self.assertRaisesRegex(TrailError, "event_id 'event-sensor-capture' already exists"):
            append_event(self.path, {**self.event(), "timestamp": "2026-09-30T09:00:01Z"})
        self.assertEqual(self.path.read_bytes(), before)
        code, _, err = self._cli("append", str(self.path), "--event-json", json.dumps(self.event()))
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

        first = make_record(1, None, self.event())
        second = make_record(2, first["record_hash"], self.event())
        self.path.write_text(
            canonical_json(first) + "\n" + canonical_json(second) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(TrailError, "line 2: duplicate event_id"):
            verify_trail(self.path)

    def test_events_without_event_id_may_repeat(self):
        event = {k: v for k, v in self.event().items() if k != "event_id"}
        append_event(self.path, event)
        append_event(self.path, event)
        self.assertEqual(verify_trail(self.path).record_count, 2)

    def test_non_monotonic_timestamps_are_accepted(self):
        # Documented behaviour: devices may have skewed clocks or report late, so
        # timestamp order is caller-supplied metadata and is not enforced.
        append_event(self.path, {**self.event("later"), "timestamp": "2026-09-30T10:00:00Z"})
        append_event(self.path, {**self.event("earlier"), "timestamp": "2026-09-30T09:00:00Z"})
        self.assertEqual(verify_trail(self.path).record_count, 2)

    def test_version_is_single_sourced_and_matches_citation(self):
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        match = re.search(r"^version: (\S+)$", citation, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(measurement_trail.__version__, match.group(1))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            measurement_trail.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(measurement_trail.__version__, out.getvalue())

    def test_metadata_shape_is_validated(self):
        for metadata in (["status"], {"": "x"}, {" ": "x"}):
            with self.subTest(metadata=metadata):
                with self.assertRaisesRegex(TrailError, "metadata"):
                    append_event(self.path, {**self.event(), "metadata": metadata})
        self.assertFalse(self.path.exists())

    def test_invalid_stored_event_is_reported_with_line_number(self):
        record = make_record(1, None, self.event())
        record["event"]["step"] = ""
        self.path.write_text(canonical_json(record) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(TrailError, "line 1: invalid event"):
            verify_trail(self.path)


if __name__ == "__main__":
    unittest.main()
