"""
Azure Key Vault client for secure secret management.

In production (Azure-hosted), uses Managed Identity (DefaultAzureCredential)
for zero-credential access. In local development, falls back to environment
variables transparently.

Usage:
    from src.services.keyvault_client import get_secret, set_secret

    # Read a secret (falls back to env var if Key Vault not configured)
    openai_key = get_secret("azure-openai-key")

    # Store a Graph API refresh token after OAuth sign-in
    set_secret("graph-refresh-token-tenant123", token.refresh_token)
"""
import os
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

# Module-level singleton — lazily initialised
_client = None
_vault_url: Optional[str] = None


def _get_client():
    global _client, _vault_url

    vault_url = os.getenv("AZURE_KEY_VAULT_URL")
    if not vault_url:
        return None

    if _client is None or _vault_url != vault_url:
        try:
            from azure.keyvault.secrets import SecretClient
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
            _client = SecretClient(vault_url=vault_url, credential=credential)
            _vault_url = vault_url
            logger.info("Key Vault client initialised: %s", vault_url)
        except ImportError:
            logger.warning(
                "azure-keyvault-secrets / azure-identity not installed — "
                "falling back to env vars"
            )
            return None
        except Exception as exc:
            logger.error("Key Vault client init failed: %s", exc)
            return None

    return _client


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """
    Retrieve a secret value.

    Resolution order:
      1. Azure Key Vault (if AZURE_KEY_VAULT_URL is set)
      2. `default` parameter

    Secret naming convention: use hyphens, e.g. "azure-openai-key"
    (Azure Key Vault does not allow underscores in secret names).
    """
    client = _get_client()
    if client is None:
        return default

    try:
        secret = client.get_secret(name)
        return secret.value
    except Exception as exc:
        logger.warning("Could not retrieve secret '%s': %s", name, exc)
        return default


def set_secret(name: str, value: str) -> bool:
    """
    Store or update a secret in Key Vault.

    Primary use case: persisting Graph API refresh tokens so the Function App
    can refresh access tokens without user interaction.

    Returns:
        True on success, False if Key Vault is unavailable
    """
    client = _get_client()
    if client is None:
        logger.warning("Key Vault not configured — cannot persist secret '%s'", name)
        return False

    try:
        client.set_secret(name, value)
        logger.info("Secret '%s' stored in Key Vault", name)
        return True
    except Exception as exc:
        logger.error("Failed to store secret '%s': %s", name, exc)
        return False


def delete_secret(name: str) -> bool:
    """Soft-delete a secret from Key Vault."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.begin_delete_secret(name).wait()
        logger.info("Secret '%s' deleted from Key Vault", name)
        return True
    except Exception as exc:
        logger.error("Failed to delete secret '%s': %s", name, exc)
        return False
