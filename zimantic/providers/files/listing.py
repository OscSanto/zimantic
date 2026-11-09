"""Small, self-contained directory pages served through the article reader."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from urllib.parse import quote


def listing_row(
    base_url: str, path: str, name: str, *, directory: bool, size: int, mtime: float
) -> str:
    href = escape(base_url + quote(path, safe="/"), quote=True)
    label = escape(name + ("/" if directory else ""))
    kind = "Folder" if directory else "File"
    display_size = "-"
    if not directory:
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                display_size = (
                    f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
                )
                break
            value /= 1024
    modified = datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%d %H:%M")
    return (
        f'<tr data-size="{size}" data-modified="{mtime}">'
        f'<td><a href="{href}">{label}</a></td>'
        f'<td>{kind}</td><td class="number">{display_size}</td>'
        f"<td><time>{modified}</time></td></tr>"
    )


def directory_page(title: str, path: str, base_url: str, rows: list[str]) -> bytes:
    crumbs = [f'<a href="{escape(base_url, quote=True)}">Files</a>']
    parts = path.strip("/").split("/") if path else []
    for index, part in enumerate(parts):
        href = escape(
            base_url + quote("/".join(parts[: index + 1]) + "/", safe="/"), quote=True
        )
        crumbs.append(f'<a href="{href}">{escape(part)}</a>')
    empty = "" if rows else "<p>This folder is empty.</p>"
    return (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>"""
        + escape(title)
        + """ - Files</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0 auto; padding: 1.5rem; max-width: 72rem; }
a { color: light-dark(#2458a6, #9cc5ff); text-decoration: none; }
a:hover { text-decoration: underline; }
nav { display: flex; flex-wrap: wrap; gap: .6rem; }
nav a + a::before { content: "/"; display: inline-block; margin-right: .6rem; color: gray; }
h1 { font-size: 1.6rem; overflow-wrap: anywhere; }
.tools { display: flex; flex-wrap: wrap; align-items: center; gap: 1rem; margin: 1.5rem 0; }
input, select { font: inherit; padding: .5rem; border: 1px solid gray; border-radius: .4rem; }
input { max-width: 100%; box-sizing: border-box; }
.table-wrap { overflow-x: auto; border: 1px solid gray; border-radius: .5rem; }
table { width: 100%; border-collapse: collapse; text-align: left; }
th, td { padding: .7rem 1rem; border-bottom: 1px solid light-dark(#ddd, #444); }
th { font-size: .85rem; color: light-dark(#555, #bbb); }
td:first-child { width: 100%; min-width: 12rem; overflow-wrap: anywhere; }
td:not(:first-child) { white-space: nowrap; }
.number { text-align: right; }
tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: light-dark(#f1f5fa, #232b35); }
#count { color: light-dark(#555, #bbb); }
@media (max-width: 36rem) { body { padding: 1rem; } th, td { padding: .6rem; } }
</style></head><body>
<nav aria-label="Folders">"""
        + " ".join(crumbs)
        + """</nav>
<h1>"""
        + escape(title)
        + """</h1>
<div class="tools"><label>Filter files <input id="filter" type="search" placeholder="File or folder name"></label>
<label>Sort <select id="sort"><option value="name">Name</option><option value="size">Size</option>
<option value="modified">Modified</option></select></label>
<span id="count" role="status">"""
        + str(len(rows))
        + """ items</span></div>
"""
        + empty
        + """<div class="table-wrap"><table>
<thead><tr><th scope="col">Name</th><th scope="col">Type</th>
<th scope="col" class="number">Size</th><th scope="col">Modified (UTC)</th></tr></thead>
<tbody>"""
        + "".join(rows)
        + """</tbody></table></div>
<p id="no-matches" hidden>No matching files.</p>
<script>
(() => {
const body = document.querySelector("tbody");
const rows = [...body.rows];
const filter = document.getElementById("filter");
const sort = document.getElementById("sort");
function update() {
    const query = filter.value.toLocaleLowerCase();
    const key = sort.value;
    rows.sort((a, b) => {
        const folders = (b.cells[1].textContent === "Folder") - (a.cells[1].textContent === "Folder");
        return folders || (key === "name"
            ? a.cells[0].textContent.localeCompare(b.cells[0].textContent, undefined, {numeric: true})
            : Number(b.dataset[key]) - Number(a.dataset[key]));
    });
    let count = 0;
    for (const row of rows) {
        row.hidden = !row.cells[0].textContent.toLocaleLowerCase().includes(query);
        if (!row.hidden) count++;
        body.appendChild(row);
    }
    document.getElementById("count").textContent = `${count} of ${rows.length} items`;
    document.getElementById("no-matches").hidden = count !== 0 || rows.length === 0;
}
filter.addEventListener("input", update);
sort.addEventListener("change", update);
})();
</script></body></html>"""
    ).encode("utf-8")
