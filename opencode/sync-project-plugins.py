#!/usr/bin/env python3
"""Sync project opencode plugins from the global config.

Each project keeps its own Hindsight recallTags and retainTags values. All other
plugin configuration comes from ~/.config/opencode/opencode.json.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

HINDSIGHT_PLUGIN = "@vectorize-io/opencode-hindsight"
SCHEMA_URL = "https://opencode.ai/config.json"
DEFAULT_PROJECT_LIST = Path(__file__).with_name("projects.txt")

JsonObject = dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sync project .opencode/opencode.json plugin arrays "
            "from global config."
        ),
    )
    parser.add_argument(
        "--global-config",
        type=Path,
        default=Path.home() / ".config/opencode/opencode.json",
        help="Global opencode config to copy plugins from.",
    )
    parser.add_argument(
        "--project-list",
        type=Path,
        default=DEFAULT_PROJECT_LIST,
        help="Text file listing project paths to sync.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.home() / "repo",
        help="Directory containing relative project-list entries.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files.",
    )
    return parser.parse_args()


def load_json(path: Path) -> JsonObject:
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SystemExit(f"missing config: {path}") from error
    except json.JSONDecodeError as error:
        message = (
            f"invalid JSON in {path}: line {error.lineno}, "
            f"column {error.colno}: {error.msg}"
        )
        raise SystemExit(message) from error

    if not isinstance(data, dict):
        raise SystemExit(f"config must be a JSON object: {path}")
    return data


def read_project_list(path: Path) -> list[str]:
    path = path.expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise SystemExit(f"missing project list: {path}") from error

    projects = []
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        projects.append(entry)

    if not projects:
        raise SystemExit(f"project list is empty: {path}")
    return projects


def project_root_for_entry(entry: str, repo_root: Path) -> Path:
    path = Path(entry).expanduser()
    if path.is_absolute():
        return path
    return repo_root.expanduser() / path


def project_config_paths(
    project_root: Path,
    dry_run: bool,
) -> tuple[Path, Path, bool]:
    target = project_root / ".opencode/opencode.json"
    legacy = project_root / "opencode.json"

    if target.exists() and legacy.exists():
        message = f"both config paths exist in {project_root}; refusing to choose"
        raise SystemExit(message)
    if target.exists():
        return target, target, False
    if not legacy.exists():
        raise SystemExit(f"missing config for {project_root}")

    if dry_run:
        return legacy, target, True

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(os.fspath(legacy), os.fspath(target))
    return target, target, True


def get_hindsight_options(config: JsonObject) -> JsonObject | None:
    plugins = config.get("plugin")
    if not isinstance(plugins, list):
        return None

    for plugin in plugins:
        if not isinstance(plugin, list) or len(plugin) != 2:
            continue
        name, options = plugin
        if name == HINDSIGHT_PLUGIN and isinstance(options, dict):
            return options
    return None


def project_tags(config: JsonObject) -> JsonObject:
    options = get_hindsight_options(config)
    if options is None:
        return {}

    tags: JsonObject = {}
    for key in ("recallTags", "retainTags"):
        value = options.get(key)
        if value is not None:
            tags[key] = copy.deepcopy(value)
    return tags


def plugin_with_project_tags(
    global_config: JsonObject,
    tags: JsonObject,
) -> list[Any]:
    plugins = global_config.get("plugin")
    if not isinstance(plugins, list):
        raise SystemExit("global config must contain a plugin array")

    plugins = copy.deepcopy(plugins)
    for plugin in plugins:
        if not isinstance(plugin, list) or len(plugin) != 2:
            continue
        name, options = plugin
        if name == HINDSIGHT_PLUGIN and isinstance(options, dict):
            options.update(tags)
    return plugins


def write_config(path: Path, config: JsonObject, dry_run: bool) -> bool:
    rendered = json.dumps(config, indent=2) + "\n"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current == rendered:
        return False
    if not dry_run:
        path.write_text(rendered, encoding="utf-8")
    return True


def sync_project(
    entry: str,
    repo_root: Path,
    global_config: JsonObject,
    dry_run: bool,
) -> str:
    project_root = project_root_for_entry(entry, repo_root)
    read_path, write_path, moved = project_config_paths(project_root, dry_run)
    config = load_json(read_path)
    config.setdefault("$schema", SCHEMA_URL)
    tags = project_tags(config)
    config["plugin"] = plugin_with_project_tags(global_config, tags)
    changed = write_config(write_path, config, dry_run)

    actions = []
    if moved:
        actions.append("moved")
    if changed:
        actions.append("updated")
    if not actions:
        actions.append("unchanged")
    return f"{entry}: {', '.join(actions)} {write_path}"


def main() -> int:
    args = parse_args()
    global_config = load_json(args.global_config)
    repo_root = args.repo_root.expanduser()
    projects = read_project_list(args.project_list)

    for project in projects:
        print(sync_project(project, repo_root, global_config, args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
