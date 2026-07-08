#!/usr/bin/env python3
"""Create .opencode/opencode.json for the git repo covering cwd.

The generated file copies the global opencode plugin array, injects the repo's
Hindsight recallTags and retainTags, and registers the repo in projects.txt.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HINDSIGHT_PLUGIN = "@vectorize-io/opencode-hindsight"
SCHEMA_URL = "https://opencode.ai/config.json"
DEFAULT_PROJECT_LIST = Path(__file__).with_name("projects.txt")

JsonObject = dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create .opencode/opencode.json for the current git repo.",
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
        help="Text file that sync-project-plugins.py reads.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.home() / "repo",
        help="Base path used for relative project-list entries.",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path.cwd(),
        help="Directory to resolve the covering git repo from.",
    )
    parser.add_argument(
        "--recall-tags",
        help="Comma-separated recallTags. Prompted when omitted.",
    )
    parser.add_argument(
        "--retain-tags",
        help="Comma-separated retainTags. Prompted when omitted.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .opencode/opencode.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without creating files.",
    )
    return parser.parse_args()


def find_git_root(cwd: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(cwd.expanduser()), "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return None

    path = result.stdout.strip()
    if not path:
        return None
    return Path(path).resolve()


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


def split_tags(value: str) -> list[str]:
    tags = []
    for part in value.split(","):
        tag = part.strip()
        if tag:
            tags.append(tag)
    return tags


def prompt_tags(label: str, default: list[str], provided: str | None) -> list[str]:
    if provided is not None:
        return split_tags(provided)

    default_text = ", ".join(default)
    prompt = f"{label} [{default_text}] (comma-separated, '-' for none): "
    try:
        answer = input(prompt).strip()
    except EOFError:
        answer = ""

    if not answer:
        return default
    if answer == "-":
        return []
    return split_tags(answer)


def global_plugins(global_config: JsonObject) -> list[Any]:
    plugins = global_config.get("plugin")
    if not isinstance(plugins, list):
        raise SystemExit("global config must contain a plugin array")
    return copy.deepcopy(plugins)


def apply_hindsight_tags(
    plugins: list[Any],
    recall_tags: list[str],
    retain_tags: list[str],
) -> list[Any]:
    for plugin in plugins:
        if not isinstance(plugin, list) or len(plugin) != 2:
            continue

        name, options = plugin
        if name != HINDSIGHT_PLUGIN or not isinstance(options, dict):
            continue

        set_or_remove(options, "recallTags", recall_tags)
        set_or_remove(options, "retainTags", retain_tags)
        return plugins

    raise SystemExit(f"global config plugin array is missing {HINDSIGHT_PLUGIN}")


def set_or_remove(options: JsonObject, key: str, tags: list[str]) -> None:
    if tags:
        options[key] = tags
    else:
        options.pop(key, None)


def output_path(repo_root: Path, force: bool) -> Path | None:
    target = repo_root / ".opencode/opencode.json"
    legacy = repo_root / "opencode.json"

    if legacy.exists():
        print(f"legacy config exists at {legacy}; move it to {target} first")
        return None
    if target.exists() and not force:
        print(f"config already exists at {target}; use --force to overwrite")
        return None
    return target


def project_list_entry(repo_root: Path, base: Path) -> str:
    base = base.expanduser().resolve()
    repo_root = repo_root.resolve()
    try:
        return repo_root.relative_to(base).as_posix()
    except ValueError:
        return repo_root.as_posix()


def read_project_entries(path: Path) -> list[str]:
    path = path.expanduser()
    if not path.exists():
        return []

    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        entries.append(entry)
    return entries


def append_project_entry(path: Path, entry: str, dry_run: bool) -> None:
    path = path.expanduser()
    entries = read_project_entries(path)
    if entry in entries:
        print(f"project list already contains {entry}")
        return

    if dry_run:
        print(f"would add {entry} to {path}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.exists() and path.read_text(encoding="utf-8"):
        prefix = "\n" if not path.read_text(encoding="utf-8").endswith("\n") else ""
    with path.open("a", encoding="utf-8") as file:
        file.write(f"{prefix}{entry}\n")
    print(f"added {entry} to {path}")


def build_config(
    global_config: JsonObject,
    recall_tags: list[str],
    retain_tags: list[str],
) -> JsonObject:
    plugins = global_plugins(global_config)
    plugins = apply_hindsight_tags(plugins, recall_tags, retain_tags)
    return {"$schema": SCHEMA_URL, "plugin": plugins}


def write_config(path: Path, config: JsonObject, dry_run: bool) -> None:
    rendered = json.dumps(config, indent=2) + "\n"
    if dry_run:
        print(rendered, end="")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    print(f"created {path}")


def main() -> int:
    args = parse_args()
    repo_root = find_git_root(args.cwd)
    if repo_root is None:
        print(f"no git repository covers {args.cwd}; nothing to do")
        return 0

    path = output_path(repo_root, args.force)
    if path is None:
        return 0

    repo_name = repo_root.name
    recall_default = [f"project:{repo_name}", "scope:global"]
    retain_default = [f"project:{repo_name}", f"repo:{repo_name}"]
    retain_default.append(f"domain:{repo_name}")
    recall_tags = prompt_tags("recallTags", recall_default, args.recall_tags)
    retain_tags = prompt_tags("retainTags", retain_default, args.retain_tags)

    global_config = load_json(args.global_config)
    config = build_config(global_config, recall_tags, retain_tags)
    write_config(path, config, args.dry_run)

    entry = project_list_entry(repo_root, args.repo_root)
    append_project_entry(args.project_list, entry, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
