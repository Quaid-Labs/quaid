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
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_SHADOW_GIT_USER_NAME = "Quaid Shadow Git"
_SHADOW_GIT_USER_EMAIL = "quaid-shadow-git@localhost"

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

    def _git_bytes(self, *args, check: bool = True,
                   timeout: int = 60) -> subprocess.CompletedProcess:
        """Run a git command and capture raw bytes."""
        cmd = [
            "git",
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.work_tree}",
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True,
            check=check, timeout=timeout,
        )

    def _normalize_file_path(self, file_path: str) -> str:
        """Normalize a user-supplied project file path to a git pathspec."""
        raw = str(file_path or "").strip()
        if not raw:
            raise ValueError("file path is required")
        path = Path(raw)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(self.work_tree)
            except ValueError as exc:
                raise ValueError(f"file path is outside project source root: {file_path}") from exc
        if any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"invalid project file path: {file_path}")
        return path.as_posix()

    @property
    def initialized(self) -> bool:
        """Check if shadow git has been initialized for this project."""
        return (self.git_dir / "HEAD").is_file()

    def _ensure_identity_config(self) -> None:
        """Shadow repos must not depend on the user's global git identity."""
        if not self.git_dir.is_dir():
            return
        for key, value in (
            ("user.name", _SHADOW_GIT_USER_NAME),
            ("user.email", _SHADOW_GIT_USER_EMAIL),
        ):
            subprocess.run(
                ["git", f"--git-dir={self.git_dir}", "config", key, value],
                capture_output=True,
                check=True,
                timeout=30,
            )

    def init(self) -> None:
        """Initialize shadow git for this project."""
        if self.initialized:
            self._ensure_identity_config()
            return

        self.git_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", str(self.git_dir)],
            capture_output=True, check=True, timeout=30,
        )
        self._ensure_identity_config()
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
        else:
            self._ensure_identity_config()

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
            status_after = self._git("status", "--porcelain", check=False)
            if status_after.stdout.strip():
                logger.warning(
                    "[shadow-git] %s: commit failed with pending changes: %s",
                    self.project_name,
                    commit.stderr.strip() or commit.stdout.strip(),
                )
                raise RuntimeError(
                    f"shadow git commit failed for {self.project_name}: "
                    f"{commit.stderr.strip() or commit.stdout.strip() or 'unknown error'}"
                )
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

    def current_head(self) -> Optional[str]:
        """Return the current shadow commit hash, if the tracker has commits."""
        if not self.initialized:
            return None
        result = self._git("rev-parse", "HEAD", check=False)
        if result.returncode != 0:
            return None
        head = result.stdout.strip()
        return head or None

    def _commit_exists(self, commit_hash: Optional[str]) -> bool:
        raw = str(commit_hash or "").strip()
        if not raw:
            return False
        result = self._git("cat-file", "-e", f"{raw}^{{commit}}", check=False)
        return result.returncode == 0

    def committed_snapshot_since(self, base_commit: Optional[str]) -> Optional[SnapshotResult]:
        """Return already-committed shadow changes newer than a docs cursor.

        This is used to recover from a worker that committed a shadow snapshot
        but died before advancing the docs cursor.
        """
        head = self.current_head()
        if not head or head == str(base_commit or "").strip():
            return None

        if self._commit_exists(base_commit):
            diff = self._git(
                "diff", "--find-renames", "--name-status", f"{base_commit}..HEAD",
                check=False,
            )
            changes = _parse_name_status(diff.stdout) if diff.returncode == 0 else []
            return SnapshotResult(changes=changes, commit_hash=head, is_initial=False)

        ls = self._git("ls-tree", "-r", "--name-only", "HEAD", check=False)
        changes = [
            FileChange(status="A", path=f)
            for f in ls.stdout.strip().splitlines()
            if f.strip()
        ] if ls.returncode == 0 else []
        return SnapshotResult(changes=changes, commit_hash=head, is_initial=True)

    def committed_diff_since(self, base_commit: Optional[str], *, full: bool = False) -> Optional[str]:
        """Return a diff for committed shadow changes newer than a docs cursor."""
        head = self.current_head()
        if not head or head == str(base_commit or "").strip():
            return None

        if self._commit_exists(base_commit):
            args = ["diff", "--find-renames"]
            if not full:
                args.append("--stat")
            args.append(f"{base_commit}..HEAD")
        else:
            args = ["show", "--format=", "--find-renames"]
            if not full:
                args.append("--stat")
            args.append("HEAD")
        result = self._git(*args, check=False)
        if result.returncode != 0:
            return None
        return result.stdout if result.stdout.strip() else None

    def pending_changes(self) -> List[FileChange]:
        """Return uncommitted work-tree changes without mutating the shadow repo."""
        if not self.initialized:
            self.init()
        if not self.work_tree.is_dir():
            return []
        status = self._git("status", "--porcelain", check=False)
        if status.returncode != 0 or not status.stdout.strip():
            return []
        changes: List[FileChange] = []
        for line in status.stdout.splitlines():
            if not line.strip():
                continue
            code = line[:2].strip() or "?"
            rest = line[3:].strip() if len(line) > 3 else ""
            if not rest:
                continue
            if " -> " in rest:
                old_path, new_path = rest.split(" -> ", 1)
                changes.append(FileChange(status="R", path=new_path.strip(), old_path=old_path.strip()))
            else:
                changes.append(FileChange(status=code[0] if code else "?", path=rest))
        return changes

    def pending_diff(self, *, full: bool = False) -> Optional[str]:
        """Return a non-mutating diff for current work-tree changes.

        Untracked files do not have a git patch until staged, so they are
        represented in the status list by pending_changes().
        """
        if not self.initialized:
            self.init()
        if not self.work_tree.is_dir():
            return None
        args = ["diff", "--find-renames"]
        if not full:
            args.append("--stat")
        result = self._git(*args, check=False)
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

    def history(self, file_path: Optional[str] = None, *, limit: int = 20) -> List[Dict[str, str]]:
        """Return shadow-git commit history, optionally scoped to one file."""
        if not self.initialized:
            return []
        bounded_limit = max(1, min(int(limit or 20), 200))
        args = ["log", f"-n{bounded_limit}", "--date=iso-strict", "--format=%H%x00%h%x00%aI%x00%s"]
        if file_path:
            args.extend(["--", self._normalize_file_path(file_path)])
        result = self._git(*args, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        entries: List[Dict[str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\0", 3)
            if len(parts) != 4:
                continue
            commit_hash, short_hash, committed_at, subject = parts
            entries.append({
                "commit": commit_hash,
                "short": short_hash,
                "committed_at": committed_at,
                "subject": subject,
            })
        return entries

    def show_file(self, commit_hash: str, file_path: str) -> bytes:
        """Return a tracked file's bytes at a shadow commit."""
        commit = str(commit_hash or "").strip()
        if not commit:
            raise ValueError("commit is required")
        normalized = self._normalize_file_path(file_path)
        result = self._git_bytes("show", f"{commit}:{normalized}", check=False)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise FileNotFoundError(detail or f"{normalized} not found at {commit}")
        return bytes(result.stdout)

    def restore_file(self, commit_hash: str, file_path: str) -> Path:
        """Restore a tracked file from a shadow commit into the work tree."""
        normalized = self._normalize_file_path(file_path)
        data = self.show_file(commit_hash, normalized)
        target = (self.work_tree / normalized).resolve()
        try:
            target.relative_to(self.work_tree)
        except ValueError as exc:
            raise ValueError(f"restore target is outside project source root: {file_path}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.quaid_tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        return target

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
