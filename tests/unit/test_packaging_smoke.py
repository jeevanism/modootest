"""Isolated packaging and installed console script test without polluting environment."""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
import pytest


def test_isolated_wheel_build_and_installed_console_script(tmp_path):
    # 1. Create clean copy of source for isolated build
    build_dir = tmp_path / "build"
    dist_dir = tmp_path / "dist"
    prefix_dir = tmp_path / "env"
    build_dir.mkdir()
    dist_dir.mkdir()

    repo_root = Path(__file__).resolve().parent.parent.parent
    shutil.copy(repo_root / "pyproject.toml", build_dir / "pyproject.toml")
    shutil.copy(repo_root / "README.md", build_dir / "README.md")
    shutil.copytree(repo_root / "src", build_dir / "src")

    # 2. Build wheel via setuptools.build_meta without installing to shared venv
    import setuptools.build_meta as bm

    old_cwd = os.getcwd()
    try:
        os.chdir(build_dir)
        wheel_name = bm.build_wheel(str(dist_dir))
    finally:
        os.chdir(old_cwd)

    wheel_path = dist_dir / wheel_name
    assert wheel_path.exists()
    assert wheel_path.suffix == ".whl"

    # 3. Create a genuinely disposable venv with its own pip and isolated site-packages
    venv_dir = tmp_path / "venv"
    proc_venv = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
    )
    assert proc_venv.returncode == 0, f"venv creation failed: {proc_venv.stderr}"

    venv_python = venv_dir / "bin" / "python"
    venv_pip = venv_dir / "bin" / "pip"
    venv_modootest = venv_dir / "bin" / "modootest"
    assert venv_python.exists(), "Disposable venv python binary not found"
    assert venv_pip.exists(), "Disposable venv pip binary not found"

    # Verify wheel preserves pytest entry point metadata
    with zipfile.ZipFile(wheel_path, "r") as zf:
        ep_files = [n for n in zf.namelist() if n.endswith("entry_points.txt")]
        assert ep_files, "entry_points.txt missing from built wheel"
        ep_content = zf.read(ep_files[0]).decode("utf-8")
        assert "modootest.cli.main:main" in ep_content
        assert "modootest.pytest_plugin.plugin" in ep_content
        schema_files = [
            name
            for name in zf.namelist()
            if name.startswith("modootest/schemas/v1/") and name.endswith(".json")
        ]
        assert len(schema_files) == 6

    # Clean execution environment: remove PYTHONPATH and PYTHONHOME to prevent host pollution
    clean_env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    clean_env["PATH"] = f"{venv_dir / 'bin'}:{clean_env.get('PATH', '')}"

    # Install wheel strictly into the disposable venv using its OWN pip (no network, no deps, no fallback)
    install_cmd = [
        str(venv_pip),
        "install",
        "--no-index",
        "--no-deps",
        str(wheel_path),
    ]
    proc_inst = subprocess.run(install_cmd, capture_output=True, text=True, env=clean_env)
    assert proc_inst.returncode == 0, f"pip install failed:\nstdout: {proc_inst.stdout}\nstderr: {proc_inst.stderr}"
    assert venv_modootest.exists(), "Console script 'modootest' was not installed in disposable venv bin/"

    # 4. Verify import provenance: modootest imports strictly from the disposable venv
    outside_cwd = tmp_path
    check_code = "import sys, modootest; print(sys.prefix); print(modootest.__file__)"
    proc_check = subprocess.run(
        [str(venv_python), "-c", check_code],
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=outside_cwd,
    )
    assert proc_check.returncode == 0
    lines = proc_check.stdout.strip().splitlines()
    assert lines[0] == str(venv_dir), f"Expected sys.prefix == {venv_dir}, got {lines[0]}"
    assert lines[1].startswith(str(venv_dir)), f"Expected modootest from {venv_dir}, got {lines[1]}"
    assert str(repo_root) not in lines[1], f"modootest imported from source tree: {lines[1]}"

    schema_check = subprocess.run(
        [
            str(venv_python),
            "-c",
            "from modootest.agent import list_schemas, load_schema; "
            "names = list_schemas(); assert len(names) == 6; "
            "assert all(load_schema(name)['$id'].endswith(name + '.json') for name in names)",
        ],
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=outside_cwd,
        timeout=10,
    )
    assert schema_check.returncode == 0, schema_check.stderr

    # 5. Test modootest --help and python -m modootest.cli plan --help
    proc_help = subprocess.run([str(venv_modootest), "--help"], capture_output=True, text=True, env=clean_env, cwd=outside_cwd)
    assert proc_help.returncode == 0
    assert "usage: modootest" in proc_help.stdout

    proc_plan_help = subprocess.run([str(venv_modootest), "plan", "--help"], capture_output=True, text=True, env=clean_env, cwd=outside_cwd)
    assert proc_plan_help.returncode == 0
    assert "usage: modootest plan" in proc_plan_help.stdout

    proc_module_help = subprocess.run([str(venv_python), "-m", "modootest.cli", "plan", "--help"], capture_output=True, text=True, env=clean_env, cwd=outside_cwd)
    assert proc_module_help.returncode == 0
    assert "usage: modootest plan" in proc_module_help.stdout

    # 6. Test installed modootest plan on a temporary repository: Exit 0 (complete), Exit 1 (incomplete), Exit 2 (invalid)
    test_repo = tmp_path / "test_repo"
    test_repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=test_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=test_repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=test_repo, check=True)

    addons_dir = test_repo / "addons"
    addon_a = addons_dir / "addon_a"
    addon_a.mkdir(parents=True)
    (addon_a / "__manifest__.py").write_text("{'name': 'addon_a'}")
    (addon_a / "models.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=test_repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=test_repo, check=True)

    # Exit 0: Clean working tree
    proc_exit0 = subprocess.run(
        [str(venv_modootest), "plan", "--repo", str(test_repo), "--addons-path", "addons", "--working-tree"],
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=outside_cwd,
    )
    assert proc_exit0.returncode == 0
    assert "Status: COMPLETE" in proc_exit0.stdout

    # Exit 1: Incomplete plan due to syntax error
    (addon_a / "models.py").write_text("def broken(\n")
    proc_exit1 = subprocess.run(
        [str(venv_modootest), "plan", "--repo", str(test_repo), "--addons-path", "addons", "--working-tree"],
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=outside_cwd,
    )
    assert proc_exit1.returncode == 1
    assert "Status: INCOMPLETE" in proc_exit1.stdout

    # Exit 2: Invalid invocation (bad arguments)
    proc_exit2 = subprocess.run(
        [str(venv_modootest), "plan", "--repo", str(test_repo)],
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=outside_cwd,
    )
    assert proc_exit2.returncode == 2
