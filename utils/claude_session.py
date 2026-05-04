"""Shared session/cost/output machinery used by both `claude` and
`claude-appmap` backends.

Each backend's `execute_code_cli` typically:
  1. Computes a run_dir via `resolve_run_dir(cwd, instance_id)`.
  2. Generates a session_id, passes `--session-id <uuid>` to claude.
  3. Calls `start_live_log(session_id, run_dir)` so session.jsonl symlink
     and a human-readable console.log appear in run_dir as claude writes.
  4. After claude exits: `archive_session_logs(...)` and
     `compute_and_save_usage(...)` to leave the run dir self-contained
     for analysis.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVES_DIR = Path(
    os.environ.get("APPMAP_ARCHIVES_DIR", str(REPO_ROOT / "appmap_archives"))
)

INDEX_DB_RE = re.compile(r"/\.appmap/data/([0-9a-f]+)/query\.db")

# Anthropic public list pricing as of 2026-05, USD per 1M tokens.
# Override via APPMAP_PRICING_JSON.
PRICING_PER_MTOK = {
    "claude-opus-4":     {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4":   {"input":  3.0, "output": 15.0, "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4":    {"input":  1.0, "output":  5.0, "cache_read": 0.10, "cache_write":  1.25},
}


# ---------- run dir resolution ----------

def resolve_run_dir(clone: Path, instance_id: str) -> Path:
    """Return the per-run directory holding outputs for this run.

    Recognized layouts (newest first):
      ./work/<backend>/<id>/<ts>/repo   — current orchestrator
      ./work/<id>/<ts>/repo             — legacy

    Walks up from `clone` looking for a parent named "work". Falls back
    to <repo_root>/appmap_archives/<id>_<ts>/ for smoke/ad-hoc clones."""
    run_dir = clone.parent
    for ancestor in run_dir.parents:
        if ancestor.name == "work":
            return run_dir
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fallback = ARCHIVES_DIR / f"{instance_id}_{ts}"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


# ---------- live session log + console mirror ----------

def start_live_log(session_id: str, run_dir: Path) -> threading.Event:
    """Symlink the claude session JSONL into run_dir as soon as it appears,
    and stream a human-readable console.log of tool calls + assistant text
    in parallel. Also flushes a partial usage.json snapshot every ~5s.

    Returns a stop Event the caller sets when claude exits."""
    stop = threading.Event()

    def watcher() -> None:
        projects = Path.home() / ".claude" / "projects"
        symlink = run_dir / "session.jsonl"
        console = run_dir / "console.log"
        src: Optional[Path] = None
        deadline = time.time() + 60
        while not stop.is_set() and src is None and time.time() < deadline:
            matches = list(projects.glob(f"*/{session_id}.jsonl"))
            if matches:
                src = matches[0]
                break
            time.sleep(0.5)
        if src is None:
            return
        try:
            if symlink.exists() or symlink.is_symlink():
                symlink.unlink()
            symlink.symlink_to(src)
        except Exception as e:
            print(f"  warning: session symlink failed: {e}", flush=True)
        try:
            last_usage_flush = 0.0
            with src.open("r", errors="ignore") as f, console.open("a") as out:
                out.write(f"# session_id={session_id}\n# source={src}\n")
                out.flush()
                buf = ""
                while not stop.is_set():
                    chunk = f.read()
                    if chunk:
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            formatted = format_session_line(line)
                            if formatted:
                                out.write(formatted + "\n")
                                out.flush()
                    else:
                        time.sleep(0.5)
                    now = time.time()
                    if now - last_usage_flush >= 5.0:
                        last_usage_flush = now
                        try:
                            _flush_live_usage(src, run_dir)
                        except Exception:
                            pass
        except Exception as e:
            print(f"  warning: live log writer stopped: {e}", flush=True)

    threading.Thread(target=watcher, daemon=True).start()
    return stop


def format_session_line(line: str) -> Optional[str]:
    """Turn one JSONL record into 0+ human-readable lines.

    No truncation — session.jsonl already has the raw form, console.log
    matches it. Multi-line tool inputs / results wrap with a continuation
    indent so the timestamp column stays aligned."""
    try:
        d = json.loads(line)
    except Exception:
        return None
    ts = (d.get("timestamp") or "")[11:19]
    msg = d.get("message", {})
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return None
        return _wrap_lines(f"[{ts}] {msg.get('role','?').upper()}: ", text)
    if not isinstance(content, list):
        return None
    out: List[str] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "tool_use":
            name = c.get("name", "?")
            summary = _summarize_tool_input(name, c.get("input", {}))
            out.append(_wrap_lines(f"[{ts}] → {name}(", summary, suffix=")"))
        elif t == "text":
            text = (c.get("text") or "").strip()
            if text:
                out.append(_wrap_lines(f"[{ts}]   ", text))
        elif t == "tool_result":
            o = c.get("content", "")
            if isinstance(o, list):
                o = (o[0] or {}).get("text", "") if o else ""
            o = str(o).strip()
            if o:
                out.append(_wrap_lines(f"[{ts}]   ← ", o))
    return "\n".join(out) if out else None


def _wrap_lines(prefix: str, body: str, suffix: str = "") -> str:
    lines = body.splitlines() or [""]
    indent = " " * len(prefix)
    first = prefix + lines[0]
    if len(lines) == 1:
        return first + suffix
    rest = [indent + line for line in lines[1:]]
    if suffix:
        rest[-1] = rest[-1] + suffix
    return "\n".join([first, *rest])


def _summarize_tool_input(name: str, inp: Dict) -> str:
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Edit", "Write"):
        base = inp.get("file_path", "")
        extras = []
        for k in ("offset", "limit", "old_string", "new_string", "content"):
            v = inp.get(k)
            if v is not None:
                extras.append(f"{k}={v!r}" if isinstance(v, (int, str)) and len(str(v)) < 60
                              else f"{k}=<{len(str(v))} chars>")
        return base + (" " + ", ".join(extras) if extras else "")
    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        return f"{cmd}" + (f"  # {desc}" if desc else "")
    if name == "Grep":
        return f"{inp.get('pattern','')}" + (
            f" in {inp.get('path','')}" if inp.get("path") else "")
    if name == "Glob":
        return inp.get("pattern", "")
    if name.startswith("mcp__"):
        return ", ".join(f"{k}={json.dumps(v) if not isinstance(v, str) else v!r}"
                         for k, v in inp.items())
    return json.dumps(inp, ensure_ascii=False)


# ---------- session log archiving ----------

def archive_session_logs(clone: Path, run_dir: Path) -> None:
    """Copy claude session JSONLs whose `cwd` matches this clone into
    <run_dir>/sessions/. Belt-and-suspenders for the live symlink: if
    claude rotated sessions or the symlink target gets cleaned, the
    archived copies still survive.

    Subagent sessions live under <project>/<parent_session_id>/subagents/
    *.jsonl and have their own usage blocks — they're billed but invisible
    to the parent. We pick those up too and store under sessions/subagents/."""
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return
    needle = f'"cwd":"{clone}"'
    target_root = run_dir / "sessions"
    copied: List[str] = []
    parent_files = list(projects.glob("*/*.jsonl"))
    for jsonl in parent_files:
        try:
            head = jsonl.read_text(errors="ignore")[:32768]
        except Exception:
            continue
        if needle in head:
            target_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(jsonl, target_root / jsonl.name)
            copied.append(jsonl.name)
            # Pick up any subagent sessions spawned by this parent.
            subagent_dir = jsonl.parent / jsonl.stem / "subagents"
            if subagent_dir.is_dir():
                sub_target = target_root / "subagents"
                sub_target.mkdir(exist_ok=True)
                for sub in subagent_dir.glob("*.jsonl"):
                    shutil.copy2(sub, sub_target / sub.name)
                    copied.append(f"subagents/{sub.name}")
                # also the .meta.json sidecars (small, useful)
                for meta in subagent_dir.glob("*.meta.json"):
                    shutil.copy2(meta, sub_target / meta.name)
    if copied:
        print(f"  archived {len(copied)} session log(s) → {target_root}", flush=True)


# ---------- usage / cost ----------

def compute_and_save_usage(run_dir: Path, instance_id: str) -> None:
    """Aggregate per-message `usage` blocks from the archived session logs
    and write <run_dir>/usage.json with token totals + estimated $ cost.

    Includes parent + subagent sessions (subagents/*.jsonl) so the
    reported cost matches what's actually billed."""
    sessions_dir = run_dir / "sessions"
    if not sessions_dir.is_dir():
        return
    files = sorted(sessions_dir.glob("*.jsonl"))
    files += sorted((sessions_dir / "subagents").glob("*.jsonl"))
    per_model, first_ts, last_ts = aggregate_usage(files)
    write_usage_json(per_model, first_ts, last_ts, run_dir,
                     instance_id=instance_id, partial=False)


def _flush_live_usage(src: Path, run_dir: Path) -> None:
    per_model, first_ts, last_ts = aggregate_usage([src])
    write_usage_json(per_model, first_ts, last_ts, run_dir,
                     instance_id=run_dir.parent.name, partial=True)


def aggregate_usage(jsonls):
    per_model: Dict[str, Dict[str, int]] = {}
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    for jsonl in jsonls:
        try:
            fh = jsonl.open(errors="ignore")
        except Exception:
            continue
        with fh as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = d.get("timestamp")
                if isinstance(ts, str):
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
                msg = d.get("message", {}) or {}
                if not isinstance(msg, dict):
                    continue
                model = msg.get("model")
                usage = msg.get("usage")
                if not (model and isinstance(usage, dict)):
                    continue
                bucket = per_model.setdefault(model, {
                    "messages": 0,
                    "input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                })
                bucket["messages"] += 1
                for k in ("input_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens", "output_tokens"):
                    v = usage.get(k, 0)
                    if isinstance(v, int):
                        bucket[k] += v
    return per_model, first_ts, last_ts


def write_usage_json(per_model, first_ts, last_ts, run_dir, instance_id, partial):
    pricing = load_pricing()
    models_out: Dict[str, Dict[str, object]] = {}
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0,
              "estimated_cost_usd": 0.0}
    for model, b in per_model.items():
        cost = estimate_cost(model, b, pricing)
        models_out[model] = {**b, "estimated_cost_usd": round(cost, 6)}
        for k in ("input_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens", "output_tokens"):
            totals[k] += b[k]
        totals["estimated_cost_usd"] += cost
    totals["estimated_cost_usd"] = round(totals["estimated_cost_usd"], 6)

    wall_seconds: Optional[float] = None
    if first_ts and last_ts:
        try:
            wall_seconds = (datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                            - datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                            ).total_seconds()
        except Exception:
            pass

    out = {
        "instance_id": instance_id,
        "partial": partial,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "wall_seconds": wall_seconds,
        "models": models_out,
        "totals": totals,
        "pricing_source": ("env:APPMAP_PRICING_JSON" if os.environ.get("APPMAP_PRICING_JSON")
                           else "builtin: see PRICING_PER_MTOK"),
    }
    (run_dir / "usage.json").write_text(json.dumps(out, indent=2))
    if not partial:
        print(
            f"  usage: {totals['input_tokens']:,} in, "
            f"{totals['output_tokens']:,} out, "
            f"{totals['cache_read_input_tokens']:,} cache-read; "
            f"~${totals['estimated_cost_usd']:.4f}",
            flush=True,
        )


def load_pricing() -> Dict[str, Dict[str, float]]:
    override = os.environ.get("APPMAP_PRICING_JSON")
    if override and Path(override).is_file():
        try:
            return json.loads(Path(override).read_text())
        except Exception:
            pass
    return PRICING_PER_MTOK


def estimate_cost(model: str, usage: Dict[str, int],
                  pricing: Dict[str, Dict[str, float]]) -> float:
    rates = None
    for family, r in pricing.items():
        if model.startswith(family):
            rates = r
            break
    if not rates:
        return 0.0
    per_tok = lambda key: rates.get(key, 0.0) / 1_000_000.0
    return (
        usage.get("input_tokens", 0)                  * per_tok("input")
        + usage.get("output_tokens", 0)               * per_tok("output")
        + usage.get("cache_read_input_tokens", 0)     * per_tok("cache_read")
        + usage.get("cache_creation_input_tokens", 0) * per_tok("cache_write")
    )
