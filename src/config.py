"""
Configuration module - Loads environment variables for Azure endpoints and keys
"""
import os
from dotenv import load_dotenv
from typing import Optional

from src.secrets import load_secrets_from_keyvault

# Load environment variables from .env file.
# override=True makes the project's .env the source of truth even when a
# variable of the same name already exists in the OS environment (e.g. a stray
# DATABASE_URL pointing at localhost).
load_dotenv(override=True)

# If KEY_VAULT_URL is configured, pull secrets from Azure Key Vault into the
# environment (overriding .env). No-op when KEY_VAULT_URL is unset, so local
# development keeps using .env exactly as before. Must run before the Config
# attributes below are evaluated at import time.
load_secrets_from_keyvault()


class Config:
    """Configuration class for Azure services"""
    
    # Azure Key Vault (optional). When set, secrets are sourced from the vault
    # instead of .env — see src/secrets.py. Example:
    #   KEY_VAULT_URL=https://shepherd-ai-kv.vault.azure.net/
    KEY_VAULT_URL: Optional[str] = os.getenv("KEY_VAULT_URL")
    
    # Azure OpenAI Configuration
    AZURE_OPENAI_ENDPOINT: Optional[str] = os.getenv("AZURE_OPENAI_ENDPOINT")
    AZURE_OPENAI_KEY: Optional[str] = os.getenv("AZURE_OPENAI_KEY")
    AZURE_OPENAI_API_VERSION: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")
    AZURE_OPENAI_DEPLOYMENT: Optional[str] = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    
    # Azure Content Understanding (OCR) Configuration
    AZURE_CONTENT_UNDERSTANDING_ENDPOINT: Optional[str] = os.getenv("AZURE_CONTENT_UNDERSTANDING_ENDPOINT")
    AZURE_CONTENT_UNDERSTANDING_KEY: Optional[str] = os.getenv("AZURE_CONTENT_UNDERSTANDING_KEY")
    AZURE_CONTENT_UNDERSTANDING_API_VERSION: str = os.getenv("AZURE_CONTENT_UNDERSTANDING_API_VERSION", "2023-07-31")
    
    # Azure Blob Storage Configuration
    AZURE_STORAGE_CONNECTION_STRING: Optional[str] = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    AZURE_STORAGE_CONTAINER_NAME: Optional[str] = os.getenv("AZURE_STORAGE_CONTAINER_NAME")
    AZURE_STORAGE_OUTPUT_CONTAINER: Optional[str] = os.getenv("AZURE_STORAGE_OUTPUT_CONTAINER")
    AZURE_STORAGE_LOGS_CONTAINER: str = os.getenv("AZURE_STORAGE_LOGS_CONTAINER", "processed-logs")
    
    # Hyperion TMS API
    HYPERION_CLIENT_ID: Optional[str] = os.getenv("HYPERION_CLIENT_ID")
    HYPERION_CLIENT_SECRET: Optional[str] = os.getenv("HYPERION_CLIENT_SECRET")

    # Brokerware TMS API
    BROKERWARE_BASE_URL: str = os.getenv("BROKERWARE_BASE_URL", "https://shepherd.brokerware.io")
    BROKERWARE_CLIENT_ID: Optional[str] = os.getenv("BROKERWARE_CLIENT_ID")
    BROKERWARE_CLIENT_SECRET: Optional[str] = os.getenv("BROKERWARE_CLIENT_SECRET")
    BROKERWARE_DEFAULT_CUSTOMER_ID: int = int(os.getenv("BROKERWARE_DEFAULT_CUSTOMER_ID", "0"))

    # Azure AI Search
    AZURE_SEARCH_ENDPOINT: Optional[str] = os.getenv("AZURE_SEARCH_ENDPOINT")
    AZURE_SEARCH_KEY: Optional[str] = os.getenv("AZURE_SEARCH_KEY")
    AZURE_SEARCH_INDEX_NAME: str = os.getenv("AZURE_SEARCH_INDEX_NAME", "customer-contacts")

    # Azure OpenAI Embedding
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: Optional[str] = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")

    # PostgreSQL (Azure Database for PostgreSQL)
    # Provide either a full DATABASE_URL, or the individual POSTGRES_* parts.
    # Example DATABASE_URL:
    #   postgresql+psycopg2://user%40server:pwd@server.postgres.database.azure.com:5432/shepherd?sslmode=require
    DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")
    POSTGRES_HOST: Optional[str] = os.getenv("POSTGRES_HOST")
    POSTGRES_PORT: str = os.getenv("POSTGRES_PORT", "5432")
    POSTGRES_DB: Optional[str] = os.getenv("POSTGRES_DB")
    POSTGRES_USER: Optional[str] = os.getenv("POSTGRES_USER")
    POSTGRES_PASSWORD: Optional[str] = os.getenv("POSTGRES_PASSWORD")

    # Microsoft Graph API / Azure AD OAuth
    # Used by GraphClient (converted from GraphTest appsettings.json AzureOutlookAPI section)
    AZURE_AD_TENANT_ID: Optional[str] = os.getenv("AZURE_AD_TENANT_ID")
    AZURE_AD_CLIENT_ID: Optional[str] = os.getenv("AZURE_AD_CLIENT_ID")
    AZURE_AD_CLIENT_SECRET: Optional[str] = os.getenv("AZURE_AD_CLIENT_SECRET")
    GRAPH_REDIRECT_URI: str = os.getenv("GRAPH_REDIRECT_URI", "http://localhost:8501")

    # Webhook / Function App settings
    # GRAPH_MAILBOX_USER_ID: UPN or object ID of the mailbox to subscribe to
    #   e.g. "inbox@company.com"  or the Azure AD object ID GUID
    GRAPH_MAILBOX_USER_ID: Optional[str] = os.getenv("GRAPH_MAILBOX_USER_ID")
    # GRAPH_WEBHOOK_NOTIFICATION_URL: public HTTPS URL of the Function App
    #   HTTP trigger that receives Graph change notifications
    #   e.g. "https://<funcapp>.azurewebsites.net/api/graph_webhook"
    GRAPH_WEBHOOK_NOTIFICATION_URL: Optional[str] = os.getenv("GRAPH_WEBHOOK_NOTIFICATION_URL")
    # GRAPH_WEBHOOK_CLIENT_STATE: secret echoed back in every notification for validation
    GRAPH_WEBHOOK_CLIENT_STATE: str = os.getenv("GRAPH_WEBHOOK_CLIENT_STATE", "shepherd-ai-webhook")

    # Azure Storage Queue used by Function App for async email processing
    AZURE_STORAGE_QUEUE_NAME: str = os.getenv("AZURE_STORAGE_QUEUE_NAME", "email-notifications")

    # Follow-up reminder settings
    # FOLLOWUP_MAX_BY_CUSTOMER: JSON mapping of customer_id (str) → max follow-ups (int)
    #   Supported values: 3, 5, or 7  (e.g. '{"101": 5, "202": 7}')
    #   Any customer not listed falls back to FOLLOWUP_DEFAULT_MAX.
    FOLLOWUP_MAX_BY_CUSTOMER: str = os.getenv("FOLLOWUP_MAX_BY_CUSTOMER", "{}")
    FOLLOWUP_DEFAULT_MAX: int = int(os.getenv("FOLLOWUP_DEFAULT_MAX", "3"))
    # How often the timer trigger re-sends reminder follow-ups.
    # Set to 5 for testing (every 5 minutes); use 1440 for production (24 hours).
    FOLLOWUP_REMINDER_INTERVAL_MINUTES: int = int(os.getenv("FOLLOWUP_REMINDER_INTERVAL_MINUTES", "5"))

    @classmethod
    def get_max_followups(cls, customer_id: Optional[int]) -> int:
        """Return the configured max follow-up count for a given customer_id.

        Looks up FOLLOWUP_MAX_BY_CUSTOMER (a JSON dict keyed by customer_id as
        a string). Falls back to FOLLOWUP_DEFAULT_MAX (default 3) when the
        customer is not listed.  Supported per-customer values: 3, 5, or 7.
        """
        import json as _json
        if customer_id is None:
            return cls.FOLLOWUP_DEFAULT_MAX
        try:
            mapping = _json.loads(cls.FOLLOWUP_MAX_BY_CUSTOMER or "{}")
            val = mapping.get(str(customer_id))
            if val is not None:
                return int(val)
        except Exception:
            pass
        return cls.FOLLOWUP_DEFAULT_MAX

    # Azure AI Search — separate index for RAG context retrieval
    AZURE_SEARCH_CONTEXT_INDEX_NAME: str = os.getenv(
        "AZURE_SEARCH_CONTEXT_INDEX_NAME", "shipment-context"
    )

    # Other Configuration
    EMAIL_EXAMPLES_DIR: str = os.getenv("EMAIL_EXAMPLES_DIR", "Load_Tender_Email_Examples")
    OUTPUT_DIR: str = os.getenv("OUTPUT_DIR", "output")
    
    @classmethod
    def validate(cls) -> bool:
        """Validate that required configuration is present"""
        required_vars = [
            cls.AZURE_OPENAI_ENDPOINT,
            cls.AZURE_OPENAI_KEY,
            cls.AZURE_OPENAI_DEPLOYMENT,
            cls.AZURE_CONTENT_UNDERSTANDING_ENDPOINT,
            cls.AZURE_CONTENT_UNDERSTANDING_KEY,
        ]
        return all(var is not None for var in required_vars)
