"""Deterministic filesystem snapshotting and change detection for watch mode."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Sequence

IGNORED_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ai-handoff",
    ".venv",
    "venv",
    "node_modules",
}

IGNORED_FILE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".swp",
    ".tmp",
}


class WatchSnapshotError(Exception):
    """Raised when watch roots are invalid, escaping, missing, or unsupported."""


def validate_watch_roots(
    git_root: Path,
    raw_roots: Sequence[str | Path],
) -> list[Path]:
    """Validate watch roots relative to git_root.

    Fails closed if any root:
    - escapes the git repository root
    - does not exist
    - contains a symlink component or is itself a symlink
    - is not a directory (e.g. FIFO, socket, device, regular file)
    - cannot be read
    """
    resolved_git_root = git_root.resolve()
    validated: list[Path] = []
    seen: set[Path] = set()

    for raw in raw_roots:
        raw_str = str(raw).strip()
        if not raw_str:
            continue
        p = PurePosixPath(raw_str)
        if raw_str.startswith("/") or ".." in p.parts:
            raise WatchSnapshotError(
                f"Watch root '{raw_str}' escapes git repository root ({git_root})."
            )

        # Check each path component for symlinks and existence
        check_path = git_root
        for part in p.parts:
            check_path = check_path / part
            try:
                st = check_path.lstat()
            except OSError as err:
                raise WatchSnapshotError(
                    f"Configured watch root '{raw_str}' component '{part}' cannot be accessed or does not exist: {err}"
                )
            if stat.S_ISLNK(st.st_mode):
                raise WatchSnapshotError(
                    f"Unsupported symlink in configured watch root '{raw_str}': component '{part}'."
                )

        root_path = (git_root / p).resolve()
        try:
            root_path.relative_to(resolved_git_root)
        except ValueError:
            raise WatchSnapshotError(
                f"Watch root '{raw_str}' escapes git repository root ({git_root})."
            )

        try:
            rst = root_path.lstat()
        except OSError as err:
            raise WatchSnapshotError(
                f"Configured watch root '{raw_str}' does not exist or cannot be accessed: {err}"
            )

        if stat.S_ISLNK(rst.st_mode):
            raise WatchSnapshotError(
                f"Unsupported symlink in configured watch root '{raw_str}'."
            )

        if not stat.S_ISDIR(rst.st_mode):
            raise WatchSnapshotError(
                f"Configured watch root '{raw_str}' is not a directory (found nonregular file, FIFO, socket, or device)."
            )

        try:
            os.listdir(str(root_path))
        except OSError as err:
            raise WatchSnapshotError(
                f"Configured watch root '{raw_str}' is not readable: {err}"
            )

        if root_path not in seen:
            seen.add(root_path)
            validated.append(root_path)

    if not validated:
        raise WatchSnapshotError("No valid watch roots provided.")

    return validated


def _compute_digest(f_path: Path) -> bytes:
    """Compute bounded content digest (up to 64KB) for reliable change detection."""
    try:
        with open(f_path, "rb") as f:
            data = f.read(65536)
            return hashlib.sha256(data).digest()
    except OSError:
        return b""


def take_snapshot(
    git_root: Path,
    watch_roots: Sequence[Path],
    ignore_paths: set[Path] | None = None,
) -> dict[str, tuple[Any, ...]]:
    """Recursively take a deterministic snapshot of regular files within watch roots.

    Never follows symlinks. Ignores .git, caches, bytecode, .ai-handoff, and configured ignore paths.
    Fails closed if any root is missing, symlinked, not a directory, or escapes git_root.
    Returns mapping from repo-relative POSIX path to (mtime_ns, size, ino, ctime_ns, digest).
    """
    resolved_git_root = git_root.resolve()
    ignored_resolved = {p.resolve() for p in (ignore_paths or set())}

    snapshot: dict[str, tuple[Any, ...]] = {}

    for root in watch_roots:
        resolved_root = root.resolve()
        try:
            resolved_root.relative_to(resolved_git_root)
        except ValueError:
            raise WatchSnapshotError(f"Watch root '{root}' escapes git repository root ({git_root}).")

        try:
            root_st = root.lstat()
        except OSError as err:
            raise WatchSnapshotError(f"Watch root '{root}' cannot be accessed or does not exist: {err}")

        if stat.S_ISLNK(root_st.st_mode):
            raise WatchSnapshotError(f"Unsupported symlink in watch root '{root}'.")

        if not stat.S_ISDIR(root_st.st_mode):
            raise WatchSnapshotError(f"Watch root '{root}' is not a directory.")

        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
            dp = Path(dirpath)
            try:
                resolved_dp = dp.resolve()
            except OSError:
                continue

            if resolved_dp in ignored_resolved:
                dirnames.clear()
                continue

            # Prune ignored directory names and symlinked directories
            safe_subdirs: list[str] = []
            for d in sorted(dirnames):
                if d in IGNORED_DIR_NAMES or d.startswith("."):
                    continue
                sub_path = dp / d
                try:
                    sub_st = sub_path.lstat()
                    if stat.S_ISLNK(sub_st.st_mode):
                        continue
                    if sub_path.resolve() in ignored_resolved:
                        continue
                    safe_subdirs.append(d)
                except OSError:
                    continue
            dirnames[:] = safe_subdirs

            for fname in sorted(filenames):
                if fname.startswith("."):
                    continue
                if any(fname.endswith(ext) for ext in IGNORED_FILE_EXTENSIONS) or fname.endswith("~"):
                    continue

                f_path = dp / fname
                try:
                    f_st = f_path.lstat()
                    if stat.S_ISLNK(f_st.st_mode) or not stat.S_ISREG(f_st.st_mode):
                        continue
                    if f_path.resolve() in ignored_resolved:
                        continue
                    rel = f_path.resolve().relative_to(resolved_git_root).as_posix()
                    digest = _compute_digest(f_path)
                    snapshot[rel] = (
                        f_st.st_mtime_ns,
                        f_st.st_size,
                        f_st.st_ino,
                        f_st.st_ctime_ns,
                        digest,
                    )
                except (OSError, ValueError):
                    continue

    return dict(sorted(snapshot.items()))


def detect_changes(
    old_snapshot: dict[str, tuple[Any, ...]],
    new_snapshot: dict[str, tuple[Any, ...]],
) -> tuple[list[str], list[str], list[str]]:
    """Compare snapshots and return (created, modified, deleted) relative file paths."""
    old_keys = set(old_snapshot.keys())
    new_keys = set(new_snapshot.keys())

    created = sorted(new_keys - old_keys)
    deleted = sorted(old_keys - new_keys)
    common = old_keys & new_keys
    modified = sorted(p for p in common if old_snapshot[p] != new_snapshot[p])

    return created, modified, deleted
