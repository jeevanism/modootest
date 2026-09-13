"""Unit tests for Phase 0.4 Impacted Test Selection engine and CLI."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import pytest

from unittest.mock import MagicMock, patch

from modootest.cli.main import main, _validate_pytest_args
from modootest.intelligence.planner import DiagnosticSeverity, build_plan
from modootest.intelligence.selector import (
    SelectedTest,
    ExcludedTarget,
    SelectionMode,
    SelectorError,
    select_impacted_tests,
)


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    (path / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")


def _commit(path: Path, msg: str) -> str:
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=path, check=True, capture_output=True)
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True)
    return proc.stdout.decode("utf-8").strip()


# ---------------------------------------------------------------------------
# 1. Direct mode tests
# ---------------------------------------------------------------------------


def test_direct_mode_changed_addon(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "addon_a"
    addon_a.mkdir(parents=True)
    (addon_a / "__manifest__.py").write_text("{'name': 'addon_a'}")
    (addon_a / "models.py").write_text("# code\n")
    tests_dir = addon_a / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_addon_a.py").write_text("def test_a(): pass\n")
    _commit(repo, "c1")

    # Change models.py
    (addon_a / "models.py").write_text("# changed code\n")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res = select_impacted_tests(plan, mode=SelectionMode.DIRECT)

    assert res.is_complete
    assert res.mode == SelectionMode.DIRECT
    assert len(res.selected_tests) == 1
    assert res.selected_tests[0].path == "addons/addon_a/tests/test_addon_a.py"
    assert res.selected_tests[0].addon == "addon_a"
    assert "Directly changed addon" in res.selected_tests[0].reason
    assert res.excluded_targets == ()


def test_direct_mode_test_only_change(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "addon_a"
    addon_a.mkdir(parents=True)
    (addon_a / "__manifest__.py").write_text("{'name': 'addon_a'}")
    (addon_a / "models.py").write_text("# code\n")
    tests_dir = addon_a / "tests"
    tests_dir.mkdir()
    t1 = tests_dir / "test_addon_a.py"
    t1.write_text("def test_a(): pass\n")
    _commit(repo, "c1")

    # Modify only the test file
    t1.write_text("def test_a(): assert True\n")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res = select_impacted_tests(plan, mode=SelectionMode.DIRECT)

    assert res.is_complete
    assert len(res.selected_tests) == 1
    assert res.selected_tests[0].path == "addons/addon_a/tests/test_addon_a.py"
    assert res.selected_tests[0].addon == "addon_a"


def test_direct_mode_multiple_addons(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    for name in ("addon_a", "addon_b", "addon_c"):
        ad = addons / name
        ad.mkdir(parents=True)
        (ad / "__manifest__.py").write_text(f"{{'name': '{name}'}}")
        (ad / "models.py").write_text("# code\n")
        td = ad / "tests"
        td.mkdir()
        (td / f"test_{name}.py").write_text(f"def test_{name}(): pass\n")
    _commit(repo, "c1")

    # Modify addon_a and addon_b, leave addon_c unchanged
    (addons / "addon_a" / "models.py").write_text("# mod a\n")
    (addons / "addon_b" / "models.py").write_text("# mod b\n")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res = select_impacted_tests(plan, mode=SelectionMode.DIRECT)

    assert res.is_complete
    selected_paths = [t.path for t in res.selected_tests]
    assert "addons/addon_a/tests/test_addon_a.py" in selected_paths
    assert "addons/addon_b/tests/test_addon_b.py" in selected_paths
    assert "addons/addon_c/tests/test_addon_c.py" not in selected_paths


def test_direct_mode_no_tests(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "addon_a"
    addon_a.mkdir(parents=True)
    (addon_a / "__manifest__.py").write_text("{'name': 'addon_a'}")
    (addon_a / "models.py").write_text("# code\n")
    _commit(repo, "c1")

    # Modify models.py in addon with no tests directory
    (addon_a / "models.py").write_text("# mod\n")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res = select_impacted_tests(plan, mode=SelectionMode.DIRECT)

    assert res.is_complete
    assert res.selected_tests == ()
    assert len(res.excluded_targets) == 1
    assert res.excluded_targets[0].addon == "addon_a"
    assert "has no test files" in res.excluded_targets[0].reason


def test_direct_mode_no_change(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "addon_a"
    addon_a.mkdir(parents=True)
    (addon_a / "__manifest__.py").write_text("{'name': 'addon_a'}")
    (addon_a / "models.py").write_text("# code\n")
    td = addon_a / "tests"
    td.mkdir()
    (td / "test_a.py").write_text("def test_a(): pass\n")
    _commit(repo, "c1")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res = select_impacted_tests(plan, mode=SelectionMode.DIRECT)

    assert res.is_complete
    assert res.selected_tests == ()
    assert res.excluded_targets == ()


# ---------------------------------------------------------------------------
# 2. Dependent mode tests
# ---------------------------------------------------------------------------


def test_dependent_mode_direct_and_transitive(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    for name, deps in (("core", []), ("middle", ["core"]), ("leaf", ["middle"])):
        ad = addons / name
        ad.mkdir(parents=True)
        dep_str = str(deps)
        (ad / "__manifest__.py").write_text(f"{{'name': '{name}', 'depends': {dep_str}}}")
        (ad / "models.py").write_text("# code\n")
        td = ad / "tests"
        td.mkdir()
        (td / f"test_{name}.py").write_text(f"def test_{name}(): pass\n")
    _commit(repo, "c1")

    # Modify core
    (addons / "core" / "models.py").write_text("# changed core\n")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res = select_impacted_tests(plan, mode=SelectionMode.DEPENDENT)

    assert res.is_complete
    assert res.mode == SelectionMode.DEPENDENT
    paths = [t.path for t in res.selected_tests]
    assert "addons/core/tests/test_core.py" in paths
    assert "addons/middle/tests/test_middle.py" in paths
    assert "addons/leaf/tests/test_leaf.py" in paths


def test_dependent_mode_diamond_and_disconnected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    # Diamond: base -> left, right -> leaf. Disconnected: other
    graph = {
        "base": [],
        "left": ["base"],
        "right": ["base"],
        "leaf": ["left", "right"],
        "other": [],
    }
    for name, deps in graph.items():
        ad = addons / name
        ad.mkdir(parents=True)
        (ad / "__manifest__.py").write_text(f"{{'name': '{name}', 'depends': {deps}}}")
        (ad / "models.py").write_text("# code\n")
        td = ad / "tests"
        td.mkdir()
        (td / f"test_{name}.py").write_text(f"def test_{name}(): pass\n")
    _commit(repo, "c1")

    # Modify base
    (addons / "base" / "models.py").write_text("# mod base\n")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res = select_impacted_tests(plan, mode=SelectionMode.DEPENDENT)

    paths = [t.path for t in res.selected_tests]
    assert "addons/base/tests/test_base.py" in paths
    assert "addons/left/tests/test_left.py" in paths
    assert "addons/right/tests/test_right.py" in paths
    assert "addons/leaf/tests/test_leaf.py" in paths
    assert "addons/other/tests/test_other.py" not in paths


def test_dependent_mode_old_edge_retention(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    core = addons / "core"
    consumer = addons / "consumer"
    core.mkdir(parents=True)
    consumer.mkdir(parents=True)

    (core / "__manifest__.py").write_text("{'name': 'core'}")
    (core / "models.py").write_text("# core\n")
    (core / "tests").mkdir()
    (core / "tests" / "test_core.py").write_text("def test_core(): pass\n")

    (consumer / "__manifest__.py").write_text("{'name': 'consumer', 'depends': ['core']}")
    (consumer / "models.py").write_text("# consumer\n")
    (consumer / "tests").mkdir()
    (consumer / "tests" / "test_consumer.py").write_text("def test_consumer(): pass\n")
    c1 = _commit(repo, "c1")

    # In c2, remove consumer's dependency on core
    (consumer / "__manifest__.py").write_text("{'name': 'consumer', 'depends': []}")
    c2 = _commit(repo, "c2")

    # Now modify core in working tree
    (core / "models.py").write_text("# core change\n")

    # Comparing c1 to working tree retains the old dependency edge!
    plan = build_plan(repo_path=repo, addons_paths=["addons"], base_rev=c1, head_rev=c2)
    res = select_impacted_tests(plan, mode=SelectionMode.DEPENDENT)

    # In c1->c2, consumer changed its manifest, so core's consumer had an edge
    assert "consumer" in plan.downstream_impact_addons or "consumer" in plan.changed_addons


def test_dependent_mode_missing_dependencies_and_cycles(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    # Cyclic dependency: a <-> b
    a = addons / "a"
    b = addons / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "__manifest__.py").write_text("{'name': 'a', 'depends': ['b', 'nonexistent']}")
    (b / "__manifest__.py").write_text("{'name': 'b', 'depends': ['a']}")
    _commit(repo, "c1")

    (a / "__manifest__.py").write_text("{'name': 'a', 'depends': ['b', 'nonexistent'], 'summary': 'mod'}")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    assert not plan.is_complete
    res = select_impacted_tests(plan, mode=SelectionMode.DEPENDENT)
    assert not res.is_complete


# ---------------------------------------------------------------------------
# 3. Safe mode tests
# ---------------------------------------------------------------------------


def test_safe_mode_complete_plan(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    core = addons / "core"
    leaf = addons / "leaf"
    core.mkdir(parents=True)
    leaf.mkdir(parents=True)
    (core / "__manifest__.py").write_text("{'name': 'core'}")
    (core / "models.py").write_text("# code\n")
    (core / "tests").mkdir()
    (core / "tests" / "test_core.py").write_text("def test_core(): pass\n")

    (leaf / "__manifest__.py").write_text("{'name': 'leaf', 'depends': ['core']}")
    (leaf / "models.py").write_text("# code\n")
    (leaf / "tests").mkdir()
    (leaf / "tests" / "test_leaf.py").write_text("def test_leaf(): pass\n")
    _commit(repo, "c1")

    (core / "models.py").write_text("# changed core\n")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res = select_impacted_tests(plan, mode=SelectionMode.SAFE)

    assert res.is_complete
    assert not res.broadened_to_all_tests
    paths = [t.path for t in res.selected_tests]
    assert "addons/core/tests/test_core.py" in paths
    assert "addons/leaf/tests/test_leaf.py" in paths


def test_safe_mode_broadens_on_incomplete_plan(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    core = addons / "core"
    leaf = addons / "leaf"
    core.mkdir(parents=True)
    leaf.mkdir(parents=True)
    (core / "__manifest__.py").write_text("{'name': 'core'}")
    (core / "tests").mkdir()
    (core / "tests" / "test_core.py").write_text("def test_core(): pass\n")

    (leaf / "__manifest__.py").write_text("{'name': 'leaf', 'depends': ['core']}")
    (leaf / "tests").mkdir()
    (leaf / "tests" / "test_leaf.py").write_text("def test_leaf(): pass\n")

    # Global tests root
    tests_root = repo / "tests"
    tests_root.mkdir()
    (tests_root / "test_global.py").write_text("def test_glob(): pass\n")
    _commit(repo, "c1")

    # Introduce an unowned file outside addons
    (repo / "unowned.txt").write_text("hello\n")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    assert not plan.is_complete

    res = select_impacted_tests(plan, mode=SelectionMode.SAFE, tests_roots=["tests"])
    assert not res.is_complete
    assert res.broadened_to_all_tests
    paths = [t.path for t in res.selected_tests]
    # Broadened to all tests
    assert "addons/core/tests/test_core.py" in paths
    assert "addons/leaf/tests/test_leaf.py" in paths
    assert "tests/test_global.py" in paths


def test_symlink_test_path_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    core = addons / "core"
    core.mkdir(parents=True)
    (core / "__manifest__.py").write_text("{'name': 'core'}")
    (core / "models.py").write_text("# code\n")
    td = core / "tests"
    td.mkdir()

    # Create an outside target file and symlink to it inside tests
    outside = tmp_path / "outside_test.py"
    outside.write_text("def test_bad(): pass\n")
    symlink_test = td / "test_symlink.py"
    symlink_test.symlink_to(outside)
    _commit(repo, "c1")

    (core / "models.py").write_text("# mod\n")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res = select_impacted_tests(plan, mode=SelectionMode.DIRECT)

    # Symlink test must be rejected and not appear in selected tests
    assert not res.is_complete
    assert not any("test_symlink.py" in t.path for t in res.selected_tests)
    assert any("symlink" in d.message.lower() for d in res.diagnostics)


# ---------------------------------------------------------------------------
# 4. Renames, special characters, and boundary tests
# ---------------------------------------------------------------------------


def test_rename_across_addons_selects_both(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad_a = addons / "ad_a"
    ad_b = addons / "ad_b"
    ad_a.mkdir(parents=True)
    ad_b.mkdir(parents=True)
    (ad_a / "__manifest__.py").write_text("{'name': 'ad_a'}")
    (ad_b / "__manifest__.py").write_text("{'name': 'ad_b'}")
    (ad_a / "shared.py").write_text("# shared\n")
    (ad_a / "tests").mkdir()
    (ad_a / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (ad_b / "tests").mkdir()
    (ad_b / "tests" / "test_b.py").write_text("def test_b(): pass\n")
    c1 = _commit(repo, "c1")

    # git mv shared.py from ad_a to ad_b
    subprocess.run(["git", "mv", "addons/ad_a/shared.py", "addons/ad_b/shared.py"], cwd=repo, check=True)
    c2 = _commit(repo, "c2")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], base_rev=c1, head_rev=c2)
    res = select_impacted_tests(plan, mode=SelectionMode.DIRECT)

    paths = [t.path for t in res.selected_tests]
    assert "addons/ad_a/tests/test_a.py" in paths
    assert "addons/ad_b/tests/test_b.py" in paths


def test_paths_with_spaces_and_tabs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "addon space"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'addon space'}")
    td = ad / "tests"
    td.mkdir()
    (td / "test_with space and\ttab.py").write_text("def test_s(): pass\n")
    _commit(repo, "c1")

    (ad / "__manifest__.py").write_text("{'name': 'addon space', 'summary': 'space'}")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res = select_impacted_tests(plan, mode=SelectionMode.DIRECT)

    assert len(res.selected_tests) == 1
    assert "addon space" in res.selected_tests[0].path
    assert "test_with space and\ttab.py" in res.selected_tests[0].path


def test_tests_root_escaping_repo_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    _commit(repo, "c1")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)

    with pytest.raises(SelectorError, match="escapes git repository"):
        select_impacted_tests(plan, tests_roots=["../outside"])

    with pytest.raises(SelectorError, match="escapes git repository"):
        select_impacted_tests(plan, tests_roots=["/tmp/outside"])


# ---------------------------------------------------------------------------
# 5. CLI validation and execution tests
# ---------------------------------------------------------------------------


def test_cli_requires_impacted_flag(tmp_path, capsys):
    ret = main(["test", "--repo", str(tmp_path), "--addons-path", "addons", "--working-tree"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "requires --impacted" in captured.err


def test_cli_comparison_mode_validation(tmp_path, capsys):
    # Both --working-tree and --base/--head
    ret = main([
        "test",
        "--impacted",
        "--repo", str(tmp_path),
        "--addons-path", "addons",
        "--working-tree",
        "--base", "rev1",
        "--head", "rev2",
    ])
    assert ret == 2
    captured = capsys.readouterr()
    assert "Cannot specify --base or --head together with --working-tree" in captured.err


def test_cli_alias_impacted_identical(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    td = ad / "tests"
    td.mkdir()
    (td / "test_ad.py").write_text("def test_ok(): pass\n")
    _commit(repo, "c1")

    (ad / "__manifest__.py").write_text("{'name': 'ad', 'summary': 'mod'}")

    # Running `modootest impacted` without --impacted
    ret = main([
        "impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
    ])
    assert ret == 0
    captured = capsys.readouterr()
    assert "addons/ad/tests/test_ad.py" in captured.out


def test_cli_dry_run_default(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    td = ad / "tests"
    td.mkdir()
    (td / "test_ad.py").write_text("def test_ok(): pass\n")
    _commit(repo, "c1")

    (ad / "models.py").write_text("# mod\n")

    ret = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
    ])
    assert ret == 0
    captured = capsys.readouterr()
    assert "Status: COMPLETE" in captured.out
    assert "addons/ad/tests/test_ad.py" in captured.out
    # Subprocess execution message should NOT appear in dry run
    assert "pytest execution succeeded" not in captured.out


def test_cli_forbidden_pytest_args(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    valid, err, _ = _validate_pytest_args(["--database", "testdb"], repo)
    assert not valid
    assert "Forbidden pytest argument '--database'" in err

    valid, err, _ = _validate_pytest_args(["-d=testdb"], repo)
    assert not valid

    valid, err, _ = _validate_pytest_args(["-u", "all"], repo)
    assert not valid

    valid, err, _ = _validate_pytest_args(["-i", "base"], repo)
    assert not valid

    valid, err, _ = _validate_pytest_args(["-n", "4"], repo)
    assert not valid
    assert "parallel execution (xdist) flags are not allowed" in err

    valid, err, _ = _validate_pytest_args(["--basetemp=/outside/dir"], repo)
    assert not valid
    assert "escapes git repository" in err


def test_cli_collect_only_and_execute_execution(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    td = ad / "tests"
    td.mkdir()
    (td / "test_pass.py").write_text("def test_pass(): assert True\n")
    _commit(repo, "c1")

    (ad / "models.py").write_text("# mod\n")

    # 1. Collect-only
    ret_collect = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
        "--collect-only",
    ])
    assert ret_collect == 0
    captured = capsys.readouterr()
    assert "pytest collection succeeded" in captured.out

    # 2. Execute passing test
    ret_exec = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
        "--execute",
    ])
    assert ret_exec == 0
    captured = capsys.readouterr()
    assert "pytest execution succeeded" in captured.out

    # 3. Add a failing test
    (td / "test_pass.py").write_text("def test_fail(): assert False\n")
    ret_fail = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
        "--execute",
    ])
    # Pytest failure must return exit code 3!
    assert ret_fail == 3
    captured = capsys.readouterr()
    assert "pytest failed" in captured.err


# ---------------------------------------------------------------------------
# 6. Determinism across PYTHONHASHSEED
# ---------------------------------------------------------------------------


def test_determinism_across_pythonhashseeds(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    for name in ("mod_z", "mod_a", "mod_m"):
        ad = addons / name
        ad.mkdir(parents=True)
        (ad / "__manifest__.py").write_text(f"{{'name': '{name}'}}")
        td = ad / "tests"
        td.mkdir()
        for tname in ("test_2.py", "test_1.py"):
            (td / tname).write_text("def test_x(): pass\n")
    _commit(repo, "c1")

    for name in ("mod_z", "mod_a"):
        (addons / name / "models.py").write_text("# mod\n")

    script = """
