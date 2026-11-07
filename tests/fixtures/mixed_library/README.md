# Mixed-library fixture

This directory contains the reproducibility inputs for the mixed-library
benchmark. The fixture does not download content or contact the network.

## Generate

From the repository root:

```bash
python tests/fixtures/mixed_library/generate.py --root /tmp/zimantic-mixed --force
```

The generator writes one fixture for each provider:

| Provider | Fixture |
|---|---|
| ZIM | `zims/fixture_en_all_2026-01.zim` with 200 front articles |
| Files | `files/` with 1,000 indexable pages, three topic directories, shared assets, one hidden path, and one excluded path |
| Kolibri | `kolibri/db.sqlite3` with a pinned v0.19.5-shaped installed schema, three topics, documents, one HTML5 app, and one video plus caption track |
| Kiwix | `kiwix_server.py`, a local deterministic catalog/search endpoint with three books |

`INPUTS.json` records the Kolibri release and fixture sizes.
`queries.txt` contains the fixed 100-query benchmark set.

## Run the benchmark

```bash
python tests/benchmarks/run_mixed.py --root /tmp/zimantic-mixed \
    --device-model "Raspberry Pi 4 Model B 4 GB" \
    --ram 4GB --os "Raspberry Pi OS 64-bit" --storage "USB SSD" \
    --commit "$(git rev-parse --short HEAD)" \
    --fixture-revision phase09-v1
```

The harness starts the server with the fixture. It records startup time until
`/health` is ready, one warm-up request, p50/p95 over the fixed query set,
`/library` payload size and render time, and peak process RSS. Use
`--build fast` or `--build full` to include a cold build measurement.

The Kiwix endpoint is optional for startup and discovery measurements. If it
is not running, its collection reports an operation-scoped discovery error.
Start it with:

```bash
python tests/fixtures/mixed_library/kiwix_server.py --port 8085
```

## Raspberry Pi 4 targets

The targets are startup under 120 seconds, mixed search p95 under 8 seconds,
and peak RSS under 1 GiB. A recorded result from the documented hardware
verifies them. A missing fixture or result is **unverified**. Each result
records the device model and RAM, OS, storage, Python version, commit, and
fixture revision.
