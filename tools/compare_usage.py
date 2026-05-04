"""Cross-backend cost / token comparison report.

For each (backend, instance_id) found under ./work/, computes the same
usage stats that ClaudeAppMapInterface writes to <run_dir>/usage.json:
  - Reads usage.json directly when present (claude-appmap runs).
  - Otherwise scans ~/.claude/projects/ for session JSONLs whose recorded
    `cwd` matches the run's clone dir, aggregates them, and writes a fresh
    usage.json so the file shape is identical across backends.

Usage:
  ./venv/bin/python tools/compare_usage.py [--instance INSTANCE_ID ...]
                                           [--latest-only]
                                           [--csv]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils.claude_appmap_interface import ClaudeAppMapInterface

WORK_ROOT = REPO_ROOT / "work"


def discover_runs(instances_filter, latest_only):
    """Yield (backend, instance_id, run_dir) tuples for each completed run."""
    for backend_dir in sorted(WORK_ROOT.glob("*")):
        if not backend_dir.is_dir():
            continue
        backend = backend_dir.name
        # Skip the legacy <id>/<ts> layout: those have no backend dir
        # (their direct children are timestamps, not instance ids). The new
        # layout has django__django-XXX as direct children.
        if not any(child.is_dir() and "__" in child.name for child in backend_dir.iterdir()):
            continue
        for inst_dir in sorted(backend_dir.iterdir()):
            if not inst_dir.is_dir():
                continue
            instance_id = inst_dir.name
            if instances_filter and instance_id not in instances_filter:
                continue
            run_dirs = sorted(d for d in inst_dir.iterdir() if d.is_dir())
            if not run_dirs:
                continue
            picked = [run_dirs[-1]] if latest_only else run_dirs
            for run_dir in picked:
                yield backend, instance_id, run_dir


def ensure_usage_json(run_dir: Path, backend: str, instance_id: str) -> dict | None:
    """Return parsed usage.json content, computing it from session JSONLs if absent."""
    usage_path = run_dir / "usage.json"
    if usage_path.exists():
        try:
            return json.loads(usage_path.read_text())
        except Exception:
            pass

    # Compute from ~/.claude/projects/ session JSONLs whose cwd matches the clone.
    clone = run_dir / "repo"
    needle = f'"cwd":"{clone}"'
    projects = Path.home() / ".claude" / "projects"
    matching = []
    for jsonl in projects.glob("*/*.jsonl"):
        try:
            head = jsonl.read_text(errors="ignore")[:32768]
        except Exception:
            continue
        if needle in head:
            matching.append(jsonl)
    if not matching:
        return None

    iface = ClaudeAppMapInterface.__new__(ClaudeAppMapInterface)
    per_model, first_ts, last_ts = iface._aggregate_usage(matching)
    iface._write_usage_json(per_model, first_ts, last_ts, run_dir,
                            instance_id=instance_id, partial=False)
    return json.loads(usage_path.read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", action="append", default=None,
                    help="Filter to specific instance_id(s); repeatable")
    ap.add_argument("--latest-only", action="store_true",
                    help="Only show the most recent timestamp per (backend, instance)")
    ap.add_argument("--csv", action="store_true",
                    help="Emit CSV instead of a fixed-width table")
    args = ap.parse_args()

    rows = []
    for backend, instance_id, run_dir in discover_runs(args.instance, args.latest_only):
        usage = ensure_usage_json(run_dir, backend, instance_id)
        if usage is None:
            continue
        totals = usage.get("totals", {})
        # Single-model assumption (the typical case); aggregate all if multi-model.
        rows.append({
            "backend": backend,
            "instance_id": instance_id,
            "run": run_dir.name,
            "wall_s": usage.get("wall_seconds") or 0,
            "messages": sum(m.get("messages", 0) for m in usage.get("models", {}).values()),
            "in": totals.get("input_tokens", 0),
            "out": totals.get("output_tokens", 0),
            "cache_r": totals.get("cache_read_input_tokens", 0),
            "cache_w": totals.get("cache_creation_input_tokens", 0),
            "cost_usd": totals.get("estimated_cost_usd", 0.0),
        })

    if args.csv:
        import csv
        w = csv.writer(sys.stdout)
        w.writerow(["backend", "instance_id", "run", "wall_s", "messages",
                    "in", "out", "cache_r", "cache_w", "cost_usd"])
        for r in rows:
            w.writerow([r[k] for k in ("backend", "instance_id", "run", "wall_s",
                                        "messages", "in", "out", "cache_r",
                                        "cache_w", "cost_usd")])
        return

    if not rows:
        print("No runs found under ./work/", file=sys.stderr)
        sys.exit(1)

    rows.sort(key=lambda r: (r["instance_id"], r["backend"], r["run"]))
    fmt = "{backend:14} {instance_id:30} {run:18} {wall_s:>7.0f}s {messages:>5}  in={in:>7,}  out={out:>8,}  cache_r={cache_r:>11,}  ${cost_usd:>8.4f}"
    print(fmt.format(backend="BACKEND", instance_id="INSTANCE", run="RUN",
                     wall_s=0, messages=0, **{"in": 0, "out": 0,
                                              "cache_r": 0, "cost_usd": 0.0})
          .replace("0s", "WALL").replace("    0", "MSGS")
          .replace("in=      0", "  INPUT")
          .replace("out=       0", "  OUTPUT")
          .replace("cache_r=          0", "  CACHE_READ")
          .replace("$  0.0000", "    COST"))
    for r in rows:
        print(fmt.format(**r))

    # Per-instance comparison summary
    print("\n— per-instance comparison —")
    by_inst = {}
    for r in rows:
        by_inst.setdefault(r["instance_id"], []).append(r)
    for inst, group in sorted(by_inst.items()):
        if len(group) < 2:
            continue
        backends = {r["backend"]: r for r in group}
        if "claude" in backends and "claude-appmap" in backends:
            v = backends["claude"]
            a = backends["claude-appmap"]
            ratio_cost = a["cost_usd"] / v["cost_usd"] if v["cost_usd"] else float("inf")
            ratio_wall = a["wall_s"] / v["wall_s"] if v["wall_s"] else float("inf")
            print(f"  {inst}:")
            print(f"    cost   claude=${v['cost_usd']:.4f}   claude-appmap=${a['cost_usd']:.4f}   ratio={ratio_cost:.2f}x")
            print(f"    wall   claude={v['wall_s']:.0f}s    claude-appmap={a['wall_s']:.0f}s    ratio={ratio_wall:.2f}x")


if __name__ == "__main__":
    main()
