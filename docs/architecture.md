# Architecture

Zimantic uses one provider-neutral collection schema, one source identity, and
one local content route.

## Providers

- **ZIM** collections use `[[collections]]` entries with `type = "zim"`.
- **Files** collections index and serve a configured directory tree. Child
  source links stay within the collection. Hidden and excluded paths are not
  served.
- **Kolibri** collections read a supported v0.19.5 SQLite installation through
  private, read-only snapshots. Each channel is a collection; topics are
  sources. Captions, OCR, and ASR are independent build-time options.
- **Kiwix** collections search remote books through the `kiwix-serve` API and
  return external links. They do not serve local content.
- The library and source APIs expose `provider`, `collection`, `media_types`,
  `indexed_count`, `truncated`, and `body_truncated_count`. ZIM, Files, and
  Kolibri content use `/content/{key}/{path}`.

Configuration uses `[[collections]]` entries. Source selection
uses canonical source keys and the `source` query parameter. Local content uses
`/content/{key}/...` or `/article/{key}/...`.

## Content safety and operation

- Local stores confine reads to the configured root and reject traversal,
  encoded separators, backslashes, NUL bytes, symlinks, hidden paths, and
  configured excludes. Directory requests do not create listings.
- HTML5 zip files use a validated manifest, bounded members and output, and a
  read deadline. Unsafe paths, links, duplicate members, overlapping members,
  and oversized output fail closed.
- MIME classification uses file extensions and constrained content
  detection. Unknown binaries are attachments. Provider content sends
  `X-Content-Type-Options: nosniff`. A response supports one validated,
  bounded byte range; partial responses do not use gzip.
- Kiwix XML rejects DTD and entity declarations in UTF-8 and UTF-16. Redirect
  origins are validated before the next request.
- Kolibri reads do not write to the installation, WAL/SHM/journal sidecars, or
  configured storage roots. Private snapshots are owner-only and are removed
  after the last reader.
- API payloads, errors, redirects, ETags, and logs above debug level do not
  expose absolute configured roots or private catalog locators.
- Discovery, search, and content failures are operation-scoped. A failed
  refresh keeps the last complete snapshot. A broken source does not stop
  other sources.

- `secure_browsing = true` sandboxes the `/content/` shell iframe. A raw
  `/article/` document opened directly or in a permitted popup is not
  sandboxed. Treat file trees and Kolibri HTML5 apps as untrusted input.

## Resource limits and status

Provider limits are configurable per entry. They include `max_sources`,
`max_entries`, `max_file_bytes`, traversal budgets, Kiwix catalog/response/source
limits, and Kolibri snapshot/HTML5-zip limits. Global limits include body size,
derived-cache size, search deadlines, and listing size.

Index truncation uses `truncated` and `indexed_count`. Body-text truncation uses
`body_truncated_count`. A truncated source still serves all permitted content.
See [limitations](limitations.md) for operational constraints and
[benchmark instructions](../tests/benchmarks/README.md) for the Raspberry Pi 4
measurement procedure.
