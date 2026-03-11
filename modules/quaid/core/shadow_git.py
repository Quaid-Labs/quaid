"""Shadow git — invisible change tracking for user project files.

Tracks changes in a user's project directory using a separate git database
stored in QUAID_HOME/.git-tracking/<project>/. No git artifacts appear in
the user's directory.

Uses --git-dir and --work-tree to separate git metadata from the tracked
files (same pattern as yadm, vcsh, and other dotfile managers).

See docs/PROJECT-SYSTEM-SPEC.md#shadow-git-tracking.
"""

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default ignore patterns — defensive, cannot be removed by LLM.
# LLM can add project-specific patterns on top of these.
_DEFAULT_EXCLUDES = """\
# === Quaid Shadow Git Defaults — DO NOT EDIT ABOVE THIS LINE ===
# These defensive ignores protect against indexing secrets, binaries, and noise.

# Secrets & credentials — NEVER track
.env
.env.*
*.pem
*.key
*.p12
*.pfx
*.keystore
*.jks
.credentials*
credentials.json
secrets.json
**/secret/
**/secrets/
.aws/
.ssh/
*.gpg
.netrc
.npmrc
.pypirc
token.txt
auth-token
.auth-token

# Dependencies & build artifacts
node_modules/
.npm/
package-lock.json
yarn.lock
pnpm-lock.yaml
__pycache__/
*.pyc
*.pyo
.venv/
venv/
env/
.eggs/
*.egg-info/
dist/
build/
*.whl
target/
vendor/
*.class
*.jar
*.war
.gradle/
.m2/
.bundle/
vendor/bundle/
out/
_build/
.build/
cmake-build*/

# IDE & OS
.idea/
.vscode/
*.swp
*.swo
*~
.project
.classpath
.settings/
*.sublime-*
.fleet/
.DS_Store
Thumbs.db
desktop.ini
*.lnk

# Databases & large files
*.db
*.sqlite
*.sqlite3
*.db-shm
*.db-wal
*.zip
*.tar
*.tar.gz
*.tgz
*.rar
*.7z
*.dmg
*.iso
*.exe
*.dll
*.so
*.dylib
*.bin
*.dat

# Media
*.mp4
*.mp3
*.wav
*.avi
*.mov
*.mkv
*.flac
*.psd
*.ai
*.raw
*.cr2
*.nef
*.tiff

# Quaid internal
.quaid/
.git-tracking/
*.snippets.md

# === END DEFAULTS — LLM-managed patterns below ===
"""


@dataclass
class FileChange:
    """A single file change detected by shadow git."""
    status: str  # A=added, M=modified, D=deleted, R=renamed
    path: str
    old_path: Optional[str] = None  # Set for renames


@dataclass
class SnapshotResult:
    """Result of a shadow git snapshot."""
    changes: List[FileChange] = field(default_factory=list)
    commit_hash: Optional[str] = None
    is_initial: bool = False


