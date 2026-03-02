"""Manage the local hardcover-lists.txt inventory file."""

from __future__ import annotations

from pathlib import Path

DEFAULT_INVENTORY = Path("/home/eve/repo/book-lists/hardcover-lists.txt")


def read_inventory(path: Path = DEFAULT_INVENTORY) -> set[str]:
    """Read existing list names from inventory file."""
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def update_inventory(
    new_names: list[str],
    path: Path = DEFAULT_INVENTORY,
) -> int:
    """Merge new names into inventory, write atomically. Returns new count."""
    existing = read_inventory(path)
    merged = existing | set(new_names)
    added = len(merged) - len(existing)

    sorted_names = sorted(merged)

    # Atomic write: temp file in same dir, then rename
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_path = parent / ".hardcover-lists.tmp"
    try:
        tmp_path.write_text("\n".join(sorted_names) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return added
