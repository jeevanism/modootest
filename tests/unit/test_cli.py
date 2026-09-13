"""Tests for modootest CLI, argument handling, exit codes, and Odoo isolation."""

import os
import subprocess
import sys
from pathlib import Path
import pytest

from modootest.cli.main import main


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)


def _commit(path: Path, msg: str) -> str:
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=path, check=True, capture_output=True)
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True)
    return proc.stdout.decode("utf-8").strip()


def test_cli_help_without_odoo_import():
    # Verify neither 'odoo' nor database connection is loaded when invoking CLI
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from modootest.cli.main import main; sys.exit(main(['plan', '--help']))"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "usage: modootest plan" in proc.stdout

    # In-process verification that 'odoo' is never imported
    assert "odoo" not in sys.modules


def test_cli_exit_2_on_invalid_invocation(tmp_path, capsys):
    # 1. No arguments
    ret = main([])
    assert ret == 2

    # 2. Unknown subcommand
    ret = main(["unknown"])
    assert ret == 2

    # 3. Missing comparison mode
    ret = main(["plan", "--addons-path", "addons"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "Exactly one comparison mode must be specified" in captured.err

    # 4. Conflicting comparison modes
    ret = main(["plan", "--addons-path", "addons", "--working-tree", "--base", "HEAD~1"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "Cannot specify --base or --head together with --working-tree" in captured.err

    # 5. Only base without head
    ret = main(["plan", "--addons-path", "addons", "--base", "HEAD~1"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "Both --base and --head must be specified" in captured.err

    # 6. Non-git repository
    ret = main(["plan", "--repo", str(tmp_path), "--addons-path", "addons", "--working-tree"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "not inside a git repository" in captured.err


def test_cli_exit_2_on_addon_path_escaping_repo(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "init.txt").write_text("hello")
    _commit(repo, "c1")

    # Addons path that escapes git root
    ret = main(["plan", "--repo", str(repo), "--addons-path", "../outside", "--working-tree"])
    assert ret == 2
    captured = capsys.readouterr()
    assert "escapes git repository root" in captured.err


def test_cli_exit_0_complete_plan(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon = addons / "my_addon"
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{'name': 'my_addon', 'depends': []}")
    (addon / "models.py").write_text("# initial\n")
    c1 = _commit(repo, "c1")

    # Change only a test file
    tests_dir = addon / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_model.py").write_text("# new test\n")

    ret = main(["plan", "--repo", str(repo), "--addons-path", "addons", "--working-tree"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "Status: COMPLETE" in captured.out
    assert "Fresh Python process: REQUIRED" in captured.out
    assert "Module update targets: NONE" in captured.out


def test_cli_exit_1_incomplete_plan_on_uncertainty(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon = addons / "my_addon"
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{'name': 'my_addon'}")
    (addon / "models.py").write_text("# initial\n")
    c1 = _commit(repo, "c1")

    # Introduce a syntax error in python file
    (addon / "models.py").write_text("def broken_syntax(\n")

    ret = main(["plan", "--repo", str(repo), "--addons-path", "addons", "--working-tree"])
    assert ret == 1
    captured = capsys.readouterr()
    assert "Status: INCOMPLETE" in captured.out
    assert "WARNING: Plan is INCOMPLETE" in captured.out


def test_cli_python_m_entrypoint(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon = addons / "my_addon"
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{'name': 'my_addon'}")
    (addon / "models.py").write_text("# initial\n")
    _commit(repo, "c1")

    env = dict(os.environ)
    env["PYTHONPATH"] = "src"

    proc = subprocess.run(
        [sys.executable, "-m", "modootest.cli", "plan", "--repo", str(repo), "--addons-path", "addons", "--working-tree"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert "modootest Change Intelligence Plan" in proc.stdout
    assert "Status: COMPLETE" in proc.stdout


def test_r3_cli_exit_1_on_unowned_renamed_to_owned(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "a"
    addon_a.mkdir(parents=True)
    (addon_a / "__manifest__.py").write_text("{'name': 'a'}")
    (addon_a / "models.py").write_text("# base\n")

    script = repo / "script.py"
    script.write_text("""from odoo import models, fields

class ScriptModel(models.Model):
    _name = 'script.model'
    name = fields.Char('Name')
    desc = fields.Text('Description')
""")
    c1 = _commit(repo, "c1")

    subprocess.run(["git", "mv", "script.py", "addons/a/new_model.py"], cwd=repo, check=True)
    (addon_a / "new_model.py").write_text("""from odoo import models, fields

class ScriptModel(models.Model):
    _name = 'script.model'
    name = fields.Char('Name')
    desc = fields.Text('Updated Description')
""")
    c2 = _commit(repo, "c2")

    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    proc = subprocess.run(
        [sys.executable, "-m", "modootest.cli", "plan", "--repo", str(repo), "--addons-path", "addons", "--base", c1, "--head", c2],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 1
    assert "Status: INCOMPLETE" in proc.stdout
    assert "script.py -> addons/a/new_model.py" in proc.stdout
    assert "Module update targets: a" in proc.stdout


def test_r3_cli_exit_2_on_symlink_search_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "init.txt").write_text("hello")
    _commit(repo, "c1")

    (repo / "addons_link").symlink_to(repo)

    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    proc = subprocess.run(
        [sys.executable, "-m", "modootest.cli", "plan", "--repo", str(repo), "--addons-path", "addons_link", "--working-tree"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2
    assert "Unsupported symlink" in proc.stderr


def test_r3_cli_multi_seed_pythonhashseed_determinism(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon = addons / "my_addon"
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{'name': 'my_addon'}")
    (addon / "models.py").write_text("""from odoo import models, fields

class MultiAttrModel(models.Model):
    _name = 'multi.attr'
    field_a = fields.Char('A')
    field_b = fields.Integer('B')
    field_c = fields.Float('C')
    field_d = fields.Boolean('D')
""")
    c1 = _commit(repo, "c1")

    # Change multiple structural attributes on model
    (addon / "models.py").write_text("""from odoo import models, fields

class MultiAttrModel(models.Model):
    _name = 'multi.attr'
    _order = 'field_a desc'
    field_a = fields.Text('A modified')
    field_b = fields.Integer('B')
    field_c = fields.Monetary('C modified')
    field_e = fields.Many2one('res.partner')
""")
    c2 = _commit(repo, "c2 multi-attr change")

    outputs = []
    seeds = ["0", "42", "123456", "999999"]
    for seed in seeds:
        env = dict(os.environ)
        env["PYTHONPATH"] = "src"
        env["PYTHONHASHSEED"] = seed
        proc = subprocess.run(
            [sys.executable, "-m", "modootest.cli", "plan", "--repo", str(repo), "--addons-path", "addons", "--base", c1, "--head", c2],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0
        outputs.append(proc.stdout)

    # Verify that all outputs are identical across distinct seeds
    first_output = outputs[0]
    for idx, out in enumerate(outputs[1:], start=1):
        assert out == first_output, f"Output with PYTHONHASHSEED={seeds[idx]} differed from seed {seeds[0]}!"
