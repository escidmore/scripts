#!/usr/bin/env python3
"""
Daily HUD - A morning dashboard for system status, tasks, and alerts.

Usage:
    ./daily_hud.py [options]

Options:
    --config PATH    Path to config file (default: ~/.config/daily-hud/config.yaml)
    --only CHECKS    Comma-separated list of checks to run
    --verbose        Show details for OK checks too
    --json           Output as JSON instead of formatted text
    --no-cache       Disable caching for all checks
    --no-color       Disable colored output
    --help           Show this help message
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from output import (
    CheckResult, Status, supports_color,
    print_header, print_section_results, print_summary, results_to_json
)
from secrets import get_secrets, SecretsError

from checks.todoist import check_todoist
from checks.github import check_github
from checks.kubernetes import check_kubernetes
from checks.truenas import check_truenas
from checks.ssh_hosts import check_ssh_hosts
from checks.windows import check_windows_hosts
from checks.certs import check_certificates
from checks.domains import check_domains
from checks.cves import check_cves
from checks.containers import check_containers


DEFAULT_CONFIG_PATH = "~/.config/daily-hud/config.yaml"

# Map of check names to their functions and required config sections
CHECKS = {
    "todoist": {
        "section": "Todoist",
        "requires_secret": "todoist_token",
    },
    "github": {
        "section": "GitHub",
        "requires_secret": "github_token",
    },
    "kubernetes": {
        "section": "Kubernetes",
    },
    "truenas": {
        "section": "TrueNAS",
        "requires_secret": "truenas_api_key",
    },
    "ssh": {
        "section": "SSH Hosts",
    },
    "windows": {
        "section": "Windows",
    },
    "certs": {
        "section": "Certificates",
    },
    "domains": {
        "section": "Domains",
    },
    "cves": {
        "section": "Security",
    },
    "containers": {
        "section": "Containers",
    },
}


def load_config(config_path: str) -> dict:
    """Load configuration from YAML file."""
    path = Path(config_path).expanduser()

    if not path.exists():
        print(f"Config file not found: {path}", file=sys.stderr)
        print(f"Create it from config.example.yaml", file=sys.stderr)
        sys.exit(1)

    try:
        with open(path, "r") as f:
            config = yaml.safe_load(f) or {}
        return config
    except yaml.YAMLError as e:
        print(f"Error parsing config file: {e}", file=sys.stderr)
        sys.exit(1)


def run_check(check_name: str, config: dict, secrets: dict,
              thresholds: dict, cache_config: dict) -> tuple[str, list[CheckResult]]:
    """
    Run a single check and return results.

    Returns:
        Tuple of (section_name, list of CheckResult)
    """
    check_info = CHECKS.get(check_name, {})
    section = check_info.get("section", check_name.title())

    try:
        if check_name == "todoist":
            token = secrets.get("todoist_token")
            todoist_config = config.get("todoist", {})
            results = check_todoist(token, todoist_config)

        elif check_name == "github":
            token = secrets.get("github_token")
            github_config = config.get("github", {})
            results = check_github(token, github_config)

        elif check_name == "kubernetes":
            k8s_config = config.get("kubernetes", {})
            k8s_config["thresholds"] = thresholds
            results = check_kubernetes(k8s_config)

        elif check_name == "truenas":
            truenas_config = config.get("truenas", {})
            host = truenas_config.get("host", "")
            api_key = secrets.get("truenas_api_key")
            results = check_truenas(host, api_key, {"thresholds": thresholds})

        elif check_name == "ssh":
            hosts = config.get("ssh_hosts", [])
            results = check_ssh_hosts(hosts, thresholds)

        elif check_name == "windows":
            hosts = config.get("windows_hosts", [])
            results = check_windows_hosts(hosts, thresholds)

        elif check_name == "certs":
            targets = config.get("certificates", [])
            results = check_certificates(targets, thresholds)

        elif check_name == "domains":
            domains = config.get("domains", [])
            results = check_domains(domains, thresholds, cache_config)

        elif check_name == "cves":
            cve_config = config.get("cves", {})
            results = check_cves(cve_config, cache_config)

        elif check_name == "containers":
            container_config = config.get("containers", {})
            results = check_containers(container_config, cache_config)

        else:
            results = [CheckResult(
                name=check_name,
                status=Status.ERROR,
                message=f"Unknown check: {check_name}"
            )]

        return section, results

    except Exception as e:
        return section, [CheckResult(
            name=check_name,
            status=Status.ERROR,
            message=f"{section}: Error - {str(e)[:50]}"
        )]


def main():
    parser = argparse.ArgumentParser(
        description="Daily HUD - Morning dashboard for system status",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Path to config file"
    )
    parser.add_argument(
        "--only",
        help="Comma-separated list of checks to run"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show details for OK checks too"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON"
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable caching"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output"
    )

    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)
    thresholds = config.get("thresholds", {})

    # Handle caching
    cache_config = config.get("cache", {})
    if args.no_cache:
        cache_config["enabled"] = False

    # Determine color support
    use_color = supports_color() and not args.no_color

    # Load secrets
    try:
        secrets = get_secrets(config)
    except SecretsError as e:
        if not args.json_output:
            print(f"Warning: Could not load secrets: {e}", file=sys.stderr)
        secrets = {}

    # Determine which checks to run
    if args.only:
        checks_to_run = [c.strip() for c in args.only.split(",")]
        # Validate check names
        invalid = [c for c in checks_to_run if c not in CHECKS]
        if invalid:
            print(f"Unknown checks: {', '.join(invalid)}", file=sys.stderr)
            print(f"Available: {', '.join(CHECKS.keys())}", file=sys.stderr)
            sys.exit(1)
    else:
        # Run all checks that have configuration
        checks_to_run = []
        for check_name in CHECKS:
            # Check if the check has relevant configuration
            if check_name == "todoist" and config.get("secrets", {}).get("todoist_token"):
                checks_to_run.append(check_name)
            elif check_name == "github" and config.get("github", {}).get("username"):
                checks_to_run.append(check_name)
            elif check_name == "kubernetes":
                checks_to_run.append(check_name)  # Always try K8s
            elif check_name == "truenas" and config.get("truenas", {}).get("host"):
                checks_to_run.append(check_name)
            elif check_name == "ssh" and config.get("ssh_hosts"):
                checks_to_run.append(check_name)
            elif check_name == "windows" and config.get("windows_hosts"):
                checks_to_run.append(check_name)
            elif check_name == "certs" and config.get("certificates"):
                checks_to_run.append(check_name)
            elif check_name == "domains" and config.get("domains"):
                checks_to_run.append(check_name)
            elif check_name == "cves" and config.get("cves", {}).get("check_local", True):
                checks_to_run.append(check_name)
            elif check_name == "containers":
                checks_to_run.append(check_name)  # Always try containers

    if not checks_to_run:
        print("No checks configured. Edit your config file.", file=sys.stderr)
        sys.exit(1)

    # Run checks in parallel
    start_time = time.time()
    all_results = {}
    all_flat_results = []

    timeout = thresholds.get("check_timeout_seconds", 30)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(
                run_check, check_name, config, secrets, thresholds, cache_config
            ): check_name
            for check_name in checks_to_run
        }

        for future in as_completed(futures, timeout=timeout * 2):
            check_name = futures[future]
            try:
                section, results = future.result(timeout=timeout)
                all_results[section] = results
                all_flat_results.extend(results)
            except Exception as e:
                section = CHECKS.get(check_name, {}).get("section", check_name.title())
                error_result = CheckResult(
                    name=check_name,
                    status=Status.ERROR,
                    message=f"{section}: Timeout or error"
                )
                all_results[section] = [error_result]
                all_flat_results.append(error_result)

    elapsed = time.time() - start_time

    # Output results
    if args.json_output:
        output = {
            "results": results_to_json(all_results),
            "summary": {
                "ok": sum(1 for r in all_flat_results if r.status == Status.OK),
                "warnings": sum(1 for r in all_flat_results if r.status == Status.WARNING),
                "errors": sum(1 for r in all_flat_results if r.status == Status.ERROR),
                "elapsed_seconds": round(elapsed, 2),
            }
        }
        print(json.dumps(output, indent=2))
    else:
        print_header(use_color)

        # Print sections in a sensible order
        section_order = [
            "Todoist", "GitHub", "Kubernetes", "TrueNAS",
            "SSH Hosts", "Windows", "Certificates", "Domains",
            "Security", "Containers"
        ]

        for section in section_order:
            if section in all_results:
                print_section_results(
                    section,
                    all_results[section],
                    use_color,
                    args.verbose
                )

        # Print any sections not in the predefined order
        for section in all_results:
            if section not in section_order:
                print_section_results(
                    section,
                    all_results[section],
                    use_color,
                    args.verbose
                )

        print_summary(all_flat_results, elapsed, use_color)

    # Exit code based on results
    has_errors = any(r.status == Status.ERROR for r in all_flat_results)
    has_warnings = any(r.status == Status.WARNING for r in all_flat_results)

    if has_errors:
        sys.exit(2)
    elif has_warnings:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
