"""Transform internal Shipment model to client's required API payload format."""
import re
import json
from typing import Optional, Tuple
from datetime import datetime

from src.models.shipment import Shipment


# ---------------------------------------------------------------------------
# Country code normalization
# ---------------------------------------------------------------------------

_COUNTRY_NORMALIZE = {
    "usa":           "US",
    "us":            "US",
    "u.s.":          "US",
    "u.s.a.":        "US",
    "united states": "US",
    "canada":        "CA",
    "can":           "CA",
    "mexico":        "MX",
    "mex":           "MX",
}


def _normalize_zip(zip_code: Optional[str]) -> Optional[str]:
    """Return only the 5-digit base zip, stripping ZIP+4 suffix (e.g. '47353-8810' → '47353')."""
    if not zip_code:
        return None
    return zip_code.strip().split("-")[0][:5] or None


def _normalize_country(country: Optional[str], default: str = "US") -> str:
    """Return ISO 3166-1 alpha-2 code. Falls back to default if unrecognised."""
    if not country:
        return default
    normalized = _COUNTRY_NORMALIZE.get(country.strip().lower())
    if normalized:
        return normalized
    # Already looks like a 2-letter code — return uppercased
    stripped = country.strip()
    if len(stripped) == 2:
        return stripped.upper()
    return default


# ---------------------------------------------------------------------------
# Equipment mode mapping
# ---------------------------------------------------------------------------

_EQUIPMENT_TYPE_MAP = {
    "dry van":       ("Truckload", "Van"),
    "flatbed":       ("Truckload", "Flatbed"),
    "fb":            ("Truckload", "Flatbed"),
    "reefer":        ("Truckload", "Reefer"),
    "refrigerated":  ("Truckload", "Reefer"),
    "step deck":     ("Truckload", "StepDeck"),
    "stepdeck":      ("Truckload", "StepDeck"),
    "box truck":     ("Truckload", "NotSpecified"),
    "straight truck":("Truckload", "NotSpecified"),
    "customer truck":("Truckload", "NotSpecified"),
    "intermodal":    ("Truckload", "NotSpecified"),
    "ltl":           ("LTL", "NotSpecified"),
    "parcel":        ("Parcel", "NotSpecified"),
}


def _map_equipment(equipment_mode: Optional[str]) -> Tuple[str, str]:
    """Return (shipmentMode, equipmentType) using Brokerware-accepted values."""
    if not equipment_mode:
        return "Truckload", "NotSpecified"
    mode = equipment_mode.lower().strip()
    for key, value in _EQUIPMENT_TYPE_MAP.items():
        if key in mode:
            return value
    return "Truckload", "NotSpecified"


# ---------------------------------------------------------------------------
# Dimension parsing
# ---------------------------------------------------------------------------

