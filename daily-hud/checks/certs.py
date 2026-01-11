"""
SSL/TLS certificate expiration checks.
"""

import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlparse

from output import CheckResult, Status


def _parse_cert_target(target: str) -> tuple[str, int]:
    """Parse a certificate target into host and port."""
    # Handle URLs
    if target.startswith("http://"):
        # HTTP doesn't have certs, but maybe they meant HTTPS
        target = target.replace("http://", "https://")

    if target.startswith("https://"):
        parsed = urlparse(target)
        host = parsed.hostname or ""
        port = parsed.port or 443
        return host, port

    # Handle host:port format
    if ":" in target:
        parts = target.rsplit(":", 1)
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            port = 443
        return host, port

    # Just a hostname, assume HTTPS
    return target, 443


def _get_cert_expiry(host: str, port: int, timeout: int = 10) -> tuple[bool, str, datetime | None]:
    """
    Get certificate expiry date for a host:port.

    Returns:
        Tuple of (success, error_message, expiry_datetime)
    """
    context = ssl.create_default_context()

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()

                if not cert:
                    return False, "No certificate returned", None

                # Parse expiry date
                expiry_str = cert.get("notAfter", "")
                if not expiry_str:
                    return False, "No expiry date in certificate", None

                # Format: 'Dec 31 23:59:59 2025 GMT'
                expiry = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z")
                expiry = expiry.replace(tzinfo=timezone.utc)

                return True, "", expiry

    except socket.timeout:
        return False, "Connection timed out", None
    except socket.gaierror:
        return False, "DNS resolution failed", None
    except ConnectionRefusedError:
        return False, "Connection refused", None
    except ssl.SSLCertVerificationError as e:
        # Still try to get expiry even if cert is invalid
        return False, f"Certificate error: {str(e)[:50]}", None
    except ssl.SSLError as e:
        return False, f"SSL error: {str(e)[:50]}", None
    except Exception as e:
        return False, f"Error: {str(e)[:50]}", None


def check_certificates(targets: list[str], thresholds: dict) -> list[CheckResult]:
    """
    Check SSL certificate expiration for all targets.

    Args:
        targets: List of URLs or host:port strings
        thresholds: Global thresholds

    Returns:
        List of CheckResult objects
    """
    if not targets:
        return []

    warn_days = thresholds.get("cert_warning_days", 14)
    crit_days = thresholds.get("cert_critical_days", 7)

    now = datetime.now(timezone.utc)

    ok_count = 0
    warnings = []
    errors = []

    for target in targets:
        host, port = _parse_cert_target(target)

        if not host:
            errors.append(f"{target}: Invalid target")
            continue

        display_name = f"{host}:{port}" if port != 443 else host

        success, error_msg, expiry = _get_cert_expiry(host, port)

        if not success:
            errors.append(f"{display_name}: {error_msg}")
            continue

        if expiry is None:
            errors.append(f"{display_name}: Could not determine expiry")
            continue

        days_left = (expiry - now).days

        if days_left < 0:
            errors.append(f"{display_name}: EXPIRED {abs(days_left)} days ago")
        elif days_left <= crit_days:
            errors.append(f"{display_name}: expires in {days_left} days")
        elif days_left <= warn_days:
            warnings.append(f"{display_name}: expires in {days_left} days")
        else:
            ok_count += 1

    # Build results
    results = []
    total = len(targets)

    if errors:
        result = CheckResult(
            name="certs",
            status=Status.ERROR,
            message=f"Certificates: {len(errors)} critical"
        )
        for e in errors:
            result.add_detail(e)
        results.append(result)
    elif warnings:
        result = CheckResult(
            name="certs",
            status=Status.WARNING,
            message=f"Certificates: {ok_count}/{total} OK, {len(warnings)} expiring soon"
        )
        for w in warnings:
            result.add_detail(w)
        results.append(result)
    else:
        results.append(CheckResult(
            name="certs",
            status=Status.OK,
            message=f"Certificates: {ok_count}/{total} valid (>{warn_days} days)"
        ))

    return results
