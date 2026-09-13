"""Impacted test selection engine for modootest.

Provides explainable, deterministic test selection across direct, dependent,
and safe modes based on conservative change intelligence plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Sequence

from modootest.intelligence.manifest import ManifestError
from modootest.intelligence.ownership import resolve_owning_addon
from modootest.intelligence.planner import Diagnostic, DiagnosticSeverity, PlanResult


class SelectionMode(str, Enum):
    """Supported impacted test selection modes."""

    DIRECT = "direct"
    DEPENDENT = "dependent"
    SAFE = "safe"


class SelectorError(Exception):
    """Raised when test root validation or selection setup fails fatally."""


@dataclass(frozen=True)
class SelectedTest:
    """An individual test file selected for execution or collection."""

    path: str
    addon: str | None
    reason: str


@dataclass(frozen=True)
class ExcludedTarget:
    """An addon or target evaluated with an explicit reason for having no tests."""

    addon: str
    reason: str


@dataclass(frozen=True)
class TestSelectionResult:
    """Structured, explainable test selection result."""

    __test__ = False

    mode: SelectionMode
    is_complete: bool
    selected_tests: tuple[SelectedTest, ...]
    excluded_targets: tuple[ExcludedTarget, ...]
    diagnostics: tuple[Diagnostic, ...]
    plan: PlanResult
    broadened_to_all_tests: bool = False
    explanation: str = ""

    @property
    def test_paths(self) -> tuple[str, ...]:
        """Return the tuple of selected test file paths."""
        return tuple(st.path for st in self.selected_tests)

    def format_human_readable(self) -> str:
        """Format the selection decision into deterministic human-readable text."""
        lines: list[str] = []
        lines.append("=" * 80)
        lines.append("modootest Impacted Test Selection")
        if self.is_complete:
            lines.append("Status: COMPLETE")
        else:
            lines.append("Status: INCOMPLETE - conservative selection applied")
        lines.append("=" * 80)

        if not self.is_complete:
            lines.append("")
            lines.append("WARNING: Selection is INCOMPLETE.")
            lines.append(
                "Analysis encountered uncertain changes, unparseable manifests, missing dependencies,"
            )
            lines.append(
                "cycles, or deleted addons. Selection may be broader or narrower than true impact."
            )

        lines.append("")
        lines.append("Selection Context:")
        lines.append(f"  Selection mode: {self.mode.value}")
        lines.append(f"  Underlying plan complete: {self.plan.is_complete}")
        lines.append(f"  Broadened to all tests: {self.broadened_to_all_tests}")
        if self.explanation:
            lines.append(f"  Explanation: {self.explanation}")

        lines.append("")
        lines.append("Addon Impact:")
        if self.plan.changed_addons:
            lines.append(f"  Directly changed addons ({len(self.plan.changed_addons)}): {', '.join(self.plan.changed_addons)}")
        else:
            lines.append("  Directly changed addons: None")

        if self.mode != SelectionMode.DIRECT:
            if self.plan.downstream_impact_addons:
                lines.append(
                    f"  Transitive downstream dependents ({len(self.plan.downstream_impact_addons)}): {', '.join(self.plan.downstream_impact_addons)}"
                )
            else:
                lines.append("  Transitive downstream dependents: None")

        lines.append("")
        if not self.selected_tests:
            lines.append("Selected Tests: None (No matching test files discovered)")
        else:
            lines.append(f"Selected Tests ({len(self.selected_tests)}):")
            for st in self.selected_tests:
                owner_str = f" [Addon: {st.addon}]" if st.addon else " [Unowned/Global]"
                lines.append(f"  - {st.path}{owner_str}")
                lines.append(f"      Reason: {st.reason}")

        if self.excluded_targets:
            lines.append("")
            lines.append(f"Evaluated Targets With No Tests ({len(self.excluded_targets)}):")
            for ext in self.excluded_targets:
                lines.append(f"  - {ext.addon}: {ext.reason}")

        if self.diagnostics:
            lines.append("")
            lines.append(f"Diagnostics ({len(self.diagnostics)}):")
            for diag in self.diagnostics:
                lines.append(f"  [{diag.severity.value}] {diag.message}")

        lines.append("=" * 80)
        return "\n".join(lines)


def _validate_and_normalize_tests_roots(
    git_root: Path,
    raw_roots: Sequence[str] | None = None,
    is_default: bool = False,
) -> list[str]:
    """Validate and normalize tests search roots relative to git root.

    Policy:
    - If `raw_roots` is omitted/None, the default root is 'tests'. If the repository
      does not contain a 'tests' directory, it is omitted safely without error.
      If it does exist, it must be a valid, readable, safe regular directory.
    - If explicit roots are configured (either passed directly or via CLI), each root
      MUST exist, be a directory, not be a symlink or nonregular object, and be readable.
      Missing, non-directory, unreadable, or symlink roots fail closed with SelectorError.
    """
    if raw_roots is None:
        raw_roots = ["tests"]
        is_default = True

    normalized: list[str] = []
    seen: set[str] = set()

    for root_str in raw_roots:
        clean = root_str.strip()
        if not clean:
            continue

        p = PurePosixPath(clean)
        if clean.startswith("/") or ".." in p.parts:
            raise SelectorError(
                f"Tests root '{clean}' escapes git repository root ({git_root})."
            )

        norm = str(p) if str(p) != "." else "."
        if norm not in seen:
            seen.add(norm)
            normalized.append(norm)

    if not normalized:
        if is_default:
            return []
        raise SelectorError("No valid tests roots provided.")

    validated_roots: list[str] = []

    for root in normalized:
        root_path = git_root if root == "." else git_root / root

        # Check default root existence
        if is_default and root == "tests":
            try:
                root_path.lstat()
            except OSError:
                # Default 'tests' does not exist in repository; omit safely
                continue

        # For all explicit roots (and present default root), validate strictly
        check_path = git_root
        if root != ".":
            for part in PurePosixPath(root).parts:
                check_path = check_path / part
                try:
                    st = check_path.lstat()
                except OSError as err:
                    raise SelectorError(
                        f"Configured tests root '{root}' component '{part}' cannot be accessed or does not exist: {err}"
                    )
                if stat.S_ISLNK(st.st_mode):
                    raise SelectorError(
                        f"Unsupported symlink in configured tests root '{root}': component '{part}'."
                    )

        try:
            rst = root_path.lstat()
        except OSError as err:
            raise SelectorError(
                f"Configured tests root '{root}' does not exist or cannot be accessed: {err}"
            )

        if stat.S_ISLNK(rst.st_mode):
            raise SelectorError(
                f"Unsupported symlink in configured tests root '{root}'."
            )

        if not stat.S_ISDIR(rst.st_mode):
            raise SelectorError(
                f"Configured tests root '{root}' is not a directory (found nonregular file, FIFO, socket, or device)."
            )

        try:
            os.listdir(str(root_path))
        except OSError as err:
            raise SelectorError(
                f"Configured tests root '{root}' is not readable: {err}"
            )

        validated_roots.append(root)

    return validated_roots


def _inspect_regular_test_file(git_root: Path, rel_path: str) -> tuple[bool, str | None]:
    """Inspect path with lstat to verify it is an ordinary repo regular file."""
    p = PurePosixPath(rel_path)
    if ".." in p.parts or rel_path.startswith("/"):
        return False, f"Path '{rel_path}' escapes repository root."

    curr = git_root
    for part in p.parts:
        curr = curr / part
        try:
            st = curr.lstat()
        except OSError as err:
            return False, f"Cannot access path '{rel_path}': {err}"

        if stat.S_ISLNK(st.st_mode):
            return False, f"Unsupported symlink in test path component: '{curr.name}' in '{rel_path}'."

    try:
        fst = curr.lstat()
    except OSError as err:
        return False, f"Cannot access file '{rel_path}': {err}"

    if not stat.S_ISREG(fst.st_mode):
        return False, f"Non-regular file in test path (FIFO, directory, or device): '{rel_path}'."

    return True, None


def _is_pytest_file_name(name: str) -> bool:
    """Check standard pytest file naming conventions."""
    if not name.endswith(".py") or name == "__init__.py":
        return False
    return name.startswith("test_") or name.endswith("_test.py") or name == "tests.py"


def _discover_addon_test_files(
    git_root: Path,
    addon_dir: str,
) -> tuple[list[str], list[Diagnostic]]:
    """Discover ordinary regular test files inside an addon's tests directory."""
    discovered: list[str] = []
    diagnostics: list[Diagnostic] = []

    tests_dir = git_root / addon_dir / "tests"
    try:
        st = tests_dir.lstat()
    except OSError:
        return [], []

    if stat.S_ISLNK(st.st_mode):
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.WARNING,
                f"Addon '{addon_dir}' contains a symlink tests directory, which cannot be traversed safely.",
                code="symlink_tests_directory",
                category="filesystem",
                remediation="Replace symlink tests directory with a regular directory or exclude it.",
            )
        )
        return [], diagnostics

    if not stat.S_ISDIR(st.st_mode):
        return [], []

    for root_dir, dirnames, filenames in os.walk(str(tests_dir)):
        # Inspect and prune symlink subdirectories
        safe_dirs = []
        for d in sorted(dirnames):
            d_path = Path(root_dir) / d
            try:
                dst = d_path.lstat()
                if stat.S_ISLNK(dst.st_mode):
                    rel_d = str(d_path.relative_to(git_root))
                    diagnostics.append(
                        Diagnostic(
                            DiagnosticSeverity.WARNING,
                            f"Unsupported symlink directory '{rel_d}' skipped during test discovery.",
                            code="symlink_tests_directory",
                            category="filesystem",
                            remediation="Avoid symlinked directories inside test roots.",
                        )
                    )
                    continue
                safe_dirs.append(d)
            except OSError:
                continue
        dirnames[:] = safe_dirs

        for f in sorted(filenames):
            if not _is_pytest_file_name(f):
                continue
            file_path = Path(root_dir) / f
            try:
                rel_p = str(file_path.relative_to(git_root))
            except ValueError:
                continue

            is_safe, err = _inspect_regular_test_file(git_root, rel_p)
            if not is_safe:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.WARNING,
                        f"Skipping test file '{rel_p}': {err}",
                        code="skipping_test_file",
                        category="filesystem",
                        remediation="Ensure test file is regular, readable, and within the repository.",
                    )
                )
                continue

            discovered.append(rel_p)

    return sorted(discovered), diagnostics


