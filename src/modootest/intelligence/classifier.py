"""Conservative change classification and AST analysis for modootest."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Callable, Sequence

from modootest.intelligence.git import GitChangeRecord
from modootest.intelligence.ownership import OwnedChangeRecord


class ChangeCategory(str, Enum):
    TESTS_ONLY = "tests-only"
    METHOD_ONLY = "method-only"
    FIELD_DECLARATION = "field-declaration"
    MODEL_CLASS = "model-class"
    XML_DATA = "xml-data"
    MANIFEST_CHANGE = "manifest-change"
    FRAMEWORK_BUILD = "framework-build"
    DOCUMENTATION = "documentation"
    UNKNOWN_UNCERTAIN = "unknown-uncertain"


@dataclass(frozen=True)
class FileClassification:
    """Classification result and lifecycle impact for a single file."""

    owned_record: OwnedChangeRecord
    category: ChangeCategory
    fresh_process: bool
    module_update: bool | None  # None indicates unknown / conservative uncertainty
    reason: str
    uncertain: bool = False

    @property
    def is_uncertain(self) -> bool:
        return self.uncertain or self.module_update is None or self.category == ChangeCategory.UNKNOWN_UNCERTAIN


_DOC_EXTENSIONS = {".md", ".rst"}
_EXACT_DOC_FILENAMES = {"readme", "license", "copying", "copyright", "contributing"}

_BUILD_FILENAMES = {
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "requirements.txt",
    "tox.ini",
    "pytest.ini",
    "modootest.conf",
    "dockerfile",
    "makefile",
    ".gitignore",
}


def is_documentation_path(path_str: str) -> bool:
    """Check if path belongs to the narrow documentation allowlist.

    Requires .md/.rst suffix or an exact approved extensionless filename.
    Never matches executable extensions (e.g. readme.py).
    """
    p = PurePosixPath(path_str)
    suffix = p.suffix.lower()
    if suffix in _DOC_EXTENSIONS:
        return True
    if not suffix and p.name.lower() in _EXACT_DOC_FILENAMES:
        return True
    return False


def is_framework_build_path(path_str: str) -> bool:
    """Check if path is a recognized framework or build configuration file."""
    p = PurePosixPath(path_str)
    name_lower = p.name.lower()
    if name_lower in _BUILD_FILENAMES or name_lower.startswith(".github"):
        return True
    if any(part.lower() == ".github" for part in p.parts):
        return True
    return False


def _is_test_file(path_str: str) -> bool:
    p = PurePosixPath(path_str)
    return "tests" in p.parts and p.suffix == ".py"


class _MethodBodyMasker(ast.NodeTransformer):
    """Replaces method bodies of direct class methods with pass, preserving class structure.
    Top-level functions, nested classes, and functions with dynamic model creation are NOT masked.
    """

    def __init__(self) -> None:
        super().__init__()
        self.class_depth = 0
        self.func_depth = 0
        self.has_unsupported_dynamic = False

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        self.class_depth += 1
        node = self.generic_visit(node)
        self.class_depth -= 1
        return node

    def _is_unsupported_dynamic(self, node: ast.AST) -> bool:
        for child in ast.walk(node):
            if child is not node and isinstance(child, ast.ClassDef):
                return True
            if isinstance(child, ast.Call):
                # type(...) call with 3 args or dynamic class creation
                if isinstance(child.func, ast.Name) and child.func.id == "type":
                    return True
                # setattr or getattr on models
                if isinstance(child.func, ast.Name) and child.func.id in ("setattr", "getattr"):
                    return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        # Only direct class methods of a top-level class (class_depth == 1, func_depth == 0) can be masked
        if self.class_depth != 1 or self.func_depth != 0:
            self.func_depth += 1
            res = self.generic_visit(node)
            self.func_depth -= 1
            return res

        if self._is_unsupported_dynamic(node):
            self.has_unsupported_dynamic = True
            self.func_depth += 1
            res = self.generic_visit(node)
            self.func_depth -= 1
            return res

        node_copy = ast.FunctionDef(
            name=node.name,
            args=node.args,
            body=[ast.Pass()],
            decorator_list=node.decorator_list,
            returns=node.returns,
            type_comment=getattr(node, "type_comment", None),
            type_params=getattr(node, "type_params", []),
        )
        return node_copy

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        if self.class_depth != 1 or self.func_depth != 0:
            self.func_depth += 1
            res = self.generic_visit(node)
            self.func_depth -= 1
            return res

        if self._is_unsupported_dynamic(node):
            self.has_unsupported_dynamic = True
            self.func_depth += 1
            res = self.generic_visit(node)
            self.func_depth -= 1
            return res

        node_copy = ast.AsyncFunctionDef(
            name=node.name,
            args=node.args,
            body=[ast.Pass()],
            decorator_list=node.decorator_list,
            returns=node.returns,
            type_comment=getattr(node, "type_comment", None),
            type_params=getattr(node, "type_params", []),
        )
        return node_copy


def _compare_python_ast(old_src: str, new_src: str) -> tuple[ChangeCategory, bool, bool | None, str]:
    """Lossless structural AST comparison preserving order, multiplicity, and class statements."""
    try:
        old_tree = ast.parse(old_src)
    except SyntaxError as err:
        return (
            ChangeCategory.UNKNOWN_UNCERTAIN,
            True,
            None,
            f"Syntax error in original Python file: {err}",
        )

    try:
        new_tree = ast.parse(new_src)
    except SyntaxError as err:
        return (
            ChangeCategory.UNKNOWN_UNCERTAIN,
            True,
            None,
            f"Syntax error in modified Python file: {err}",
        )

    # 1. Bytecode / semantic AST equivalence check (formatting or comments only)
    if ast.dump(old_tree, include_attributes=False) == ast.dump(new_tree, include_attributes=False):
        return (
            ChangeCategory.METHOD_ONLY,
            True,
            False,
            "No semantic AST difference (formatting or comments only); fresh process required for changed Python.",
        )

    # 2. Mask method bodies and compare structural skeleton
    masker_old = _MethodBodyMasker()
    masked_old = masker_old.visit(ast.fix_missing_locations(ast.parse(old_src)))

    masker_new = _MethodBodyMasker()
    masked_new = masker_new.visit(ast.fix_missing_locations(ast.parse(new_src)))

    if masker_old.has_unsupported_dynamic or masker_new.has_unsupported_dynamic:
        return (
            ChangeCategory.MODEL_CLASS,
            True,
            True,
            "Method defines nested classes or dynamic models; module update required.",
        )

    dump_old = ast.dump(masked_old, include_attributes=False)
    dump_new = ast.dump(masked_new, include_attributes=False)

    if dump_old == dump_new:
        # Proven method-only logic change!
        return (
            ChangeCategory.METHOD_ONLY,
            True,
            False,
            "Proven method-only Python logic change: class structure, field declarations, and method decorators unchanged; only method body modified.",
        )

    # 3. Structural change detected outside method bodies.
    # Check if decorators changed on any class or function:
    old_decorators = [ast.dump(d, include_attributes=False) for n in ast.walk(masked_old) if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) for d in n.decorator_list]
    new_decorators = [ast.dump(d, include_attributes=False) for n in ast.walk(masked_new) if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) for d in n.decorator_list]
    if old_decorators != new_decorators:
        return (
            ChangeCategory.MODEL_CLASS,
            True,
            True,
            "Decorators modified on class or method; module update required.",
        )

    # Check function / method signatures (arguments, defaults)
    old_signatures = [ast.dump(n.args, include_attributes=False) for n in ast.walk(masked_old) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    new_signatures = [ast.dump(n.args, include_attributes=False) for n in ast.walk(masked_new) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if old_signatures != new_signatures:
        return (
            ChangeCategory.MODEL_CLASS,
            True,
            True,
            "Signature modified on method or function; module update required.",
        )

    # Check if structural model attributes changed (_name, _inherit, _inherits, etc.)
    def get_attr_assignments(tree: ast.AST) -> dict[str, str]:
        attrs = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id.startswith("_"):
                        attrs[t.id] = ast.dump(n.value, include_attributes=False)
        return attrs

    old_attrs = get_attr_assignments(masked_old)
    new_attrs = get_attr_assignments(masked_new)
    if old_attrs != new_attrs:
        diff_keys = sorted(
            set(old_attrs.keys()) ^ set(new_attrs.keys())
            | {k for k in old_attrs if old_attrs[k] != new_attrs.get(k)}
        )
        diff_attr = diff_keys[0] if diff_keys else "attribute"
        return (
            ChangeCategory.MODEL_CLASS,
            True,
            True,
            f"Structural model attribute '{diff_attr}' modified; module update required.",
        )

    # Check class inheritance / bases
    old_bases = [ast.dump(b, include_attributes=False) for n in ast.walk(masked_old) if isinstance(n, ast.ClassDef) for b in n.bases]
    new_bases = [ast.dump(b, include_attributes=False) for n in ast.walk(masked_new) if isinstance(n, ast.ClassDef) for b in n.bases]
    if old_bases != new_bases:
        return (
            ChangeCategory.MODEL_CLASS,
            True,
            True,
            "Class inheritance or bases modified; module update required.",
        )

    # Check if field declarations or assignments were touched
    def has_field_nodes(tree: ast.AST) -> bool:
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in (
                "Char", "Integer", "Float", "Boolean", "Many2one", "One2many",
                "Many2many", "Selection", "Text", "Html", "Date", "Datetime",
                "Binary", "Monetary", "Reference", "Id"
            ):
                return True
        return False

    if has_field_nodes(masked_old) != has_field_nodes(masked_new) or "fields." in old_src or "fields." in new_src:
        return (
            ChangeCategory.FIELD_DECLARATION,
            True,
            True,
            "ORM field declaration or class attribute added, removed, or modified; module update required.",
        )

    return (
        ChangeCategory.MODEL_CLASS,
        True,
        True,
        "Structural Python changes detected (classes, inheritance, imports, or attributes); module update required.",
    )


def _classify_single_path_role(
    path_str: str,
    owning_addon: str | None,
    read_file: Callable[[str], bytes | None],
    manifest_data_files: set[str],
    is_removal: bool = False,
) -> tuple[ChangeCategory, bool, bool | None, str]:
    """Determine the lifecycle role of a single path in a snapshot."""
    p = PurePosixPath(path_str)

    # Unowned files
    if owning_addon is None:
        if is_documentation_path(path_str):
            return (
                ChangeCategory.DOCUMENTATION,
                False,
                False,
                "Documentation file; no runtime or schema impact.",
            )
        if is_framework_build_path(path_str):
            return (
                ChangeCategory.FRAMEWORK_BUILD,
                True,
                None,
                "Framework or build configuration file.",
            )
        return (
            ChangeCategory.UNKNOWN_UNCERTAIN,
            True,
            None,
            f"Unowned runtime source or unclassified file: '{path_str}'.",
        )

    # Manifest file
    if p.name == "__manifest__.py":
        return (
            ChangeCategory.MANIFEST_CHANGE,
            True,
            True,
            f"Addon manifest '{path_str}'.",
        )

    # Test file
    if _is_test_file(path_str):
        raw = read_file(path_str)
        if raw is None:
            return (
                ChangeCategory.UNKNOWN_UNCERTAIN,
                True,
                None,
                f"Could not read test file '{path_str}'.",
            )
        try:
            src_text = raw.decode("utf-8")
            ast.parse(src_text)
        except SyntaxError as err:
            return (
                ChangeCategory.UNKNOWN_UNCERTAIN,
                True,
                None,
                f"Syntax error in test file '{path_str}': {err}",
            )
        except UnicodeDecodeError:
            return (
                ChangeCategory.UNKNOWN_UNCERTAIN,
                True,
                None,
                f"Could not decode test file '{path_str}' as UTF-8.",
            )
        return (
            ChangeCategory.TESTS_ONLY,
            True,
            False,
            "Addon tests Python file; execution requires fresh Python process.",
        )

    # XML / CSV / Manifest-listed data files
    is_xml_or_csv = p.suffix.lower() in (".xml", ".csv")
    is_listed_data = path_str in manifest_data_files
    if is_xml_or_csv or is_listed_data:
        return (
            ChangeCategory.XML_DATA,
            True,
            True,
            "XML views, data, or security CSV definitions; requires module update.",
        )

    # Documentation files within addon
    if is_documentation_path(path_str):
        return (
            ChangeCategory.DOCUMENTATION,
            False,
            False,
            "Documentation file; no runtime or schema impact.",
        )

    # Python source files in addon
    if p.suffix == ".py":
        raw = read_file(path_str)
        if raw is None:
            return (
                ChangeCategory.UNKNOWN_UNCERTAIN,
                True,
                None,
                f"Could not read Python source file '{path_str}'.",
            )
        try:
            src_text = raw.decode("utf-8")
            ast.parse(src_text)
        except SyntaxError as err:
            return (
                ChangeCategory.UNKNOWN_UNCERTAIN,
                True,
                None,
                f"Syntax error in Python source '{path_str}': {err}",
            )
        except UnicodeDecodeError:
            return (
                ChangeCategory.UNKNOWN_UNCERTAIN,
                True,
                None,
                f"Could not decode Python file '{path_str}' as UTF-8.",
            )
        return (
            ChangeCategory.MODEL_CLASS,
            True,
            True,
            f"Python source file '{path_str}' in addon '{owning_addon}'.",
        )

    # Unknown file in addon
    return (
        ChangeCategory.UNKNOWN_UNCERTAIN,
        True,
        None,
        f"Unclassified file type or uncertain ownership in addon: '{path_str}'.",
    )


def classify_file_change(
    owned_rec: OwnedChangeRecord,
    read_old_file: Callable[[str], bytes | None],
    read_new_file: Callable[[str], bytes | None],
    manifest_data_files: set[str],
) -> FileClassification:
    """Classify an owned Git change record into category and lifecycle impact."""
    rec = owned_rec.record
    eff_path = rec.effective_path

    # 1. Unmerged Git conflicts
    if rec.is_unmerged:
        return FileClassification(
            owned_record=owned_rec,
            category=ChangeCategory.UNKNOWN_UNCERTAIN,
            fresh_process=True,
            module_update=None,
            reason=f"Unmerged Git conflict detected at '{eff_path}'.",
        )

    # 2. Submodules
    if rec.is_submodule:
        return FileClassification(
            owned_record=owned_rec,
            category=ChangeCategory.UNKNOWN_UNCERTAIN,
            fresh_process=True,
            module_update=None,
            reason=f"Submodule change detected at '{eff_path}'.",
        )

    # 3. Symlinks
    if rec.is_symlink:
        return FileClassification(
            owned_record=owned_rec,
            category=ChangeCategory.UNKNOWN_UNCERTAIN,
            fresh_process=True,
            module_update=None,
            reason=f"Unsupported symlink detected at '{eff_path}'.",
        )

    # 4. Renames across paths or roles (Contract B)
    if rec.status == "R" and rec.old_path and rec.new_path:
        old_cat, old_fp, old_mu, old_reason = _classify_single_path_role(
            rec.old_path,
            owned_rec.old_owner,
            read_old_file,
            manifest_data_files,
            is_removal=True,
        )
        new_cat, new_fp, new_mu, new_reason = _classify_single_path_role(
            rec.new_path,
            owned_rec.new_owner,
            read_new_file,
            manifest_data_files,
            is_removal=False,
        )

        comb_fp = old_fp or new_fp

        old_is_uncertain = (old_mu is None or old_cat == ChangeCategory.UNKNOWN_UNCERTAIN)
        new_is_uncertain = (new_mu is None or new_cat == ChangeCategory.UNKNOWN_UNCERTAIN)
        comb_uncertain = old_is_uncertain or new_is_uncertain

        if old_mu is True or new_mu is True:
            comb_mu: bool | None = True
        elif old_mu is None or new_mu is None:
            comb_mu = None
        else:
            comb_mu = False

        if comb_uncertain or comb_mu is None:
            cat = ChangeCategory.UNKNOWN_UNCERTAIN
        elif comb_mu is True:
            cat = ChangeCategory.MODEL_CLASS if old_cat == ChangeCategory.MODEL_CLASS or new_cat == ChangeCategory.MODEL_CLASS else ChangeCategory.XML_DATA
        else:
            cat = ChangeCategory.TESTS_ONLY if (old_cat == ChangeCategory.TESTS_ONLY and new_cat == ChangeCategory.TESTS_ONLY) else ChangeCategory.DOCUMENTATION

        return FileClassification(
            owned_record=owned_rec,
            category=cat,
            fresh_process=comb_fp,
            module_update=comb_mu,
            reason=f"Renamed '{rec.old_path}' ({old_reason}) -> '{rec.new_path}' ({new_reason})",
            uncertain=comb_uncertain,
        )

    # 5. Added files
    if rec.status == "A" and rec.new_path:
        cat, fp, mu, reason = _classify_single_path_role(
            rec.new_path,
            owned_rec.new_owner,
            read_new_file,
            manifest_data_files,
            is_removal=False,
        )
        return FileClassification(
            owned_record=owned_rec,
            category=cat,
            fresh_process=fp,
            module_update=mu,
            reason=f"Added file: {reason}",
            uncertain=(cat == ChangeCategory.UNKNOWN_UNCERTAIN or mu is None),
        )

    # 6. Deleted files
    if rec.status == "D" and rec.old_path:
        cat, fp, mu, reason = _classify_single_path_role(
            rec.old_path,
            owned_rec.old_owner,
            read_old_file,
            manifest_data_files,
            is_removal=True,
        )
        return FileClassification(
            owned_record=owned_rec,
            category=cat,
            fresh_process=fp,
            module_update=mu,
            reason=f"Deleted file: {reason}",
            uncertain=(cat == ChangeCategory.UNKNOWN_UNCERTAIN or mu is None),
        )

    # 7. Modified files
    # Check unowned runtime files first (Contract D)
    if owned_rec.old_owner is None and owned_rec.new_owner is None:
        if is_documentation_path(eff_path):
            return FileClassification(
                owned_record=owned_rec,
                category=ChangeCategory.DOCUMENTATION,
                fresh_process=False,
                module_update=False,
                reason="Documentation file change; no runtime or schema impact.",
            )
        if is_framework_build_path(eff_path):
            return FileClassification(
                owned_record=owned_rec,
                category=ChangeCategory.FRAMEWORK_BUILD,
                fresh_process=True,
                module_update=None,
                reason="Framework or build configuration changed.",
            )
        return FileClassification(
            owned_record=owned_rec,
            category=ChangeCategory.UNKNOWN_UNCERTAIN,
            fresh_process=True,
            module_update=None,
            reason=f"Unowned runtime source or unclassified file: '{eff_path}'.",
        )

    p = PurePosixPath(eff_path)

    # Manifest file
    if p.name == "__manifest__.py":
        return FileClassification(
            owned_record=owned_rec,
            category=ChangeCategory.MANIFEST_CHANGE,
            fresh_process=True,
            module_update=True,
            reason=f"Addon manifest modified at '{eff_path}'.",
        )

    # Test file
    if _is_test_file(eff_path):
        # Validate syntax of modified test file! (Contract D)
        old_bytes = read_old_file(eff_path)
        new_bytes = read_new_file(eff_path)
        if old_bytes is None or new_bytes is None:
            return FileClassification(
                owned_record=owned_rec,
                category=ChangeCategory.UNKNOWN_UNCERTAIN,
                fresh_process=True,
                module_update=None,
                reason=f"Could not read test file contents for '{eff_path}'.",
                uncertain=True,
            )
        try:
            ast.parse(new_bytes.decode("utf-8"))
        except SyntaxError as err:
            return FileClassification(
                owned_record=owned_rec,
                category=ChangeCategory.UNKNOWN_UNCERTAIN,
                fresh_process=True,
                module_update=None,
                reason=f"Syntax error in test file '{eff_path}': {err}",
                uncertain=True,
            )
        except UnicodeDecodeError:
            return FileClassification(
                owned_record=owned_rec,
                category=ChangeCategory.UNKNOWN_UNCERTAIN,
                fresh_process=True,
                module_update=None,
                reason=f"Could not decode test file '{eff_path}' as UTF-8.",
                uncertain=True,
            )
        return FileClassification(
            owned_record=owned_rec,
            category=ChangeCategory.TESTS_ONLY,
            fresh_process=True,
            module_update=False,
            reason="Addon tests Python file change; execution requires fresh Python process.",
        )

    # XML / CSV / Manifest data files
    is_xml_or_csv = p.suffix.lower() in (".xml", ".csv")
    is_listed_data = eff_path in manifest_data_files
    if is_xml_or_csv or is_listed_data:
        return FileClassification(
            owned_record=owned_rec,
            category=ChangeCategory.XML_DATA,
            fresh_process=True,
            module_update=True,
            reason="XML views, data, or security CSV definitions changed; requires module update.",
        )

    # Documentation files in addon
    if is_documentation_path(eff_path):
        return FileClassification(
            owned_record=owned_rec,
            category=ChangeCategory.DOCUMENTATION,
            fresh_process=False,
            module_update=False,
            reason="Documentation file change; no runtime or schema impact.",
        )

    # Python source in addon
    if p.suffix == ".py":
        old_bytes = read_old_file(rec.old_path or eff_path)
        new_bytes = read_new_file(rec.new_path or eff_path)

        if old_bytes is None or new_bytes is None:
            return FileClassification(
                owned_record=owned_rec,
                category=ChangeCategory.UNKNOWN_UNCERTAIN,
                fresh_process=True,
                module_update=None,
                reason=f"Could not read Python source contents for '{eff_path}'.",
                uncertain=True,
            )

        try:
            old_src = old_bytes.decode("utf-8")
            new_src = new_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return FileClassification(
                owned_record=owned_rec,
                category=ChangeCategory.UNKNOWN_UNCERTAIN,
                fresh_process=True,
                module_update=None,
                reason=f"Could not decode Python file '{eff_path}' as UTF-8.",
                uncertain=True,
            )

        cat, fp, mu, reason = _compare_python_ast(old_src, new_src)
        return FileClassification(
            owned_record=owned_rec,
            category=cat,
            fresh_process=fp,
            module_update=mu,
            reason=reason,
            uncertain=(cat == ChangeCategory.UNKNOWN_UNCERTAIN or mu is None),
        )

    return FileClassification(
        owned_record=owned_rec,
        category=ChangeCategory.UNKNOWN_UNCERTAIN,
        fresh_process=True,
        module_update=None,
        reason=f"Unclassified file type or uncertain ownership: '{eff_path}'.",
    )
