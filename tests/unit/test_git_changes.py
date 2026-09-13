"""Tests for Git changes, diff parsing, working-tree scanner, and ownership."""

import os
from pathlib import Path
import subprocess
import pytest

from modootest.intelligence.git import (
    GitChangeRecord,
    GitError,
    check_head_exists,
    discover_git_root,
    get_diff_tree,
    get_working_tree_diff,
    parse_raw_diff_z,
    read_file_at_commit,
    read_file_working_tree,
    resolve_commit,
)
from modootest.intelligence.ownership import (
    resolve_change_ownership,
    resolve_owning_addon,
)


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)


def _commit(path: Path, msg: str) -> str:
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=path, check=True, capture_output=True)
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True)
    return proc.stdout.decode("utf-8").strip()


def test_discover_git_root_and_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    sub = repo / "a" / "b"
    sub.mkdir(parents=True)

    assert discover_git_root(sub) == repo.resolve()

    # Unborn repository
    with pytest.raises(GitError, match="unborn"):
        check_head_exists(repo)

    # Make initial commit
    (repo / "init.txt").write_text("hello")
    commit_sha = _commit(repo, "initial commit")

    assert check_head_exists(repo) == commit_sha
    assert resolve_commit(repo, "HEAD") == commit_sha

    # Invalid ref
    with pytest.raises(GitError, match="Cannot resolve revision 'invalid-ref'"):
        resolve_commit(repo, "invalid-ref")


def test_historical_diff_despite_divergent_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    f = repo / "data.txt"
    f.write_text("v1")
    c1 = _commit(repo, "c1")

    f.write_text("v2")
    c2 = _commit(repo, "c2")

    # Current working tree is changed to v3 (unstaged) and another file untracked
    f.write_text("v3-dirty")
    (repo / "untracked.txt").write_text("dirty")

    # Historical diff between c1 and c2 MUST reflect c1->c2, ignoring v3-dirty!
    diffs = get_diff_tree(repo, c1, c2)
    assert len(diffs) == 1
    assert diffs[0].status == "M"
    assert diffs[0].old_path == "data.txt"
    assert diffs[0].new_path == "data.txt"

    # Reading file at commit must read historical content
    assert read_file_at_commit(repo, c1, "data.txt") == b"v1"
    assert read_file_at_commit(repo, c2, "data.txt") == b"v2"
    # Whereas working tree reads dirty content
    assert read_file_working_tree(repo, "data.txt") == b"v3-dirty"


