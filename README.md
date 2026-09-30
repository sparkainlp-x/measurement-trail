# Measurement Trail

A compact, offline prototype for recording provenance as a measurement moves through device and software processing steps. It uses only the Python standard library and stores one canonical JSON record per line.

## Requirements

- Python 3.10 or newer
- No packages, network access, sensor integration, or service required

## Quick start

From this directory:

```sh
python3 measurement_trail.py verify samples/example.jsonl   # bundled three-record sample trail

python3 measurement_trail.py append /tmp/measurement.jsonl \
  --event-json '{"timestamp":"2026-09-30T09:00:00Z","step":"filter-v2","actor":"edge-gateway-7","metadata":{"software_version":"2.4.1","status":"processed"},"payload_digest":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}'

python3 measurement_trail.py verify /tmp/measurement.jsonl
python3 -m unittest discover -s tests -v
```

The `append` command accepts an event as JSON via `--event-json`; it validates the existing trail before adding a line and refuses to append if that trail is invalid. To use the Python API, import `append_event` and `verify_trail` from `measurement_trail.py`.

## Event and record format

An event has three required fields and three optional fields:

- Required: `timestamp` (timezone-aware ISO-8601 / RFC 3339 string of the form `YYYY-MM-DDTHH:MM:SS[.ffffff](Z|±HH:MM)`, validated identically on every supported Python version), `step` (processing step), and `actor` (device or software identity as supplied by the caller).
- Optional: `event_id`, `metadata` (string-to-string descriptive metadata), and `payload_digest` (a 64-character lowercase SHA-256 digest).

The event schema intentionally has no measurement-value field. Metadata must remain descriptive: do not put raw readings or measurement values in it. The validator requires string metadata values and rejects common value-like metadata keys, but software cannot reliably recognize every value hidden in arbitrary text. Pass a digest when a payload needs a reference; the payload itself is never read or stored by this tool.

Each line is an envelope with `schema_version`, one-based `seq`, `prev_hash`, `event`, and `record_hash`. The first record uses `prev_hash: null`; each later record points to the previous record's hash. `record_hash` is SHA-256 over the canonical JSON encoding of the other four fields (UTF-8, sorted keys, compact separators, no NaN/Infinity). Verification checks the schema, canonical line encoding, sequence, previous-hash link, and record hash. Blank lines, duplicate JSON keys, and incomplete final lines are rejected.

Example (this is the exact line produced by the `append` command in the quick start):

```json
{"event":{"actor":"edge-gateway-7","metadata":{"software_version":"2.4.1","status":"processed"},"payload_digest":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","step":"filter-v2","timestamp":"2026-09-30T09:00:00Z"},"prev_hash":null,"record_hash":"746799031f6f935aeeae3e844f794512b7b4b0b2cfe78bab296f3fc5cc4fd4e3","schema_version":1,"seq":1}
```

## Important limitations

This is an integrity-checking prototype, **not tamper-proof storage**. An unanchored valid prefix cannot reveal that later lines were truncated or removed: verifying a trail after deleting complete trailing records will succeed for the remaining prefix. A hash chain also does not establish who wrote a record, whether a sensor was accurate, or protect an editable trail; someone who can rewrite the file can recompute its hashes. It provides no anonymity or hardware assurance.

A production system needs trusted signatures and key management to authenticate authorship, plus independently stored checkpoints (for example, signed or externally retained chain heads) to make later edits or truncation detectable relative to a known checkpoint. Sensor calibration, access control, durable storage, and concurrency handling also require separate design. This prototype performs no network operations and has no live sensor integration.

## License

This software is available under the GNU Affero General Public License v3.0 only (AGPL-3.0-only); see [LICENSE](LICENSE).

Organizations that want to use it in proprietary products or services without AGPL obligations can contact the author about a commercial license via https://sparkainlpx.xyz.
