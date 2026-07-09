"""
Brokerware TMS API client.

Handles OAuth 2.0 Client Credentials token lifecycle and shipment creation.
Token is fetched on first use and refreshed automatically when it expires.

API Endpoints:
  Auth:           POST {base_url}/connect/token
  CreateShipment: POST {base_url}/api/clientv1/CreateShipmentAPI
"""
import time
import requests
from dataclasses import dataclass
from typing import Optional

from src.config import Config
from src.models.client_format import format_client_json
from src.models.shipment import Shipment
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Token cache (module-level — lives for the duration of the process)
# ---------------------------------------------------------------------------
_access_token: Optional[str] = None
_token_expires_at: float = 0.0       # epoch seconds
_TOKEN_EXPIRY_BUFFER: int = 60       # refresh 60 s before actual expiry


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CreateShipmentResult:
    success: bool
    shipment_id: Optional[str] = None   # ID returned by Brokerware on success
    raw_response: Optional[dict] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _fetch_token() -> str:
    """Obtain a new Bearer token using OAuth 2.0 Client Credentials flow."""
    global _access_token, _token_expires_at

    url = f"{Config.BROKERWARE_BASE_URL}/connect/token"
    logger.info("Fetching new Brokerware access token")

    response = requests.post(
        url,
        data={
            "grant_type":    "client_credentials",
            "client_id":     Config.BROKERWARE_CLIENT_ID,
            "client_secret": Config.BROKERWARE_CLIENT_SECRET,
        },
        timeout=15,
    )
    response.raise_for_status()

    data = response.json()
    _access_token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    _token_expires_at = time.time() + expires_in - _TOKEN_EXPIRY_BUFFER

    logger.info(f"Brokerware token acquired, expires in {expires_in}s")
    return _access_token


def _get_token() -> str:
    """Return a valid Bearer token, refreshing if expired."""
    if _access_token is None or time.time() >= _token_expires_at:
        return _fetch_token()
    return _access_token


# ---------------------------------------------------------------------------
# CreateShipment
# ---------------------------------------------------------------------------

def create_shipment(
    shipment: Shipment,
    customer_id: Optional[int] = None,
) -> CreateShipmentResult:
    """
    Submit a shipment to Brokerware TMS via the CreateShipmentAPI endpoint.

    Args:
        shipment:    Extracted Shipment model (from OpenAI agent).
        customer_id: Brokerware customerId. Falls back to
                     Config.BROKERWARE_DEFAULT_CUSTOMER_ID if not provided.

    Returns:
        CreateShipmentResult with success flag, shipment ID, and raw response.
    """
    resolved_customer_id = customer_id or Config.BROKERWARE_DEFAULT_CUSTOMER_ID

    payload = format_client_json(shipment, customer_id=resolved_customer_id)
    url = f"{Config.BROKERWARE_BASE_URL}/api/clientv1/CreateShipmentAPI"

    logger.info(
        f"Creating Brokerware shipment for customerId={resolved_customer_id} "
        f"pickup={payload.get('shipperZip')} → drop={payload.get('consigneeZip')}"
    )

    try:
        token = _get_token()
        response = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        # Brokerware returns {"loadId": <int>} on success
        shipment_id = (
            data.get("loadId")
            or data.get("shipmentId")
            or data.get("ShipmentId")
            or data.get("id")
            or (str(data) if isinstance(data, (int, str)) else None)
        )

        logger.info(f"Brokerware shipment created successfully: id={shipment_id}")
        return CreateShipmentResult(
            success=True,
            shipment_id=str(shipment_id) if shipment_id else None,
            raw_response=data,
        )

    except requests.HTTPError as e:
        error_body = ""
        try:
            error_body = e.response.json()
        except Exception:
            error_body = e.response.text if e.response else str(e)

        logger.error(f"Brokerware API HTTP error {e.response.status_code}: {error_body}")
        return CreateShipmentResult(
            success=False,
            error=f"HTTP {e.response.status_code}: {error_body}",
        )

    except Exception as e:
        logger.error(f"Brokerware API request failed: {e}")
        return CreateShipmentResult(success=False, error=str(e))


def is_configured() -> bool:
    """Return True when Brokerware credentials are present."""
    return bool(Config.BROKERWARE_CLIENT_ID and Config.BROKERWARE_CLIENT_SECRET)
