"""
TrueNAS integration via REST API.
"""

import requests
from typing import Optional
import urllib3

from output import CheckResult, Status

# Disable SSL warnings for self-signed certs (common in homelab)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def check_truenas(host: str, api_key: Optional[str], config: dict) -> list[CheckResult]:
    """
    Check TrueNAS status via REST API.

    Args:
        host: TrueNAS hostname or IP
        api_key: TrueNAS API key
        config: Full config with thresholds

    Returns:
        List of CheckResult objects
    """
    if not host:
        return [CheckResult(
            name="truenas",
            status=Status.ERROR,
            message="TrueNAS: No host configured"
        )]

    if not api_key:
        return [CheckResult(
            name="truenas",
            status=Status.ERROR,
            message="TrueNAS: No API key configured"
        )]

    base_url = f"https://{host}/api/v2.0"
    headers = {"Authorization": f"Bearer {api_key}"}
    thresholds = config.get("thresholds", {})
    disk_warn = thresholds.get("disk_warning_percent", 80)
    disk_crit = thresholds.get("disk_critical_percent", 90)

    results = []

    # Check system info
    try:
        response = requests.get(
            f"{base_url}/system/info",
            headers=headers,
            verify=False,
            timeout=15
        )
        response.raise_for_status()
        system_info = response.json()

        # System is reachable
        version = system_info.get("version", "unknown")
        hostname = system_info.get("hostname", host)

    except requests.exceptions.Timeout:
        return [CheckResult(
            name="truenas",
            status=Status.ERROR,
            message="TrueNAS: Connection timed out"
        )]
    except requests.exceptions.ConnectionError:
        return [CheckResult(
            name="truenas",
            status=Status.ERROR,
            message=f"TrueNAS: Cannot connect to {host}"
        )]
    except requests.exceptions.RequestException as e:
        return [CheckResult(
            name="truenas",
            status=Status.ERROR,
            message=f"TrueNAS: API error - {str(e)}"
        )]

    # Check pools
    try:
        response = requests.get(
            f"{base_url}/pool",
            headers=headers,
            verify=False,
            timeout=15
        )
        response.raise_for_status()
        pools = response.json()

        pool_warnings = []
        pool_errors = []

        for pool in pools:
            name = pool.get("name", "unknown")
            status = pool.get("status", "UNKNOWN")

            # Get usage from topology
            topology = pool.get("topology", {})
            # Calculate used/free from the pool properties
            used = pool.get("used", {}).get("parsed", 0)
            free = pool.get("free", {}).get("parsed", 0)
            total = used + free

            if total > 0:
                used_pct = (used / total) * 100
                free_tb = free / (1024 ** 4)  # Convert to TB
                free_gb = free / (1024 ** 3)  # Convert to GB

                if free_tb >= 1:
                    free_str = f"{free_tb:.1f}TB free"
                else:
                    free_str = f"{free_gb:.0f}GB free"

                if used_pct >= disk_crit:
                    pool_errors.append(f"{name}: {used_pct:.0f}% used ({free_str})")
                elif used_pct >= disk_warn:
                    pool_warnings.append(f"{name}: {used_pct:.0f}% used ({free_str})")

            # Check pool health status
            if status != "ONLINE":
                pool_errors.append(f"{name}: {status}")

        if pool_errors:
            result = CheckResult(
                name="truenas-storage",
                status=Status.ERROR,
                message="Storage: Critical"
            )
            for err in pool_errors:
                result.add_detail(err)
            results.append(result)
        elif pool_warnings:
            result = CheckResult(
                name="truenas-storage",
                status=Status.WARNING,
                message=f"Storage: {len(pool_warnings)} pool(s) high usage"
            )
            for warn in pool_warnings:
                result.add_detail(warn)
            results.append(result)
        else:
            results.append(CheckResult(
                name="truenas-storage",
                status=Status.OK,
                message=f"Storage: {len(pools)} pool(s) OK"
            ))

    except requests.exceptions.RequestException:
        results.append(CheckResult(
            name="truenas-storage",
            status=Status.ERROR,
            message="Storage: Failed to fetch pool data"
        ))

    # Check for updates
    try:
        response = requests.get(
            f"{base_url}/update/check_available",
            headers=headers,
            verify=False,
            timeout=30  # This can be slow
        )
        response.raise_for_status()
        update_info = response.json()

        if update_info.get("status") == "AVAILABLE":
            version = update_info.get("version", "unknown")
            results.append(CheckResult(
                name="truenas-updates",
                status=Status.WARNING,
                message=f"Updates: {version} available"
            ))
        else:
            results.append(CheckResult(
                name="truenas-updates",
                status=Status.OK,
                message="Updates: Up to date"
            ))

    except requests.exceptions.RequestException:
        results.append(CheckResult(
            name="truenas-updates",
            status=Status.OK,
            message="Updates: Check skipped"
        ))

    # Add system status as first result
    results.insert(0, CheckResult(
        name="truenas-system",
        status=Status.OK,
        message=f"System: OK ({hostname})"
    ))

    return results