class ShadowGit:
    """Shadow git tracker for a project's source files.

    All git metadata lives in git_dir (inside QUAID_HOME). The work_tree
    points to the user's actual project directory. No artifacts are created
    in the user's directory.
    """

    def __init__(self, project_name: str, source_root: Path,
                 tracking_base: Optional[Path] = None):
        """
        Args:
            project_name: Project name (used for the git dir name)
            source_root: Path to the user's project directory (work tree)
            tracking_base: Base directory for git tracking dirs.
                          Defaults to QUAID_HOME/.git-tracking/
        """
        if tracking_base is None:
            from lib.runtime_context import get_workspace_dir
            tracking_base = get_workspace_dir() / ".git-tracking"

        self.project_name = project_name
        self.git_dir = tracking_base / project_name
        self.work_tree = Path(source_root).resolve()

    def _git(self, *args, check: bool = True,
             timeout: int = 60) -> subprocess.CompletedProcess:
        """Run a git command with --git-dir and --work-tree set."""
        cmd = [
            "git",
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.work_tree}",
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True,
            check=check, timeout=timeout,
        )

    @property
    def initialized(self) -> bool:
        """Check if shadow git has been initialized for this project."""
        return (self.git_dir / "HEAD").is_file()

    def init(self) -> None:
        """Initialize shadow git for this project."""
        if self.initialized:
            return

        self.git_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", str(self.git_dir)],
            capture_output=True, check=True, timeout=30,
        )
        self._apply_default_excludes()
        logger.info("[shadow-git] Initialized tracking for %s at %s",
                    self.project_name, self.git_dir)

    def _apply_default_excludes(self) -> None:
        """Write default exclude patterns to info/exclude."""
        exclude_file = self.git_dir / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        exclude_file.write_text(_DEFAULT_EXCLUDES, encoding="utf-8")

    def add_ignore_patterns(self, patterns: List[str]) -> None:
        """Add LLM-managed ignore patterns (appended after defaults)."""
        exclude_file = self.git_dir / "info" / "exclude"
        if not exclude_file.is_file():
            self._apply_default_excludes()

        content = exclude_file.read_text(encoding="utf-8")
        new_patterns = []
        for p in patterns:
            p = p.strip()
            if p and p not in content:
                new_patterns.append(p)

        if new_patterns:
            with exclude_file.open("a", encoding="utf-8") as f:
                f.write("\n".join(new_patterns) + "\n")
            logger.info("[shadow-git] %s: added %d ignore patterns",
                        self.project_name, len(new_patterns))

    def snapshot(self) -> Optional[SnapshotResult]:
        """Snapshot current state, return changes if anything changed.

        Returns None if nothing changed. Returns SnapshotResult with the
        list of file changes if a new commit was created.
        """
        if not self.initialized:
            self.init()

        if not self.work_tree.is_dir():
            logger.warning("[shadow-git] %s: work tree missing: %s",
                           self.project_name, self.work_tree)
            return None

        # Check for changes
        status = self._git("status", "--porcelain", check=False)
        if status.returncode != 0:
            logger.warning("[shadow-git] %s: git status failed: %s",
                           self.project_name, status.stderr.strip())
            return None

        if not status.stdout.strip():
            return None  # Nothing changed

        # Check if this is the initial commit
        has_commits = self._git("rev-parse", "HEAD", check=False).returncode == 0

        # Stage all changes
        self._git("add", "-A")

        # Commit
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commit = self._git(
            "commit", "-m", f"snapshot {ts}",
            "--allow-empty-message",
            check=False,
        )
        if commit.returncode != 0:
            # Nothing to commit (race condition or all ignored)
            return None

        # Get commit hash
        rev = self._git("rev-parse", "HEAD", check=False)
        commit_hash = rev.stdout.strip() if rev.returncode == 0 else None

        if not has_commits:
            # Initial commit — report all files as added
            ls = self._git("ls-files", check=False)
            changes = [
                FileChange(status="A", path=f)
                for f in ls.stdout.strip().splitlines()
                if f.strip()
            ]
            return SnapshotResult(
                changes=changes, commit_hash=commit_hash, is_initial=True,
            )

        # Get diff from previous commit
        diff = self._git(
            "diff", "--find-renames", "--name-status", "HEAD~1..HEAD",
            check=False,
        )
        if diff.returncode != 0:
            return SnapshotResult(commit_hash=commit_hash)

        changes = _parse_name_status(diff.stdout)
        return SnapshotResult(changes=changes, commit_hash=commit_hash)

    def get_diff(self, commits_back: int = 1) -> Optional[str]:
        """Get the unified diff from the last N commits.

        Returns the patch text, or None if no commits or diff fails.
        Useful for feeding to an LLM to understand what changed.
        """
        if not self.initialized:
            return None

        result = self._git(
            "diff", "--find-renames", f"HEAD~{commits_back}..HEAD",
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout if result.stdout.strip() else None

    def get_tracked_files(self) -> List[str]:
        """List all currently tracked files."""
        if not self.initialized:
            return []
        result = self._git("ls-files", check=False)
        if result.returncode != 0:
            return []
        return [f for f in result.stdout.strip().splitlines() if f.strip()]

    def destroy(self) -> None:
        """Remove the shadow git directory entirely."""
        import shutil
        if self.git_dir.is_dir():
            shutil.rmtree(self.git_dir)
            logger.info("[shadow-git] Destroyed tracking for %s", self.project_name)


def _parse_name_status(output: str) -> List[FileChange]:
    """Parse git diff --name-status output into FileChange objects."""
    changes = []
    for line in output.strip().splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        status_code = parts[0].strip()
        if status_code.startswith("R") and len(parts) >= 3:
            changes.append(FileChange(
                status="R", path=parts[2], old_path=parts[1],
            ))
        elif len(parts) >= 2:
            changes.append(FileChange(
                status=status_code[0] if status_code else "?",
                path=parts[1],
            ))
    return changes
