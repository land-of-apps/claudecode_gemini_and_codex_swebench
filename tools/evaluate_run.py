"""Run the SWE-bench harness against a single run's prediction.jsonl
and write the eval result back into the run dir.

Usage:
  ./venv/bin/python tools/evaluate_run.py <run_dir> [--dataset NAME]

Where <run_dir> is like:
  work/<backend>/<instance_id>/<timestamp>/

Outputs:
  <run_dir>/evaluation/             — swebench harness logs/results
  <run_dir>/evaluation_result.json  — flat summary (resolved? errors? duration)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATASET = "princeton-nlp/SWE-bench_Verified"


def discover_docker_host() -> str | None:
    if os.environ.get("DOCKER_HOST"):
        return os.environ["DOCKER_HOST"]
    try:
        r = subprocess.run(["podman", "machine", "inspect"],
                           capture_output=True, text=True, check=True)
        for m in json.loads(r.stdout):
            p = m.get("ConnectionInfo", {}).get("PodmanSocket", {}).get("Path")
            if p:
                return f"unix://{p}"
    except Exception:
        return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--namespace", default="swebench",
                    help="Image namespace (default 'swebench' = pull prebuilt)")
    args = ap.parse_args()

    run_dir = args.run_dir.resolve()
    pred_path = run_dir / "prediction.jsonl"
    if not pred_path.is_file():
        print(f"no prediction.jsonl in {run_dir}", file=sys.stderr)
        sys.exit(2)

    # SWE-bench harness reads instance_id from each prediction line; pass it
    # to scope the eval to just this one (else it'd try every instance in the
    # dataset). Also unique run_id so concurrent evals don't collide.
    pred = json.loads(pred_path.read_text().splitlines()[0])
    instance_id = pred["instance_id"]
    run_id = f"{run_dir.parent.parent.name}_{run_dir.parent.name}_{run_dir.name}"  # backend_id_ts

    host = discover_docker_host()
    env = {**os.environ}
    if host:
        env["DOCKER_HOST"] = host

    eval_dir = run_dir / "evaluation"
    eval_dir.mkdir(exist_ok=True)

    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--predictions_path", str(pred_path),
        "--max_workers", "1",
        "--run_id", run_id,
        "--dataset_name", args.dataset,
        "--instance_ids", instance_id,
        "--namespace", args.namespace,
        "--cache_level", "instance",
    ]
    print(f"running: {' '.join(cmd)}", flush=True)
    print(f"  DOCKER_HOST={env.get('DOCKER_HOST','(none)')}", flush=True)

    # SWE-bench writes logs under ./logs/run_evaluation/<run_id>/<model>/<instance>/
    # from CWD; redirect via running from REPO_ROOT then copying results into
    # the run dir.
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env)

    # Locate the harness's output and copy a flat summary into the run dir.
    model = pred.get("model", "unknown")
    src = REPO_ROOT / "logs" / "run_evaluation" / run_id / model / instance_id
    if src.is_dir():
        dst = eval_dir / instance_id
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        report_json = src / "report.json"
        summary = {"run_id": run_id, "instance_id": instance_id, "model": model,
                   "harness_exit_code": proc.returncode}
        if report_json.is_file():
            try:
                rep = json.loads(report_json.read_text())
                summary["report"] = rep
            except Exception:
                pass
        (run_dir / "evaluation_result.json").write_text(json.dumps(summary, indent=2))
        print(f"\nresult → {run_dir}/evaluation_result.json")
    else:
        print(f"\nharness output not found at {src}", file=sys.stderr)

    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
