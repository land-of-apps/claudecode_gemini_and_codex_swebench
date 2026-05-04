import re
import os
import subprocess
from typing import List, Dict, Optional, Tuple, Set
from unidiff import PatchSet
import tempfile
import difflib

class PatchExtractor:
    """Extract patches from Claude Code's responses and file changes."""
    
    def __init__(self):
        self.file_edit_pattern = re.compile(
            r"(?:Creating|Editing|Modifying|Writing to) file: (.*?)$",
            re.MULTILINE
        )
        self.diff_pattern = re.compile(
            r"```diff\n(.*?)```",
            re.DOTALL
        )
        
    def extract_from_cli_output(self, output: str, repo_path: str,
                                 base_commit: Optional[str] = None) -> str:
        """Extract patch from Claude Code CLI output by analyzing git diff.

        When `base_commit` is supplied, the patch is filtered to drop hunks
        for files not present at that commit. Necessary for backends (like
        claude-appmap) that commit scaffolding files into HEAD: a raw
        `git diff HEAD` would include modifications to those scaffolding
        files, and the SWE-bench harness applies the patch to base_commit
        where they don't exist — so the patch fails to apply.
        """
        try:
            # Change to repo directory
            original_cwd = os.getcwd()
            os.chdir(repo_path)

            # First, add any untracked files to the index so they appear in diff
            subprocess.run(
                ["git", "add", "-N", "."],
                capture_output=True,
                text=True
            )

            # Get the diff against HEAD to capture all changes
            result = subprocess.run(
                ["git", "diff", "HEAD", "--no-color", "--no-ext-diff"],
                capture_output=True,
                text=True
            )

            os.chdir(original_cwd)

            if result.returncode != 0:
                print(f"Git diff failed: {result.stderr}")
                return ""

            patch = result.stdout
            if base_commit:
                patch = self._filter_to_base_commit_files(patch, repo_path, base_commit)
            return patch

        except Exception as e:
            print(f"Error extracting patch: {e}")
            return ""

    @staticmethod
    def _filter_to_base_commit_files(patch: str, repo_path: str,
                                      base_commit: str) -> str:
        """Drop diff hunks whose target file did not exist at base_commit
        AND is not a new test file (the agent's reproducer tests under
        tests/ are kept — harmless, may be useful for analysis)."""
        try:
            r = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", base_commit],
                cwd=repo_path, capture_output=True, text=True, check=True,
            )
            base_files = set(r.stdout.splitlines())
        except Exception as e:
            print(f"  warning: could not list base_commit files, returning unfiltered patch: {e}")
            return patch

        # Split patch into per-file blocks: each begins with `diff --git`.
        blocks = re.split(r'(?m)^(?=diff --git )', patch)
        kept = [b for b in blocks if b and PatchExtractor._block_is_kept(b, base_files)]
        filtered = "".join(kept)
        dropped = len(blocks) - len(kept)
        if dropped:
            print(f"  patch filter: dropped {dropped} non-base-commit file(s) from patch")
        return filtered

    @staticmethod
    def _block_is_kept(block: str, base_files: set) -> bool:
        # First line: `diff --git a/<path> b/<path>` — extract path.
        first = block.splitlines()[0] if block else ""
        m = re.match(r'^diff --git a/(.+?) b/', first)
        if not m:
            return False
        path = m.group(1)
        if path in base_files:
            return True
        # Allow agent-authored reproducer tests under tests/ (harmless to apply).
        if path.startswith("tests/") and "test_repro" in path:
            return True
        return False
            
    def extract_from_response(self, response: str) -> List[Dict[str, str]]:
        """Extract file changes from Claude's response text."""
        changes = []
        
        # Look for diff blocks
        diff_matches = self.diff_pattern.findall(response)
        for diff in diff_matches:
            changes.append({
                "type": "diff",
                "content": diff
            })
            
        # Look for file edits mentioned in the response
        file_mentions = self.file_edit_pattern.findall(response)
        for file_path in file_mentions:
            changes.append({
                "type": "file_mention",
                "path": file_path.strip()
            })
            
        return changes
    
    def create_patch_from_changes(self, before_state: Dict[str, str],
                                after_state: Dict[str, str]) -> str:
        """Create a unified diff patch from before/after file states."""
        patch_lines = []

        # Find all files that changed
        all_files = set(before_state.keys()) | set(after_state.keys())

        for file_path in sorted(all_files):
            before_content = before_state.get(file_path, "").splitlines(keepends=True)
            after_content = after_state.get(file_path, "").splitlines(keepends=True)

            if before_content == after_content:
                continue

            diff_output = difflib.unified_diff(
                before_content,
                after_content,
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
            )

            patch_lines.extend(diff_output)

        return "".join(patch_lines)
    
    def validate_patch(self, patch: str) -> Tuple[bool, Optional[str]]:
        """Validate that a patch is well-formed."""
        if not patch or not patch.strip():
            return False, "Empty patch"
            
        try:
            # Try to parse the patch
            patchset = PatchSet(patch)
            
            # Check if patch has any files
            if not patchset:
                return False, "Patch contains no file changes"
                
            # Basic validation passed
            return True, None
            
        except Exception as e:
            return False, f"Invalid patch format: {str(e)}"
            
    def apply_patch_test(self, patch: str, repo_path: str) -> Tuple[bool, str]:
        """Test if a patch can be applied cleanly."""
        try:
            # Save patch to temporary file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False) as f:
                f.write(patch)
                patch_file = f.name
                
            original_cwd = os.getcwd()
            os.chdir(repo_path)
            
            # Test patch application
            result = subprocess.run(
                ["git", "apply", "--check", patch_file],
                capture_output=True,
                text=True
            )
            
            os.chdir(original_cwd)
            os.unlink(patch_file)
            
            if result.returncode == 0:
                return True, "Patch can be applied cleanly"
            else:
                return False, f"Patch application failed: {result.stderr}"
                
        except Exception as e:
            return False, f"Error testing patch: {str(e)}"
            
    def format_for_swebench(self, patch: str, instance_id: str, model_name: str = "claude-code") -> Dict:
        """Format patch for SWE-bench submission."""
        return {
            "instance_id": instance_id,
            "model": model_name,
            "prediction": patch
        }