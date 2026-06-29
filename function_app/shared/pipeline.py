"""
Shared pipeline bootstrap for all Azure Function triggers.

All four triggers (webhook_handler, email_processor, polling_fallback,
subscription_renewal) use this module to get a fully configured Orchestrator
and GraphClient without duplicating setup code.

Token management:
  - The Graph API refresh token is stored in Azure Key Vault
    under the secret name 'graph-refresh-token-{tenant_id}'
  - Before processing, validate_and_refresh_token() loads and refreshes
    the token, then persists the new value back to Key Vault

Environment variables required (set in Application Settings or local.settings.json):
  AZURE_AD_TENANT_ID, AZURE_AD_CLIENT_ID, AZURE_AD_CLIENT_SECRET
  AZURE_STORAGE_CONNECTION_STRING
  AZURE_KEY_VAULT_URL                (for production token storage)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger("shepherd.pipeline")


def get_connection_string() -> Optional[str]:
    """Return the Azure Storage connection string from environment."""
    return os.getenv("AZURE_STORAGE_CONNECTION_STRING")


def get_graph_client():
    """Instantiate a GraphClient from environment configuration."""
    try:
        from src.services.graph_client import GraphClient
        from src.models.graph_models import GraphConfig

        config = GraphConfig(
            tenant_id=os.getenv("AZURE_AD_TENANT_ID", ""),
            client_id=os.getenv("AZURE_AD_CLIENT_ID", ""),
            client_secret=os.getenv("AZURE_AD_CLIENT_SECRET", ""),
            redirect_uri=os.getenv("GRAPH_REDIRECT_URI", "http://localhost:8501"),
        )
        return GraphClient(config)
    except Exception as exc:
        logger.error("Failed to create GraphClient: %s", exc)
        return None


def load_token(tenant_id: str = "default"):
    """
    Load the Graph API token from Key Vault (production) or local env.
    Returns a GraphToken or None.
    """
    try:
        from src.services.keyvault_client import get_secret
        from src.models.graph_models import GraphToken

        secret_name = f"graph-refresh-token-{tenant_id}"
        raw = get_secret(secret_name)
        if not raw:
            # Try local env as fallback for development
            raw = os.getenv("GRAPH_REFRESH_TOKEN")
        if not raw:
            return None

        data = json.loads(raw)
        from datetime import datetime
        expiration = datetime.fromisoformat(data["expiration"])
        return GraphToken(
            access_token=data.get("access_token", ""),
            refresh_token=data["refresh_token"],
            expiration=expiration,
        )
    except Exception as exc:
        logger.error("load_token failed: %s", exc)
        return None


def save_token(token, tenant_id: str = "default") -> None:
    """Persist the Graph API token to Key Vault after refresh."""
    try:
        from src.services.keyvault_client import set_secret
        raw = json.dumps({
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expiration": token.expiration.isoformat(),
        })
        set_secret(f"graph-refresh-token-{tenant_id}", raw)
    except Exception as exc:
        logger.error("save_token failed: %s", exc)


def validate_and_get_token(tenant_id: str = "default") -> Tuple[Optional[object], Optional[str]]:
    """
    Load + validate + refresh the Graph token.
    Returns (GraphToken, access_token_str) or (None, None) on failure.
    """
    graph_client = get_graph_client()
    token = load_token(tenant_id)

    if not graph_client or not token:
        logger.warning("Graph client or token not available for tenant %s", tenant_id)
        return None, None

    try:
        refreshed_token = asyncio.run(graph_client.validate_token(token))
        if refreshed_token.refresh_token != token.refresh_token:
            save_token(refreshed_token, tenant_id)
        return refreshed_token, refreshed_token.access_token
    except Exception as exc:
        logger.error("Token validation failed: %s", exc)
        return None, None


def get_orchestrator(access_token: Optional[str] = None, graph_client=None):
    """Build a fully configured Orchestrator."""
    try:
        from src.agents.orchestrator import Orchestrator
        conn = get_connection_string()
        return Orchestrator(
            connection_string=conn,
            graph_client=graph_client,
            access_token=access_token,
        )
    except Exception as exc:
        logger.error("Failed to create Orchestrator: %s", exc)
        return None
