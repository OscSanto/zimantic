"""How fast does this machine build an index? Prints a Markdown table row to paste into the PR / docs.

Runs the real build loop (read the ZIM + embed) on a slice of one ZIM, without writing an index.
Run it from the project folder, with config.yaml in place:

    python scripts/bench_embed.py wikipedia_en_top_nopic_2026-09
    python scripts/bench_embed.py wikipedia_en_top_nopic_2026-09 --workers 3 --batch 8 --order id

Settings default to config.yaml (with "auto" filled in, like a real build); flags override them.
"""
import argparse
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import yaml
from libzim.reader import Archive

from zimantic.build import build_settings, embed_windows
from zimantic.embed import Embedder
from zimantic.hardware import GB, detect
from zimantic.zim import cluster_order

COLUMNS = ["Hardware", "Cores / threads", "RAM", "OS", "Workers x threads", "Batch", "Order",
           "Articles/s", "Peak RAM", "Full-ZIM embed time (est.)", "ZIM"]


def peak_ram() -> int | None:
    """Most memory this process has held (bytes)."""
    if sys.platform == "win32":
        class Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + \
                       [(name, ctypes.c_size_t) for name in ("peak_working_set", "working_set", "peak_paged",
                        "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
        counters = Counters(cb=ctypes.sizeof(Counters))
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p  # a handle is pointer-sized, not a 32-bit int
        kernel32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
        if kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.peak_working_set
        return None
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # macOS reports bytes, Linux kB


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("zim", help="ZIM name in zim_dir (or a path to a .zim file)")
    parser.add_argument("--entries", type=int, default=40_000, help="entries to read (default 40,000)")
    parser.add_argument("--workers", type=int, help="embedding_workers (default: config.yaml)")
    parser.add_argument("--threads", type=int, help="embed_threads (default: config.yaml)")
    parser.add_argument("--batch", type=int, help="batch_size (default: config.yaml)")
    parser.add_argument("--order", choices=["cluster", "id"], default="cluster", help="reading order (default cluster)")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    for key, value in (("embedding_workers", args.workers), ("embed_threads", args.threads), ("batch_size", args.batch)):
        if value is not None:
            cfg[key] = value
    hw = detect()
    workers, threads, batch_size = build_settings(cfg, hw)

    path = Path(args.zim) if args.zim.endswith(".zim") else Path(cfg["zim_dir"]) / f"{args.zim}.zim"
    zim = Archive(str(path))
    # A slice of entry ids from a third of the way in (the very start of a ZIM is often unusual pages).
    # Both orders read the same entries; "cluster" reads them in the order they are stored in the file.
    start = zim.entry_count // 3
    stop = min(zim.entry_count, start + args.entries)
    ids = np.arange(start, stop, dtype=np.uint32)
    order = cluster_order(path, zim.entry_count) if args.order == "cluster" else None
    order_name = args.order if order is not None or args.order == "id" else "id (cluster order unavailable)"
    if order is not None:
        stored_at = np.empty_like(order)
        stored_at[order] = np.arange(len(order), dtype=np.uint32)  # entry id -> its place in storage order
        ids = ids[np.argsort(stored_at[ids], kind="stable")]

    embedder = Embedder(cfg["model_dir"], threads=threads or None)
    embedder.embed(["passage: warm up"])
    print(f"hardware: {hw.describe()}", file=sys.stderr)
    print(f"reading entries {start:,}-{stop:,} of {zim.entry_count:,}, {workers} workers x "
          f"{threads or 'default'} threads, batch {batch_size}, {order_name} order", file=sys.stderr)

    began = time.perf_counter()
    articles = sum(len(vectors) for _, vectors, _ in
                   embed_windows(zim, ids, 0, len(ids), embedder, batch_size, workers))
    seconds = time.perf_counter() - began
    full = seconds * zim.entry_count / (stop - start)  # assumes the slice is typical of the whole ZIM
    peak = peak_ram()

    row = [hw.device or hw.cpu or hw.machine, f"{hw.cores} / {hw.threads}", f"{hw.ram_total / GB:.1f} GB",
           f"{hw.system} {hw.machine}", f"{workers} x {threads or 'default'}", str(batch_size), order_name,
           f"{articles / seconds:.1f}", f"{peak / GB:.2f} GB" if peak else "?",
           f"{full / 60:.0f} min" if full < 5400 else f"{full / 3600:.1f} h", path.stem]
    print(f"\n{articles:,} articles embedded in {seconds:.0f} s. Paste this row into the results table:\n",
          file=sys.stderr)
    print("| " + " | ".join(COLUMNS) + " |")
    print("|" + "---|" * len(COLUMNS))
    print("| " + " | ".join(row) + " |")


if __name__ == "__main__":
    main()
