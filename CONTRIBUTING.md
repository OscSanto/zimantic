# Contributing to Zimantic

Zimantic is an offline, multilingual search engine for Kiwix ZIM files. It runs on small devices, including Raspberry Pi computers. Contributions include bug reports, code, documentation, evaluation queries, and features.

## Project goals

1. Offline operation. The application runs without network access after setup.
2. Multilingual search. Search supports languages beyond English. Language-specific rules do not reduce search quality in other languages.
3. Measured ranking and indexing. Evaluation results guide ranking and indexing work.

## How to contribute

1. Open an issue before work that needs design discussion.
2. Fork the repository and create a branch.
3. Keep each pull request focused on one topic. Use a draft pull request while work is in progress.
4. Open the pull request against `main`. Describe its purpose and behavior.

To update your fork from `main`, use GitHub's Sync fork button or run `git pull upstream main`.

## Development install

For a development environment based on the checkout:

```bash
uv sync
. .venv/bin/activate              # Windows: .venv\Scripts\activate
```

`uv sync --no-dev` omits test tools. `pip install .` also works in a virtual environment.

## Pull request requirements

- Tests. Add tests in `tests/` for testable behavior, and run `python -m pytest`.
- Evaluation. Ranking and indexing use the evaluation harness. Measure `main` and the pull request branch on the same machine with the same ZIM files. Include the comparison in the pull request (see [evaluation/README.md](evaluation/README.md)):
  ```bash
  # 1. On main, start the server (python -m zimantic serve), then in a second terminal:
  python evaluation/run.py --engine zimantic --url http://localhost:8090 --label main
  # 2. Stop the server, switch to your branch (git switch my-branch), start it again, then:
  python evaluation/run.py --engine zimantic --url http://localhost:8090 --label my-branch
  # 3. Compare (the first file is the baseline):
  python evaluation/compare.py evaluation/results/main.json evaluation/results/my-branch.json --split test
  ```
  The harness measures ZIM files with indexes and lists the others without scoring them. Report results by language and query type, and report fixed and broken counts. For indexing work, build separate indexes on each branch. Tune with `--split dev` and report with `--split test`.
- Documentation. Keep the README consistent with documented behavior, configuration, commands, and published measurements. Use measurements from the documented version. Write concise present-tense prose with consistent technical terms.
- Compatibility. Document supported configuration, index, and command formats. Zimantic supports documented formats only; it does not guarantee backward compatibility or migrations. Reject unsupported formats with a clear diagnostic that tells users to update configuration or rebuild caches and indexes. Cross-model embedding-probe reuse is supported.

## Code style

Follow the surrounding style. Use clear Python and comments that explain why. Keep dependencies few. Check memory use for code that runs on a Raspberry Pi.

## Provider contracts

The core uses one provider interface for every provider. Every in-tree provider implements the contract in `zimantic/providers/`:

- `SourceProvider.discover(config, context)` returns a `DiscoveryBatch`. One configured entry yields zero or several collections.
- A `CollectionRuntime` exposes immutable `CollectionInfo` descriptor values, a published `CollectionSnapshot`, its sources, one-source `search`/`suggest`/`preview`, an optional `ContentStore`, and a per-search source limit.
- `provider` is the only discriminator, and `SourceInfo.key` is the only identity. Do not add `mode`, `content_kind`, `name`, `book`, or `local_name` fields or aliases. Keys are stable, URL-safe, and never derived from an absolute path.
- `SourceResult` keeps four channels. Status is `ok`, `partial`, `error`, or `cancelled`; errors are operation-scoped and are never cached or persisted as source availability.
- Content contracts and errors live in `zimantic/content/store.py`; `providers.base` re-exports the types. `ContentStore` is frozen, including inclusive `read_range`. Shared path normalization and request-target safety live in `zimantic/content/path.py`, and every local store passes `tests/providers/test_content_store_contract.py`. A remote-only provider such as Kiwix returns `None` from `content_store()`.
- `zimantic/providers/zim/` is the reference provider. Additional providers implement the interface independently and do not import the ZIM package.
- Provider-private metadata and filesystem locators are not serialized. Only the approved `SourceInfo.to_dict(include_metadata=...)` fields and the collection descriptor/snapshot payloads reach clients.
- A provider failure or timeout is isolated to its source result and does not starve or crash other providers. A provider cannot turn a failed operation into an empty successful result.

The contract tests in `tests/providers/` and the collection/source serializers define these contracts. Compatibility limits are documented in [`docs/limitations.md`](docs/limitations.md).

### Adding an in-tree provider

