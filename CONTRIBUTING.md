# Contributing to Zimantic

Thanks for helping! Zimantic is an offline, multilingual search engine for Kiwix ZIM files, built to
run on small hardware such as a Raspberry Pi. Contributions of all sizes are welcome: bug reports, fixes,
documentation, new query sets for the evaluation, and new features.

## What Zimantic aims for

Please keep these in mind; they guide what gets merged:

1. **Offline.** Everything runs without internet access once set up.
2. **Multilingual.** Search should work across languages, not only English. Language-specific rules
   (word lists, stemming, phrases) should not make other languages worse.
3. **Measured.** Changes to ranking or indexing should be measured, not random. 

## How to contribute

1. **Open an issue first** for anything bigger than a small fix, so we can agree on the approach.
2. **Fork** the repository and create a **branch** for your change 
3. Keep each pull request to **one topic**. Several small PRs are easier to review and merge than one
   large one. If you are still working on it, open it as a **draft** and mark it ready when done.
4. Open a pull request against `main` and describe what changed and why.

To pick up changes from `main` while your PR is open, sync your fork (GitHub's **Sync fork** button,
or `git pull upstream main`).

## Development install

For a development environment based on the checkout:

```bash
uv sync
. .venv/bin/activate              # Windows: .venv\Scripts\activate
```

`uv sync --no-dev` omits test tools. `pip install .` also works in a virtual environment.

## Before you open a pull request

- **Tests:** if you add or change behaviour that can be tested, add tests in `tests/` (pytest) and make
  sure `python -m pytest` passes. (The project doesn't have a test suite yet; the first PR to add one
  is welcome.)