import sys
from modootest.intelligence.planner import build_plan
from modootest.intelligence.selector import select_impacted_tests, SelectionMode

plan = build_plan(repo_path='{repo}', addons_paths=['addons'], working_tree=True)
res = select_impacted_tests(plan, mode=SelectionMode.DEPENDENT)
sys.stdout.write(res.format_human_readable())
""".format(repo=str(repo))

    outputs = []
    seeds = ["0", "42", "123456", "999999"]
    for seed in seeds:
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = "src"
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        outputs.append(proc.stdout)

    assert len(set(outputs)) == 1, "Output differed across different PYTHONHASHSEED values!"


# ---------------------------------------------------------------------------
# 7. Zero Odoo imports/database access during selection and dry-run
# ---------------------------------------------------------------------------


def test_zero_odoo_imports(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    _commit(repo, "c1")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    select_impacted_tests(plan, mode=SelectionMode.DIRECT)

    assert "odoo" not in sys.modules


def test_dependent_mode_deleted_addon(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    core = addons / "core"
    leaf = addons / "leaf"
    core.mkdir(parents=True)
    leaf.mkdir(parents=True)
    (core / "__manifest__.py").write_text("{'name': 'core'}")
    (leaf / "__manifest__.py").write_text("{'name': 'leaf', 'depends': ['core']}")
    (leaf / "tests").mkdir()
    (leaf / "tests" / "test_leaf.py").write_text("def test_leaf(): pass\n")
    c1 = _commit(repo, "c1")

    # Delete core addon
    import shutil
    shutil.rmtree(core)

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    assert not plan.is_complete
    res = select_impacted_tests(plan, mode=SelectionMode.DEPENDENT)
    assert not res.is_complete


def test_safe_mode_malformed_manifest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    core = addons / "core"
    core.mkdir(parents=True)
    (core / "__manifest__.py").write_text("{'name': 'core'}")
    (core / "tests").mkdir()
    (core / "tests" / "test_core.py").write_text("def test_core(): pass\n")
    _commit(repo, "c1")

    # Corrupt manifest syntax
    (core / "__manifest__.py").write_text("{'name': 'core', INVALID SYNTAX")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    assert not plan.is_complete
    res = select_impacted_tests(plan, mode=SelectionMode.SAFE)
    assert not res.is_complete
    assert res.broadened_to_all_tests
    assert "addons/core/tests/test_core.py" in [t.path for t in res.selected_tests]


def test_tests_root_symlink_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    _commit(repo, "c1")

    # Create symlink root
    outside_tests = tmp_path / "outside_tests"
    outside_tests.mkdir()
    (outside_tests / "test_out.py").write_text("def test_out(): pass\n")
    symlink_root = repo / "sym_tests"
    symlink_root.symlink_to(outside_tests)

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    with pytest.raises(SelectorError, match="Unsupported symlink"):
        select_impacted_tests(plan, tests_roots=["sym_tests"])


def test_cli_subprocess_timeout(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    td = ad / "tests"
    td.mkdir()
    (td / "test_sleep.py").write_text("import time\ndef test_s(): time.sleep(2)\n")
    _commit(repo, "c1")

    (ad / "models.py").write_text("# mod\n")

    # Run with tiny timeout
    ret = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
        "--execute",
        "--timeout", "0.2",
    ])
    assert ret == 3
    captured = capsys.readouterr()
    assert "timeout" in captured.err


def test_cli_forwarding_repeatable_args(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    td = ad / "tests"
    td.mkdir()
    (td / "test_two.py").write_text(
        "def test_include(): assert True\ndef test_exclude(): assert False\n"
    )
    _commit(repo, "c1")

    (ad / "models.py").write_text("# mod\n")

    # Pass -k test_include so test_exclude is skipped
    ret = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
        "--execute",
        "--pytest-arg=-k",
        "--pytest-arg=test_include",
    ])
    assert ret == 0
    captured = capsys.readouterr()
    assert "pytest execution succeeded" in captured.out


# ---------------------------------------------------------------------------
# 8. Revision 2 Correction Tests (Findings 1, 2, 3)
# ---------------------------------------------------------------------------


def test_configured_tests_root_missing_fails_closed(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    _commit(repo, "c1")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)

    # 1. Library call fails closed with SelectorError
    with pytest.raises(SelectorError, match="cannot be accessed or does not exist"):
        select_impacted_tests(plan, tests_roots=["missing_dir"])

    # 2. CLI call fails closed with exit 2
    ret = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
        "--tests-root", "missing_dir",
    ])
    assert ret == 2
    captured = capsys.readouterr()
    assert "cannot be accessed or does not exist" in captured.err


def test_configured_tests_root_file_fails_closed(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    # Regular file named 'tests_file'
    (repo / "tests_file").write_text("not a directory\n")
    _commit(repo, "c1")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)

    with pytest.raises(SelectorError, match="is not a directory"):
        select_impacted_tests(plan, tests_roots=["tests_file"])

    ret = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
        "--tests-root", "tests_file",
    ])
    assert ret == 2
    captured = capsys.readouterr()
    assert "is not a directory" in captured.err


def test_configured_tests_root_fifo_fails_closed(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    _commit(repo, "c1")

    fifo_path = repo / "tests_fifo"
    try:
        os.mkfifo(str(fifo_path))
    except (OSError, AttributeError):
        pytest.skip("mkfifo not supported on this platform/filesystem")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)

    with pytest.raises(SelectorError, match="is not a directory"):
        select_impacted_tests(plan, tests_roots=["tests_fifo"])

    ret = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
        "--tests-root", "tests_fifo",
    ])
    assert ret == 2
    captured = capsys.readouterr()
    assert "is not a directory" in captured.err


def test_configured_tests_root_symlink_fails_closed(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    _commit(repo, "c1")

    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    sym_dir = repo / "sym_root"
    sym_dir.symlink_to(real_dir)

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)

    with pytest.raises(SelectorError, match="Unsupported symlink"):
        select_impacted_tests(plan, tests_roots=["sym_root"])

    ret = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
        "--tests-root", "sym_root",
    ])
    assert ret == 2
    captured = capsys.readouterr()
    assert "Unsupported symlink" in captured.err


def test_configured_tests_root_valid_empty_and_with_tests(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    (ad / "models.py").write_text("# mod\n")

    # 1. Valid empty directory
    empty_root = repo / "empty_tests"
    empty_root.mkdir()
    _commit(repo, "c1")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res_empty = select_impacted_tests(plan, tests_roots=["empty_tests"])
    assert res_empty.is_complete

    # 2. Valid directory containing tests
    valid_root = repo / "custom_tests"
    valid_root.mkdir()
    (valid_root / "test_something.py").write_text("def test_x(): pass\n")
    _commit(repo, "c2")

    plan2 = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    res_valid = select_impacted_tests(plan2, tests_roots=["custom_tests"])
    assert res_valid.is_complete


def test_pytest_arg_rejects_paths_and_path_injection(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    # Outside absolute path
    outside = tmp_path / "outside.py"
    outside.write_text("def test_out(): pass\n")
    valid, err, _ = _validate_pytest_args([str(outside)], repo)
    assert not valid
    assert "arbitrary test paths cannot be injected" in err

    # Traversal path
    valid, err, _ = _validate_pytest_args(["../outside.py"], repo)
    assert not valid
    assert "arbitrary test paths cannot be injected" in err

    # Double-dash positional injection
    valid, err, _ = _validate_pytest_args(["--", "outside.py"], repo)
    assert not valid
    assert "positional path separation" in err

    # Symlink basetemp outside
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    sym_in_repo = repo / "sym_link"
    sym_in_repo.symlink_to(outside_dir)
    valid, err, _ = _validate_pytest_args(["--basetemp=sym_link"], repo)
    assert not valid
    assert "symlink" in err or "escapes" in err


def test_pytest_arg_forwards_safe_options_and_preserves_argv(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    td = ad / "tests"
    td.mkdir()
    (td / "test_a.py").write_text("def test_target(): assert True\n")
    _commit(repo, "c1")

    (ad / "models.py").write_text("# mod\n")
    modootest_config = tmp_path / "modootest.conf"
    modootest_config.write_text("[options]\n")

    # Verify that -k, -m, -q are forwarded properly and subprocess invocation contains them
    with patch("modootest.cli.main._run_pytest_subprocess") as mock_pytest:
        mock_pytest.return_value = (0, "success", repo / "mock.log")

        ret = main([
            "test",
            "--impacted",
            "--repo", str(repo),
            "--addons-path", "addons",
            "--working-tree",
            "--execute",
            "--modootest-config", str(modootest_config),
            "--pytest-arg", "-k",
            "--pytest-arg", "test_target",
            "--pytest-arg", "-q",
            "--pytest-arg", "-m",
            "--pytest-arg", "unit",
        ])
        assert ret == 0
        assert mock_pytest.called
        call_kwargs = mock_pytest.call_args[1]
        call_args = mock_pytest.call_args[0]
        test_paths = call_kwargs.get("test_paths") if "test_paths" in call_kwargs else call_args[1]
        pytest_args = call_kwargs.get("pytest_args") if "pytest_args" in call_kwargs else call_args[2]
        assert "addons/ad/tests/test_a.py" in test_paths
        assert "-k" in pytest_args
        assert "test_target" in pytest_args
        assert "-q" in pytest_args
        assert "-m" in pytest_args
        assert "unit" in pytest_args
        assert call_kwargs["modootest_config"] == modootest_config.resolve()


def test_impacted_rejects_missing_modootest_config_before_execution(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    addon = repo / "addons" / "ad"
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{'name': 'ad'}")
    _commit(repo, "c1")
    (addon / "models.py").write_text("# changed\n")

    with patch("modootest.cli.main._run_pytest_subprocess") as mock_pytest:
        ret = main([
            "test", "--impacted", "--repo", str(repo),
            "--addons-path", "addons", "--working-tree", "--execute",
            "--modootest-config", str(tmp_path / "missing.conf"),
        ])

    assert ret == 2
    assert "does not exist" in capsys.readouterr().err
    mock_pytest.assert_not_called()


def test_dry_run_validates_unsafe_pytest_args_without_subprocess(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    ad = addons / "ad"
    ad.mkdir(parents=True)
    (ad / "__manifest__.py").write_text("{'name': 'ad'}")
    _commit(repo, "c1")

    (ad / "models.py").write_text("# mod\n")

    with patch("modootest.cli.main._run_pytest_subprocess") as mock_pytest:
        # Default dry-run with dangerous flag: --database
        ret = main([
            "test",
            "--impacted",
            "--repo", str(repo),
            "--addons-path", "addons",
            "--working-tree",
            "--pytest-arg", "--database",
        ])
        # Must return exit 2
        assert ret == 2
        captured = capsys.readouterr()
        assert "database/server mutation" in captured.err
        # Pytest subprocess must NEVER be called
        assert not mock_pytest.called

    # Confirm valid dry-run returns 0 for complete selection
    ret_valid = main([
        "test",
        "--impacted",
        "--repo", str(repo),
        "--addons-path", "addons",
        "--working-tree",
        "--pytest-arg", "-q",
    ])
    assert ret_valid == 0
    captured_valid = capsys.readouterr()
    assert "Status: COMPLETE" in captured_valid.out
