"""
SSH-based host checks for disk usage and package updates.
"""

import subprocess
from typing import Optional

from output import CheckResult, Status


def _run_ssh(host: str, user: str, command: str,
             timeout: int = 30) -> tuple[bool, str]:
    """Run a command via SSH and return success status and output."""
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
        command
    ]

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode != 0:
            error = result.stderr.strip() or "Command failed"
            return False, error
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "Connection timed out"
    except FileNotFoundError:
        return False, "ssh not found in PATH"


def _check_disk(host: str, user: str, thresholds: dict) -> Optional[CheckResult]:
    """Check disk usage on remote host."""
    warn_pct = thresholds.get("disk_warning_percent", 80)
    crit_pct = thresholds.get("disk_critical_percent", 90)

    success, output = _run_ssh(
        host, user,
        "df -h --output=target,pcent,avail | grep -E '^/' | grep -v '/dev|/run|/sys|/proc'"
    )

    if not success:
        return None  # Will be reported as connection error

    warnings = []
    errors = []

    for line in output.split("\n"):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 3:
            continue

        mount = parts[0]
        pct_str = parts[1].rstrip("%")
        avail = parts[2]

        try:
            pct = int(pct_str)
        except ValueError:
            continue

        if pct >= crit_pct:
            errors.append(f"{mount}: {pct}% used ({avail} free)")
        elif pct >= warn_pct:
            warnings.append(f"{mount}: {pct}% used ({avail} free)")

    if errors:
        result = CheckResult(
            name="disk",
            status=Status.ERROR,
            message="Disk critical"
        )
        for e in errors:
            result.add_detail(e)
        return result
    elif warnings:
        result = CheckResult(
            name="disk",
            status=Status.WARNING,
            message=f"Disk: {len(warnings)} mount(s) high"
        )
        for w in warnings:
            result.add_detail(w)
        return result
    else:
        return CheckResult(
            name="disk",
            status=Status.OK,
            message="Disk: OK"
        )


def _check_updates(host: str, user: str) -> Optional[CheckResult]:
    """Check for pending package updates on remote host."""
    # Try apt first (Debian/Ubuntu)
    success, output = _run_ssh(
        host, user,
        "apt list --upgradable 2>/dev/null | grep -v '^Listing' | wc -l"
    )

    if success:
        try:
            count = int(output.strip())
            if count > 0:
                return CheckResult(
                    name="updates",
                    status=Status.WARNING,
                    message=f"Updates: {count} pending (apt)"
                )
            else:
                return CheckResult(
                    name="updates",
                    status=Status.OK,
                    message="Updates: Up to date"
                )
        except ValueError:
            pass

    # Try dnf (RHEL/Fedora)
    success, output = _run_ssh(
        host, user,
        "dnf check-update -q 2>/dev/null | wc -l"
    )

    if success:
        try:
            count = int(output.strip())
            if count > 0:
                return CheckResult(
                    name="updates",
                    status=Status.WARNING,
                    message=f"Updates: {count} pending (dnf)"
                )
            else:
                return CheckResult(
                    name="updates",
                    status=Status.OK,
                    message="Updates: Up to date"
                )
        except ValueError:
            pass

    # Try yum (older RHEL/CentOS)
    success, output = _run_ssh(
        host, user,
        "yum check-update -q 2>/dev/null | wc -l"
    )

    if success:
        try:
            count = int(output.strip())
            if count > 0:
                return CheckResult(
                    name="updates",
                    status=Status.WARNING,
                    message=f"Updates: {count} pending (yum)"
                )
            else:
                return CheckResult(
                    name="updates",
                    status=Status.OK,
                    message="Updates: Up to date"
                )
        except ValueError:
            pass

    # No package manager found or working
    return CheckResult(
        name="updates",
        status=Status.OK,
        message="Updates: Check skipped (no package manager)"
    )


def check_ssh_host(host_config: dict, thresholds: dict) -> list[CheckResult]:
    """
    Run all configured checks on a single SSH host.

    Args:
        host_config: Host configuration with name, host, user, checks
        thresholds: Global thresholds

    Returns:
        List of CheckResult objects
    """
    name = host_config.get("name", "unknown")
    host = host_config.get("host", "")
    user = host_config.get("user", "root")
    checks = host_config.get("checks", ["disk"])

    if not host:
        return [CheckResult(
            name=name,
            status=Status.ERROR,
            message=f"{name}: No host configured"
        )]

    # First, verify connectivity
    success, _ = _run_ssh(host, user, "echo ok", timeout=15)
    if not success:
        return [CheckResult(
            name=name,
            status=Status.ERROR,
            message=f"{name}: Connection failed"
        )]

    results = []
    all_ok = True

    for check in checks:
        if check == "disk":
            result = _check_disk(host, user, thresholds)
            if result:
                if result.status != Status.OK:
                    all_ok = False
                    # Prefix with host name
                    result.message = f"{name}: {result.message}"
                    results.append(result)

        elif check == "updates":
            result = _check_updates(host, user)
            if result:
                if result.status != Status.OK:
                    all_ok = False
                    result.message = f"{name}: {result.message}"
                    results.append(result)

    # If all checks passed, just show one OK line
    if all_ok:
        return [CheckResult(
            name=name,
            status=Status.OK,
            message=f"{name}: OK"
        )]

    return results


def check_ssh_hosts(hosts: list[dict], thresholds: dict) -> list[CheckResult]:
    """
    Check all configured SSH hosts.

    Args:
        hosts: List of host configurations
        thresholds: Global thresholds

    Returns:
        List of CheckResult objects
    """
    if not hosts:
        return []

    all_results = []
    for host_config in hosts:
        results = check_ssh_host(host_config, thresholds)
        all_results.extend(results)

    return all_results