- **Evaluation, for ranking or indexing changes:** measure `main` and your branch on the same machine,
  with the same ZIM files, and include the comparison in the PR (see
  [evaluation/README.md](evaluation/README.md)):
  ```bash
  # 1. on main: start the server (python -m zimantic serve), then in a second terminal
  python evaluation/run.py --engine zimantic --url http://localhost:8090 --label main
  # 2. stop the server, switch to your branch (git switch my-branch), start it again, then
  python evaluation/run.py --engine zimantic --url http://localhost:8090 --label my-branch
  # 3. compare (the first file is the baseline)
  python evaluation/compare.py evaluation/results/main.json evaluation/results/my-branch.json --split test
  ```
  Only the ZIMs your server has indexes for are measured; the others are listed and left out. Report the
  results **by language and by query type**, and the **fixed / broken** counts, not only the overall
  number. If your change affects how indexes are built, rebuild the indexes on your branch before step 2
  (keep a copy of main's). Tune settings on `--split dev` and report on `--split test`.
- **Documentation:** update the README when behaviour, configuration, commands or published numbers
  change. Numbers in the README should come from a measurement of that version.
- **Compatibility:** if a change breaks existing configs, indexes or commands, say so in the PR and
  give users a clear message or migration path (e.g. "index built with an older version, please rebuild").

## Code style

Match the surrounding code: plain, readable Python, comments that explain *why*. Keep dependencies few;
anything that must run on the Pi should be checked for memory use.

## Provider contracts

The core does not branch on a source's kind. Every in-tree provider implements
the seam in `zimantic/providers/`:

- `SourceProvider.discover(config, context)` returns a `DiscoveryBatch`; one
  configured entry may yield zero or several collections.
- A `CollectionRuntime` exposes immutable `CollectionInfo` descriptor values and
  a published `CollectionSnapshot`, its sources, one-source `search`/`suggest`/
  `preview`, an optional `ContentStore`, and a per-search source cap.
- `provider` is the only discriminator and `SourceInfo.key` the only identity.
  Do not add `mode`, `content_kind`, `name`, `book`, or `local_name` fields or
  aliases. Keys are stable, URL-safe, and never derived from an absolute path.
- `SourceResult` keeps four channels. Status is `ok`, `partial`, `error`, or
  `cancelled`; errors are operation-scoped and are never cached or persisted as
  source availability.
- Content contracts and errors live in `zimantic/content/store.py`;
  `providers.base` re-exports the types. `ContentStore` is frozen, including
  inclusive `read_range`. Shared path normalization and request-target safety
  live in `zimantic/content/path.py`, and every local store must pass
  `tests/providers/test_content_store_contract.py`. A remote-only provider such
  as Kiwix returns `None` from `content_store()`.
- `zimantic/providers/zim/` is the reference provider. New providers implement
  the seam independently and must not import the ZIM package.
- Provider-private metadata and filesystem locators must not be serialized.
  Only the approved `SourceInfo.to_dict(include_metadata=...)` fields and the
  collection descriptor/snapshot payloads reach clients.
- A provider failure or timeout is isolated to its source result and must not
  starve or crash other providers. No provider may turn a failed operation into
  an empty successful result.

Changes to these contracts require updating the contract tests in
`tests/providers/`, the collection/source serializers, and the migration notes
in `plans/`.

### Adding an in-tree provider

Providers ship in this repository. There is no third-party plugin loading,
entry-point discovery, download, or dynamic execution of untrusted provider
code. Add a provider only when it belongs in-tree and can meet the same
contracts:

1. Add a typed config dataclass and a `ProviderConfigSpec` in
   `zimantic/providers/config.py` with an allowed-key set, a parser, provider
   defaults, and whether the provider is buildable. Generic `type`/`name`/
   duplicate validation stays in `parse_collections`; provider-specific keys
   and positive-int/range checks stay in the provider parser.
2. Register the `SourceProvider` in `zimantic/providers/registry.py`. Registry
   lookup failures stay explicit; there is no fallback provider.
3. Implement discovery and runtime contracts in `zimantic/providers/<name>/`:
   one configured entry may yield zero or several collections, a runtime
   publishes an immutable snapshot with `loading`/`ready`/`error`/`closed`,
   and a buildable local provider implements `BuildableRuntime.entry_producer`.
4. Use the shared `IndexLocator` for every local index path and generation.
   Never compute an index path in the provider, and never derive a logical key
   or filename from an absolute source path.
5. Enforce caps and path confinement before reading content. Discovery must be
   bounded by the configured `max_sources`/`max_entries` and traversal budgets;
   local content must be confined to the configured root; remote responses need
   byte/time bounds. A capped operation reports truncation or partial scope,
   not an empty success.
6. Isolate failures per entry and per source. One provider's timeout or error
   must not crash `serve`, hide another provider, or turn into an empty
   successful result. Preserve the last complete snapshot on a failed refresh.
7. Add contract tests in `tests/providers/` for source identity, snapshot states,
   content-store behaviour (or remote-only `None`), search/suggest errors,
   caps, path confinement, and failure isolation. Add CLI/config tests for the
   new entry type.
8. Update `config.toml`, the README provider section, and `deploy/` examples
   (build path/timer and service read paths) in the same change.

## Release and deploy

The Makefile can increment the patch version, update `uv.lock`, commit and publish an
annotated tag:

```bash
make release                 # 0.1.4 -> 0.1.5
make release VERSION=1.0.0  # explicit version override
```

To deploy a published tag to the default test box:

```bash
make deploy VERSION=v0.1.5
make release-deploy           # release, then deploy the new version
```

Set `DEPLOY_HOST` and `DEPLOY_PATH` to override the SSH target and remote checkout.
The deploy target updates the existing checkout, reinstalls the package, restarts
`zimantic.service`, and verifies the HTTP service and installed package version.

For a development deploy, stage the changes to send and apply them to the existing
remote checkout without checking out a tag. Before applying the patch, the deploy
resets tracked files in the remote checkout to `HEAD`, discarding previous
development-deploy changes:

```bash
git add path/to/changed-file
./scripts/deploy.sh --dev
# or: make deploy DEV=1
```

The development deploy leaves the newly applied changes in the remote checkout.

## Licensing

Zimantic is licensed under the **GNU General Public License v3.0 or later** (see [LICENSE](LICENSE)).
By contributing, you agree that your contributions are licensed under the same terms. Content-derived
files (such as evaluation queries quoting Wikipedia) keep their own licenses, noted where they are used.
