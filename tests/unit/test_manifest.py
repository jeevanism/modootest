"""Tests for safe manifest parsing, discovery, and dependency graph."""

import pytest
from pathlib import Path
from modootest.intelligence.manifest import (
    ManifestError,
    ManifestInfo,
    build_dependency_graph,
    parse_manifest_ast,
    validate_and_extract_manifest,
)


def test_literal_manifest_success():
    content = (
        "# Header comment\n"
        "\"\"\"Addon docstring.\"\"\"\n"
        "{\n"
        "    'name': 'Test Addon',\n"
        "    'version': '1.0.0',\n"
        "    'depends': ['base', 'web'],\n"
        "    'data': ['views/test_views.xml', 'security/ir.model.access.csv'],\n"
        "    'demo': ['demo/demo_data.xml'],\n"
        "}\n"
    )
    info = validate_and_extract_manifest("test_addon", "addons/test_addon", "addons/test_addon/__manifest__.py", content)
    assert info.is_valid
    assert info.name == "test_addon"
    assert info.depends == ("base", "web")
    assert "addons/test_addon/views/test_views.xml" in info.data_files
    assert "addons/test_addon/security/ir.model.access.csv" in info.data_files
    assert "addons/test_addon/demo/demo_data.xml" in info.data_files
    assert info.error_message is None


def test_malicious_call_and_import_rejection(tmp_path):
    side_effect_file = tmp_path / "hacked.txt"
    malicious_content = f"""
import os
os.system("touch {side_effect_file}")
{{
    'name': 'Malicious',
    'depends': ['base'],
}}
"""
    data, err = parse_manifest_ast(malicious_content)
    assert not side_effect_file.exists(), "Side effect executed during manifest parsing!"
    assert err is not None
    assert "Malformed manifest" in err

    info = validate_and_extract_manifest("malicious", "addons/malicious", "addons/malicious/__manifest__.py", malicious_content)
    assert not info.is_valid
    assert not side_effect_file.exists()


def test_manifest_function_call_rejection():
    content = """
{
    'name': 'evil',
    'version': str(1 + 2),
}
"""
    data, err = parse_manifest_ast(content)
    assert err is not None
    assert "Failed to evaluate literal dictionary" in err


def test_malformed_depends():
    # 1. depends is not a list/tuple
    c1 = "{'depends': 'base'}"
    i1 = validate_and_extract_manifest("a", "addons/a", "addons/a/__manifest__.py", c1)
    assert not i1.is_valid
    assert "'depends' must be a list or tuple" in (i1.error_message or "")

    # 2. depends contains non-string
    c2 = "{'depends': ['base', 123]}"
    i2 = validate_and_extract_manifest("a", "addons/a", "addons/a/__manifest__.py", c2)
    assert not i2.is_valid
    assert "'depends' entries must be non-empty strings" in (i2.error_message or "")

    # 3. depends contains empty string
    c3 = "{'depends': ['base', '   ']}"
    i3 = validate_and_extract_manifest("a", "addons/a", "addons/a/__manifest__.py", c3)
    assert not i3.is_valid
    assert "'depends' entries must be non-empty strings" in (i3.error_message or "")


def test_depends_deterministic_deduplication():
    content = "{'depends': ['web', 'base', 'web', 'mail', 'base']}"
    info = validate_and_extract_manifest("a", "addons/a", "addons/a/__manifest__.py", content)
    assert info.is_valid
    assert info.depends == ("web", "base", "mail")


def test_missing_and_unresolved_dependencies():
    m_a = ManifestInfo("a", "addons/a", "addons/a/__manifest__.py", ("b", "external_dep"), ())
    m_b = ManifestInfo("b", "addons/b", "addons/b/__manifest__.py", (), ())

    graph = build_dependency_graph([m_a, m_b])
    assert "a" in graph.addons
    assert "b" in graph.addons
    assert "external_dep" not in graph.addons
    # Unresolved dependency is tracked explicitly
    assert graph.unresolved_dependencies == {"a": ("external_dep",)}
    # Reverse dependency exists even for unresolved
    assert "a" in graph.reverse_dependencies["external_dep"]
    assert graph.reverse_dependencies["b"] == ("a",)


def test_cycle_detection_and_termination():
    # A -> B -> C -> A (cycle)
    # D -> B (dependent on cycle)
    m_a = ManifestInfo("a", "addons/a", "addons/a/__manifest__.py", ("b",), ())
    m_b = ManifestInfo("b", "addons/b", "addons/b/__manifest__.py", ("c",), ())
    m_c = ManifestInfo("c", "addons/c", "addons/c/__manifest__.py", ("a",), ())
    m_d = ManifestInfo("d", "addons/d", "addons/d/__manifest__.py", ("b",), ())

    graph = build_dependency_graph([m_a, m_b, m_c, m_d])
    assert graph.has_cycles
    assert len(graph.cycles) == 1
    assert graph.cycles[0] == ("a", "b", "c", "a")

    # Transitive traversal terminates safely despite cycle
    deps_d = graph.get_transitive_dependencies("d")
    assert set(deps_d) == {"a", "b", "c"}

    rev_a = graph.get_transitive_dependents("a")
    assert set(rev_a) == {"a", "b", "c", "d"}


def test_diamond_and_disconnected_graphs():
    # Diamond: D -> B -> A, D -> C -> A
    # Disconnected: X -> Y
    m_a = ManifestInfo("a", "addons/a", "addons/a/__manifest__.py", (), ())
    m_b = ManifestInfo("b", "addons/b", "addons/b/__manifest__.py", ("a",), ())
    m_c = ManifestInfo("c", "addons/c", "addons/c/__manifest__.py", ("a",), ())
    m_d = ManifestInfo("d", "addons/d", "addons/d/__manifest__.py", ("b", "c"), ())

    m_x = ManifestInfo("x", "addons/x", "addons/x/__manifest__.py", ("y",), ())
    m_y = ManifestInfo("y", "addons/y", "addons/y/__manifest__.py", (), ())

    graph = build_dependency_graph([m_a, m_b, m_c, m_d, m_x, m_y])
    assert not graph.has_cycles

    # Transitive dependencies of d: a, b, c
    assert graph.get_transitive_dependencies("d") == ("a", "b", "c")

    # Transitive dependents of a: b, c, d
    assert graph.get_transitive_dependents("a") == ("b", "c", "d")

    # Disconnected component x, y
    assert graph.get_transitive_dependencies("x") == ("y",)
    assert graph.get_transitive_dependents("y") == ("x",)
    assert graph.get_transitive_dependents("x") == ()


def test_deterministic_repeatability():
    m_d = ManifestInfo("d", "addons/d", "addons/d/__manifest__.py", ("c", "b"), ())
    m_c = ManifestInfo("c", "addons/c", "addons/c/__manifest__.py", ("a",), ())
    m_b = ManifestInfo("b", "addons/b", "addons/b/__manifest__.py", ("a",), ())
    m_a = ManifestInfo("a", "addons/a", "addons/a/__manifest__.py", (), ())

    g1 = build_dependency_graph([m_d, m_c, m_b, m_a])
    g2 = build_dependency_graph([m_a, m_b, m_c, m_d])

    assert g1.get_transitive_dependencies("d") == g2.get_transitive_dependencies("d")
    assert g1.get_transitive_dependents("a") == g2.get_transitive_dependents("a")
    assert g1.get_transitive_dependents("a") == ("b", "c", "d")
