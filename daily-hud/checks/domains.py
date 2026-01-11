"""
Domain expiration checks via WHOIS.
"""

import os
import json
from datetime import datetime, timezone
from pathlib import Path

from output import CheckResult, Status

# Try to import whois, but make it optional
try:
    import whois
    WHOIS_AVAILABLE = True
except ImportError:
    WHOIS_AVAILABLE = False


def _get_cache_path(cache_dir: str) -> Path:
    """Get the cache file path for domain data."""
    cache_path = Path(cache_dir).expanduser() / "domains.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    return cache_path


def _load_cache(cache_path: Path, max_age_hours: int) -> dict:
    """Load cached domain data if not expired."""
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
    """Save domain data to cache."""
    try:
        with open(cache_path, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _get_domain_expiry(domain: str) -> tuple[bool, str, datetime | None]:
    """
    Get domain expiry date via WHOIS.

    Returns:
        Tuple of (success, error_message, expiry_datetime)
    """
    if not WHOIS_AVAILABLE:
        return False, "python-whois not installed", None

    try:
        w = whois.whois(domain)

        if not w:
            return False, "No WHOIS data", None

        expiry = w.expiration_date

        # Some domains return a list of dates
        if isinstance(expiry, list):
            expiry = expiry[0]

        if not expiry:
            return False, "No expiration date in WHOIS", None

        # Ensure timezone-aware
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        return True, "", expiry

    except Exception as e:
        error_msg = str(e)
        if "No match for" in error_msg:
            return False, "Domain not found", None
        if "connect" in error_msg.lower():
            return False, "WHOIS server unavailable", None
        return False, f"WHOIS error: {error_msg[:40]}", None


def check_domains(domains: list[str], thresholds: dict, cache_config: dict) -> list[CheckResult]:
    """
    Check domain expiration for all configured domains.

    Args:
        domains: List of domain names
        thresholds: Global thresholds
        cache_config: Cache configuration

    Returns:
        List of CheckResult objects
    """
    if not domains:
        return []

    if not WHOIS_AVAILABLE:
        return [CheckResult(
            name="domains",
            status=Status.WARNING,
            message="Domains: python-whois not installed"
        )]

    warn_days = thresholds.get("domain_warning_days", 30)
    now = datetime.now(timezone.utc)

    # Handle caching
    use_cache = cache_config.get("enabled", True)
    cache_dir = cache_config.get("directory", "~/.cache/daily-hud")
    cache_hours = cache_config.get("durations", {}).get("domains", 24)

    cached_data = {}
    if use_cache:
        cache_path = _get_cache_path(cache_dir)
        cached_data = _load_cache(cache_path, cache_hours)

    ok_count = 0
    warnings = []
    errors = []
    new_cache = {}

    for domain in domains:
        # Check cache first
        if domain in cached_data:
            cache_entry = cached_data[domain]
            if cache_entry.get("error"):
                errors.append(f"{domain}: {cache_entry['error']}")
                new_cache[domain] = cache_entry
                continue

            expiry_str = cache_entry.get("expiry")
            if expiry_str:
                try:
                    expiry = datetime.fromisoformat(expiry_str)
                    days_left = (expiry - now).days

                    if days_left < 0:
                        errors.append(f"{domain}: EXPIRED")
                    elif days_left <= warn_days:
                        warnings.append(f"{domain}: expires in {days_left} days")
                    else:
                        ok_count += 1

                    new_cache[domain] = cache_entry
                    continue
                except ValueError:
                    pass

        # Fetch fresh data
        success, error_msg, expiry = _get_domain_expiry(domain)

        if not success:
            errors.append(f"{domain}: {error_msg}")
            new_cache[domain] = {"error": error_msg}
            continue

        if expiry is None:
            errors.append(f"{domain}: Could not determine expiry")
            new_cache[domain] = {"error": "No expiry date"}
            continue

        days_left = (expiry - now).days
        new_cache[domain] = {"expiry": expiry.isoformat()}

        if days_left < 0:
            errors.append(f"{domain}: EXPIRED")
        elif days_left <= warn_days:
            warnings.append(f"{domain}: expires in {days_left} days")
        else:
            ok_count += 1

    # Save cache
    if use_cache:
        _save_cache(cache_path, new_cache)

    # Build results
    results = []
    total = len(domains)

    if errors:
        result = CheckResult(
            name="domains",
            status=Status.ERROR,
            message=f"Domains: {len(errors)} issue(s)"
        )
        for e in errors:
            result.add_detail(e)
        results.append(result)
    elif warnings:
        result = CheckResult(
            name="domains",
            status=Status.WARNING,
            message=f"Domains: {len(warnings)} expiring within {warn_days} days"
        )
        for w in warnings:
            result.add_detail(w)
        results.append(result)
    else:
        results.append(CheckResult(
            name="domains",
            status=Status.OK,
            message=f"Domains: All {total} valid (>{warn_days} days)"
        ))

    return results
