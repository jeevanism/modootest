"""Validation and sanitization of pytest passthrough arguments."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
from typing import Sequence

FORBIDDEN_EXACT_ARGS = {
    "-d",
    "--database",
    "-u",
    "--update",
    "-i",
    "--init",
    "--db_host",
    "--db_user",
    "--db_password",
    "--db_port",
    "--db_name",
    "--db-filter",
    "--http-port",
    "--workers",
    "--dev",
    "--upgrade-path",
    "--load",
    "--load-language",
    "--save",
    "--stop-after-init",
    "-n",
    "--numprocesses",
    "--dist",
}

FORBIDDEN_PREFIX_ARGS = (
    "-d=",
    "--database=",
    "-u=",
    "--update=",
    "-i=",
    "--init=",
    "--db_host=",
    "--db_user=",
    "--db_password=",
    "--db_port=",
    "--db_name=",
    "--db-filter=",
    "--http-port=",
    "--workers=",
    "--dev=",
    "--upgrade-path=",
    "--load=",
    "--load-language=",
    "--save=",
    "--stop-after-init=",
    "-n=",
    "--numprocesses=",
    "--dist=",
)

ALLOWED_STANDALONE_FLAGS = {
    "-v",
    "-vv",
    "-vvv",
    "-vvvv",
    "--verbose",
    "-q",
    "-qq",
    "--quiet",
    "-x",
    "--exitfirst",
    "-s",
    "-l",
    "--showlocals",
    "-P",
    "--runxfail",
    "--disable-warnings",
    "--disable-pytest-warnings",
    "--strict-markers",
    "--failed-first",
    "--ff",
    "--new-first",
    "--nf",
    "--cache-show",
    "--cache-clear",
    "--stepwise",
    "--sw",
    "--collect-only",
}

ALLOWED_VALUE_OPTIONS = {
    "-k": "keyword",
    "--keyword": "keyword",
    "-m": "marker",
    "--maxfail": "int",
    "--tb": "tb",
    "--color": "color",
    "--capture": "capture",
    "-o": "ini",
    "--override-ini": "ini",
    "--durations": "int",
    "--import-mode": "import_mode",
    "--basetemp": "path",
    "--rootdir": "path",
    "--confcutdir": "path",
}

ALLOWED_TB_STYLES = {"auto", "long", "short", "line", "native", "no"}
ALLOWED_COLOR_OPTIONS = {"yes", "no", "auto"}
ALLOWED_CAPTURE_OPTIONS = {"fd", "sys", "no", "tee-sys"}
ALLOWED_IMPORT_MODES = {"prepend", "append", "importlib"}

_SENSITIVE_TOKENS = ("password", "secret", "token", "credential", "api_key", "api-key", "auth")


def _redact_pytest_arg(arg: str) -> str:
    """Redact sensitive attached values from argument tokens."""
    if "=" in arg:
        opt, val = arg.split("=", 1)
        if any(s in opt.lower() for s in _SENSITIVE_TOKENS):
            return f"{opt}=[REDACTED]"
    return arg


redact_pytest_arg = _redact_pytest_arg


def validate_pytest_args(
    raw_args: Sequence[str],
    git_root: Path,
) -> tuple[bool, str, list[str]]:
    """Validate pytest passthrough arguments against mutation, escaping, path injection, and parallel execution."""
    resolved_git_root = git_root.resolve()
    validated: list[str] = []

    tokens: list[str] = []
    for raw in raw_args:
        if "\0" in raw:
            return False, "Pytest argument contains null bytes.", []
        for pfx in ("-k ", "-m ", "-o "):
            if raw.startswith(pfx):
                parts = raw.split(" ", 1)
                tokens.extend(parts)
                break
        else:
            tokens.append(raw)

    i = 0
    n = len(tokens)
    while i < n:
        arg = tokens[i]

        if arg == "--":
            return (
                False,
                "Forbidden pytest argument '--': positional path separation and path injection are not allowed.",
                [],
            )

        if arg in FORBIDDEN_EXACT_ARGS:
            safe_arg = _redact_pytest_arg(arg)
            return (
                False,
                f"Forbidden pytest argument '{safe_arg}': database/server mutation and parallel execution (xdist) flags are not allowed.",
                [],
            )

        for pfx in FORBIDDEN_PREFIX_ARGS:
            if arg.startswith(pfx):
                safe_arg = _redact_pytest_arg(arg)
                return (
                    False,
                    f"Forbidden pytest argument '{safe_arg}': database/server mutation and parallel execution (xdist) flags are not allowed.",
                    [],
                )

        if not arg.startswith("-"):
            prev = tokens[i - 1] if i > 0 else ""
            if any(s in prev.lower() for s in _SENSITIVE_TOKENS):
                safe_arg = "[REDACTED]"
            else:
                safe_arg = arg
            return (
                False,
                f"Forbidden positional pytest argument '{safe_arg}': arbitrary test paths cannot be injected via --pytest-arg. The selector generates all execution paths.",
                [],
            )

        if arg in ALLOWED_STANDALONE_FLAGS:
            validated.append(arg)
            i += 1
            continue

        if arg.startswith("-r") and len(arg) > 2 and all(c in "afEsxXpPNAw" for c in arg[2:]):
            validated.append(arg)
            i += 1
            continue

        if "=" in arg:
            opt, val = arg.split("=", 1)
            if opt not in ALLOWED_VALUE_OPTIONS:
                safe_arg = _redact_pytest_arg(arg)
                return (
                    False,
                    f"Forbidden or unsupported pytest argument '{safe_arg}'. Only safe options from the allowlist are permitted.",
                    [],
                )

            kind = ALLOWED_VALUE_OPTIONS[opt]
            if kind == "path":
                try:
                    target = (
                        (git_root / val).resolve()
                        if not os.path.isabs(val)
                        else Path(val).resolve()
                    )
                    target.relative_to(resolved_git_root)
                except ValueError:
                    return (
                        False,
                        f"Forbidden pytest argument '{arg}': path escapes git repository ({git_root}).",
                        [],
                    )
                curr = git_root
                for part in PurePosixPath(val).parts:
                    if part == "..":
                        return (
                            False,
                            f"Forbidden pytest argument '{arg}': path traversal '..' is not allowed.",
                            [],
                        )
                    if part != "/":
                        curr = curr / part
                        if curr.is_symlink():
                            return (
                                False,
                                f"Forbidden pytest argument '{arg}': symlink in path '{val}' is not allowed.",
                                [],
                            )
            elif kind == "int":
                if not val.isdigit():
                    return False, f"Invalid integer value '{val}' for pytest argument '{opt}'.", []
            elif kind == "tb":
                if val not in ALLOWED_TB_STYLES:
                    return False, f"Invalid traceback style '{val}' for '{opt}'. Choose from: {sorted(ALLOWED_TB_STYLES)}.", []
            elif kind == "color":
                if val not in ALLOWED_COLOR_OPTIONS:
                    return False, f"Invalid color option '{val}' for '{opt}'. Choose from: {sorted(ALLOWED_COLOR_OPTIONS)}.", []
            elif kind == "capture":
                if val not in ALLOWED_CAPTURE_OPTIONS:
                    return False, f"Invalid capture option '{val}' for '{opt}'. Choose from: {sorted(ALLOWED_CAPTURE_OPTIONS)}.", []
            elif kind == "import_mode":
                if val not in ALLOWED_IMPORT_MODES:
                    return False, f"Invalid import mode '{val}' for '{opt}'. Choose from: {sorted(ALLOWED_IMPORT_MODES)}.", []

            validated.append(arg)
            i += 1
            continue

        if arg in ALLOWED_VALUE_OPTIONS:
            if i + 1 >= n:
                return False, f"Pytest argument '{arg}' requires a value.", []
            val = tokens[i + 1]
            kind = ALLOWED_VALUE_OPTIONS[arg]
            if kind == "path":
                try:
                    target = (
                        (git_root / val).resolve()
                        if not os.path.isabs(val)
                        else Path(val).resolve()
                    )
                    target.relative_to(resolved_git_root)
                except ValueError:
                    return (
                        False,
                        f"Forbidden pytest argument '{arg} {val}': path escapes git repository ({git_root}).",
                        [],
                    )
                curr = git_root
                for part in PurePosixPath(val).parts:
                    if part == "..":
                        return (
                            False,
                            f"Forbidden pytest argument '{arg} {val}': path traversal '..' is not allowed.",
                            [],
                        )
                    if part != "/":
                        curr = curr / part
                        if curr.is_symlink():
                            return (
                                False,
                                f"Forbidden pytest argument '{arg} {val}': symlink in path '{val}' is not allowed.",
                                [],
                            )
            elif kind == "int":
                if not val.isdigit():
                    return False, f"Invalid integer value '{val}' for pytest argument '{arg}'.", []
            elif kind == "tb":
                if val not in ALLOWED_TB_STYLES:
                    return False, f"Invalid traceback style '{val}' for '{arg}'. Choose from: {sorted(ALLOWED_TB_STYLES)}.", []
            elif kind == "color":
                if val not in ALLOWED_COLOR_OPTIONS:
                    return False, f"Invalid color option '{val}' for '{arg}'. Choose from: {sorted(ALLOWED_COLOR_OPTIONS)}.", []
            elif kind == "capture":
                if val not in ALLOWED_CAPTURE_OPTIONS:
                    return False, f"Invalid capture option '{val}' for '{arg}'. Choose from: {sorted(ALLOWED_CAPTURE_OPTIONS)}.", []
            elif kind == "import_mode":
                if val not in ALLOWED_IMPORT_MODES:
                    return False, f"Invalid import mode '{val}' for '{arg}'. Choose from: {sorted(ALLOWED_IMPORT_MODES)}.", []

            validated.append(arg)
            validated.append(val)
            i += 2
            continue

        return (
            False,
            f"Forbidden or unsupported pytest argument '{arg}'. Only safe options from the allowlist are permitted.",
            [],
        )

    return True, "", validated


_validate_pytest_args = validate_pytest_args
