"""Tests for the change intelligence planner orchestration."""

import subprocess
from pathlib import Path
import pytest

from modootest.intelligence.classifier import ChangeCategory
from modootest.intelligence.planner import build_plan


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)


def _commit(path: Path, msg: str) -> str:
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=path, check=True, capture_output=True)
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True)
    return proc.stdout.decode("utf-8").strip()


def test_planner_clean_no_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "addon_a"
    addon_a.mkdir(parents=True)
    (addon_a / "__manifest__.py").write_text("{'name': 'addon_a'}")
    (addon_a / "models.py").write_text("# code\n")
    c1 = _commit(repo, "c1")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    assert plan.is_complete
    assert not plan.fresh_process_required
    assert plan.module_update_targets == ()
    assert plan.changed_addons == ()
    assert plan.downstream_impact_addons == ()
    assert "No changes detected" in plan.format_human_readable()


def test_planner_method_only_vs_field_change_and_downstream(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_core = addons / "core"
    addon_ext = addons / "ext"
    addon_core.mkdir(parents=True)
    addon_ext.mkdir(parents=True)

    (addon_core / "__manifest__.py").write_text("{'name': 'core'}")
    (addon_ext / "__manifest__.py").write_text("{'name': 'ext', 'depends': ['core']}")

    (addon_core / "models.py").write_text("""
from odoo import models, fields, api

class Core(models.Model):
    _name = 'core.model'
    name = fields.Char('Name')

    @api.model
    def do_work(self):
        return 1
""")
    c1 = _commit(repo, "c1")

    # 1. Modify only method body in core
    (addon_core / "models.py").write_text("""
from odoo import models, fields, api

class Core(models.Model):
    _name = 'core.model'
    name = fields.Char('Name')

    @api.model
    def do_work(self):
        # Changed implementation
        return 42
""")
    c2 = _commit(repo, "c2 method change")

    plan_method = build_plan(repo_path=repo, addons_paths=["addons"], base_rev=c1, head_rev=c2)
    assert plan_method.is_complete
    assert plan_method.fresh_process_required is True
    # Method only does NOT require module update!
    assert plan_method.module_update_targets == ()
    assert plan_method.changed_addons == ("core",)
    # Downstream dependent ext is identified
    assert plan_method.downstream_impact_addons == ("ext",)

    # 2. Add an ORM field in core
    (addon_core / "models.py").write_text("""
from odoo import models, fields, api

class Core(models.Model):
    _name = 'core.model'
    name = fields.Char('Name')
    active = fields.Boolean('Active', default=True)

    @api.model
    def do_work(self):
        return 42
""")
    c3 = _commit(repo, "c3 field change")

    plan_field = build_plan(repo_path=repo, addons_paths=["addons"], base_rev=c2, head_rev=c3)
    assert plan_field.is_complete
    assert plan_field.fresh_process_required is True
    # Field declaration requires module update!
    assert plan_field.module_update_targets == ("core",)
    assert plan_field.changed_addons == ("core",)
    assert plan_field.downstream_impact_addons == ("ext",)


def test_planner_old_dependency_edges_retained(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "addon_a"
    addon_b = addons / "addon_b"
    addon_a.mkdir(parents=True)
    addon_b.mkdir(parents=True)

    (addon_a / "__manifest__.py").write_text("{'name': 'addon_a'}")
    # In base commit, addon_b depends on addon_a
    (addon_b / "__manifest__.py").write_text("{'name': 'addon_b', 'depends': ['addon_a']}")
    (addon_a / "models.py").write_text("x = 1\n")
    (addon_b / "models.py").write_text("y = 1\n")
    c1 = _commit(repo, "c1")

    # In head commit, addon_b removes dependency on addon_a!
    (addon_b / "__manifest__.py").write_text("{'name': 'addon_b', 'depends': []}")
    (addon_a / "models.py").write_text("x = 2\n")
    c2 = _commit(repo, "c2 remove dependency and change addon_a")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], base_rev=c1, head_rev=c2)
    # Even though head manifest graph has no edge addon_a -> addon_b,
    # the old graph DOES have it! The conservative snapshot policy combines both!
    assert "addon_b" in plan.downstream_impact_addons or "addon_b" in plan.changed_addons