def _discover_tests_under_root(
    git_root: Path,
    root_rel: str,
) -> tuple[list[str], list[Diagnostic]]:
    """Discover ordinary regular test files under a configured tests root."""
    discovered: list[str] = []
    diagnostics: list[Diagnostic] = []

    target_dir = git_root if root_rel == "." else git_root / root_rel
    try:
        st = target_dir.lstat()
    except OSError:
        return [], []

    if stat.S_ISLNK(st.st_mode):
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.WARNING,
                f"Tests root '{root_rel}' is a symlink, which cannot be traversed safely.",
                code="symlink_tests_root",
                category="filesystem",
                remediation="Ensure configured tests-root is a regular directory.",
            )
        )
        return [], diagnostics

    if not stat.S_ISDIR(st.st_mode):
        return [], []

    for root_dir, dirnames, filenames in os.walk(str(target_dir)):
        safe_dirs = []
        for d in sorted(dirnames):
            d_path = Path(root_dir) / d
            try:
                dst = d_path.lstat()
                if stat.S_ISLNK(dst.st_mode):
                    rel_d = str(d_path.relative_to(git_root))
                    diagnostics.append(
                        Diagnostic(
                            DiagnosticSeverity.WARNING,
                            f"Unsupported symlink directory '{rel_d}' skipped in tests root '{root_rel}'.",
                            code="symlink_tests_directory",
                            category="filesystem",
                            remediation="Avoid symlinked directories inside test roots.",
                        )
                    )
                    continue
                safe_dirs.append(d)
            except OSError:
                continue
        dirnames[:] = safe_dirs

        for f in sorted(filenames):
            if not _is_pytest_file_name(f):
                continue
            file_path = Path(root_dir) / f
            try:
                rel_p = str(file_path.relative_to(git_root))
            except ValueError:
                continue

            is_safe, err = _inspect_regular_test_file(git_root, rel_p)
            if not is_safe:
                diagnostics.append(
                    Diagnostic(
                        DiagnosticSeverity.WARNING,
                        f"Skipping test file '{rel_p}': {err}",
                        code="skipping_test_file",
                        category="filesystem",
                        remediation="Ensure test file is regular, readable, and within the repository.",
                    )
                )
                continue

            discovered.append(rel_p)

    return sorted(discovered), diagnostics


