"""
Configuration module - Loads environment variables for Azure endpoints and keys
"""
import os
from dotenv import load_dotenv
from typing import Optional

# Load environment variables from .env file
load_dotenv()


class Config:
    """Configuration class for Azure services"""
    
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
    HYPERION_CLIENT_ID: str = os.getenv("HYPERION_CLIENT_ID", "eeb5772a6bef4827b3e4d2c6fea946b2")
    HYPERION_CLIENT_SECRET: str = os.getenv("HYPERION_CLIENT_SECRET", "eMcLvyWAEMVqGjTU0F0e0Az9OKW3Ymr8ov0cdNSX8xs")

    # Azure AI Search
    AZURE_SEARCH_ENDPOINT: Optional[str] = os.getenv("AZURE_SEARCH_ENDPOINT")
    AZURE_SEARCH_KEY: Optional[str] = os.getenv("AZURE_SEARCH_KEY")
    AZURE_SEARCH_INDEX_NAME: str = os.getenv("AZURE_SEARCH_INDEX_NAME", "customer-contacts")

    # Azure OpenAI Embedding
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: Optional[str] = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")

    # Microsoft Graph API / Azure AD OAuth
    # Matches GraphTest appsettings.json AzureOutlookAPI section exactly
    AZURE_AD_TENANT_ID: Optional[str] = os.getenv("AZURE_AD_TENANT_ID")
    AZURE_AD_CLIENT_ID: Optional[str] = os.getenv("AZURE_AD_CLIENT_ID")
    AZURE_AD_CLIENT_SECRET: Optional[str] = os.getenv("AZURE_AD_CLIENT_SECRET")
    GRAPH_REDIRECT_URI: str = os.getenv("GRAPH_REDIRECT_URI", "http://localhost:8501")

    # App-Only (client credentials) mailbox target.
    # When set, Shepherd AI reads this mailbox automatically without any user sign-in.
    # Requires Mail.Read Application permission + admin consent in Azure AD.
    # Example: dispatch@3plsystems.com
    GRAPH_MAILBOX_UPN: Optional[str] = os.getenv("GRAPH_MAILBOX_UPN")

    # Pre-stored Graph API tokens — equivalent to GraphTest appsettings.json Tokens section.
    # If set, the app skips OAuth sign-in and uses these tokens directly (same as GraphTest
    # GetGraphModel() fallback path when accessToken/refreshToken are not passed as params).
    # Leave blank to require interactive sign-in.
    GRAPH_ACCESS_TOKEN: Optional[str] = os.getenv("GRAPH_ACCESS_TOKEN")
    GRAPH_REFRESH_TOKEN: Optional[str] = os.getenv("GRAPH_REFRESH_TOKEN")
    GRAPH_TOKEN_EXPIRATION: Optional[str] = os.getenv("GRAPH_TOKEN_EXPIRATION")  # ISO-8601 UTC

    # Azure Key Vault
    AZURE_KEY_VAULT_URL: Optional[str] = os.getenv("AZURE_KEY_VAULT_URL")

    # Azure Queue Storage (async email processing pipeline)
    AZURE_STORAGE_QUEUE_NAME: str = os.getenv("AZURE_STORAGE_QUEUE_NAME", "shepherd-email-queue")

    # Microsoft Graph Webhook / Subscription settings
    GRAPH_NOTIFICATION_URL: Optional[str] = os.getenv("GRAPH_NOTIFICATION_URL")
    GRAPH_WEBHOOK_CLIENT_STATE: str = os.getenv("GRAPH_WEBHOOK_CLIENT_STATE", "shepherd-secret")

    # ShipMind / Brokerware TMS (Week 7 integration)
    SHIPMIND_BASE_URL: str = os.getenv("SHIPMIND_BASE_URL", "")
    SHIPMIND_API_KEY: str = os.getenv("SHIPMIND_API_KEY", "")

    # Application Insights telemetry
    APPINSIGHTS_CONNECTION_STRING: Optional[str] = os.getenv("APPINSIGHTS_CONNECTION_STRING")

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
