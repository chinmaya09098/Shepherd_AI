"""
Azure Key Vault secret loading with graceful .env fallback.

Behaviour:
  - When ``KEY_VAULT_URL`` is set, secrets are pulled from Azure Key Vault at
    startup using ``DefaultAzureCredential`` (Managed Identity inside Azure,
    ``az login`` locally) and injected into ``os.environ`` so the rest of
    ``Config`` reads them transparently — no code elsewhere changes.
  - When ``KEY_VAULT_URL`` is NOT set (e.g. local dev), this is a no-op and the
    app keeps using ``.env`` / OS environment values exactly as before.

This never raises: any error (missing packages, auth failure, vault unreachable)
is logged and the app falls back to the existing environment. That keeps local
development and any not-yet-migrated environment working.

Key Vault secret names use hyphens (Key Vault does not allow underscores); this
module maps each vault secret name to the underscore env-var name Config expects.
"""
import os
import logging

logger = logging.getLogger(__name__)

# Map: Key Vault secret name  ->  environment variable name used by Config.
# Only *secrets* belong here — endpoints/other non-sensitive config stay in .env.
SECRET_MAP = {
    "azure-openai-key":                 "AZURE_OPENAI_KEY",
    "azure-content-understanding-key":  "AZURE_CONTENT_UNDERSTANDING_KEY",
    "azure-storage-connection-string":  "AZURE_STORAGE_CONNECTION_STRING",
    "azure-search-key":                 "AZURE_SEARCH_KEY",
    "database-url":                     "DATABASE_URL",
    "postgres-password":                "POSTGRES_PASSWORD",
    "azure-ad-client-secret":           "AZURE_AD_CLIENT_SECRET",
    "hyperion-client-id":               "HYPERION_CLIENT_ID",
    "hyperion-client-secret":           "HYPERION_CLIENT_SECRET",
    "brokerware-client-id":             "BROKERWARE_CLIENT_ID",
    "brokerware-client-secret":         "BROKERWARE_CLIENT_SECRET",
    "graph-webhook-client-state":       "GRAPH_WEBHOOK_CLIENT_STATE",
}


def load_secrets_from_keyvault() -> bool:
    """
    Load mapped secrets from Azure Key Vault into ``os.environ`` when
    ``KEY_VAULT_URL`` is configured. Vault values take precedence over any value
    already present (the vault is the source of truth once configured).

    Returns:
        True  — the vault was used and at least one secret was loaded.
        False — no vault configured, packages missing, or the load failed
                (the app then continues on .env / OS-env values).
    """
    vault_url = os.getenv("KEY_VAULT_URL")
    if not vault_url:
        logger.debug("KEY_VAULT_URL not set — using .env / OS environment for secrets")
        return False

    # Lazy import so the app runs even if the Azure SDK isn't installed locally.
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError:
        logger.warning(
            "KEY_VAULT_URL is set but azure-identity/azure-keyvault-secrets are not "
            "installed — falling back to .env. Run: pip install azure-identity azure-keyvault-secrets"
        )
        return False

    try:
        client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
    except Exception as e:
        logger.error("Could not initialise Key Vault client (%s) — falling back to .env", e)
        return False

    loaded = 0
    for secret_name, env_var in SECRET_MAP.items():
        try:
            value = client.get_secret(secret_name).value
        except Exception:
            # Secret not present in this vault (or no access to it) — skip quietly;
            # a missing secret should not break the whole load.
            continue
        if value is not None:
            os.environ[env_var] = value
            loaded += 1

    if loaded:
        logger.info("Loaded %d secret(s) from Key Vault (%s)", loaded, vault_url)
    else:
        logger.warning("Key Vault reachable but no mapped secrets were loaded from %s", vault_url)
    return loaded > 0