def test_planner_deleted_addon_handling(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "addon_a"
    addon_dep = addons / "addon_dep"
    addon_a.mkdir(parents=True)
    addon_dep.mkdir(parents=True)

    (addon_a / "__manifest__.py").write_text("{'name': 'addon_a'}")
    (addon_dep / "__manifest__.py").write_text("{'name': 'addon_dep', 'depends': ['addon_a']}")
    c1 = _commit(repo, "c1")

    # Delete addon_a entirely
    subprocess.run(["git", "rm", "-r", str(addon_a)], cwd=repo, check=True)
    c2 = _commit(repo, "c2 delete addon_a")

    plan = build_plan(repo_path=repo, addons_paths=["addons"], base_rev=c1, head_rev=c2)
    # Deleted addon marks plan INCOMPLETE (manual action / migration review needed)
    assert not plan.is_complete
    assert "addon_a" in plan.deleted_addons
    # Removed addon cannot be an update target!
    assert "addon_a" not in plan.module_update_targets
    # Former downstream dependent is preserved
    assert "addon_dep" in plan.downstream_impact_addons
    # Warning diagnostic present
    assert any("removed addons cannot be update targets" in d.message for d in plan.diagnostics)


def test_planner_unmerged_conflict_marks_incomplete(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "addon_a"
    addon_a.mkdir(parents=True)
    (addon_a / "__manifest__.py").write_text("{'name': 'addon_a'}")
    conflict_file = addon_a / "conflict.txt"
    conflict_file.write_text("base\n")
    c1 = _commit(repo, "c1")

    # Create a branch and conflicting change
    subprocess.run(["git", "checkout", "-b", "branch1"], cwd=repo, check=True)
    conflict_file.write_text("branch1 version\n")
    _commit(repo, "branch1 commit")

    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)
    conflict_file.write_text("main version\n")
    _commit(repo, "main commit")

    # Merge causing conflict
    subprocess.run(["git", "merge", "branch1"], cwd=repo, check=False)

    plan = build_plan(repo_path=repo, addons_paths=["addons"], working_tree=True)
    assert not plan.is_complete
    assert any("Unmerged Git conflict" in d.message for d in plan.diagnostics) or any(
        "Unmerged" in fc.reason for fc in plan.file_classifications
    )


def test_contract_e_root_discovery_consistency(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # Place manifest at nested path: nested/a/__manifest__.py
    nested_a = repo / "nested" / "a"
    nested_a.mkdir(parents=True)
    (nested_a / "__manifest__.py").write_text("{'name': 'a'}")
    (nested_a / "models.py").write_text("x = 1\n")

    # Place an immediate addon at root: addon_root/__manifest__.py
    addon_root = repo / "addon_root"
    addon_root.mkdir()
    (addon_root / "__manifest__.py").write_text("{'name': 'addon_root'}")
    (addon_root / "models.py").write_text("y = 1\n")

    _commit(repo, "fixture")

    # Under '.', only immediate children of '.' should be discovered: addon_root.
    # nested/a should not be discovered as a root child in either snapshot!
    plan = build_plan(repo, ["."], working_tree=True)
    assert plan.deleted_addons == ()
    assert plan.is_complete
    assert plan.changed_addons == ()


def test_r3_symlink_search_roots_rejected(tmp_path):
    from modootest.intelligence.manifest import ManifestError

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addons.mkdir()
    (addons / "a" / "__manifest__.py").parent.mkdir(parents=True)
    (addons / "a" / "__manifest__.py").write_text("{'name': 'a'}")
    _commit(repo, "c1")

    # 1. Internal symlink search root
    (repo / "addons_link").symlink_to(repo / "addons")
    with pytest.raises(ManifestError, match="Unsupported symlink"):
        build_plan(repo, ["addons_link"], working_tree=True)

    # 2. External symlink search root
    ext_dir = tmp_path / "external_addons"
    ext_dir.mkdir()
    (repo / "ext_link").symlink_to(ext_dir)
    with pytest.raises(ManifestError, match="Unsupported symlink"):
        build_plan(repo, ["ext_link"], working_tree=True)

    # 3. Dangling symlink search root
    (repo / "dangling_link").symlink_to(repo / "nonexistent")
    with pytest.raises(ManifestError, match="Unsupported symlink"):
        build_plan(repo, ["dangling_link"], working_tree=True)

    # 4. Commit mode with committed symlink root
    subprocess.run(["git", "add", "addons_link"], cwd=repo, check=True)
    c2 = _commit(repo, "c2 with link")
    with pytest.raises(ManifestError, match="Unsupported symlink"):
        build_plan(repo, ["addons_link"], base_rev="HEAD~1", head_rev="HEAD")


def test_r3_symlink_addon_directory_and_manifest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addons.mkdir()

    # Normal addon unaffected
    addon_normal = addons / "normal_addon"
    addon_normal.mkdir()
    (addon_normal / "__manifest__.py").write_text("{'name': 'normal_addon'}")
    (addon_normal / "models.py").write_text("# normal\n")

    # Symlink addon directory pointing to an external directory
    ext_addon = tmp_path / "ext_addon"
    ext_addon.mkdir()
    (ext_addon / "__manifest__.py").write_text("{'name': 'ext_addon'}")
    (addons / "symlink_dir_addon").symlink_to(ext_addon)

    # Symlink manifest in a separate addon
    addon_sym_manifest = addons / "sym_manifest_addon"
    addon_sym_manifest.mkdir()
    real_manifest = tmp_path / "real_manifest.py"
    real_manifest.write_text("{'name': 'sym_manifest_addon'}")
    (addon_sym_manifest / "__manifest__.py").symlink_to(real_manifest)

    c1 = _commit(repo, "c1")

    # In working tree mode:
    plan = build_plan(repo, ["addons"], working_tree=True)
    # The normal addon was discovered, but symlinks generated warnings and made plan incomplete
    assert not plan.is_complete
    diag_messages = [d.message for d in plan.diagnostics]
    assert any("Unsupported symlink addon directory" in msg for msg in diag_messages)
    assert any("Unsupported symlink manifest" in msg for msg in diag_messages)


def test_r3_historical_symlink_mode_with_divergent_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "a"
    addon_a.mkdir(parents=True)
    (addon_a / "__manifest__.py").write_text("{'name': 'a'}")
    c1 = _commit(repo, "c1")

    # In c2, replace manifest with a symlink to another file
    target = addon_a / "target.py"
    target.write_text("{'name': 'a'}")
    (addon_a / "__manifest__.py").unlink()
    (addon_a / "__manifest__.py").symlink_to(target)
    c2 = _commit(repo, "c2 with symlink manifest")

    # Now in current worktree, replace symlink with a normal regular file again
    (addon_a / "__manifest__.py").unlink()
    (addon_a / "__manifest__.py").write_text("{'name': 'a', 'depends': []}")

    # Comparing c1 to c2 MUST read the historical Git mode (120000) for c2,
    # completely ignoring that the current worktree has a regular file!
    plan = build_plan(repo, ["addons"], base_rev=c1, head_rev=c2)
    assert not plan.is_complete
    assert any("Unsupported git mode '120000'" in d.message for d in plan.diagnostics)


def test_r3_rename_end_to_end_incomplete_plan_with_updates(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_a = addons / "a"
    addon_a.mkdir(parents=True)
    (addon_a / "__manifest__.py").write_text("{'name': 'a'}")
    (addon_a / "models.py").write_text("# base\n")

    # An unowned script outside addons
    script = repo / "script.py"
    script.write_text("""from odoo import models, fields

class ScriptModel(models.Model):
    _name = 'script.model'
    name = fields.Char('Name')
    desc = fields.Text('Description')
""")
    c1 = _commit(repo, "c1")

    # Rename script.py into addons/a/new_model.py
    subprocess.run(["git", "mv", "script.py", "addons/a/new_model.py"], cwd=repo, check=True)
    (addon_a / "new_model.py").write_text("""from odoo import models, fields

class ScriptModel(models.Model):
    _name = 'script.model'
    name = fields.Char('Name')
    desc = fields.Text('Updated Description')
""")
    c2 = _commit(repo, "c2 rename unowned to owned")

    plan = build_plan(repo, ["addons"], base_rev=c1, head_rev=c2)
    # The plan must capture that 'a' requires module update
    assert "a" in plan.module_update_targets
    assert plan.fresh_process_required is True
    # But because an unowned source was renamed into an addon, uncertainty is preserved:
    assert not plan.is_complete
    # Human-readable output must preserve both old and new paths
    human = plan.format_human_readable()
    assert "script.py -> addons/a/new_model.py" in human
    assert "Status: INCOMPLETE" in human


def test_r3_no_external_file_read_as_addon_contents(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addons.mkdir()
    (addons / "a" / "__manifest__.py").parent.mkdir(parents=True)
    (addons / "a" / "__manifest__.py").write_text("{'name': 'a'}")
    _commit(repo, "c1")

    external_dir = tmp_path / "external_addon"
    external_dir.mkdir()
    external_manifest = external_dir / "__manifest__.py"
    external_manifest.write_text("{'name': 'external_addon'}")
    external_model = external_dir / "models.py"
    external_model.write_text("x = 1\n")

    (addons / "symlink_ext").symlink_to(external_dir)

    # Track all paths accessed by builtins open or Path.read_bytes
    opened_paths: list[str] = []
    orig_read_bytes = Path.read_bytes

    def spy_read_bytes(path_obj):
        opened_paths.append(str(path_obj.resolve()))
        return orig_read_bytes(path_obj)

    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)

    plan = build_plan(repo, ["addons"], working_tree=True)
    assert not plan.is_complete

    # Verify that neither external_manifest nor external_model was read
    ext_manifest_resolved = str(external_manifest.resolve())
    ext_model_resolved = str(external_model.resolve())
    assert ext_manifest_resolved not in opened_paths
    assert ext_model_resolved not in opened_paths


def test_r4_fifo_manifest_guarded_and_rejected(tmp_path, monkeypatch):
    import os
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "init.txt").write_text("init")

    addons = repo / "addons"
    addon_fifo = addons / "addon_fifo"
    addon_fifo.mkdir(parents=True)
    manifest_fifo = addon_fifo / "__manifest__.py"
    try:
        os.mkfifo(manifest_fifo)
    except (AttributeError, OSError):
        pytest.skip("mkfifo not supported on this platform/filesystem")

    orig_read_text = Path.read_text
    # Guard Path.read_text to assert it is never called on the FIFO
    def forbidden(self, *args, **kwargs):
        if self.resolve() == manifest_fifo.resolve():
            raise AssertionError(f"Nonregular manifest '{self}' reached read method!")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbidden)

    # Valid regular addon alongside the FIFO addon
    addon_valid = addons / "addon_valid"
    addon_valid.mkdir()
    (addon_valid / "__manifest__.py").write_text("{'name': 'addon_valid'}")
    _commit(repo, "c1")

    plan = build_plan(repo, ["addons"], working_tree=True)
    assert not plan.is_complete
    assert any("nonregular" in d.message.lower() for d in plan.diagnostics)
    assert any("addon_fifo" in d.message for d in plan.diagnostics)


def test_r4_directory_named_manifest_rejected_without_read(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "init.txt").write_text("init")

    addons = repo / "addons"
    addon_dir = addons / "addon_dir_manifest"
    addon_dir.mkdir(parents=True)
    # Create directory named __manifest__.py
    manifest_dir = addon_dir / "__manifest__.py"
    manifest_dir.mkdir()

    orig_read_text = Path.read_text
    # Guard read_text to assert it is never called on directory
    def forbidden(self, *args, **kwargs):
        if self.resolve() == manifest_dir.resolve():
            raise AssertionError(f"Directory manifest '{self}' reached read method!")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbidden)

    _commit(repo, "c1")
    plan = build_plan(repo, ["addons"], working_tree=True)
    assert not plan.is_complete
    assert any("nonregular" in d.message.lower() for d in plan.diagnostics)


