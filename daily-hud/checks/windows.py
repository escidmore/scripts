"""
Windows host checks via SSH or WinRM.
"""

import subprocess
from typing import Optional

from output import CheckResult, Status


def _run_ssh(host: str, user: str, command: str,
             timeout: int = 30) -> tuple[bool, str]:
    """Run a PowerShell command via SSH."""
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{user}@{host}",
        f"powershell -Command \"{command}\""
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


def _check_disk_ssh(host: str, user: str, thresholds: dict) -> Optional[CheckResult]:
    """Check disk usage on Windows via SSH."""
    warn_pct = thresholds.get("disk_warning_percent", 80)
    crit_pct = thresholds.get("disk_critical_percent", 90)

    # PowerShell command to get drive info
    ps_cmd = (
        "Get-PSDrive -PSProvider FileSystem | "
        "Where-Object { $_.Used -gt 0 } | "
        "ForEach-Object { "
        "$pct = [math]::Round(($_.Used / ($_.Used + $_.Free)) * 100); "
        "$freeGB = [math]::Round($_.Free / 1GB, 1); "
        "Write-Output \\\"$($_.Name): $pct% ($($freeGB)GB free)\\\" "
        "}"
    )

    success, output = _run_ssh(host, user, ps_cmd)
    if not success:
        return None

    warnings = []
    errors = []

    for line in output.split("\n"):
        if not line.strip() or ":" not in line:
            continue

        # Parse "C: 75% (120GB free)"
        try:
            drive = line.split(":")[0].strip()
            rest = line.split(":")[1].strip()
            pct_str = rest.split("%")[0].strip()
            pct = int(pct_str)
            free_info = rest.split("(")[1].rstrip(")") if "(" in rest else ""

            if pct >= crit_pct:
                errors.append(f"{drive}: {pct}% ({free_info})")
            elif pct >= warn_pct:
                warnings.append(f"{drive}: {pct}% ({free_info})")
        except (ValueError, IndexError):
            continue

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
            message=f"Disk: {len(warnings)} drive(s) high"
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


def _check_updates_ssh(host: str, user: str) -> Optional[CheckResult]:
    """Check for Windows updates via SSH."""
    # This requires PSWindowsUpdate module or we check Windows Update service
    # Simplified: just check if updates are pending via registry/service

    # Try to count pending updates (requires admin and PSWindowsUpdate)
    ps_cmd = (
        "try { "
        "$updates = (Get-WindowsUpdate -ErrorAction Stop).Count; "
        "Write-Output $updates "
        "} catch { "
        "Write-Output 'unavailable' "
        "}"
    )

    success, output = _run_ssh(host, user, ps_cmd, timeout=60)

    if not success or output.strip() == "unavailable":
        # Fallback: check Windows Update service status
        ps_cmd2 = "(Get-Service wuauserv).Status"
        success2, output2 = _run_ssh(host, user, ps_cmd2)

        if success2:
            return CheckResult(
                name="updates",
                status=Status.OK,
                message="Updates: Check unavailable (no PSWindowsUpdate)"
            )
        return None

    try:
        count = int(output.strip())
        if count > 0:
            return CheckResult(
                name="updates",
                status=Status.WARNING,
                message=f"Updates: {count} pending"
            )
        else:
            return CheckResult(
                name="updates",
                status=Status.OK,
                message="Updates: Up to date"
            )
    except ValueError:
        return CheckResult(
            name="updates",
            status=Status.OK,
            message="Updates: Check skipped"
        )


def check_windows_host(host_config: dict, thresholds: dict) -> list[CheckResult]:
    """
    Run all configured checks on a single Windows host.

    Args:
        host_config: Host configuration
        thresholds: Global thresholds

    Returns:
        List of CheckResult objects
    """
    name = host_config.get("name", "unknown")
    host = host_config.get("host", "")
    user = host_config.get("user", "")
    method = host_config.get("method", "ssh")
    checks = host_config.get("checks", ["disk"])

    if not host:
        return [CheckResult(
            name=name,
            status=Status.ERROR,
            message=f"{name}: No host configured"
        )]

    if method != "ssh":
        # WinRM not implemented yet
        return [CheckResult(
            name=name,
            status=Status.ERROR,
            message=f"{name}: WinRM not yet supported (use SSH)"
        )]

    # Verify connectivity
    success, _ = _run_ssh(host, user, "Write-Output 'ok'", timeout=15)
    if not success:
        return [CheckResult(
            name=name,
            status=Status.ERROR,
            message=f"{name}: Connection failed"
        )]

    results = []
    all_ok = True
    update_info = ""

    for check in checks:
        if check == "disk":
            result = _check_disk_ssh(host, user, thresholds)
            if result:
                if result.status != Status.OK:
                    all_ok = False
                    result.message = f"{name}: {result.message}"
                    results.append(result)

        elif check == "updates":
            result = _check_updates_ssh(host, user)
            if result:
                if result.status == Status.WARNING:
                    # For updates, we just note it but don't expand
                    update_info = f" ({result.message.split(': ')[1]})"
                elif result.status != Status.OK:
                    all_ok = False
                    result.message = f"{name}: {result.message}"
                    results.append(result)

    # If all checks passed, show OK with update info
    if all_ok:
        return [CheckResult(
            name=name,
            status=Status.OK,
            message=f"{name}: OK{update_info}"
        )]

    return results


def check_windows_hosts(hosts: list[dict], thresholds: dict) -> list[CheckResult]:
    """
    Check all configured Windows hosts.

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
        results = check_windows_host(host_config, thresholds)
        all_results.extend(results)

    return all_results
