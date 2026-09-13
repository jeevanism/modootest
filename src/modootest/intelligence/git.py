"""Git subprocess abstraction and NUL-delimited diff parser for modootest."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import subprocess
from typing import Sequence


class GitError(Exception):
    """Raised when a git command fails or repository state is invalid."""


@dataclass(frozen=True)
class GitChangeRecord:
    """Represents a single changed file in a Git diff."""

    status: str  # 'A', 'M', 'D', 'R', 'C', 'T', 'U'
    old_path: str | None
    new_path: str | None
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    status_field: str

    @property
    def effective_path(self) -> str:
        """Return new_path if present, else old_path."""
        return self.new_path if self.new_path is not None else (self.old_path or "")

    @property
    def is_symlink(self) -> bool:
        return self.old_mode == "120000" or self.new_mode == "120000"

    @property
    def is_submodule(self) -> bool:
        return self.old_mode == "160000" or self.new_mode == "160000"

    @property
    def is_unmerged(self) -> bool:
        return self.status == "U" or self.status_field.startswith("U")


def _run_git(
    args: Sequence[str],
    cwd: Path,
    timeout: float = 30.0,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Execute a git command safely with bounded timeout and argument vectors."""
    cmd = ["git"] + list(args)
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as err:
        raise GitError("git executable not found in PATH.") from err
    except subprocess.TimeoutExpired as err:
        raise GitError(f"Git command timed out after {timeout}s: {' '.join(cmd)}") from err

    if check and proc.returncode != 0:
        err_msg = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"Git command failed (exit code {proc.returncode}): {err_msg}")

    return proc


def discover_git_root(path: Path | str) -> Path:
    """Discover the top-level directory of the Git repository."""
    target = Path(path).resolve()
    if not target.exists():
        raise GitError(f"Path does not exist: {target}")
    proc = _run_git(["rev-parse", "--show-toplevel"], cwd=target, check=False)
    if proc.returncode != 0:
        raise GitError(f"Directory is not inside a git repository: {target}")
    out = proc.stdout.decode("utf-8", errors="surrogateescape").strip()
    return Path(out).resolve()


def check_head_exists(git_root: Path) -> str:
    """Verify that HEAD exists and return its commit hash, or raise GitError."""
    proc = _run_git(["rev-parse", "--verify", "HEAD"], cwd=git_root, check=False)
    if proc.returncode != 0:
        raise GitError("Repository has no HEAD commit (unborn repository or no commits yet).")
    return proc.stdout.decode("utf-8", errors="surrogateescape").strip()


