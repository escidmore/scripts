"""
CVE advisory checks using OSV.dev API.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests

from output import CheckResult, Status


OSV_API_URL = "https://api.osv.dev/v1/query"


def _get_cache_path(cache_dir: str) -> Path:
    """Get the cache file path for CVE data."""
    cache_path = Path(cache_dir).expanduser() / "cves.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    return cache_path


def _load_cache(cache_path: Path, max_age_hours: int) -> dict:
    """Load cached CVE data if not expired."""
    if not cache_path.exists():
        return {}

    try:
        mtime = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600

        if age_hours > max_age_hours:
            return {}

        with open(cache_path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache_path: Path, data: dict):
    """Save CVE data to cache."""
    try:
        with open(cache_path, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _get_local_packages() -> list[dict]:
    """Get installed packages on the local system."""
    packages = []

    # Try dpkg (Debian/Ubuntu)
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f", "${Package} ${Version}\n"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    packages.append({
                        "name": parts[0],
                        "version": parts[1],
                        "ecosystem": "Debian"
                    })
            return packages
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Try rpm (RHEL/Fedora)
    try:
        result = subprocess.run(
            ["rpm", "-qa", "--qf", "%{NAME} %{VERSION}-%{RELEASE}\n"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    packages.append({
                        "name": parts[0],
                        "version": parts[1],
                        "ecosystem": "Red Hat"
                    })
            return packages
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return packages


def _query_osv(package: dict) -> list[dict]:
    """Query OSV.dev for vulnerabilities affecting a package."""
    try:
        payload = {
            "package": {
                "name": package["name"],
                "ecosystem": package["ecosystem"]
            },
            "version": package["version"]
        }

        response = requests.post(
            OSV_API_URL,
            json=payload,
            timeout=10
        )

        if response.status_code != 200:
            return []

        data = response.json()
        vulns = data.get("vulns", [])

        results = []
        for vuln in vulns:
            # Get severity
            severity = "unknown"
            for s in vuln.get("severity", []):
                if s.get("type") == "CVSS_V3":
                    score = s.get("score", "")
                    if score:
                        try:
                            # Extract base score from CVSS vector
                            # or use the numeric score if provided
                            severity = _classify_cvss(float(score) if score.replace(".", "").isdigit() else 5.0)
                        except ValueError:
                            severity = "medium"

            # Check for fix
            has_fix = False
            for affected in vuln.get("affected", []):
                for r in affected.get("ranges", []):
                    if r.get("events"):
                        for event in r["events"]:
                            if "fixed" in event:
                                has_fix = True
                                break

            results.append({
                "id": vuln.get("id", "Unknown"),
                "summary": vuln.get("summary", "")[:80],
                "severity": severity,
                "has_fix": has_fix,
                "package": package["name"]
            })

        return results

    except requests.exceptions.RequestException:
        return []


def _classify_cvss(score: float) -> str:
    """Classify CVSS score into severity level."""
    if score >= 9.0:
        return "critical"
    elif score >= 7.0:
        return "high"
    elif score >= 4.0:
        return "medium"
    else:
        return "low"


def check_cves(config: dict, cache_config: dict) -> list[CheckResult]:
    """
    Check for CVE advisories affecting installed packages.

    Args:
        config: CVE check configuration
        cache_config: Cache configuration

    Returns:
        List of CheckResult objects
    """
    check_local = config.get("check_local", True)

    if not check_local:
        return []

    # Handle caching
    use_cache = cache_config.get("enabled", True)
    cache_dir = cache_config.get("directory", "~/.cache/daily-hud")
    cache_hours = cache_config.get("durations", {}).get("cves", 12)

    cached_data = {}
    cache_path = None
    if use_cache:
        cache_path = _get_cache_path(cache_dir)
        cached_data = _load_cache(cache_path, cache_hours)

    # If we have valid cache, use it
    if cached_data.get("vulns"):
        vulns = cached_data["vulns"]
    else:
        # Get installed packages
        packages = _get_local_packages()

        if not packages:
            return [CheckResult(
                name="cves",
                status=Status.OK,
                message="CVEs: No package manager detected"
            )]

        # Query OSV for a sample of important packages
        # (Checking all packages would be too slow)
        important_packages = [
            "openssl", "openssh", "curl", "wget", "bash", "sudo",
            "git", "python3", "python", "nodejs", "nginx", "apache2",
            "httpd", "postgresql", "mysql", "mariadb", "redis",
            "docker", "containerd", "linux-image"
        ]

        vulns = []
        packages_to_check = [
            p for p in packages
            if any(imp in p["name"].lower() for imp in important_packages)
        ][:20]  # Limit to 20 packages

        for package in packages_to_check:
            package_vulns = _query_osv(package)
            vulns.extend(package_vulns)

        # Cache results
        if use_cache and cache_path:
            _save_cache(cache_path, {"vulns": vulns})

    if not vulns:
        return [CheckResult(
            name="cves",
            status=Status.OK,
            message="CVEs: No known vulnerabilities"
        )]

    # Group by severity
    critical = [v for v in vulns if v["severity"] == "critical"]
    high = [v for v in vulns if v["severity"] == "high"]
    medium = [v for v in vulns if v["severity"] == "medium"]
    low = [v for v in vulns if v["severity"] == "low"]

    # Determine overall status
    if critical:
        status = Status.ERROR
    elif high:
        status = Status.WARNING
    else:
        status = Status.OK

    total = len(vulns)
    result = CheckResult(
        name="cves",
        status=status,
        message=f"CVEs: {total} advisory(ies) affecting installed packages"
    )

    # Add details for critical and high
    for vuln in (critical + high)[:10]:
        fix_str = " - update available" if vuln["has_fix"] else ""
        result.add_detail(f"{vuln['id']}: {vuln['package']} ({vuln['severity']}){fix_str}")

    if len(critical + high) > 10:
        result.add_detail(f"... and {len(critical + high) - 10} more high/critical")

    if medium or low:
        result.add_detail(f"Plus {len(medium)} medium, {len(low)} low severity")

    return [result]
