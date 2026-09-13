"""Safe manifest discovery, AST parsing, and dependency graph for Odoo addons."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence


class ManifestError(Exception):
    """Raised when manifest validation or discovery fails fatally."""


@dataclass(frozen=True)
class ManifestInfo:
    """Metadata extracted from an addon manifest."""

    name: str
    relative_dir: str
    manifest_path: str
    depends: tuple[str, ...]
    data_files: tuple[str, ...]
    is_valid: bool = True
    error_message: str | None = None


def parse_manifest_ast(content: str, filename: str = "__manifest__.py") -> tuple[dict, str | None]:
    """Safely parse an Odoo manifest string using AST parsing and literal_eval.

    Accepts an optional module docstring followed by a single literal dictionary expression.
    Rejects any executable code, imports, function calls, or non-literal statements.
    """
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError as err:
        return {}, f"Syntax error in {filename}: {err}"

    body = tree.body
    # Allow optional module docstring as first statement
    dict_expr: ast.Dict | None = None

    if len(body) == 1:
        stmt = body[0]
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Dict):
            dict_expr = stmt.value
    elif len(body) == 2:
        stmt1, stmt2 = body
        is_docstring = (
            isinstance(stmt1, ast.Expr)
            and isinstance(stmt1.value, ast.Constant)
            and isinstance(stmt1.value.value, str)
        )
        if is_docstring and isinstance(stmt2, ast.Expr) and isinstance(stmt2.value, ast.Dict):
            dict_expr = stmt2.value

    if dict_expr is None:
        return (
            {},
            f"Malformed manifest in {filename}: expected a single literal dict expression (optional docstring allowed), found non-literal or multiple statements.",
        )

    try:
        data = ast.literal_eval(dict_expr)
    except Exception as err:
        return {}, f"Failed to evaluate literal dictionary in {filename}: {err}"

    if not isinstance(data, dict):
        return {}, f"Manifest in {filename} did not evaluate to a dict."

    return data, None


def validate_and_extract_manifest(
    addon_name: str,
    relative_dir: str,
    manifest_path: str,
    content: str,
) -> ManifestInfo:
    """Parse and validate manifest content into ManifestInfo."""
    data, err = parse_manifest_ast(content, filename=manifest_path)
    if err is not None:
        return ManifestInfo(
            name=addon_name,
            relative_dir=relative_dir,
            manifest_path=manifest_path,
            depends=(),
            data_files=(),
            is_valid=False,
            error_message=err,
        )

    # Validate depends
    raw_depends = data.get("depends", [])
    if not isinstance(raw_depends, (list, tuple)):
        return ManifestInfo(
            name=addon_name,
            relative_dir=relative_dir,
            manifest_path=manifest_path,
            depends=(),
            data_files=(),
            is_valid=False,
            error_message=f"In {manifest_path}: 'depends' must be a list or tuple of strings, got {type(raw_depends).__name__}.",
        )

    validated_depends: list[str] = []
    for item in raw_depends:
        if not isinstance(item, str) or not item.strip():
            return ManifestInfo(
                name=addon_name,
                relative_dir=relative_dir,
                manifest_path=manifest_path,
                depends=(),
                data_files=(),
                is_valid=False,
                error_message=f"In {manifest_path}: 'depends' entries must be non-empty strings, got {item!r}.",
            )
        validated_depends.append(item.strip())

    # Deterministic deduplication preserving first occurrence
    unique_depends = tuple(dict.fromkeys(validated_depends))

    # Collect data/demo files
    data_files: list[str] = []
    for key in ("data", "demo", "views", "qweb"):
        items = data.get(key, [])
        if isinstance(items, (list, tuple)):
            for item in items:
                if isinstance(item, str) and item.strip():
                    # Normalize path relative to addon directory
                    norm = str(PurePosixPath(item.strip()))
                    if not norm.startswith("/") and not norm.startswith(".."):
                        full_rel = str(PurePosixPath(relative_dir) / norm)
                        data_files.append(full_rel)

    return ManifestInfo(
        name=addon_name,
        relative_dir=relative_dir,
        manifest_path=manifest_path,
        depends=unique_depends,
        data_files=tuple(dict.fromkeys(data_files)),
        is_valid=True,
        error_message=None,
    )


@dataclass(frozen=True)
class DependencyGraph:
    """Addon dependency graph with cycle detection and transitive closures."""

    addons: dict[str, ManifestInfo]
    dependencies: dict[str, tuple[str, ...]]
    reverse_dependencies: dict[str, tuple[str, ...]]
    unresolved_dependencies: dict[str, tuple[str, ...]]
    cycles: tuple[tuple[str, ...], ...]

    @property
    def has_cycles(self) -> bool:
        return len(self.cycles) > 0

    def get_transitive_dependencies(self, addon_name: str) -> tuple[str, ...]:
        """Return all direct and indirect dependencies for an addon in deterministic order."""
        visited: set[str] = set()
        queue = list(self.dependencies.get(addon_name, ()))

        while queue:
            dep = queue.pop(0)
            if dep not in visited:
                visited.add(dep)
                # Deterministic traversal order
                for next_dep in self.dependencies.get(dep, ()):
                    if next_dep not in visited:
                        queue.append(next_dep)

        return tuple(sorted(visited))

    def get_transitive_dependents(self, addon_name: str) -> tuple[str, ...]:
        """Return all direct and indirect reverse dependents for an addon in deterministic order."""
        visited: set[str] = set()
        queue = list(self.reverse_dependencies.get(addon_name, ()))

        while queue:
            dependent = queue.pop(0)
            if dependent not in visited:
                visited.add(dependent)
                for next_dep in self.reverse_dependencies.get(dependent, ()):
                    if next_dep not in visited:
                        queue.append(next_dep)

        return tuple(sorted(visited))


def _find_cycles_in_graph(graph: dict[str, tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Find all simple cycles in a directed graph deterministically."""
    visited: dict[str, int] = {}  # 0: unvisited, 1: visiting (in stack), 2: finished
    cycles: set[tuple[str, ...]] = set()

    for node in sorted(graph.keys()):
        if visited.get(node, 0) == 0:
            stack: list[str] = []

            def dfs(current: str) -> None:
                visited[current] = 1
                stack.append(current)

                for neighbor in sorted(graph.get(current, ())):
                    if neighbor not in graph:
                        continue
                    state = visited.get(neighbor, 0)
                    if state == 1:
                        # Cycle found! Extract sub-path
                        cycle_start_idx = stack.index(neighbor)
                        cycle_path = stack[cycle_start_idx:]
                        # Canonical rotation (start with min element)
                        min_node = min(cycle_path)
                        min_idx = cycle_path.index(min_node)
                        canonical = cycle_path[min_idx:] + cycle_path[:min_idx] + [min_node]
                        cycles.add(tuple(canonical))
                    elif state == 0:
                        dfs(neighbor)

                stack.pop()
                visited[current] = 2

            dfs(node)

    return sorted(cycles)