def test_working_tree_staged_unstaged_net_revert_and_untracked(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    f1 = repo / "file1.txt"
    f2 = repo / "file2.txt"
    revert_file = repo / "revert.txt"
    gitignore = repo / ".gitignore"

    f1.write_text("orig1")
    f2.write_text("orig2")
    revert_file.write_text("keep_me")
    gitignore.write_text("*.ignored\n")
    _commit(repo, "c1")

    # 1. Unstaged modification
    f1.write_text("mod1_unstaged")

    # 2. Staged modification
    f2.write_text("mod2_staged")
    subprocess.run(["git", "add", "file2.txt"], cwd=repo, check=True)

    # 3. Net-reverted change: modified, staged, then reverted in worktree to HEAD content
    revert_file.write_text("changed_then_staged")
    subprocess.run(["git", "add", "revert.txt"], cwd=repo, check=True)
    revert_file.write_text("keep_me")  # Reverted back to HEAD content in worktree!

    # 4. Untracked file (non-ignored)
    (repo / "new_untracked.txt").write_text("new")

    # 5. Ignored untracked file
    (repo / "temp.ignored").write_text("skip me")

    diffs = get_working_tree_diff(repo)
    diff_map = {d.effective_path: d for d in diffs}

    assert "file1.txt" in diff_map
    assert diff_map["file1.txt"].status == "M"

    assert "file2.txt" in diff_map
    assert diff_map["file2.txt"].status == "M"

    # Net-reverted file MUST NOT appear in working-tree diff!
    assert "revert.txt" not in diff_map

    # Untracked non-ignored file MUST appear as 'A'
    assert "new_untracked.txt" in diff_map
    assert diff_map["new_untracked.txt"].status == "A"

    # Ignored file MUST NOT appear
    assert "temp.ignored" not in diff_map


def test_special_characters_in_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    special_file = repo / "file with spaces and unicode éà.txt"
    special_file.write_text("hello")
    c1 = _commit(repo, "commit special")

    special_file.write_text("hello modified")
    diffs = get_working_tree_diff(repo)

    assert len(diffs) == 1
    assert diffs[0].status == "M"
    assert "file with spaces and unicode éà.txt" in diffs[0].effective_path


def test_rename_across_addons(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons_dir = repo / "addons"
    addon_a = addons_dir / "addon_a"
    addon_b = addons_dir / "addon_b"
    addon_a.mkdir(parents=True)
    addon_b.mkdir(parents=True)

    (addon_a / "__manifest__.py").write_text("{'name': 'addon_a'}")
    (addon_b / "__manifest__.py").write_text("{'name': 'addon_b'}")

    shared_code = addon_a / "models" / "shared.py"
    shared_code.parent.mkdir()
    shared_code.write_text("# shared logic\n")
    c1 = _commit(repo, "c1")

    # Rename across addons: move shared.py from addon_a to addon_b
    dest_code = addon_b / "models" / "shared.py"
    dest_code.parent.mkdir(exist_ok=True)
    subprocess.run(["git", "mv", str(shared_code), str(dest_code)], cwd=repo, check=True)
    c2 = _commit(repo, "c2 rename across addons")

    diffs = get_diff_tree(repo, c1, c2)
    rename_rec = [d for d in diffs if d.status == "R"][0]
    assert rename_rec.old_path == "addons/addon_a/models/shared.py"
    assert rename_rec.new_path == "addons/addon_b/models/shared.py"

    addon_dirs = {"addons/addon_a": "addon_a", "addons/addon_b": "addon_b"}
    owned = resolve_change_ownership([rename_rec], addon_dirs, addon_dirs)[0]
    assert owned.old_owner == "addon_a"
    assert owned.new_owner == "addon_b"
    assert set(owned.affected_addons) == {"addon_a", "addon_b"}


def test_component_prefix_collision():
    addon_dirs = {
        "addons/sale": "sale",
        "addons/sale_stock": "sale_stock",
        "addons/sale_stock_margin": "sale_stock_margin",
    }

    assert resolve_owning_addon("addons/sale/models/sale.py", addon_dirs) == "sale"
    assert resolve_owning_addon("addons/sale_stock/models/stock.py", addon_dirs) == "sale_stock"
    assert resolve_owning_addon("addons/sale_stock_margin/models/margin.py", addon_dirs) == "sale_stock_margin"
    assert resolve_owning_addon("addons/sale_reports/models/report.py", addon_dirs) is None
    assert resolve_owning_addon("addons/sale", addon_dirs) == "sale"
    assert resolve_owning_addon("README.md", addon_dirs) is None


def test_contract_f_unmerged_git_status_and_metadata_parsing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    f = repo / "conflict.txt"
    f.write_text("base content\n")
    c1 = _commit(repo, "c1")

    branch_res = subprocess.run(["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True, check=True)
    base_branch = branch_res.stdout.strip()

    # Create conflict
    subprocess.run(["git", "checkout", "-b", "branch1"], cwd=repo, check=True)
    f.write_text("branch1 content\n")
    _commit(repo, "branch1")

    subprocess.run(["git", "checkout", base_branch], cwd=repo, check=True)
    f.write_text("main content\n")
    _commit(repo, "main")

    # Merge branch1 into base branch producing conflict
    subprocess.run(["git", "merge", "branch1"], cwd=repo, check=False)

    diffs = get_working_tree_diff(repo)
    conflict_records = [d for d in diffs if d.effective_path == "conflict.txt"]
    assert len(conflict_records) == 1
    assert conflict_records[0].status == "U"
    assert conflict_records[0].old_path == "conflict.txt"
    assert conflict_records[0].new_path == "conflict.txt"


def test_contract_f_untracked_broken_symlink(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "init.txt").write_text("hello")
    _commit(repo, "init")

    # Create a broken symlink
    broken_link = repo / "broken_link.txt"
    broken_link.symlink_to(repo / "nonexistent.txt")

    diffs = get_working_tree_diff(repo)
    link_records = [d for d in diffs if d.effective_path == "broken_link.txt"]
    assert len(link_records) == 1
    assert link_records[0].status == "A"
    assert link_records[0].new_mode == "120000"


def test_contract_f_fifo_non_regular_file_protection(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    fifo_path = repo / "my_fifo"
    try:
        os.mkfifo(fifo_path)
    except (AttributeError, OSError):
        pytest.skip("mkfifo not supported on this platform/filesystem")

    # Attempting to read FIFO via read_file_working_tree must return None safely without blocking
    assert read_file_working_tree(repo, "my_fifo") is None
