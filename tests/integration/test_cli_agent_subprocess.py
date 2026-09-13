"""Integration test executing real CLI subprocess with PYTEST_DISABLE_PLUGIN_AUTOLOAD=1."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parent.parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    cmd = [sys.executable, "-m", "modootest.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_subprocess_argparse_json_malformed() -> None:
    res = _run_cli(["plan", "--format", "json", "--addons-path", "src", "--working-tree", "--unrecognized-bogus-option"])
    assert res.returncode == 2
    assert res.stderr == ""
    data = json.loads(res.stdout)
    assert data["contract_version"] == "1.0"
    assert data["status"] == "error"
    assert data["exit_code"] == 2
    assert len(data["diagnostics"]) > 0
    assert any("unrecognized arguments" in d["message"].lower() for d in data["diagnostics"])


def test_cli_subprocess_missing_subcommand_json() -> None:
    res = _run_cli(["--format", "json"])
    assert res.returncode == 2
    assert res.stderr == ""
    data = json.loads(res.stdout)
    assert data["status"] == "error"
    assert data["exit_code"] == 2


def test_cli_subprocess_plan_help_remains_text() -> None:
    res = _run_cli(["plan", "--help"])
    assert res.returncode == 0
    assert "usage: modootest plan" in res.stdout
    assert "Analyze Git changes" in res.stdout


def test_cli_subprocess_test_missing_impacted_json() -> None:
    res = _run_cli(["test", "--format", "json", "--repo", ".", "--addons-path", "src", "--working-tree"])
    assert res.returncode == 2
    assert res.stderr == ""
    data = json.loads(res.stdout)
    assert data["command"] == "test"
    assert data["status"] == "error"
    assert data["exit_code"] == 2
    assert any("requires --impacted" in d["message"].lower() for d in data["diagnostics"])


def test_cli_subprocess_plan_format_json() -> None:
    res = _run_cli(["plan", "--format", "json", "--addons-path", "src", "--working-tree"])
    # May be 0 (success) or 1 (incomplete analysis when unowned files exist)
    assert res.returncode in (0, 1)
    assert res.stderr == ""
    data = json.loads(res.stdout)
    assert data["contract_version"] == "1.0"
    assert data["command"] == "plan"
    assert "payload" in data
    assert "diagnostics" in data


def test_cli_subprocess_global_format_json_placement() -> None:
    res = _run_cli(["--format", "json", "plan", "--addons-path", "src", "--working-tree"])
    assert res.returncode in (0, 1)
    assert res.stderr == ""
    data = json.loads(res.stdout)
    assert data["contract_version"] == "1.0"
    assert data["command"] == "plan"


def test_cli_subprocess_secret_redaction() -> None:
    res = _run_cli([
        "impacted",
        "--format",
        "json",
        "--addons-path",
        "src",
        "--working-tree",
        "--pytest-arg",
        "--db_password=SUPER_SECRET_SUBPROCESS_VALUE",
    ])
    assert res.returncode == 2
    assert res.stderr == ""
    assert "SUPER_SECRET_SUBPROCESS_VALUE" not in res.stdout
    data = json.loads(res.stdout)
    assert data["status"] == "error"
    diag_msg = data["diagnostics"][0]["message"]
    assert "--db_password=[REDACTED]" in diag_msg