def test_r4_manifest_read_error_produces_diagnostic_not_type_error(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_err = addons / "addon_err"
    addon_err.mkdir(parents=True)
    (addon_err / "__manifest__.py").write_text("{'name': 'addon_err'}")
    _commit(repo, "c1")

    # Simulate PermissionError on read_text
    def denied(self, *args, **kwargs):
        raise PermissionError("Simulated permission denied on manifest read")

    monkeypatch.setattr(Path, "read_text", denied)

    plan = build_plan(repo, ["addons"], working_tree=True)
    assert not plan.is_complete
    assert any("could not read manifest" in d.message.lower() for d in plan.diagnostics)


def test_r4_manifest_unicode_decode_error_produces_diagnostic(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    addons = repo / "addons"
    addon_bad_utf8 = addons / "addon_bad_utf8"
    addon_bad_utf8.mkdir(parents=True)
    # Write invalid UTF-8 bytes to __manifest__.py
    (addon_bad_utf8 / "__manifest__.py").write_bytes(b"\xff\xfe\x00\x00{'name': 'bad'}")
    _commit(repo, "c1")

    plan = build_plan(repo, ["addons"], working_tree=True)
    assert not plan.is_complete
    assert any("could not read manifest" in d.message.lower() for d in plan.diagnostics)