def _parse_dimensions(dimensions: Optional[str]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Parse a 'LxWxH' style string into (length, width, height) floats."""
    if not dimensions:
        return None, None, None
    nums = re.findall(r'\d+(?:\.\d+)?', dimensions)
    if len(nums) >= 3:
        return float(nums[0]), float(nums[1]), float(nums[2])
    return None, None, None


# ---------------------------------------------------------------------------
# Date / time helpers
# ---------------------------------------------------------------------------

def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Format a datetime to ISO string with milliseconds."""
    if not dt:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _at_time(base: Optional[datetime], hour: int, minute: int = 0) -> Optional[str]:
    """Return ISO string with the given hour/minute applied to base date."""
    if not base:
        return None
    return _iso(base.replace(hour=hour, minute=minute, second=0, microsecond=0))


def _parse_pickup_window(
    pickup_window: Optional[str],
    pickup_date: Optional[datetime],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract open/close times from pickupWindow string (e.g. "8AM-5PM", "08:00-17:00").
    Falls back to 08:00–18:00 on pickupDate if window is absent or unparseable.
    """
    open_hour, close_hour = 8, 18

    if pickup_window:
        matches = re.findall(r'(\d{1,2})(?::(\d{2}))?\s*(AM|PM|am|pm)?', pickup_window)
        if len(matches) >= 2:
            def _to_24h(h_str, _m_str, ampm):
                h = int(h_str)
                if ampm and ampm.upper() == "PM" and h != 12:
                    h += 12
                elif ampm and ampm.upper() == "AM" and h == 12:
                    h = 0
                return h
            parsed_open  = _to_24h(*matches[0])
            parsed_close = _to_24h(*matches[1])
            # Only use parsed values if both are valid hours
            if 0 <= parsed_open <= 23 and 0 <= parsed_close <= 23:
                open_hour  = parsed_open
                close_hour = parsed_close

    return _at_time(pickup_date, open_hour), _at_time(pickup_date, close_hour)


# ---------------------------------------------------------------------------
# Main transformation
# ---------------------------------------------------------------------------

def format_client_json(shipment: Shipment, customer_id: Optional[int] = None) -> dict:
    """
    Transform a Shipment into the client's required API payload.

    - customerId is left as None — resolved later via client API by matching
      sender/receiver email addresses.
    - Fields absent from the source document are set to None (omitted from
      output if the caller uses exclude_none serialisation).
    """
    rf  = shipment.required_fields
    ntf = shipment.nice_to_have_fields

    # ── Locations ────────────────────────────────────────────────────────────
    pickup      = rf.pickup_location
    drop        = rf.drop_location
    pickup_addr = pickup.address if pickup else None
    drop_addr   = drop.address   if drop   else None

    # ── Equipment ────────────────────────────────────────────────────────────
    shipment_mode, equipment_type = _map_equipment(rf.equipment_mode)

    # ── Time windows ─────────────────────────────────────────────────────────
    pickup_open, pickup_close = _parse_pickup_window(rf.pickup_window, rf.pickup_date)
    delivery_open  = _at_time(rf.delivery_date, 8)
    delivery_close = _at_time(rf.delivery_date, 16)

    # ── Items ─────────────────────────────────────────────────────────────────
    any_palletized = any((i.pallets or 0) > 0 for i in rf.items)

    items = []
    for item in rf.items:
        length, width, height = _parse_dimensions(item.dimensions)
        # Brokerware rejects null for decimal fields — only include them when available
        item_dict: dict = {
            # Fall back to "General Freight" when LLM didn't extract a description
            "productDescription": item.description or "General Freight",
            "class":              "100",  # default; Brokerware requires a non-null value
            "pieces":             int(item.pieces) if item.pieces is not None else 1,
            "weight":             item.weight if item.weight is not None else 0,
            "isHazardous":        False,
            "packaging":          "pallets" if (item.pallets or 0) > 0 else "truckloads",
        }
        if length is not None:
            item_dict["length"] = length
        if width is not None:
            item_dict["width"] = width
        if height is not None:
            item_dict["height"] = height
        items.append(item_dict)

    # Brokerware requires at least one item — add a placeholder when none were extracted
    if not items:
        total_weight = rf.total_weight or 0
        items = [{
            "productDescription": "General Freight",
            "class":              "100",
            "pieces":             1,
            "weight":             total_weight,
            "isHazardous":        False,
            "packaging":          "truckloads",
        }]

    # ── Carrier ──────────────────────────────────────────────────────────────
    carrier_val = ntf.carrier
    # Only use as SCAC if it looks like one (2–4 uppercase letters)
    is_scac = bool(carrier_val and re.match(r'^[A-Z]{2,4}$', (carrier_val or "").strip()))
    scac    = carrier_val.strip() if is_scac else None

    # ── Status ───────────────────────────────────────────────────────────────
    status = "Booked" if shipment.email_type == "shipment_tender" else "Quoted"

    return {
        # Required location fields
        "shipperZip":      _normalize_zip(pickup_addr.zip_code if pickup_addr else None),
        "shipperCountry":  _normalize_country(pickup_addr.country if pickup_addr else None),
        "consigneeZip":    _normalize_zip(drop_addr.zip_code if drop_addr else None),
        "consigneeCountry":_normalize_country(drop_addr.country if drop_addr else None),

        # Equipment
        "shipmentMode":    shipment_mode,
        "equipmentType":   equipment_type,

        # Items (at least one required)
        "items": items,

        # Shipper details — Brokerware requires shipperAddress; fall back to city+state
        "shipperAddress":  (
            pickup_addr.street if (pickup_addr and pickup_addr.street)
            else (f"{pickup_addr.city}, {pickup_addr.state}" if pickup_addr and pickup_addr.city else None)
        ),
        "shipperName":     (pickup.name if pickup and pickup.name else rf.customer_name),
        "shipperContact":  ntf.pickup_contact.name  if ntf.pickup_contact else None,
        "shipperEmail":    ntf.pickup_contact.email if ntf.pickup_contact else None,
        "shipperPhone":    ntf.pickup_contact.phone if ntf.pickup_contact else None,

        # Consignee details — fall back to city+state when street is absent
        "consigneeAddress": (
            drop_addr.street if (drop_addr and drop_addr.street)
            else (f"{drop_addr.city}, {drop_addr.state}" if drop_addr and drop_addr.city else None)
        ),
        "consigneeName":    (drop.name if drop and drop.name else rf.customer_name),
        "consigneeContact": ntf.drop_contact.name  if ntf.drop_contact else None,
        "consigneeEmail":   ntf.drop_contact.email if ntf.drop_contact else None,
        "consigneePhone":   ntf.drop_contact.phone if ntf.drop_contact else None,

        # References
        "poReference":    ntf.purchase_order,
        "shipperNumber":  ntf.shipment_id or ntf.order_number,

        # Palletisation — boolean required by Brokerware API
        "isPalletized": bool(any_palletized),

        # Special instructions → Bill of Lading note
        "billOfLandingNote": ntf.special_instructions,

        # Required date/time fields
        "pickupDate":                    _iso(rf.pickup_date),
        "pickupOpenTime":                pickup_open,
        "pickupCloseTime":               pickup_close,
        "estimatedDelivery":             _iso(rf.delivery_date),
        "estimatedDeliveryOpenTime":     delivery_open,
        "estimatedDeliveryCloseTime":    delivery_close,

        # Status
        "shipmentStatus": status,

        # Carrier block
        "carrier": {
            "carrierScac":  scac,
            "providerScac": scac,
            "proNumber":    ntf.tracking_number,
        },

        # Housekeeping
        "test":       False,
        "customerId": customer_id,
    }


def format_client_json_str(shipment: Shipment, customer_id: Optional[int] = None) -> str:
    """Return the client JSON payload as a formatted string."""
    return json.dumps(format_client_json(shipment, customer_id=customer_id), indent=2)