def select_impacted_tests(
    plan: PlanResult,
    mode: SelectionMode | str = SelectionMode.DIRECT,
    tests_roots: Sequence[str] | None = None,
) -> TestSelectionResult:
    """Select impacted tests based on plan and selection mode.

    Parameters
    ----------
    plan : PlanResult
        The change intelligence plan from phase 0.3.
    mode : SelectionMode or str
        Selection strategy: 'direct', 'dependent', or 'safe'.
    tests_roots : sequence of str
        Explicit test discovery roots relative to repository root.

    Returns
    -------
    TestSelectionResult
        Structured, immutable selection decision with explainable reasons.
    """
    if isinstance(mode, str):
        try:
            sel_mode = SelectionMode(mode.lower())
        except ValueError:
            raise SelectorError(
                f"Unknown selection mode '{mode}'. Choose from: 'direct', 'dependent', 'safe'."
            )
    else:
        sel_mode = mode

    git_root = Path(plan.git_root)
    norm_tests_roots = _validate_and_normalize_tests_roots(git_root, tests_roots)

    # Build addon directories map from plan classifications and addon paths
    addon_dirs_map: dict[str, str] = {}
    for fc in plan.file_classifications:
        rec = fc.owned_record
        if rec.new_owner and rec.new_owner not in addon_dirs_map and rec.record.new_path:
            p = PurePosixPath(rec.record.new_path)
            for parent in p.parents:
                if parent.name == rec.new_owner:
                    addon_dirs_map[rec.new_owner] = str(parent)
                    break
        if rec.old_owner and rec.old_owner not in addon_dirs_map and rec.record.old_path:
            p = PurePosixPath(rec.record.old_path)
            for parent in p.parents:
                if parent.name == rec.old_owner:
                    addon_dirs_map[rec.old_owner] = str(parent)
                    break

    # Also check configured addon roots directly on filesystem for any missing directories
    for root in plan.addons_paths:
        root_path = git_root if root == "." else git_root / root
        if root_path.exists() and root_path.is_dir():
            for child in root_path.iterdir():
                if (child / "__manifest__.py").exists():
                    addon_dirs_map.setdefault(child.name, str(PurePosixPath(root) / child.name))

    diagnostics: list[Diagnostic] = list(plan.diagnostics)
    is_complete: bool = plan.is_complete

    # Any uncertain file classification keeps selection incomplete
    if any(fc.is_uncertain for fc in plan.file_classifications):
        is_complete = False

    selected_tests_map: dict[str, SelectedTest] = {}
    excluded_targets_map: dict[str, ExcludedTarget] = {}

    # Pre-discover tests under configured test roots for cross-root ownership mapping
    tests_root_files: dict[str, list[str]] = {}
    for tr in norm_tests_roots:
        r_tests, r_diags = _discover_tests_under_root(git_root, tr)
        diagnostics.extend(r_diags)
        if r_diags:
            is_complete = False
        tests_root_files[tr] = r_tests

    # Helper to scan an addon for tests
    def _collect_for_addon(addon_name: str, target_reason: str) -> None:
        adir = addon_dirs_map.get(addon_name)
        addon_tests: list[str] = []
        if adir:
            tests, diags = _discover_addon_test_files(git_root, adir)
            diagnostics.extend(diags)
            if diags:
                nonlocal is_complete
                is_complete = False
            addon_tests.extend(tests)

        # Also find tests under tests_roots that belong to this addon
        for tr, t_list in tests_root_files.items():
            for t_path in t_list:
                parts = PurePosixPath(t_path).parts
                if addon_name in parts:
                    if t_path not in addon_tests:
                        addon_tests.append(t_path)

        if addon_tests:
            for t in sorted(addon_tests):
                selected_tests_map[t] = SelectedTest(
                    path=t,
                    addon=addon_name,
                    reason=f"{target_reason} '{addon_name}'",
                )
        else:
            reason_loc = f"'{adir}/tests'" if adir else "manifest directory"
            excluded_targets_map[addon_name] = ExcludedTarget(
                addon=addon_name,
                reason=f"{target_reason} '{addon_name}' has no test files under {reason_loc} or configured test roots",
            )

    def _collect_changed_test_files() -> None:
        nonlocal is_complete
        for fc in plan.file_classifications:
            eff_path = fc.owned_record.record.effective_path
            p = PurePosixPath(eff_path)
            if _is_pytest_file_name(p.name) or ("tests" in p.parts and eff_path.endswith(".py")):
                if (git_root / eff_path).exists():
                    is_safe, err = _inspect_regular_test_file(git_root, eff_path)
                    if is_safe:
                        owner = fc.owned_record.new_owner or fc.owned_record.old_owner
                        selected_tests_map[eff_path] = SelectedTest(
                            path=eff_path,
                            addon=owner,
                            reason="Directly modified test file",
                        )
                    else:
                        diagnostics.append(
                            Diagnostic(
                                DiagnosticSeverity.WARNING,
                                f"Changed test file '{eff_path}' cannot be selected safely: {err}",
                                code="unsafe_changed_test_file",
                                category="filesystem",
                                remediation="Ensure changed test file is a readable regular file.",
                            )
                        )
                        is_complete = False

    # 1. DIRECT MODE SELECTION
    if sel_mode == SelectionMode.DIRECT:
        for a in plan.changed_addons:
            _collect_for_addon(a, "Directly changed addon")

        _collect_changed_test_files()

        explanation = "Direct mode selected tests belonging only to directly changed addons."

        sorted_selected = tuple(selected_tests_map[k] for k in sorted(selected_tests_map))
        sorted_excluded = tuple(excluded_targets_map[k] for k in sorted(excluded_targets_map))

        return TestSelectionResult(
            mode=SelectionMode.DIRECT,
            is_complete=is_complete,
            selected_tests=sorted_selected,
            excluded_targets=sorted_excluded,
            diagnostics=tuple(diagnostics),
            plan=plan,
            broadened_to_all_tests=False,
            explanation=explanation,
        )

    # 2. DEPENDENT MODE SELECTION
    if sel_mode == SelectionMode.DEPENDENT:
        for a in plan.changed_addons:
            _collect_for_addon(a, "Directly changed addon")

        for d in plan.downstream_impact_addons:
            _collect_for_addon(d, "Transitive downstream dependent of changed addons:")

        _collect_changed_test_files()

        explanation = (
            "Dependent mode selected tests for directly changed addons and their transitive downstream dependents."
        )

        sorted_selected = tuple(selected_tests_map[k] for k in sorted(selected_tests_map))
        sorted_excluded = tuple(excluded_targets_map[k] for k in sorted(excluded_targets_map))

        return TestSelectionResult(
            mode=SelectionMode.DEPENDENT,
            is_complete=is_complete,
            selected_tests=sorted_selected,
            excluded_targets=sorted_excluded,
            diagnostics=tuple(diagnostics),
            plan=plan,
            broadened_to_all_tests=False,
            explanation=explanation,
        )

    # 3. SAFE MODE SELECTION
    # Safe mode checks if the underlying plan analysis is complete and certain
    is_plan_completely_certain = (
        plan.is_complete
        and not any(fc.is_uncertain for fc in plan.file_classifications)
        and not any(d.severity in (DiagnosticSeverity.WARNING, DiagnosticSeverity.ERROR) for d in plan.diagnostics)
    )

    if is_plan_completely_certain:
        # Complete analysis: execute dependent closure
        for a in plan.changed_addons:
            _collect_for_addon(a, "Directly changed addon")

        for d in plan.downstream_impact_addons:
            _collect_for_addon(d, "Transitive downstream dependent of changed addons:")

        _collect_changed_test_files()

        explanation = (
            "Plan analysis is complete; safe mode executed dependent closure of changed addons."
        )
        sorted_selected = tuple(selected_tests_map[k] for k in sorted(selected_tests_map))
        sorted_excluded = tuple(excluded_targets_map[k] for k in sorted(excluded_targets_map))

        return TestSelectionResult(
            mode=SelectionMode.SAFE,
            is_complete=is_complete,
            selected_tests=sorted_selected,
            excluded_targets=sorted_excluded,
            diagnostics=tuple(diagnostics),
            plan=plan,
            broadened_to_all_tests=False,
            explanation=explanation,
        )
    else:
        # Incomplete or uncertain plan: conservatively broaden to all eligible tests under tests-roots and addons
        diagnostics.append(
            Diagnostic(
                DiagnosticSeverity.WARNING,
                "Safe mode: underlying plan is incomplete or uncertain; broadening selection to all eligible test files under configured test roots.",
                code="broadened_safe_mode",
                category="selection",
                remediation="Resolve warnings and uncertainties in plan to enable targeted selection.",
            )
        )
        is_complete = False

        # Discover all eligible tests under tests_roots
        for tr in norm_tests_roots:
            root_tests, r_diags = _discover_tests_under_root(git_root, tr)
            diagnostics.extend(r_diags)
            for t in root_tests:
                selected_tests_map[t] = SelectedTest(
                    path=t,
                    addon=None,
                    reason=f"Safe mode fallback: broadened to all eligible tests under '{tr}' due to incomplete plan",
                )

        # Also discover all tests across known addons under configured addon roots
        for aname, adir in sorted(addon_dirs_map.items()):
            atests, a_diags = _discover_addon_test_files(git_root, adir)
            diagnostics.extend(a_diags)
            for t in atests:
                selected_tests_map[t] = SelectedTest(
                    path=t,
                    addon=aname,
                    reason=f"Safe mode fallback: broadened to all eligible tests in addon '{aname}' due to incomplete plan",
                )

        explanation = (
            "Plan analysis is incomplete or uncertain; safe mode conservatively broadened selection to all eligible test files across configured roots."
        )

        sorted_selected = tuple(selected_tests_map[k] for k in sorted(selected_tests_map))
        sorted_excluded = tuple(excluded_targets_map[k] for k in sorted(excluded_targets_map))

        return TestSelectionResult(
            mode=SelectionMode.SAFE,
            is_complete=False,
            selected_tests=sorted_selected,
            excluded_targets=sorted_excluded,
            diagnostics=tuple(diagnostics),
            plan=plan,
            broadened_to_all_tests=True,
            explanation=explanation,
        )
