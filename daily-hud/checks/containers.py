"""
Container image vulnerability checks using Trivy.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from output import CheckResult, Status


def _get_cache_path(cache_dir: str) -> Path:
    """Get the cache file path for container scan data."""
    cache_path = Path(cache_dir).expanduser() / "container_vulns.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    return cache_path


def _load_cache(cache_path: Path, max_age_hours: int) -> dict:
    """Load cached vulnerability data if not expired."""
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
    """Save vulnerability data to cache."""
    try:
        with open(cache_path, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _get_running_images() -> list[str]:
    """Get list of container images currently running in Kubernetes."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-A", "-o",
             "jsonpath={range .items[*]}{range .spec.containers[*]}{.image}{'\\n'}{end}{end}"],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            return []

        # Deduplicate images
        images = set()
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                images.add(line.strip())

        return list(images)

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def _scan_image_trivy(image: str, timeout: int = 120) -> dict:
    """
    Scan a container image using Trivy.

    Returns:
        Dict with vulnerability counts by severity
    """
    try:
        result = subprocess.run(
            [
                "trivy", "image",
                "--format", "json",
                "--severity", "CRITICAL,HIGH,MEDIUM",
                "--quiet",
                image
            ],
            capture_output=True,
            text=True,
            timeout=timeout
        )

        if result.returncode != 0:
            return {"error": "Scan failed"}

        data = json.loads(result.stdout)

        # Count vulnerabilities by severity
        counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0}

        for target in data.get("Results", []):
            for vuln in target.get("Vulnerabilities", []):
                severity = vuln.get("Severity", "UNKNOWN")
                if severity in counts:
                    counts[severity] += 1

        return counts

    except subprocess.TimeoutExpired:
        return {"error": "Scan timed out"}
    except FileNotFoundError:
        return {"error": "Trivy not found"}
    except json.JSONDecodeError:
        return {"error": "Invalid scan output"}


def _check_trivy_available() -> bool:
    """Check if Trivy is installed."""
    try:
        result = subprocess.run(
            ["trivy", "--version"],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def check_containers(config: dict, cache_config: dict) -> list[CheckResult]:
    """
    Check container images for vulnerabilities.

    Args:
        config: Container check configuration
        cache_config: Cache configuration

    Returns:
        List of CheckResult objects
    """
    scanner = config.get("scanner", "trivy")

    if scanner != "trivy":
        return [CheckResult(
            name="containers",
            status=Status.OK,
            message="Containers: Only Trivy scanner supported"
        )]

    if not _check_trivy_available():
        return [CheckResult(
            name="containers",
            status=Status.OK,
            message="Containers: Trivy not installed"
        )]

    # Handle caching
    use_cache = cache_config.get("enabled", True)
    cache_dir = cache_config.get("directory", "~/.cache/daily-hud")
    cache_hours = cache_config.get("durations", {}).get("container_vulns", 6)

    cached_data = {}
    cache_path = None
    if use_cache:
        cache_path = _get_cache_path(cache_dir)
        cached_data = _load_cache(cache_path, cache_hours)

    # Get running images
    images = _get_running_images()

    if not images:
        return [CheckResult(
            name="containers",
            status=Status.OK,
            message="Containers: No running images found"
        )]

    # Limit to a reasonable number of images
    images = images[:10]

    total_critical = 0
    total_high = 0
    total_medium = 0
    errors = []
    scanned = 0

    for image in images:
        # Check cache first
        if image in cached_data and "error" not in cached_data[image]:
            counts = cached_data[image]
        else:
            counts = _scan_image_trivy(image)
            if use_cache and cache_path:
                if image not in cached_data:
                    cached_data[image] = {}
                cached_data[image] = counts

        if "error" in counts:
            errors.append(f"{image}: {counts['error']}")
            continue

        scanned += 1
        total_critical += counts.get("CRITICAL", 0)
        total_high += counts.get("HIGH", 0)
        total_medium += counts.get("MEDIUM", 0)

    # Save cache
    if use_cache and cache_path:
        _save_cache(cache_path, cached_data)

    # Build result
    if scanned == 0:
        if errors:
            result = CheckResult(
                name="containers",
                status=Status.WARNING,
                message="Containers: Scan failed"
            )
            for e in errors[:3]:
                result.add_detail(e)
            return [result]
        else:
            return [CheckResult(
                name="containers",
                status=Status.OK,
                message="Containers: No images scanned"
            )]

    # Determine status
    if total_critical > 0:
        status = Status.ERROR
    elif total_high > 0:
        status = Status.WARNING
    else:
        status = Status.OK

    if status == Status.OK:
        return [CheckResult(
            name="containers",
            status=Status.OK,
            message=f"Containers: {scanned} images scanned, no critical/high vulnerabilities"
        )]

    result = CheckResult(
        name="containers",
        status=status,
        message=f"Containers: {scanned} images scanned"
    )

    if total_critical > 0:
        result.add_detail(f"{total_critical} critical vulnerabilities")
    if total_high > 0:
        result.add_detail(f"{total_high} high vulnerabilities")
    if total_medium > 0:
        result.add_detail(f"{total_medium} medium vulnerabilities")

    return [result]