Providers ship in this repository. Zimantic does not load third-party plugins, discover entry points, download providers, or run untrusted provider code. Every in-tree provider meets the same contracts:

1. Define a typed config dataclass and a `ProviderConfigSpec` in `zimantic/providers/config.py` with an allowed-key set, a parser, and provider defaults. Generic `type` and `name` validation, plus duplicate detection, stays in `parse_collections`; provider-specific keys and positive-int/range checks stay in the provider parser. Buildability is a provider capability, not part of the config spec; add a buildable local provider to the CLI's buildable-provider policy and implement `BuildableRuntime.entry_producer`.
2. Register the `SourceProvider` in `zimantic/providers/registry.py`. Registry lookup failures are explicit; no fallback provider exists.
3. Implement discovery and runtime contracts in `zimantic/providers/<name>/`. One configured entry yields zero or several collections, a runtime publishes an immutable snapshot with `loading`/`ready`/`error`/`closed`, and a buildable local provider implements `BuildableRuntime.entry_producer`.
4. Use the shared `IndexLocator` for every local index path and generation. Providers do not compute index paths or derive logical keys or filenames from absolute source paths.
5. Enforce limits and path confinement before reading content. Configured `max_sources`, `max_entries`, and traversal budgets bound discovery. Local content stays inside the configured root. Remote responses have byte and time limits. An operation at a limit reports truncation or partial scope, not success with an empty result.
6. Isolate failures by entry and source. A provider timeout or error does not stop `serve`, hide another provider, or return an empty success. A failed refresh preserves the last complete snapshot.
7. Cover source identity, snapshot states, content-store behavior (or remote-only `None`), search/suggest errors, limits, path confinement, and failure isolation in `tests/providers/`. Add CLI and config tests for the provider.
8. Document the provider in `config.toml`, the README, and the relevant `deploy/` examples (build path/timer and service read paths).

## Hardening and release gates

Provider, path, MIME, range, XML, SQLite, and build/index changes pass these checks:

- Run the full suite with `python -m pytest`.
- Add an adversarial test for each path, MIME, range, ZIM-file, or database input. Cover `..`, encoded and double-encoded separators, backslashes, NUL bytes, absolute paths, symlink replacement, zip-slip, oversized members, unknown binaries, single-range validation, and XML DTD/entity rejection in UTF-8 and UTF-16.
- Do not expose configured absolute paths, private catalog locators, or raw exception details in API payloads, redirects, ETags, or logs above debug level. Use `zimantic.diagnostics` for redaction and stable causes.
- Keep provider failures operation-scoped. A broken discovery, search, index, or content read does not hide another source or crash `serve`.
- Preserve the `truncated`, `indexed_count`, `body_truncated_count`, `last_refresh`, `collapsed`, and index-state fields across source, library, collection, health, and `zimantic sources --json` payloads.
- Security review covers path, zip, MIME, and range handling before release.

The mixed-library benchmark fixture is versioned under `tests/fixtures/mixed_library/`, and the harness is under `tests/benchmarks/`. Generate the fixture with `python tests/fixtures/mixed_library/generate.py --root /tmp/zimantic-mixed --force`. Record Raspberry Pi 4 results with `tests/benchmarks/run_mixed.py`. A missing hardware result is unverified, not a pass. Document required configuration, catalog, and index rebuilds, and unsupported compatibility paths.

## Release and deploy

The Makefile increments the patch version, updates `uv.lock`, commits, and publishes an annotated tag:

```bash
make release                 # 0.1.4 -> 0.1.5
make release VERSION=1.0.0  # explicit version override
```

Deploy a published tag to the default test host:

```bash
make deploy VERSION=v0.1.5
make release-deploy           # publish and deploy a release
```

Set `DEPLOY_HOST` and `DEPLOY_PATH` to select the SSH host and remote checkout. The deploy target updates the checkout, reinstalls the package, restarts `zimantic.service`, and checks the HTTP service and installed package version.

For a development deploy, stage the files to send. The deploy applies them to the existing remote checkout without checking out a tag. Before it applies the patch, it resets tracked files in the remote checkout to `HEAD`. This removes tracked edits on that checkout:

```bash
git add path/to/file
./scripts/deploy.sh --dev
# or: make deploy DEV=1
```

The development deploy leaves the applied files in the remote checkout.

## Licensing

Zimantic is licensed under the GNU General Public License v3.0 or later (see [LICENSE](LICENSE)). By contributing, you agree that your contributions are licensed under the same terms. Content-derived files (such as evaluation queries that quote Wikipedia) keep their own licenses, noted where they use them.