def resolve_commit(git_root: Path, rev: str) -> str:
    """Resolve a revision specifier to a 40-character commit SHA."""
    if not rev.strip():
        raise GitError("Revision cannot be empty.")
    proc = _run_git(["rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=git_root, check=False)
    if proc.returncode != 0:
        err_msg = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"Cannot resolve revision '{rev}' to a valid commit: {err_msg}")
    return proc.stdout.decode("utf-8", errors="surrogateescape").strip()


def parse_raw_diff_z(raw_bytes: bytes) -> list[GitChangeRecord]:
    """Parse NUL-delimited git raw diff output into GitChangeRecord objects."""
    records: list[GitChangeRecord] = []
    tokens = raw_bytes.split(bytes([0]))
    idx = 0
    total = len(tokens)

    while idx < total:
        token = tokens[idx]
        if not token:
            idx += 1
            continue

        if token.startswith(b":"):
            # Format: :<old_mode> <new_mode> <old_sha> <new_sha> <status>
            header_str = token.decode("utf-8", errors="surrogateescape")
            parts = header_str.split()
            if len(parts) < 5:
                idx += 1
                continue

            old_mode = parts[0][1:]
            new_mode = parts[1]
            old_sha = parts[2]
            new_sha = parts[3]
            status_field = parts[4]
            status = status_field[0]
            idx += 1

            if status in ("R", "C"):
                # Renames and copies have two paths: old_path then new_path
                if idx + 1 < total:
                    old_path = tokens[idx].decode("utf-8", errors="surrogateescape")
                    idx += 1
                    new_path = tokens[idx].decode("utf-8", errors="surrogateescape")
                    idx += 1
                else:
                    break
            elif status == "D":
                old_path = tokens[idx].decode("utf-8", errors="surrogateescape") if idx < total else ""
                new_path = None
                idx += 1
            elif status == "A":
                old_path = None
                new_path = tokens[idx].decode("utf-8", errors="surrogateescape") if idx < total else ""
                idx += 1
            else:  # 'M', 'T', 'U', etc.
                p = tokens[idx].decode("utf-8", errors="surrogateescape") if idx < total else ""
                old_path = p
                new_path = p
                idx += 1

            records.append(
                GitChangeRecord(
                    status=status,
                    old_path=old_path,
                    new_path=new_path,
                    old_mode=old_mode,
                    new_mode=new_mode,
                    old_sha=old_sha,
                    new_sha=new_sha,
                    status_field=status_field,
                )
            )
        else:
            idx += 1

    return records


def get_diff_tree(git_root: Path, base_commit: str, head_commit: str) -> list[GitChangeRecord]:
    """Obtain diff between two exact commit trees using git diff-tree."""
    proc = _run_git(
        ["diff-tree", "-z", "-r", "--no-commit-id", "--raw", "-M", base_commit, head_commit, "--"],
        cwd=git_root,
    )
    return parse_raw_diff_z(proc.stdout)


def get_working_tree_diff(git_root: Path) -> list[GitChangeRecord]:
    """Obtain net working tree diff relative to HEAD (staged, unstaged, and untracked)."""
    # 1. Staged and unstaged tracked changes vs HEAD
    proc_tracked = _run_git(
        ["diff", "-z", "--raw", "-M", "HEAD", "--"],
        cwd=git_root,
    )
    records = parse_raw_diff_z(proc_tracked.stdout)

    # 2. Check for unmerged files
    proc_unmerged = _run_git(
        ["ls-files", "-z", "-u", "--"],
        cwd=git_root,
    )
    if proc_unmerged.stdout:
        # Each entry in ls-files -u -z is <mode> <sha> <stage>\t<path>
        unmerged_paths = set()
        for token in proc_unmerged.stdout.split(bytes([0])):
            if not token:
                continue
            parts = token.split(b"\t", 1)
            if len(parts) == 2:
                unmerged_paths.add(parts[1].decode("utf-8", errors="surrogateescape"))
            else:
                unmerged_paths.add(token.decode("utf-8", errors="surrogateescape"))

        # Any record matching an unmerged path must have status 'U'
        new_records = []
        existing_paths = set()
        for r in records:
            if r.effective_path in unmerged_paths:
                new_records.append(
                    GitChangeRecord(
                        status="U",
                        old_path=r.old_path,
                        new_path=r.new_path,
                        old_mode=r.old_mode,
                        new_mode=r.new_mode,
                        old_sha=r.old_sha,
                        new_sha=r.new_sha,
                        status_field="U",
                    )
                )
            else:
                new_records.append(r)
            existing_paths.add(r.effective_path)

        for u_path in sorted(unmerged_paths):
            if u_path not in existing_paths:
                new_records.append(
                    GitChangeRecord(
                        status="U",
                        old_path=u_path,
                        new_path=u_path,
                        old_mode="100644",
                        new_mode="100644",
                        old_sha="0" * 40,
                        new_sha="0" * 40,
                        status_field="U",
                    )
                )
        records = new_records

    # 3. Untracked non-ignored files
    proc_untracked = _run_git(
        ["ls-files", "-z", "--others", "--exclude-standard", "--"],
        cwd=git_root,
    )
    if proc_untracked.stdout:
        import stat
        for item in proc_untracked.stdout.split(bytes([0])):
            if item:
                u_path = item.decode("utf-8", errors="surrogateescape")
                full_p = git_root / u_path
                mode_str = "100644"
                try:
                    st = os.lstat(full_p)
                    if stat.S_ISLNK(st.st_mode):
                        mode_str = "120000"
                    elif stat.S_ISDIR(st.st_mode):
                        mode_str = "040000"
                    elif stat.S_ISREG(st.st_mode):
                        mode_str = oct(stat.S_IMODE(st.st_mode) | 0o100000)[2:]
                    else:
                        # Non-regular file (FIFO, socket, device)
                        mode_str = oct(st.st_mode)[2:]
                except OSError:
                    mode_str = "100644"

                records.append(
                    GitChangeRecord(
                        status="A",
                        old_path=None,
                        new_path=u_path,
                        old_mode="000000",
                        new_mode=mode_str,
                        old_sha="0" * 40,
                        new_sha="0" * 40,
                        status_field="A",
                    )
                )

    # Deterministic sorting by effective path
    return sorted(records, key=lambda r: (r.effective_path, r.status))


def read_file_at_commit(git_root: Path, commit: str, rel_path: str) -> bytes | None:
    """Read file contents from git tree at specified commit."""
    proc = _run_git(["cat-file", "-p", f"{commit}:{rel_path}"], cwd=git_root, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def read_file_working_tree(git_root: Path, rel_path: str) -> bytes | None:
    """Read file contents from working tree safely, checking bounds and non-regular files."""
    import stat
    raw_path = git_root / rel_path
    try:
        st = os.lstat(raw_path)
    except OSError:
        return None

    # Never open FIFOs, sockets, or special devices (avoids blocking indefinitely)
    if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode) or stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
        return None

    if stat.S_ISLNK(st.st_mode):
        try:
            return os.readlink(raw_path).encode("utf-8", errors="surrogateescape")
        except OSError:
            return None

    full_path = raw_path.resolve()
    try:
        full_path.relative_to(git_root.resolve())
    except ValueError:
        # Escapes git root (e.g. symlink escape)
        return None

    if not full_path.is_file():
        return None

    try:
        return full_path.read_bytes()
    except OSError:
        return None


def list_tree_files(git_root: Path, commit: str, search_root: str) -> list[str]:
    """List all tracked file paths under search_root in the given commit."""
    entries = list_tree_entries(git_root, commit, search_root)
    return [e[3] for e in entries]


def list_tree_entries(git_root: Path, commit: str, search_root: str) -> list[tuple[str, str, str, str]]:
    """List tracked entries (mode, type, sha, path) under search_root in the given commit."""
    norm_root = str(PurePosixPath(search_root))
    args = ["ls-tree", "-z", "-r", commit]
    if norm_root and norm_root != ".":
        args.extend(["--", norm_root])
    proc = _run_git(args, cwd=git_root)
    if not proc.stdout:
        return []
    entries: list[tuple[str, str, str, str]] = []
    for token in proc.stdout.split(bytes([0])):
        if not token:
            continue
        parts = token.split(b"\t", 1)
        if len(parts) != 2:
            continue
        meta, raw_path = parts
        meta_parts = meta.decode("ascii", errors="replace").split()
        if len(meta_parts) < 3:
            continue
        mode, obj_type, sha = meta_parts[0], meta_parts[1], meta_parts[2]
        path_str = raw_path.decode("utf-8", errors="surrogateescape")
        entries.append((mode, obj_type, sha, path_str))
    return sorted(entries, key=lambda e: e[3])
