# Measurement Trail

[![tests](https://github.com/sparkainlp-x/measurement-trail/actions/workflows/tests.yml/badge.svg)](https://github.com/sparkainlp-x/measurement-trail/actions/workflows/tests.yml)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23067465.svg)](https://doi.org/10.5281/zenodo.23067465)

A compact, offline prototype for recording provenance as a measurement moves through device and software processing steps. It uses only the Python standard library and stores one canonical JSON record per line.

## Install

Python 3.10 or newer; no third-party packages, network access, or services. Either run the single script from this directory, or install it (from a clone) to get a `measurement-trail` command:

```sh
python3 -m pip install .
measurement-trail --version
```

## Quick start

```sh
python3 measurement_trail.py verify samples/example.jsonl   # bundled three-record sample trail

python3 measurement_trail.py append /tmp/measurement.jsonl \
  --event-json '{"timestamp":"2026-09-30T09:00:00Z","step":"filter-v2","actor":"edge-gateway-7","metadata":{"software_version":"2.4.1","status":"processed"},"payload_digest":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}'

python3 measurement_trail.py verify /tmp/measurement.jsonl
python3 -m unittest discover -s tests -v
```

(After `pip install .`, `measurement-trail` can replace `python3 measurement_trail.py`.) `append` verifies the existing trail first and refuses to append to an invalid one. Exit status is 0 on success and 2 for any invalid input or trail, with the reason on standard error. The Python API is `append_event` and `verify_trail`.

## Event and record format

An event has three required fields and three optional fields:

- Required: `timestamp` (timezone-aware ISO-8601 / RFC 3339 string of the form `YYYY-MM-DDTHH:MM:SS[.ffffff](Z|±HH:MM)`, validated identically on every supported Python version), `step` (processing step), and `actor` (device or software identity as supplied by the caller).
- Optional: `event_id`, `metadata` (string-to-string descriptive metadata), and `payload_digest` (a 64-character lowercase SHA-256 digest).

The schema has no measurement-value field: reference a payload by its digest (the tool never reads or stores payloads). Metadata values must be strings and common value-like keys (`value`, `reading`, `raw_value`, ...) are rejected, but this is a guard rail, not a filter: a key such as `Temp_C` or text such as `reading=23.5` is still accepted, so keep metadata descriptive yourself.

An `event_id`, when given, must be unique within the trail: `append` refuses a repeat and `verify` reports it. Timestamps are caller-supplied and are **not** required to increase (devices may have skewed clocks or report late); record order is given by `seq`.

Each line is an envelope with `schema_version`, one-based `seq`, `prev_hash`, `event`, and `record_hash`. The first record uses `prev_hash: null`; each later record points to the previous record's hash. `record_hash` is SHA-256 over the canonical JSON encoding of the other four fields (UTF-8, sorted keys, compact separators, no NaN/Infinity). Verification checks the schema, canonical line encoding, sequence, previous-hash link, and record hash. Blank lines, duplicate JSON keys, excessively nested JSON, and incomplete final lines are rejected.

On Linux and macOS, `append` holds an exclusive `fcntl` advisory lock on the trail file for the whole verify-then-append step, and `verify` holds a shared lock, so concurrent appenders on one machine cannot interleave. On Windows no lock is taken: do not run concurrent appends to the same trail there. Advisory locks are also not reliable on some network file systems.

Example (this is the exact line produced by the `append` command in the quick start):

```json
{"event":{"actor":"edge-gateway-7","metadata":{"software_version":"2.4.1","status":"processed"},"payload_digest":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","step":"filter-v2","timestamp":"2026-09-30T09:00:00Z"},"prev_hash":null,"record_hash":"746799031f6f935aeeae3e844f794512b7b4b0b2cfe78bab296f3fc5cc4fd4e3","schema_version":1,"seq":1}
```

## Limitations

This is an integrity-checking prototype, **not tamper-proof storage**:

- Anyone who can rewrite the file can recompute every hash; there are no signatures, so authorship is not authenticated.
- Deleting complete trailing records is not detectable: the remaining prefix still verifies. Detecting truncation or rewrites needs independently stored (ideally signed) checkpoints of the chain head.
- It says nothing about sensor accuracy or calibration, and provides no access control or durable storage.

## Related work / when to use something else

- **Signed, externally witnessed logs:** [Sigstore Rekor](https://docs.sigstore.dev/logging/overview/) or other Certificate-Transparency-style transparency logs, if you need third parties to detect rewrites or truncation.
- **Trusted timestamps:** RFC 3161 timestamping authorities, to prove a chain head existed at a given time.
- **Supply-chain provenance:** [in-toto](https://in-toto.io/) and SLSA provenance attestations for signed step-by-step build/processing metadata.
- **Provenance data models:** [W3C PROV](https://www.w3.org/TR/prov-overview/) if you need an interoperable vocabulary rather than this minimal schema.
- **Git** already gives a hash-chained, signable history if your records can live in a repository.

Measurement Trail is useful when you want a tiny, dependency-free, offline, append-only JSONL format that is easy to read and verify, and you can add signing and checkpointing yourself.

## Citation

See [CITATION.cff](CITATION.cff).

## License

This software is available under the GNU Affero General Public License v3.0 only (AGPL-3.0-only); see [LICENSE](LICENSE).

Organizations that want to use it in proprietary products or services without AGPL obligations can contact the author about a commercial license via https://sparkainlpx.xyz.
