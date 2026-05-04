"""Estimate RCA / code-fix / verify cost for any backend.

For 3-step backends the breakdown is structural — each phase already
lives under its own ``step{1,2,3}_*/`` dir.

For single-session backends (vanilla, mcp) we apply a heuristic:
the agent's RCA phase ends when it makes its first ``Edit`` or
``Write`` to a source file. Everything before (and including) that
assistant message is RCA cost; everything after is fix + verify.

Source file = anything not in {bin/, .claude/, appmap.yml, .mcp.json,
.gitignore, issue.md, CLAUDE.md}.

Usage:
  ./venv/bin/python tools/rca_breakdown.py <run_dir> [<run_dir> ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils.claude_session import estimate_cost, load_pricing  # noqa: E402

# Files we treat as scaffolding (not "real" source edits).
SCAFFOLDING_PREFIXES = (
    "bin/", ".claude/", "tmp/appmap/", "appmap.yml", ".mcp.json",
    ".gitignore", "issue.md", "CLAUDE.md",
)


def _is_source_file(path: str) -> bool:
    if not path:
        return False
    p = path.lstrip("./")
    return not any(p.startswith(pref) for pref in SCAFFOLDING_PREFIXES)


def _empty_bucket() -> Dict[str, int]:
    return {
        "messages": 0,
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
    }


def _add_to(bucket: Dict[str, int], usage: Dict[str, int]) -> None:
    bucket["messages"] += 1
    for k in ("input_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens", "output_tokens"):
        v = usage.get(k, 0)
        if isinstance(v, int):
            bucket[k] += v


def _per_model_cost(per_model: Dict[str, Dict[str, int]]) -> float:
    pricing = load_pricing()
    return sum(estimate_cost(m, b, pricing) for m, b in per_model.items())


def _scan_session_jsonl(path: Path) -> Tuple[
        List[Tuple[Optional[str], Dict[str, int], List[Tuple[str, str]]]], ]:
    """Return list of (model, usage, [(tool_name, target_path)]) per
    assistant message in chronological order.
    """
    messages: List[Tuple[Optional[str], Dict[str, int], List[Tuple[str, str]]]] = []
    with path.open(errors="ignore") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message", {}) or {}
            usage = msg.get("usage") or {}
            model = msg.get("model")
            tool_uses: List[Tuple[str, str]] = []
            for item in msg.get("content", []) or []:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                name = item.get("name", "")
                inp = item.get("input", {}) or {}
                target = inp.get("file_path") or inp.get("path") or ""
                tool_uses.append((name, str(target)))
            messages.append((model, usage, tool_uses))
    return (messages,)


def _split_at_first_source_edit(
    messages: List[Tuple[Optional[str], Dict[str, int], List[Tuple[str, str]]]],
) -> Tuple[Dict[str, Dict[str, int]],
           Dict[str, Dict[str, int]],
           Optional[int]]:
    """Returns (rca_per_model, post_per_model, split_idx)."""
    rca: Dict[str, Dict[str, int]] = {}
    post: Dict[str, Dict[str, int]] = {}
    split_idx: Optional[int] = None

    for idx, (model, usage, tool_uses) in enumerate(messages):
        target_bucket = post if split_idx is not None else rca
        if model:
            _add_to(target_bucket.setdefault(model, _empty_bucket()), usage)
        if split_idx is None:
            for name, path in tool_uses:
                if name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and _is_source_file(path):
                    split_idx = idx
                    break
    return rca, post, split_idx


def _walk_session_files(d: Path) -> Iterable[Path]:
    sess = d / "sessions"
    if sess.is_dir():
        yield from sorted(sess.glob("*.jsonl"))
        sub = sess / "subagents"
        if sub.is_dir():
            yield from sorted(sub.glob("*.jsonl"))


def _aggregate_dir(d: Path) -> Dict[str, Dict[str, int]]:
    """Sum usage across every session jsonl under d/sessions/ (incl subagents)."""
    out: Dict[str, Dict[str, int]] = {}
    for jsonl in _walk_session_files(d):
        for model, usage, _ in _scan_session_jsonl(jsonl)[0]:
            if not model:
                continue
            _add_to(out.setdefault(model, _empty_bucket()), usage)
    return out


def _aggregate_step(run_dir: Path, step_dir: Path,
                     subagent_type: Optional[str]) -> Dict[str, Dict[str, int]]:
    """Sum usage for one 3-step phase. Maps step_dir → its parent session
    jsonl via the symlink, then adds the subagent jsonl whose meta.json
    matches `subagent_type` if any."""
    out: Dict[str, Dict[str, int]] = {}
    sym = step_dir / "session.jsonl"
    if not sym.is_symlink():
        return out
    parent_basename = Path(sym.resolve()).name
    parent_path = run_dir / "sessions" / parent_basename
    if parent_path.is_file():
        for model, usage, _ in _scan_session_jsonl(parent_path)[0]:
            if model:
                _add_to(out.setdefault(model, _empty_bucket()), usage)
    if subagent_type:
        sub_dir = run_dir / "sessions" / "subagents"
        if sub_dir.is_dir():
            for meta in sub_dir.glob("*.meta.json"):
                try:
                    info = json.loads(meta.read_text())
                except Exception:
                    continue
                if info.get("agentType") != subagent_type:
                    continue
                jsonl = sub_dir / (meta.stem.replace(".meta", "") + ".jsonl")
                if jsonl.is_file():
                    for model, usage, _ in _scan_session_jsonl(jsonl)[0]:
                        if model:
                            _add_to(out.setdefault(model, _empty_bucket()), usage)
    return out


def _format_cost(per_model: Dict[str, Dict[str, int]]) -> str:
    cost = _per_model_cost(per_model)
    msgs = sum(b["messages"] for b in per_model.values())
    out_tok = sum(b["output_tokens"] for b in per_model.values())
    return f"${cost:>7.4f}  ({msgs:>3d} msgs, {out_tok:>7,d} out)"


def analyze(run_dir: Path) -> None:
    backend = run_dir.parent.parent.name
    instance = run_dir.parent.name
    label = f"{backend} / {instance} / {run_dir.name}"
    print(f"\n=== {label} ===")

    is_3step = (run_dir / "step1_rca").is_dir()

    if is_3step:
        for step_name, label2, sub_type in (
            ("step1_rca", "RCA       ", "appmap-rca"),
            ("step2_codefix", "code-fix  ", None),
            ("step3_verify", "verify    ", "appmap-verify"),
        ):
            step_dir = run_dir / step_name
            if not step_dir.is_dir():
                print(f"  {label2}  (missing dir {step_name})")
                continue
            per_model = _aggregate_step(run_dir, step_dir, sub_type)
            print(f"  {label2}  {_format_cost(per_model)}")
        total = _aggregate_dir(run_dir)
        print(f"  TOTAL     {_format_cost(total)}")
    else:
        # Single-session: walk parent + subagent jsonl in chronological order
        # but treat the parent's first source-edit as the RCA cutoff. Subagent
        # tokens are attributed to whichever phase the parent was in when it
        # dispatched the subagent.
        all_messages: List[Tuple[Optional[str], Dict[str, int], List[Tuple[str, str]]]] = []
        for jsonl in _walk_session_files(run_dir):
            all_messages.extend(_scan_session_jsonl(jsonl)[0])
        if not all_messages:
            print("  (no session jsonl found)")
            return
        rca, post, split_idx = _split_at_first_source_edit(all_messages)
        if split_idx is None:
            print(f"  RCA       {_format_cost(rca)}")
            print(f"  code-fix  $0.0000  (agent never edited a source file)")
        else:
            print(f"  RCA       {_format_cost(rca)}   "
                  f"(split at message #{split_idx + 1} of {len(all_messages)})")
            print(f"  post-RCA  {_format_cost(post)}   "
                  f"(code-fix + verify, single session)")
        total: Dict[str, Dict[str, int]] = {}
        for d in (rca, post):
            for m, b in d.items():
                tgt = total.setdefault(m, _empty_bucket())
                for k, v in b.items():
                    tgt[k] += v
        print(f"  TOTAL     {_format_cost(total)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+", type=Path,
                    help="Per-run directories under work/<backend>/<instance>/<ts>/")
    args = ap.parse_args()
    for d in args.run_dirs:
        if not d.is_dir():
            print(f"missing: {d}", file=sys.stderr)
            continue
        analyze(d)


if __name__ == "__main__":
    main()
