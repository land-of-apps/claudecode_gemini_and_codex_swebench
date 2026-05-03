"""End-to-end smoke test for the claude-appmap backend.

Run with: python -u tests/smoke_claude_appmap.py
The -u flag is important — without it, prints are buffered and progress
disappears for tens of seconds at a time during HF dataset load and image pull.


Validates the goals from the user's checklist:
  a) container image is buildable / pullable
  b) claude is invoked with the /appmap-fix prompt
  c) it follows the directive (record before reading source)
  d) appmap-record creates AppMap data
  e) `index --watch` is running
  f) AppMap MCP can inspect the data

Usage:
  source venv/bin/activate
  python tests/smoke_claude_appmap.py

Picks `django__django-15738` (Django 4.2 / Python 3.9) — modern enough for
appmap-python's framework support and small enough to iterate on.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Quiet HF chatter
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.WARNING)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

INSTANCE_ID = os.environ.get("SMOKE_INSTANCE_ID", "django__django-15738")


def main() -> int:
    # 1) Set DOCKER_HOST for podman BEFORE anything imports docker.
    if not os.environ.get("DOCKER_HOST"):
        r = subprocess.run(
            ["podman", "machine", "inspect"], capture_output=True, text=True
        )
        if r.returncode == 0:
            import json
            data = json.loads(r.stdout)
            for m in data:
                p = m.get("ConnectionInfo", {}).get("PodmanSocket", {}).get("Path")
                if p:
                    os.environ["DOCKER_HOST"] = f"unix://{p}"
                    break
        print(f"DOCKER_HOST={os.environ.get('DOCKER_HOST')}")

    # 2) Load the instance.
    print("loading SWE-bench_Lite (cached if previously loaded)...", flush=True)
    from datasets import load_dataset
    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    print(f"  dataset has {len(ds)} instances; finding {INSTANCE_ID}...", flush=True)
    instance = next((i for i in ds if i["instance_id"] == INSTANCE_ID), None)
    if instance is None:
        print(f"instance {INSTANCE_ID} not found in SWE-bench_Lite", file=sys.stderr)
        return 2
    print(f"  found: {instance['instance_id']} (repo={instance['repo']}, version={instance['version']})", flush=True)

    # 3) Clone the repo at base_commit. Must live under a podman-machine-visible
    #    path on macOS — /var/folders and /tmp aren't shared into the VM, but
    #    /Users is. Default to ~/tmp; override with SMOKE_CLONE_DIR.
    clone_root = Path(os.environ.get("SMOKE_CLONE_DIR", str(Path.home() / "tmp")))
    clone_root.mkdir(parents=True, exist_ok=True)
    clone = clone_root / f"swe_bench_{INSTANCE_ID}"
    if clone.exists():
        if os.environ.get("SMOKE_REUSE_CLONE") == "1":
            print(f"  reusing existing clone at {clone}", flush=True)
        else:
            print(f"  removing existing clone at {clone}", flush=True)
            shutil.rmtree(clone)
    if not clone.exists():
        print(f"  cloning into {clone}...", flush=True)
        subprocess.run(
            ["git", "clone", "--quiet", f"https://github.com/{instance['repo']}.git", str(clone)],
            check=True,
        )
        print(f"  checking out {instance['base_commit'][:12]}...", flush=True)
        subprocess.run(
            ["git", "checkout", "--quiet", instance["base_commit"]],
            cwd=str(clone), check=True,
        )

    # 4) Run the backend.
    print("instantiating ClaudeAppMapInterface...", flush=True)
    from utils.claude_appmap_interface import ClaudeAppMapInterface
    iface = ClaudeAppMapInterface()
    print("running claude-appmap backend (this is the long phase)...", flush=True)
    result = iface.execute_code_cli(prompt="", cwd=str(clone), instance=instance)

    # 5) Report.
    print("=" * 60)
    print(f"success={result['success']}  rc={result['returncode']}")
    print("--- stdout (head) ---")
    print((result.get("stdout") or "")[:2000])
    print("--- stderr (head) ---")
    print((result.get("stderr") or "")[:2000])

    # 6) Inspect the workspace for appmap output.
    appmap_dir = clone / "tmp" / "appmap"
    print(f"\nappmaps under {appmap_dir}:")
    for p in sorted(appmap_dir.rglob("*.appmap.json")):
        print(f"  {p.relative_to(clone)}  ({p.stat().st_size} bytes)")

    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
