"""Path-component containment ownership resolution for modootest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping, Sequence

from modootest.intelligence.git import GitChangeRecord


@dataclass(frozen=True)
class OwnedChangeRecord:
    """A GitChangeRecord annotated with old and new owning addons."""

    record: GitChangeRecord
    old_owner: str | None
    new_owner: str | None

    @property
    def affected_addons(self) -> tuple[str, ...]:
        """Return deduplicated tuple of affected addon names in deterministic order."""
        owners = set()
        if self.old_owner is not None:
            owners.add(self.old_owner)
        if self.new_owner is not None:
            owners.add(self.new_owner)
        return tuple(sorted(owners))


def resolve_owning_addon(
    path_str: str | None,
    addon_directories: Mapping[str, str],
) -> str | None:
    """Resolve which addon owns a path using path-component containment.

    addon_directories maps relative addon directory path (e.g. 'addons/sale')
    to addon name ('sale').
    """
    if not path_str:
        return None

    path_obj = PurePosixPath(path_str)
    # Sort candidate addon directories by depth (most specific path first)
    candidates = sorted(
        addon_directories.items(),
        key=lambda item: len(PurePosixPath(item[0]).parts),
        reverse=True,
    )

    for addon_dir, addon_name in candidates:
        addon_p = PurePosixPath(addon_dir)
        try:
            path_obj.relative_to(addon_p)
            return addon_name
        except ValueError:
            continue

    return None


def resolve_change_ownership(
    change_records: Sequence[GitChangeRecord],
    old_addon_dirs: Mapping[str, str],
    new_addon_dirs: Mapping[str, str],
) -> list[OwnedChangeRecord]:
    """Resolve old and new addon ownership for each Git change record."""
    results: list[OwnedChangeRecord] = []
    for rec in change_records:
        old_owner = resolve_owning_addon(rec.old_path, old_addon_dirs)
        new_owner = resolve_owning_addon(rec.new_path, new_addon_dirs)
        results.append(
            OwnedChangeRecord(
                record=rec,
                old_owner=old_owner,
                new_owner=new_owner,
            )
        )
    return results
