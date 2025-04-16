#!/usr/bin/env python3

import os
import sys
import argparse
import logging
import subprocess
import re
from pathlib import Path
from typing import List, Tuple, Optional
from urllib.parse import urlparse

# Add the parent directory to Python path to find the lib module
script_dir = Path(__file__).absolute()
parent_dir = script_dir.parent
sys.path.append(str(parent_dir))

from lib.actions import Action

Action.set_logger("rebase")


class GitCommandExecutor:
    """Base class for git command execution."""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def execute_git_command(
        self, cmd: List[str], cwd: Optional[str] = None
    ) -> Tuple[int, str]:
        """Execute a git command and return its result."""
        with Action(f"git {' '.join(cmd)}", level=logging.DEBUG) as action:
            result = subprocess.run(
                ["git"] + cmd, cwd=cwd or self.work_dir, capture_output=True, text=True
            )
            action.note(f"Exit code: {result.returncode}")
            if result.stderr:
                action.note(f"stderr: {result.stderr}")
            return result.returncode, result.stdout


class DiffGenerator(GitCommandExecutor):
    """Handles generation of git diffs between references."""

    def __init__(self, work_dir: Path, diff_dir: Path) -> None:
        super().__init__(work_dir)
        self.diff_dir = diff_dir
        self.diff_dir.mkdir(exist_ok=True, parents=True)

    def generate_diff(self, base_ref: str, target_ref: str, output_file: str) -> None:
        """Generate a diff between two git references."""
        with Action(f"Generating diff {output_file}") as action:
            diff_file = self.diff_dir / f"{output_file}.patch"
            self.execute_git_command(
                ["diff", base_ref, target_ref, "--output", str(diff_file)]
            )
            action.note(f"Generated diff file: {diff_file}")

    def generate_temp_branch_diff(
        self, base_ref: str, source_branch: str, output_file: str
    ) -> None:
        """Generate a diff using a temporary branch."""
        with Action(f"Generating diff {output_file} using temporary branch") as action:
            temp_branch = f"temp_{source_branch}"
            self.execute_git_command(["checkout", "-b", temp_branch, source_branch])

            try:
                self.generate_diff(base_ref, temp_branch, output_file)
            finally:
                self.execute_git_command(["checkout", source_branch])
                self.execute_git_command(["branch", "-D", temp_branch])


class PatchApplier(GitCommandExecutor):
    """Handles application of git patches and conflict detection."""

    def __init__(self, work_dir: Path, diff_dir: Path) -> None:
        super().__init__(work_dir)
        self.diff_dir = diff_dir
        self.conflict_files: List[str] = []

    def apply_patch(self, patch_file: Path) -> None:
        """Apply a patch file and handle conflicts."""
        with Action(f"Applying patch {patch_file.name}") as action:
            result = self.execute_git_command(["apply", "--check", str(patch_file)])
            if result[0] == 0:
                action.note("Patch can be applied cleanly")
                self.execute_git_command(["apply", str(patch_file)])
            else:
                action.note("Conflict detected, marking for manual resolution")
                self.conflict_files.append(patch_file.stem.replace("custom_", ""))

    def apply_changes(self, new_branch: str) -> None:
        """Apply all custom patches to the new branch."""
        with Action("Applying changes to new branch") as action:
            for diff_file in self.diff_dir.glob("custom_*.patch"):
                self.apply_patch(diff_file)
            action.note(f"Changes applied to branch: {new_branch}")


