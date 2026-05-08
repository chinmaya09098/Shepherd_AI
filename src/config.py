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
