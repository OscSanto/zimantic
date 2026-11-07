# Mixed-library release fixture

This directory contains the phase-09 reproducibility inputs for the mixed-library
release gate.  Nothing here downloads content or contacts the network.

## Generate

From the repository root:

```bash
python tests/fixtures/mixed_library/generate.py --root /tmp/zimantic-mixed --force
```

The generator writes one fixture of each provider:

| Provider | Fixture |
|---|---|
| ZIM | `zims/fixture_en_all_2026-01.zim` with 200 front articles |
| Files | `files/` with 1,000 indexable pages, three topic directories, shared assets, one hidden path, and one excluded path |
| Kolibri | `kolibri/db.sqlite3` with a pinned v0.19.5-shaped installed schema, three topics, documents, one HTML5 app, and one video plus caption track |
| Kiwix | `kiwix_server.py`, a local deterministic catalog/search endpoint with three books |

`INPUTS.json` records the pinned Kolibri release and fixture sizes.
`queries.txt` is the fixed 100-query set used by the benchmark.

## Run the benchmark

```bash
python tests/benchmarks/run_mixed.py --root /tmp/zimantic-mixed \
    --device-model "Raspberry Pi 4 Model B 4 GB" \
    --ram 4GB --os "Raspberry Pi OS 64-bit" --storage "USB SSD" \
    --commit "$(git rev-parse --short HEAD)" \
    --fixture-revision phase09-v1
```

The harness starts the real server over the fixture, records startup to an
HTTP-ready `/health`, one warm-up request, p50/p95 over the fixed query set,
`/library` payload size and render time, and peak process RSS.  Pass
`--build fast` or `--build full` to include a cold build measurement.

The Kiwix endpoint is optional for startup/discovery measurements; when it is
not running its collection reports an operation-scoped discovery error.  Start
it with:

```bash
python tests/fixtures/mixed_library/kiwix_server.py --port 8085
```

## Gate status

The Pi 4 targets (startup under 120 s, mixed search p95 under 8 s, peak RSS
under 1 GiB) are verified only by a recorded result from the documented
hardware.  A missing fixture or result is **unverified**, not a pass.  Record
the device model/RAM, OS, storage, Python version, commit, and fixture revision
with every result.
