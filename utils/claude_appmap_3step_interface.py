"""Claude + AppMap, run as a 3-step strategy: RCA → code-fix → verify.

Architecture (relative to claude_appmap_mcp_interface):
- Same scaffolding (.mcp.json, appmap.yml, bin/record-appmap.sh,
  bin/run-tests.sh, issue.md, .gitignore commit) — all reused via
  inheritance.
- ALSO writes `<clone>/.claude/agents/{appmap-rca,appmap-verify}.md`
  so the project-scoped subagent definitions are loaded for every
  claude session in this clone.
- Runs `claude` three times sequentially against the same clone,
  with different prompts and different scopes:
    Step 1 (RCA)     — main agent dispatches Agent(appmap-rca, ...).
                       MCP enabled; main also has access but is told
                       not to use it directly.
    Step 2 (codefix) — main agent applies the fix from the RCA
                       report. NO --mcp-config (no AppMap surface).
    Step 3 (verify)  — main agent dispatches Agent(appmap-verify, ...).
                       MCP enabled.
- Each step's session JSONL + console.log + partial usage.json land
  under <run_dir>/step{1,2,3}_*/ . The aggregated usage.json at
  <run_dir>/usage.json sums across all three (claude_session's
  usage aggregator walks <run_dir>/sessions/ which catches them).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from utils import claude_session
from utils.claude_appmap_mcp_interface import ClaudeAppMapMcpInterface

REPO_ROOT = Path(__file__).resolve().parent.parent
RCA_AGENT_SRC = REPO_ROOT / "agents" / "appmap-rca.md"
VERIFY_AGENT_SRC = REPO_ROOT / "agents" / "appmap-verify.md"


def _fail(msg: str) -> Dict[str, object]:
    return {"success": False, "stdout": "", "stderr": msg, "returncode": -1}


class ClaudeAppMap3StepInterface(ClaudeAppMapMcpInterface):
    """Runs claude three times: RCA → code-fix → verify.

    Subclasses the MCP interface so all the scaffolding helpers
    (image pull, .mcp.json, appmap.yml, bin/* wrappers, watcher,
    archive/cleanup) are reused as-is. Only `execute_code_cli`
    is replaced.
    """

    def __init__(self) -> None:
        super().__init__()
        for src in (RCA_AGENT_SRC, VERIFY_AGENT_SRC):
            if not src.is_file():
                raise RuntimeError(f"missing agent definition: {src}")

    # ---- public entrypoint ----------------------------------------------

    def execute_code_cli(
        self,
        prompt: str,                         # ignored
        cwd: str,
        model: Optional[str] = None,
        instance: Optional[Dict] = None,
    ) -> Dict[str, object]:
        """Legacy entry point (v1 fixture flow): prepare workspace, commit
        scaffolding to git, then run the 3-step loop. New v2 flow calls
        `prepare_workspace` and `run_claude` directly so it can git-init
        the whole tree at once.
        """
        if instance is None:
            return _fail("3-step backend requires the SWE-bench instance dict")

        clone = Path(cwd).resolve()
        if not str(clone).startswith(str(Path.home())):
            return _fail(
                f"clone {clone} is outside $HOME — podman cannot bind-mount it. "
                "Set TMPDIR or SMOKE_CLONE_DIR to a path under your home directory."
            )

        self._index_sha: Optional[str] = None
        try:
            self.prepare_workspace(clone, instance)
            self._commit_scaffolding(clone)
        except Exception as e:
            return _fail(f"setup failed: {e}")

        return self.run_claude(clone, model, instance)

    def prepare_workspace(self, clone: Path, instance: Dict) -> None:
        """Extends the MCP interface's prepare to also drop the
        appmap-rca and appmap-verify subagent definitions into
        `<clone>/.claude/agents/`."""
        super().prepare_workspace(clone, instance)
        self._write_agent_definitions(clone)

    def run_claude(self, clone: Path, model: Optional[str],
                    instance: Dict) -> Dict[str, object]:
        """Run the 3-step loop against an already-prepared clone.
        Manages AppMap watcher lifecycle and post-run archive/cleanup
        across all three steps.
        """
        instance_id = instance.get("instance_id", "unknown")
        run_dir = claude_session.resolve_run_dir(clone, instance_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        timeout = int(os.environ.get("CLAUDE_APPMAP_TIMEOUT", "1800"))
        watcher = self._start_watcher(clone)

        stdouts: List[str] = []
        stderrs: List[str] = []
        final_rc = 0
        success = True
        rca_report = ""

        try:
            # ===== Step 1: RCA =====
            print("\n  3-step / step 1: RCA dispatch", flush=True)
            step1_dir = run_dir / "step1_rca"
            step1_dir.mkdir(exist_ok=True)
            r1 = self._run_step(
                name="step1_rca", clone=clone, step_dir=step1_dir,
                model=model, timeout=timeout,
                user_prompt=self._step1_prompt(),
                with_mcp=True,
            )
            stdouts.append(f"=== STEP 1: RCA ===\n{r1['stdout']}")
            if r1.get("stderr"):
                stderrs.append(f"--- step1 ---\n{r1['stderr']}")
            if r1["returncode"] != 0:
                return self._compose_result(False, stdouts, stderrs, r1["returncode"])
            try:
                rca_report = claude_session.extract_subagent_report(
                    step1_dir / "session.jsonl",
                    marker_strings=["## Root cause", "## Evidence"],
                )
                (run_dir / "rca_report.md").write_text(rca_report)
                print(f"    rca report: {len(rca_report):,} chars", flush=True)
            except Exception as e:
                # If main agent didn't actually dispatch (or dispatch failed),
                # we have nothing to feed into Step 2. Bail with a clear message.
                stderrs.append(f"could not extract RCA report: {e}")
                return self._compose_result(False, stdouts, stderrs, 2)

            # ===== Step 2: code fix =====
            print("\n  3-step / step 2: code fix", flush=True)
            step2_dir = run_dir / "step2_codefix"
            step2_dir.mkdir(exist_ok=True)
            r2 = self._run_step(
                name="step2_codefix", clone=clone, step_dir=step2_dir,
                model=model, timeout=timeout,
                user_prompt=self._step2_prompt(rca_report, instance),
                with_mcp=False,  # no AppMap during fix
            )
            stdouts.append(f"\n\n=== STEP 2: CODE FIX ===\n{r2['stdout']}")
            if r2.get("stderr"):
                stderrs.append(f"--- step2 ---\n{r2['stderr']}")
            if r2["returncode"] != 0:
                return self._compose_result(False, stdouts, stderrs, r2["returncode"])

            # ===== Step 3: verify =====
            print("\n  3-step / step 3: verify dispatch", flush=True)
            step3_dir = run_dir / "step3_verify"
            step3_dir.mkdir(exist_ok=True)
            r3 = self._run_step(
                name="step3_verify", clone=clone, step_dir=step3_dir,
                model=model, timeout=timeout,
                user_prompt=self._step3_prompt(rca_report, instance),
                with_mcp=True,
            )
            stdouts.append(f"\n\n=== STEP 3: VERIFY ===\n{r3['stdout']}")
            if r3.get("stderr"):
                stderrs.append(f"--- step3 ---\n{r3['stderr']}")

            final_rc = r3["returncode"]
            success = (final_rc == 0)
            return self._compose_result(success, stdouts, stderrs, final_rc)
        finally:
            self._stop_watcher(watcher)
            try:
                self._archive_appmap_data(clone, instance_id, run_dir)
            except Exception as e:
                print(f"  warning: failed to archive appmap data: {e}", flush=True)
            try:
                claude_session.archive_session_logs(clone, run_dir)
            except Exception as e:
                print(f"  warning: failed to archive session logs: {e}", flush=True)
            try:
                claude_session.compute_and_save_usage(run_dir, instance_id)
            except Exception as e:
                print(f"  warning: failed to compute usage stats: {e}", flush=True)
            try:
                self._wipe_appmap_data(clone)
            except Exception as e:
                print(f"  warning: failed to wipe appmap data: {e}", flush=True)

    # ---- per-step orchestration -----------------------------------------

    def _run_step(
        self,
        name: str,
        clone: Path,
        step_dir: Path,
        model: Optional[str],
        timeout: int,
        user_prompt: str,
        with_mcp: bool,
    ) -> Dict[str, object]:
        """Run one claude invocation; live-mirror its session into step_dir."""
        session_id = str(uuid.uuid4())
        live_stop = claude_session.start_live_log(session_id, step_dir)
        cmd = [
            "claude",
            "--session-id", session_id,
            "--dangerously-skip-permissions",
        ]
        if with_mcp:
            cmd += ["--mcp-config", str(clone / ".mcp.json"),
                    "--strict-mcp-config"]
        if model:
            cmd += ["--model", model]
        try:
            r = subprocess.run(
                cmd, input=user_prompt, cwd=str(clone),
                capture_output=True, text=True, timeout=timeout,
            )
            return {
                "stdout": r.stdout,
                "stderr": r.stderr,
                "returncode": r.returncode,
                "session_id": session_id,
            }
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"{name} timed out after {timeout}s",
                "returncode": -1,
                "session_id": session_id,
            }
        finally:
            live_stop.set()

    def _write_agent_definitions(self, clone: Path) -> None:
        agents_dir = clone / ".claude" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RCA_AGENT_SRC, agents_dir / "appmap-rca.md")
        shutil.copy2(VERIFY_AGENT_SRC, agents_dir / "appmap-verify.md")

    @staticmethod
    def _claude_md() -> str:
        """Project CLAUDE.md (auto-loaded). Describes the workflow once;
        each step's user prompt then says which step the agent is on."""
        return textwrap.dedent("""\
            # 3-step bug-fix workflow

            This project uses three sequential claude sessions to fix a
            single bug:

            1. **Step 1 (RCA):** main agent dispatches the
               `appmap-rca` subagent to identify the root cause.
               Main agent does NOT investigate or edit source.
            2. **Step 2 (code fix):** main agent applies the fix
               described by the RCA report (passed in the user
               prompt). One or two edits, run tests, stop. AppMap
               MCP is NOT available in this step.
            3. **Step 3 (verify):** main agent dispatches the
               `appmap-verify` subagent to confirm the runtime change.
               Main agent does NOT edit anything.

            Each step's user prompt tells you which step you are on.
            Do not deviate.

            ## Runtime environment

            - `bin/run-tests.sh <args>`     — same container, no instrumentation
            - `bin/record-appmap.sh <args>` — same container, recorded under
                                              appmap-python; produces
                                              `.appmap.json` files under
                                              `tmp/appmap/` that the host
                                              indexer picks up.

            Project deps are pre-installed in the container; do not pip
            install on the host.
            """)

    # ---- step prompts ---------------------------------------------------

    def _step1_prompt(self) -> str:
        return textwrap.dedent("""\
            **Step 1 — RCA dispatch.**

            Read `issue.md`, then dispatch the `appmap-rca` subagent
            via `Agent(subagent_type="appmap-rca", ...)`.

            Brief the subagent in your dispatch prompt:
            - Paste the contents of `issue.md`.
            - Note that NO AppMap recordings exist yet — the subagent
              must record fresh via `bin/record-appmap.sh`.
            - Ask for the standard RCA report format (## Root cause,
              ## Evidence, ## Files / lines, ## Caveats).

            When the subagent returns, **print its report verbatim**.
            Then stop.

            Do not investigate yourself. Do not edit source. Do not
            call AppMap MCP tools directly — the subagent will.
            """)

    def _step2_prompt(self, rca_report: str, instance: Dict) -> str:
        issue = self._issue_md(instance).strip()
        return textwrap.dedent(f"""\
            **Step 2 — apply the fix.**

            A root-cause analysis has been completed. Apply the fix
            it prescribes. Do NOT re-investigate; do NOT call any
            subagent or AppMap tool (none are available in this step).

            ## Bug report

            {issue}

            ## Root-cause analysis (from appmap-rca subagent)

            {rca_report}

            ## What to do

            1. Read the file:line locations the RCA cites.
            2. Make the minimum edit that resolves the cause.
            3. Run `bin/run-tests.sh pytest <relevant tests>`. Pick
               the tests cited or implied by the RCA, plus a small
               regression sweep over adjacent modules.
            4. Stop.
            """)

    def _step3_prompt(self, rca_report: str, instance: Dict) -> str:
        issue = self._issue_md(instance).strip()
        return textwrap.dedent(f"""\
            **Step 3 — verify the fix.**

            The fix has been applied. Dispatch the `appmap-verify`
            subagent via `Agent(subagent_type="appmap-verify", ...)`
            to confirm the runtime change.

            ## Bug report (for context)

            {issue}

            ## Root-cause analysis (what was supposed to be fixed)

            {rca_report}

            ## Briefing the subagent

            Tell the subagent in your dispatch prompt:
            - The bug summary (one paragraph from issue.md).
            - The **expected runtime change**: which buggy
              call/branch should no longer fire, or which should
              now fire instead. Pull this directly from the RCA's
              "Root cause" section.
            - The **reproducer command** to run under
              `bin/record-appmap.sh`. The RCA cited specific tests;
              use those, or the test the fix step added.
            - There are no pre-existing recordings; the subagent
              must record fresh.

            When the subagent returns, **print its verdict verbatim**.
            Then stop. Do not edit anything.
            """)

    @staticmethod
    def _compose_result(success: bool, stdouts: List[str],
                         stderrs: List[str], rc: int) -> Dict[str, object]:
        return {
            "success": success,
            "stdout": "\n".join(stdouts),
            "stderr": "\n".join(stderrs),
            "returncode": rc,
        }