def build_dependency_graph(addons: Sequence[ManifestInfo]) -> DependencyGraph:
    """Construct DependencyGraph from a collection of ManifestInfo objects."""
    addon_map = {addon.name: addon for addon in addons}

    dependencies: dict[str, tuple[str, ...]] = {}
    reverse_deps_builder: dict[str, list[str]] = {name: [] for name in addon_map}
    unresolved_builder: dict[str, list[str]] = {}

    for addon in addons:
        valid_deps: list[str] = []
        unresolved: list[str] = []
        for dep in addon.depends:
            if dep in addon_map:
                valid_deps.append(dep)
                reverse_deps_builder[dep].append(addon.name)
            else:
                unresolved.append(dep)
                if dep not in reverse_deps_builder:
                    reverse_deps_builder[dep] = []
                reverse_deps_builder[dep].append(addon.name)

        dependencies[addon.name] = tuple(valid_deps)
        if unresolved:
            unresolved_builder[addon.name] = unresolved

    reverse_dependencies = {
        dep: tuple(sorted(set(deps))) for dep, deps in reverse_deps_builder.items()
    }
    unresolved_dependencies = {
        name: tuple(sorted(deps)) for name, deps in unresolved_builder.items()
    }

    cycles = tuple(_find_cycles_in_graph(dependencies))

    return DependencyGraph(
        addons=addon_map,
        dependencies=dependencies,
        reverse_dependencies=reverse_dependencies,
        unresolved_dependencies=unresolved_dependencies,
        cycles=cycles,
    )
