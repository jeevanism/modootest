"""Change intelligence planner orchestrating Git diff, manifest graph, and classification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path, PurePosixPath
from typing import Sequence

from modootest.intelligence.classifier import (
    ChangeCategory,
    FileClassification,
    classify_file_change,
)
from modootest.intelligence.git import (
    GitChangeRecord,
    GitError,
    check_head_exists,
    discover_git_root,
    get_diff_tree,
    get_working_tree_diff,
    list_tree_entries,
    list_tree_files,
    read_file_at_commit,
    read_file_working_tree,
    resolve_commit,
)


def _safe_format_path(path_str: str) -> str:
    """Format path escaping raw control characters and whitespace if needed."""
    if any(ord(c) < 32 or ord(c) >= 127 or c.isspace() for c in path_str):
        return repr(path_str)
    return path_str
from modootest.intelligence.manifest import (
    DependencyGraph,
    ManifestError,
    ManifestInfo,
    build_dependency_graph,
    validate_and_extract_manifest,
)
from modootest.intelligence.ownership import (
    OwnedChangeRecord,
    resolve_change_ownership,
    resolve_owning_addon,
)


class DiagnosticSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Diagnostic:
    severity: DiagnosticSeverity
    message: str
    code: str = "generic_diagnostic"
    category: str = "general"
    remediation: str | None = None
    context: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.severity, DiagnosticSeverity):
            if isinstance(self.severity, str) and self.severity in DiagnosticSeverity._value2member_map_:
                object.__setattr__(self, "severity", DiagnosticSeverity(self.severity))
        if self.code:
            object.__setattr__(self, "code", self.code.lower())
        if self.category:
            object.__setattr__(self, "category", self.category.lower())


@dataclass(frozen=True)
class PlanResult:
    """Structured change intelligence plan."""

    git_root: str
    comparison_mode: str  # 'commit' or 'working-tree'
    base_commit: str
    head_commit: str | None  # None when in working-tree mode
    addons_paths: tuple[str, ...]
    is_complete: bool
    fresh_process_required: bool
    module_update_targets: tuple[str, ...]
    changed_addons: tuple[str, ...]
    deleted_addons: tuple[str, ...]
    downstream_impact_addons: tuple[str, ...]
    file_classifications: tuple[FileClassification, ...]
    diagnostics: tuple[Diagnostic, ...]
    graph_snapshot_policy: str = (
        "Downstream impact combined conservatively from both old and new manifest graphs."
    )

    def format_human_readable(self) -> str:
        lines: list[str] = []
        lines.append("=" * 80)
        lines.append("modootest Change Intelligence Plan")
        if self.is_complete:
            lines.append("Status: COMPLETE")
        else:
            lines.append("Status: INCOMPLETE - broad conservative follow-up required")
        lines.append("=" * 80)

        if not self.is_complete:
            lines.append("")
            lines.append("WARNING: Plan is INCOMPLETE.")
            lines.append(
                "Analysis encountered uncertain changes, unparseable manifests, dependency cycles,"
            )
            lines.append(
                "or deleted addons. Do not rely on a narrow safe execution scope."
            )

        lines.append("")
        lines.append("Comparison Context:")
        lines.append(f"  Git repository: {self.git_root}")
        lines.append(f"  Mode: {self.comparison_mode}")
        lines.append(f"  Base revision: {self.base_commit}")
        if self.head_commit:
            lines.append(f"  Head revision: {self.head_commit}")
        else:
            lines.append("  Head revision: Working tree (including staged, unstaged, untracked)")
        lines.append(f"  Configured addon roots: {', '.join(self.addons_paths)}")
        lines.append(f"  Graph policy: {self.graph_snapshot_policy}")

        lines.append("")
        if not self.file_classifications:
            lines.append("Changed Files: None (No changes detected)")
        else:
            lines.append(f"Changed Files ({len(self.file_classifications)}):")
            for fc in self.file_classifications:
                rec = fc.owned_record.record
                if rec.status in ("R", "C") and rec.old_path and rec.new_path:
                    display_path = f"{_safe_format_path(rec.old_path)} -> {_safe_format_path(rec.new_path)}"
                else:
                    display_path = _safe_format_path(rec.effective_path)
                lines.append(f"  [{rec.status}] {display_path}")
                lines.append(
                    f"      Old owner: {fc.owned_record.old_owner or 'None'} | New owner: {fc.owned_record.new_owner or 'None'}"
                )
                lines.append(f"      Category: {fc.category.value}")
                lines.append(f"      Reason: {fc.reason}")

        lines.append("")
        lines.append("Addon Impact Analysis:")
        if self.changed_addons:
            lines.append(f"  Directly changed addons ({len(self.changed_addons)}):")
            for a in self.changed_addons:
                status_suffix = " (DELETED)" if a in self.deleted_addons else ""
                lines.append(f"    - {a}{status_suffix}")
        else:
            lines.append("  Directly changed addons: None")

        if self.downstream_impact_addons:
            lines.append(f"  Transitive downstream dependents ({len(self.downstream_impact_addons)}):")
            for d in self.downstream_impact_addons:
                lines.append(f"    - {d}")
        else:
            lines.append("  Transitive downstream dependents: None")

        lines.append("")
        lines.append("Lifecycle Requirements:")
        fp_str = "REQUIRED" if self.fresh_process_required else "NOT REQUIRED"
        lines.append(f"  Fresh Python process: {fp_str}")
        if not self.is_complete and any(fc.module_update is None for fc in self.file_classifications):
            lines.append("  Module update targets: UNKNOWN (conservative follow-up required)")
        elif self.module_update_targets:
            lines.append(f"  Module update targets: {', '.join(self.module_update_targets)}")
        else:
            lines.append("  Module update targets: NONE")

        if self.diagnostics:
            lines.append("")
            lines.append(f"Diagnostics ({len(self.diagnostics)}):")
            for diag in self.diagnostics:
                lines.append(f"  [{diag.severity.value}] {diag.message}")

        lines.append("=" * 80)
        return "\n".join(lines)


def _validate_and_normalize_addon_roots(git_root: Path, raw_roots: Sequence[str]) -> list[str]:
    """Validate and normalize explicit addon search roots relative to git root."""
    if not raw_roots:
        raise ManifestError("At least one explicit --addons-path search root is required.")

    normalized: list[str] = []
    seen: set[str] = set()

    for root_str in raw_roots:
        clean = root_str.strip()
        if not clean:
            continue

        # Lexical normalization independent of current filesystem state
        p = PurePosixPath(clean)
        if clean.startswith("/") or ".." in p.parts:
            raise ManifestError(
                f"Addon search root '{clean}' escapes git repository root ({git_root})."
            )

        norm = str(p) if str(p) != "." else "."
        if norm not in seen:
            seen.add(norm)
            normalized.append(norm)

    if not normalized:
        raise ManifestError("No valid addon search roots specified.")

    return normalized


def _discover_manifests_commit(
    git_root: Path,
    commit: str,
    addon_roots: Sequence[str],
) -> list[ManifestInfo]:
    """Discover addon manifests at a specific historical git commit."""
    discovered: list[ManifestInfo] = []
    seen_names: dict[str, str] = {}

    for root in addon_roots:
        entries = list_tree_entries(git_root, commit, root)
        expected_parent = PurePosixPath(root)
        # Find paths ending in __manifest__.py directly below root
        for mode, obj_type, sha, fpath in entries:
            p = PurePosixPath(fpath)
            if mode in ("120000", "160000") and p.parent == expected_parent:
                addon_name = p.name
                addon_dir = str(p)
                info = ManifestInfo(
                    name=addon_name,
                    relative_dir=addon_dir,
                    manifest_path=f"{addon_dir}/__manifest__.py",
                    depends=(),
                    data_files=(),
                    is_valid=False,
                    error_message=f"Unsupported git mode '{mode}' (symlink or submodule) for addon directory '{addon_dir}' in commit '{commit}'.",
                )
                discovered.append(info)
                continue

            if p.name == "__manifest__.py":
                # Check that addon_dir is immediately below search root
                parent_of_addon = p.parent.parent
                if parent_of_addon != expected_parent:
                    continue  # nested addon outside root immediate children

                addon_dir = str(p.parent)
                addon_name = p.parent.name

                if addon_name in seen_names:
                    raise ManifestError(
                        f"Duplicate addon name '{addon_name}' across search roots: '{seen_names[addon_name]}' and '{addon_dir}'."
                    )
                seen_names[addon_name] = addon_dir

                # Preserve symlink (120000) or submodule (160000) evidence and diagnose unsupported types
                if mode in ("120000", "160000"):
                    info = ManifestInfo(
                        name=addon_name,
                        relative_dir=addon_dir,
                        manifest_path=fpath,
                        depends=(),
                        data_files=(),
                        is_valid=False,
                        error_message=f"Unsupported git mode '{mode}' (symlink or submodule) for manifest at '{fpath}' in commit '{commit}'.",
                    )
                    discovered.append(info)
                    continue

                raw_bytes = read_file_at_commit(git_root, commit, fpath)
                if raw_bytes is None:
                    info = ManifestInfo(
                        name=addon_name,
                        relative_dir=addon_dir,
                        manifest_path=fpath,
                        depends=(),
                        data_files=(),
                        is_valid=False,
                        error_message=f"Could not read manifest at '{fpath}' in commit '{commit}'.",
                    )
                    discovered.append(info)
                    continue

                content = raw_bytes.decode("utf-8", errors="replace")
                info = validate_and_extract_manifest(addon_name, addon_dir, fpath, content)
                discovered.append(info)

    return discovered


def _discover_manifests_worktree(
    git_root: Path,
    addon_roots: Sequence[str],
) -> list[ManifestInfo]:
    """Discover addon manifests on the current filesystem working tree."""
    discovered: list[ManifestInfo] = []
    seen_names: dict[str, str] = {}
    import stat

    for root in addon_roots:
        root_path = git_root if root == "." else git_root / root

        # Check if root or any component is a symlink
        check_path = git_root
        if root != ".":
            for part in PurePosixPath(root).parts:
                check_path = check_path / part
                try:
                    st = check_path.lstat()
                    if stat.S_ISLNK(st.st_mode):
                        raise ManifestError(f"Unsupported symlink in addon search root '{root}'.")
                except OSError:
                    break

        try:
            rst = root_path.lstat()
            if stat.S_ISLNK(rst.st_mode):
                raise ManifestError(f"Unsupported symlink search root '{root}'.")
        except OSError:
            pass

        if not root_path.exists() or not root_path.is_dir():
            continue

        # Look only at immediate child entries using lstat
        for entry in sorted(root_path.iterdir(), key=lambda e: e.name):
            try:
                st = entry.lstat()
            except OSError:
                continue

            if stat.S_ISLNK(st.st_mode):
                # Unsupported symlink addon directory (internal, external, or dangling)
                addon_name = entry.name
                rel_dir = str(PurePosixPath(root) / addon_name)
                info = ManifestInfo(
                    name=addon_name,
                    relative_dir=rel_dir,
                    manifest_path=f"{rel_dir}/__manifest__.py",
                    depends=(),
                    data_files=(),
                    is_valid=False,
                    error_message=f"Unsupported symlink addon directory: '{entry.name}'",
                )
                discovered.append(info)
                continue

            if not stat.S_ISDIR(st.st_mode):
                continue

            manifest_file = entry / "__manifest__.py"
            try:
                mst = manifest_file.lstat()
                manifest_is_link = stat.S_ISLNK(mst.st_mode)
                manifest_exists = True
            except OSError:
                manifest_exists = False
                manifest_is_link = False

            if not manifest_exists:
                continue

            addon_name = entry.name
            rel_dir = str(PurePosixPath(root) / addon_name)
            rel_manifest = f"{rel_dir}/__manifest__.py"

            if manifest_is_link:
                info = ManifestInfo(
                    name=addon_name,
                    relative_dir=rel_dir,
                    manifest_path=rel_manifest,
                    depends=(),
                    data_files=(),
                    is_valid=False,
                    error_message=f"Unsupported symlink manifest file: '{rel_manifest}'",
                )
                discovered.append(info)
                continue

            if not stat.S_ISREG(mst.st_mode):
                info = ManifestInfo(
                    name=addon_name,
                    relative_dir=rel_dir,
                    manifest_path=rel_manifest,
                    depends=(),
                    data_files=(),
                    is_valid=False,
                    error_message=f"Unsupported nonregular manifest file (not a regular file): '{rel_manifest}'",
                )
                discovered.append(info)
                continue

            if addon_name in seen_names:
                raise ManifestError(
                    f"Duplicate addon name '{addon_name}' across search roots: '{seen_names[addon_name]}' and '{rel_dir}'."
                )
            seen_names[addon_name] = rel_dir

            try:
                content = manifest_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as err:
                info = ManifestInfo(
                    name=addon_name,
                    relative_dir=rel_dir,
                    manifest_path=rel_manifest,
                    depends=(),
                    data_files=(),
                    is_valid=False,
                    error_message=f"Could not read manifest file '{rel_manifest}': {err}",
                )
                discovered.append(info)
                continue

            info = validate_and_extract_manifest(addon_name, rel_dir, rel_manifest, content)
            discovered.append(info)

    return discovered


def build_plan(
    repo_path: str | Path = ".",
    addons_paths: Sequence[str] = (),
    base_rev: str | None = None,
    head_rev: str | None = None,
    working_tree: bool = False,
) -> PlanResult:
    """Compute a deterministic change intelligence plan."""
    git_root = discover_git_root(repo_path)
    head_sha = check_head_exists(git_root)

    # Validate mode
    if working_tree:
        if base_rev is not None or head_rev is not None:
            raise GitError("Cannot specify --base or --head with --working-tree mode.")
        comparison_mode = "working-tree"
        base_commit = head_sha
        head_commit = None
    else:
        if not base_rev or not head_rev:
            raise GitError("Must specify both --base and --head in commit comparison mode.")
        comparison_mode = "commit"
        base_commit = resolve_commit(git_root, base_rev)
        head_commit = resolve_commit(git_root, head_rev)

    norm_addon_roots = _validate_and_normalize_addon_roots(git_root, addons_paths)

    diagnostics: list[Diagnostic] = []

    # 1. Discover manifests and build dependency graphs for old and new snapshots
    old_manifests = _discover_manifests_commit(git_root, base_commit, norm_addon_roots)
    if comparison_mode == "working-tree":
        new_manifests = _discover_manifests_worktree(git_root, norm_addon_roots)
    else:
        assert head_commit is not None
        for rev in (base_commit, head_commit):
            for r in norm_addon_roots:
                if r != ".":
                    entries = list_tree_entries(git_root, rev, r)
                    for mode, otype, sha, p in entries:
                        if p == r and mode in ("120000", "160000"):
                            raise ManifestError(
                                f"Unsupported symlink search root '{r}' in commit '{rev}'."
                            )
        new_manifests = _discover_manifests_commit(git_root, head_commit, norm_addon_roots)

    # Check for malformed manifests
    for m in old_manifests:
        if not m.is_valid:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.WARNING,
                    f"Base revision manifest invalid in '{m.manifest_path}': {m.error_message}",
                    code="manifest_invalid",
                    category="manifest",
                    remediation="Fix syntax or structural errors in the base revision manifest.",
                )
            )
    for m in new_manifests:
        if not m.is_valid:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.WARNING,
                    f"Head revision manifest invalid in '{m.manifest_path}': {m.error_message}",
                    code="manifest_invalid",
                    category="manifest",
                    remediation="Fix syntax or structural errors in the addon manifest.",
                )
            )

    old_graph = build_dependency_graph(old_manifests)
    new_graph = build_dependency_graph(new_manifests)

    if old_graph.has_cycles:
        for c in old_graph.cycles:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.WARNING,
                    f"Dependency cycle detected in base revision: {' -> '.join(c)}",
                    code="dependency_cycle",
                    category="dependency",
                    remediation="Break circular dependency between addons.",
                )
            )
    if new_graph.has_cycles:
        for c in new_graph.cycles:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.WARNING,
                    f"Dependency cycle detected in head revision: {' -> '.join(c)}",
                    code="dependency_cycle",
                    category="dependency",
                    remediation="Break circular dependency between addons.",
                )
            )

    # Record unresolved external dependencies
    for addon_name, unresolved in sorted(new_graph.unresolved_dependencies.items()):
        for unres in unresolved:
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.INFO,
                    f"Addon '{addon_name}' declares unresolved external dependency '{unres}'.",
                    code="unresolved_external_dependency",
                    category="dependency",
                    remediation=f"Ensure external dependency '{unres}' is available in configured addon paths.",
                )
            )

    old_addon_dirs = {m.relative_dir: m.name for m in old_manifests}
    new_addon_dirs = {m.relative_dir: m.name for m in new_manifests}

    # 2. Get Git diff
    if comparison_mode == "working-tree":
        change_records = get_working_tree_diff(git_root)
    else:
        assert head_commit is not None
        change_records = get_diff_tree(git_root, base_commit, head_commit)

    # 3. Ownership resolution
    owned_records = resolve_change_ownership(change_records, old_addon_dirs, new_addon_dirs)

    # 4. Classification
    all_manifest_data_files: set[str] = set()
    for m in old_manifests:
        all_manifest_data_files.update(m.data_files)
    for m in new_manifests:
        all_manifest_data_files.update(m.data_files)

    def read_old(p: str) -> bytes | None:
        return read_file_at_commit(git_root, base_commit, p)

    def read_new(p: str) -> bytes | None:
        if comparison_mode == "working-tree":
            return read_file_working_tree(git_root, p)
        else:
            assert head_commit is not None
            return read_file_at_commit(git_root, head_commit, p)

    file_classifications: list[FileClassification] = []
    for owned_rec in owned_records:
        fc = classify_file_change(
            owned_rec=owned_rec,
            read_old_file=read_old,
            read_new_file=read_new,
            manifest_data_files=all_manifest_data_files,
        )
        file_classifications.append(fc)

    # 5. Addon changes & Deletion detection
    old_addon_names = {m.name for m in old_manifests}
    new_addon_names = {m.name for m in new_manifests}

    deleted_addons = sorted(old_addon_names - new_addon_names)
    for del_a in deleted_addons:
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.WARNING,
                f"Addon '{del_a}' was deleted; removed addons cannot be update targets and require manual migration/uninstall review.",
                code="addon_deleted",
                category="lifecycle",
                remediation="Review removed addon and execute migration or uninstallation before testing.",
            )
        )

    # Changed addons: all addons owning at least one changed file, plus added/deleted addons
    changed_addon_set: set[str] = set(deleted_addons)
    for fc in file_classifications:
        for a in fc.owned_record.affected_addons:
            changed_addon_set.add(a)

    changed_addons = tuple(sorted(changed_addon_set))

    # 6. Downstream impact: combined conservatively from both old and new graphs
    downstream_impact_set: set[str] = set()
    for a in changed_addons:
        downstream_impact_set.update(old_graph.get_transitive_dependents(a))
        downstream_impact_set.update(new_graph.get_transitive_dependents(a))

    # Downstream dependents do not include the changed addons themselves
    downstream_impact_addons = tuple(sorted(downstream_impact_set - changed_addon_set))

    # 7. Lifecycle requirements
    fresh_process_required = any(fc.fresh_process for fc in file_classifications)

    # Module update targets: non-deleted addons with module_update True
    update_targets_set: set[str] = set()
    for fc in file_classifications:
        if fc.module_update is True:
            for a in fc.owned_record.affected_addons:
                if a in new_addon_names:
                    update_targets_set.add(a)

    module_update_targets = tuple(sorted(update_targets_set))

    # 8. Check completeness and record uncertainty diagnostics
    # Incomplete if:
    # - Any file classification is uncertain
    # - Any unmerged conflicts
    # - Any cycle in old or new graph
    # - Any malformed manifests
    # - Any deleted addons
    is_complete = True

    for fc in file_classifications:
        if fc.owned_record.record.is_unmerged:
            is_complete = False
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.WARNING,
                    f"Unmerged Git conflict detected at '{fc.owned_record.record.effective_path}'.",
                    code="unmerged_git_conflict",
                    category="git",
                    remediation="Resolve Git merge conflicts before running planning or impacted tests.",
                )
            )
        elif fc.is_uncertain:
            is_complete = False
            diagnostics.append(
                Diagnostic(
                    DiagnosticSeverity.WARNING,
                    f"Uncertain change at '{fc.owned_record.record.effective_path}': {fc.reason}",
                    code="uncertain_change",
                    category="planning",
                    remediation="Review uncertain file classification or broaden test scope.",
                )
            )

    if any(rec.is_unmerged for rec in change_records):
        is_complete = False
    if old_graph.has_cycles or new_graph.has_cycles:
        is_complete = False
    if any(not m.is_valid for m in old_manifests + new_manifests):
        is_complete = False
    if len(deleted_addons) > 0:
        is_complete = False

    return PlanResult(
        git_root=str(git_root),
        comparison_mode=comparison_mode,
        base_commit=base_commit,
        head_commit=head_commit,
        addons_paths=tuple(norm_addon_roots),
        is_complete=is_complete,
        fresh_process_required=fresh_process_required,
        module_update_targets=module_update_targets,
        changed_addons=changed_addons,
        deleted_addons=tuple(deleted_addons),
        downstream_impact_addons=downstream_impact_addons,
        file_classifications=tuple(file_classifications),
        diagnostics=tuple(diagnostics),
    )
