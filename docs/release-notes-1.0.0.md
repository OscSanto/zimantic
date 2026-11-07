# Zimantic 1.0 release notes

Zimantic 1.0 is the provider-neutral release.  It replaces the ZIM-only
configuration and index assumptions with one `[[collections]]` schema, one
source identity, and one local content route.

## New providers and surfaces

- **ZIM** collections are declared explicitly with `[[collections]] type = "zim"`.
- **Files** collections index and serve a configured directory tree.  A child
  source's links stay collection-relative, and hidden/excluded paths are never
  served.
- **Kolibri** collections read a pinned Kolibri v0.19.5 SQLite installation
  through private, read-only snapshots.  Each channel is a collection; topics
  are sources.  Captions, OCR, and ASR are independent build-time opt-ins.
- **Kiwix** remains a first-class remote provider and returns external
  `kiwix-serve` links.  It remains search-only.
- The library and source APIs expose `provider`, `collection`, `media_types`,
  `indexed_count`, `truncated`, and `body_truncated_count`.  Files and Kolibri
  use the same `/content/{key}/{path}` serving route as ZIM.

There is no compatibility view for the old flat `zim_dir`/`kiwix_server`
configuration, old source aliases, or old index filenames.

## Required upgrade steps

1. **Rewrite configuration.** Replace flat provider keys with explicit
   `[[collections]]` entries.  Removed keys are fatal errors with a migration
   message.  Start from the commented examples in `config.toml`.
2. **Restart the server.**  A normal `zimantic reload` rescans configured
   entries but does not reread `config.toml`.
3. **Rebuild the catalog.**  `catalog.sqlite` is disposable metadata.  An
   incompatible catalog is discarded and rebuilt by a full local-provider scan.
   Do not run old and new releases against the same catalog at once.
4. **Rebuild indexes.**  The provider-neutral locator and schema replace the
   old stem-based layout.  The one-time importer adopts only recognized,
   validated old ZIM indexes; unknown or incompatible indexes are rebuilt
   without deleting a usable last-good generation.
5. **Update bookmarks and clients.**  Use canonical source keys, the `source`
   query parameter, and `/content/{key}/...` or `/article/{key}/...`.
6. **Reclaim replaced files.**  `zimantic prune --dry-run` reports owned orphan
   generations; `zimantic prune --yes` removes them under the build lock.

## Security and operational hardening

- Local stores use root-confined, no-follow opens and reject traversal,
  encoded/backslash/NUL paths, symlinks, hidden paths, and configured excludes.
  Directory requests never produce listings.
- HTML5 zips are manifest-validated with bounded members, total output, and a
  read deadline.  Zip-slip, links, duplicate members, and overlapping members
  fail closed.
- MIME classification prefers extensions and constrained sniffing.  Unknown
  binaries are attachments.  Provider content always sends
  `X-Content-Type-Options: nosniff`; single, validated, capped byte ranges are
  supported without gzip on partial responses.
- Kiwix XML rejects DTD/entity declarations in UTF-8 and UTF-16, and validates
  redirect origins before contacting the next origin.
- Kolibri reads never write the installation, its WAL/SHM/journal sidecars, or
  configured storage roots.  Snapshots are owner-only and removed after the last
  reader.
- Absolute configured roots and private catalog locators are not emitted in API
  payloads, errors, redirects, ETags, or logs above debug level.
- Provider discovery, search, and content failures are operation-scoped.  A
  failed refresh keeps the last complete snapshot; a broken source does not
  stop serving or searching other sources.

`secure_browsing = true` isolates the `/content/` shell iframe.  A raw
`/article/` document opened directly or in a permitted popup is still
unsandboxed and can use the app origin and persistent storage.  Treat file trees
and Kolibri HTML5 apps as untrusted input regardless of that setting.

## Resource limits

The release keeps the roadmap defaults and makes provider limits configurable
per entry: `max_sources`, `max_entries`, `max_file_bytes`, traversal budgets,
Kiwix catalog/response/source limits, and Kolibri snapshot/HTML5-zip limits.
Global body, derived-cache, search-deadline, and listing limits remain global.
Index truncation is reported through `truncated` and `indexed_count`; body text
truncation is reported separately through `body_truncated_count`.  A truncated
source still serves all permitted content.

## Known limitations

See [known-issues.md](known-issues.md).  The Raspberry Pi 4 mixed-library
benchmark is a release gate, not a claim made by the source tree alone: record
a result with `tests/benchmarks/run_mixed.py` on the documented hardware.  A
missing result is unverified.
