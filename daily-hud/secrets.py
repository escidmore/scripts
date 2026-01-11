"""
1Password CLI integration for fetching secrets.
"""

import subprocess
import shutil
from functools import lru_cache


class SecretsError(Exception):
    """Raised when secret retrieval fails."""
    pass


def _check_op_available() -> bool:
    """Check if 1Password CLI is installed and available."""
    return shutil.which("op") is not None


def _check_op_signed_in() -> bool:
    """Check if user is signed into 1Password CLI."""
    try:
        result = subprocess.run(
            ["op", "account", "list"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


@lru_cache(maxsize=64)
def get_secret(op_reference: str) -> str:
    """
    Fetch a secret from 1Password using an op:// reference.

    Args:
        op_reference: 1Password reference URI (e.g., "op://Vault/Item/field")

    Returns:
        The secret value as a string.

    Raises:
        SecretsError: If the secret cannot be retrieved.
    """
    if not op_reference.startswith("op://"):
        raise SecretsError(f"Invalid 1Password reference: {op_reference}")

    if not _check_op_available():
        raise SecretsError("1Password CLI (op) is not installed or not in PATH")

    try:
        result = subprocess.run(
            ["op", "read", op_reference],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown error"
            if "not signed in" in error_msg.lower():
                raise SecretsError("Not signed in to 1Password. Run 'op signin' first.")
            raise SecretsError(f"Failed to read secret: {error_msg}")

        return result.stdout.strip()

    except subprocess.TimeoutExpired:
        raise SecretsError("Timeout waiting for 1Password CLI")


def get_secrets(config: dict) -> dict:
    """
    Fetch all secrets defined in config['secrets'].

    Args:
        config: The full configuration dictionary.

    Returns:
        Dictionary mapping secret names to their values.
    """
    secrets_config = config.get("secrets", {})
    secrets = {}

    for name, reference in secrets_config.items():
        try:
            secrets[name] = get_secret(reference)
        except SecretsError as e:
            # Store the error instead of failing completely
            secrets[name] = None

    return secrets


def clear_cache():
    """Clear the secrets cache."""
    get_secret.cache_clear()