class RebaseManager(GitCommandExecutor):
    """Manages the rebase process for a fork branch."""

    def __init__(
        self,
        upstream_new_tag: str,
        upstream_base_tag: str,
        custom_branch: str,
        work_dir: Path,
        fork_repo: str,
        output_branch: Optional[str] = None,
    ) -> None:
        super().__init__(work_dir)
        self.upstream_new_tag = upstream_new_tag
        self.upstream_base_tag = upstream_base_tag
        self.custom_branch = custom_branch
        self.work_dir = work_dir
        self.fork_repo = fork_repo
        self.upstream_repo = "https://github.com/ClickHouse/ClickHouse.git"
        self.diff_dir = work_dir / "diffs"
        self.diff_generator = DiffGenerator(work_dir, self.diff_dir)
        self.patch_applier = PatchApplier(work_dir, self.diff_dir)

        self.upstream_new_version = self._extract_version_number(upstream_new_tag)
        self.upstream_base_version = self._extract_version_number(upstream_base_tag)

        if not all([self.upstream_new_version, self.upstream_base_version]):
            raise ValueError("Failed to extract version information from tags")

        self.output_branch = (
            output_branch or f"{self.custom_branch}-{self.upstream_new_tag}"
        )

    def _extract_version_number(self, tag: str) -> Optional[str]:
        """Extract version number from tag."""
        match = re.search(r"v([\d.]+(?:-[a-z]+)?)", tag)
        return match.group(1) if match else None

    def is_directory_empty(self) -> bool:
        """Check if the directory is empty or only contains hidden files."""
        with os.scandir(self.work_dir) as entries:
            return all(entry.name.startswith(".") for entry in entries)

    def _handle_remote(
        self, remote_name: str, expected_url: str, action: Action
    ) -> None:
        """Validate remote configuration."""
        result = self.execute_git_command(["remote", "get-url", remote_name])
        if result[0] != 0:
            if remote_name == "upstream":
                action.note(f"Upstream remote not found, will add it")
            else:
                raise ValueError(f"{remote_name.capitalize()} remote not found")
        elif result[1].strip() != expected_url:
            if remote_name == "upstream":
                action.note(
                    f"Upstream remote URL mismatch. Expected: {expected_url}, Got: {result[1].strip()}"
                )
                action.note("Will update upstream remote URL")
            else:
                raise ValueError(
                    f"{remote_name.capitalize()} remote does not match. Expected: {expected_url}, Got: {result[1].strip()}"
                )

    def _setup_remote(self, remote_name: str, url: str, action: Action) -> None:
        """Set up or update a remote."""
        result = self.execute_git_command(["remote", "get-url", remote_name])
        if result[0] != 0:
            self.execute_git_command(["remote", "add", remote_name, url])
            action.note(f"Added {remote_name} remote: {url}")
        elif result[1].strip() != url:
            self.execute_git_command(["remote", "set-url", remote_name, url])
            action.note(f"Updated {remote_name} remote URL to: {url}")

    def validate_working_directory(self) -> None:
        """Validate the working directory state."""
        with Action("Validating working directory") as action:
            if not (self.work_dir / ".git").exists():
                raise ValueError(
                    "Not a git repository. Please run this script from a git repository."
                )

            result = self.execute_git_command(["status", "--porcelain"])
            if result[1].strip():
                raise ValueError(
                    "Working directory has uncommitted changes. Please commit or stash them first."
                )

            result = self.execute_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
            current_branch = result[1].strip()
            if current_branch != self.custom_branch:
                raise ValueError(
                    f"Expected to be on branch '{self.custom_branch}', but on '{current_branch}'"
                )

            self._handle_remote("origin", self.fork_repo, action)
            self._handle_remote("upstream", self.upstream_repo, action)

    def clone_repository(self) -> None:
        """Clone the fork repository if the directory is empty."""
        with Action("Cloning repository") as action:
            if self.is_directory_empty():
                action.note(f"Directory is empty, cloning {self.fork_repo}")
                self.execute_git_command(["clone", self.fork_repo, "."])
                self._setup_remote("upstream", self.upstream_repo, action)
                self.execute_git_command(["checkout", self.custom_branch])
                action.note(f"Checked out branch: {self.custom_branch}")
            else:
                action.note("Directory is not empty, skipping clone")

    def setup_workspace(self) -> None:
        """Set up the workspace for rebasing."""
        with Action("Setting up workspace") as action:
            self.clone_repository()
            self.validate_working_directory()
            self._setup_remote("upstream", self.upstream_repo, action)
            self.execute_git_command(
                ["fetch", "upstream", f"refs/tags/{self.upstream_base_tag}"]
            )
            self.execute_git_command(
                ["fetch", "upstream", f"refs/tags/{self.upstream_new_tag}"]
            )

    def generate_custom_base_diff(self) -> None:
        """Generate diff between custom branch and base tag."""
        with Action("Generating custom branch vs base diff") as action:
            self.diff_generator.generate_temp_branch_diff(
                f"refs/tags/{self.upstream_base_tag}",
                self.custom_branch,
                "custom_vs_base",
            )

    def generate_upstream_base_diff(self) -> None:
        """Generate diff between upstream tags."""
        with Action("Generating upstream vs base diff") as action:
            self.diff_generator.generate_diff(
                f"refs/tags/{self.upstream_base_tag}",
                f"refs/tags/{self.upstream_new_tag}",
                "upstream_vs_base",
            )

    def create_new_branch(self) -> str:
        """Create a new branch based on the upstream tag."""
        with Action("Creating new branch based on upstream tag") as action:
            self.execute_git_command(["checkout", f"refs/tags/{self.upstream_new_tag}"])
            self.execute_git_command(["checkout", "-b", self.output_branch])
            action.note(f"Created new branch: {self.output_branch}")
            return self.output_branch

    def apply_changes(self, new_branch: str) -> None:
        """Apply changes to the new branch."""
        self.patch_applier.apply_changes(new_branch)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Rebase custom branch to new upstream version"
    )
    parser.add_argument(
        "--new-tag", required=True, help="New upstream tag (e.g. v25.3.2.39-lts)"
    )
    parser.add_argument(
        "--base-tag", required=True, help="Base upstream tag (e.g. v25.2.1.3085-stable)"
    )
    parser.add_argument(
        "--custom-branch", required=True, help="Your custom branch name (e.g. antalya)"
    )
    parser.add_argument(
        "--output-branch", help="Output branch name (default: custom-branch-new-tag)"
    )
    parser.add_argument(
        "--fork-repo",
        default="https://github.com/Altinity/ClickHouse.git",
        help="Fork repository URL (default: https://github.com/Altinity/ClickHouse.git)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path.cwd(),
        help="Working directory for the rebase process (default: current directory)",
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point for the rebase script."""
    args = parse_args()

    with Action("Rebase Configuration") as action:
        action.note(f"New Upstream Tag: {args.new_tag}")
        action.note(f"Base Upstream Tag: {args.base_tag}")
        action.note(f"Custom Branch: {args.custom_branch}")
        action.note(
            f"Output Branch: {args.output_branch or f'{args.custom_branch}-{args.new_tag}'}"
        )
        action.note(f"Fork Repository: {args.fork_repo}")
        action.note(f"Work Directory: {args.work_dir}")

    with Action("Rebase Process") as action:
        rebase_manager = RebaseManager(
            args.new_tag,
            args.base_tag,
            args.custom_branch,
            args.work_dir,
            args.fork_repo,
            args.output_branch,
        )

        action.note("Starting rebase process")
        rebase_manager.setup_workspace()
        rebase_manager.generate_custom_base_diff()
        rebase_manager.generate_upstream_base_diff()

        new_branch = rebase_manager.create_new_branch()
        rebase_manager.apply_changes(new_branch)

        if rebase_manager.patch_applier.conflict_files:
            action.note("The following files need manual conflict resolution:")
            for file in rebase_manager.patch_applier.conflict_files:
                action.note(f"  - {file}")
            action.note("Use 'meld' to resolve conflicts:")
            action.note(f"meld <base> <upstream> <custom>")
            action.note(f"Current branch: {new_branch}")
        else:
            action.note("All changes applied successfully!")
            action.note(f"New branch created: {new_branch}")


if __name__ == "__main__":
    main()
