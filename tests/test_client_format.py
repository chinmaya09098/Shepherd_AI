"""
Tests for src/models/client_format.py

Covers:
  - _normalize_zip      : ZIP+4 stripping, empty input
  - _normalize_country  : USA/Canada/Mexico aliases, 2-letter pass-through, fallback
  - _map_equipment      : dry van, flatbed, reefer, LTL, unknown
  - _parse_dimensions   : LxWxH string parsing, missing/malformed
  - _parse_pickup_window: AM/PM, 24-hour, fallback defaults
  - format_client_json  : full payload structure with a minimal Shipment
"""
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub src.secrets so src.config doesn't break when it calls
# load_secrets_from_keyvault() at import time.
# ---------------------------------------------------------------------------
sys.modules.setdefault(
    "src.secrets",
    types.SimpleNamespace(load_secrets_from_keyvault=lambda: None),
)

# test_brokerware_client.py (which sorts before this file) may have installed
# lightweight stub versions of these modules.  Remove them so we import the
# real implementations here.
for _mod in ("src.models.client_format", "src.models.shipment"):
    sys.modules.pop(_mod, None)

# Now import everything under test
from src.models.client_format import (   # noqa: E402
    _normalize_zip,
    _normalize_country,
    _map_equipment,
    _parse_dimensions,
    _parse_pickup_window,
    format_client_json,
)
from src.models.shipment import (        # noqa: E402
    Shipment,
    RequiredFields,
    NiceToHaveFields,
    LocationInfo,
    ShipmentAddress,
    ShipmentItem,
    ContactInfo,
)


# ===========================================================================
# _normalize_zip
# ===========================================================================

class TestNormalizeZip(unittest.TestCase):

    def test_strips_zip4_suffix(self):
        self.assertEqual(_normalize_zip("47353-8810"), "47353")

    def test_five_digit_zip_unchanged(self):
        self.assertEqual(_normalize_zip("90210"), "90210")

    def test_truncates_to_five_chars(self):
        # 6-digit zip-like string → first 5 chars
        self.assertEqual(_normalize_zip("123456"), "12345")

    def test_none_returns_none(self):
        self.assertIsNone(_normalize_zip(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_normalize_zip(""))

    def test_whitespace_stripped(self):
        self.assertEqual(_normalize_zip("  90210  "), "90210")


# ===========================================================================
# _normalize_country
# ===========================================================================

class TestNormalizeCountry(unittest.TestCase):

    def test_usa_variants(self):
        for alias in ("usa", "US", "u.s.", "u.s.a.", "United States", "united states"):
            self.assertEqual(_normalize_country(alias), "US", msg=f"alias={alias!r}")

    def test_canada_variants(self):
        for alias in ("canada", "can", "CA"):
            self.assertEqual(_normalize_country(alias), "CA", msg=f"alias={alias!r}")

    def test_mexico_variants(self):
        for alias in ("mexico", "mex", "MX"):
            self.assertEqual(_normalize_country(alias), "MX", msg=f"alias={alias!r}")

    def test_two_letter_passthrough(self):
        self.assertEqual(_normalize_country("GB"), "GB")
        self.assertEqual(_normalize_country("de"), "DE")

    def test_unknown_falls_back_to_default(self):
        self.assertEqual(_normalize_country("Narnia"), "US")
        self.assertEqual(_normalize_country("Narnia", default="CA"), "CA")

    def test_none_returns_default(self):
        self.assertEqual(_normalize_country(None), "US")
        self.assertEqual(_normalize_country(None, default="MX"), "MX")


# ===========================================================================
# _map_equipment
# ===========================================================================

class TestMapEquipment(unittest.TestCase):

    def test_dry_van(self):
        self.assertEqual(_map_equipment("Dry Van"), ("Truckload", "Van"))

    def test_flatbed_alias(self):
        self.assertEqual(_map_equipment("FB"), ("Truckload", "Flatbed"))
        self.assertEqual(_map_equipment("Flatbed"), ("Truckload", "Flatbed"))

    def test_reefer(self):
        self.assertEqual(_map_equipment("Reefer"), ("Truckload", "Reefer"))
        self.assertEqual(_map_equipment("Refrigerated"), ("Truckload", "Reefer"))

    def test_step_deck(self):
        self.assertEqual(_map_equipment("Step Deck"), ("Truckload", "StepDeck"))
        self.assertEqual(_map_equipment("StepDeck"), ("Truckload", "StepDeck"))

    def test_ltl(self):
        self.assertEqual(_map_equipment("LTL"), ("LTL", "NotSpecified"))

    def test_parcel(self):
        self.assertEqual(_map_equipment("Parcel"), ("Parcel", "NotSpecified"))

    def test_unknown_defaults_to_truckload(self):
        self.assertEqual(_map_equipment("Hovercraft"), ("Truckload", "NotSpecified"))

    def test_none_defaults_to_truckload(self):
        self.assertEqual(_map_equipment(None), ("Truckload", "NotSpecified"))

    def test_case_insensitive(self):
        self.assertEqual(_map_equipment("DRY VAN"), ("Truckload", "Van"))
        self.assertEqual(_map_equipment("  flatbed  "), ("Truckload", "Flatbed"))

    def test_intermodal(self):
        self.assertEqual(_map_equipment("Intermodal"), ("Truckload", "NotSpecified"))


# ===========================================================================
# _parse_dimensions
# ===========================================================================

class TestParseDimensions(unittest.TestCase):

    def test_standard_lxwxh(self):
        l, w, h = _parse_dimensions("48x96x48")
        self.assertEqual((l, w, h), (48.0, 96.0, 48.0))

    def test_with_spaces(self):
        l, w, h = _parse_dimensions("48 x 96 x 48")
        self.assertEqual((l, w, h), (48.0, 96.0, 48.0))

    def test_decimal_values(self):
        l, w, h = _parse_dimensions("48.5x96.0x42.25")
        self.assertAlmostEqual(l, 48.5)
        self.assertAlmostEqual(w, 96.0)
        self.assertAlmostEqual(h, 42.25)

    def test_none_returns_triple_none(self):
        self.assertEqual(_parse_dimensions(None), (None, None, None))

    def test_empty_string_returns_triple_none(self):
        self.assertEqual(_parse_dimensions(""), (None, None, None))

    def test_fewer_than_3_numbers_returns_triple_none(self):
        self.assertEqual(_parse_dimensions("48x96"), (None, None, None))

    def test_only_one_number(self):
        self.assertEqual(_parse_dimensions("48"), (None, None, None))


# ===========================================================================
# _parse_pickup_window
# ===========================================================================

class TestParsePickupWindow(unittest.TestCase):

    BASE_DATE = datetime(2025, 6, 15)

    def test_am_pm_window(self):
        open_t, close_t = _parse_pickup_window("8AM-5PM", self.BASE_DATE)
        self.assertIn("T08:00", open_t)
        self.assertIn("T17:00", close_t)

    def test_24h_window(self):
        open_t, close_t = _parse_pickup_window("08:00-17:00", self.BASE_DATE)
        self.assertIn("T08:00", open_t)
        self.assertIn("T17:00", close_t)

    def test_noon_handling(self):
        open_t, close_t = _parse_pickup_window("12PM-6PM", self.BASE_DATE)
        self.assertIn("T12:00", open_t)
        self.assertIn("T18:00", close_t)

    def test_midnight_as_12am(self):
        open_t, close_t = _parse_pickup_window("12AM-8AM", self.BASE_DATE)
        self.assertIn("T00:00", open_t)
        self.assertIn("T08:00", close_t)

    def test_fallback_when_no_window_string(self):
        """Missing window → defaults 08:00–18:00."""
        open_t, close_t = _parse_pickup_window(None, self.BASE_DATE)
        self.assertIn("T08:00", open_t)
        self.assertIn("T18:00", close_t)

    def test_fallback_when_no_base_date(self):
        """No pickup_date → both return None."""
        open_t, close_t = _parse_pickup_window("8AM-5PM", None)
        self.assertIsNone(open_t)
        self.assertIsNone(close_t)

    def test_unparseable_window_uses_defaults(self):
        """Garbage window string → fall back to 08:00–18:00."""
        open_t, close_t = _parse_pickup_window("ASAP", self.BASE_DATE)
        self.assertIn("T08:00", open_t)
        self.assertIn("T18:00", close_t)


# ===========================================================================
# format_client_json — integration
# ===========================================================================

def _make_shipment(
    pickup_city="Chicago", pickup_state="IL", pickup_zip="60601",
    drop_city="Detroit",   drop_state="MI",   drop_zip="48201",
    equipment="Dry Van",
    pickup_date=None,
    delivery_date=None,
    email_type="shipment_tender",
    items=None,
) -> Shipment:
    pickup_addr = ShipmentAddress(city=pickup_city, state=pickup_state,
                                  zip_code=pickup_zip, country="US")
    drop_addr   = ShipmentAddress(city=drop_city,   state=drop_state,
                                  zip_code=drop_zip,   country="US")
    rf = RequiredFields(
        pickupLocation=LocationInfo(name="Shipper Co", address=pickup_addr),
        dropLocation=LocationInfo(name="Consignee Inc", address=drop_addr),
        equipmentMode=equipment,
        pickupDate=pickup_date or datetime(2025, 7, 10, 8, 0),
        deliveryDate=delivery_date or datetime(2025, 7, 12, 8, 0),
        items=items if items is not None else [ShipmentItem(description="Pallets", weight=5000, pieces=10, pallets=10)],
    )
    ntf = NiceToHaveFields()
    return Shipment(emailType=email_type, requiredFields=rf, niceToHaveFields=ntf)


class TestFormatClientJson(unittest.TestCase):

    def test_basic_structure(self):
        payload = format_client_json(_make_shipment())
        self.assertIn("shipperZip",    payload)
        self.assertIn("consigneeZip",  payload)
        self.assertIn("shipmentMode",  payload)
        self.assertIn("equipmentType", payload)
        self.assertIn("items",         payload)
        self.assertIn("carrier",       payload)

    def test_shipper_zip_normalized(self):
        s = _make_shipment(pickup_zip="60601-1234")
        payload = format_client_json(s)
        self.assertEqual(payload["shipperZip"], "60601")

    def test_consignee_zip_normalized(self):
        s = _make_shipment(drop_zip="48201-5678")
        payload = format_client_json(s)
        self.assertEqual(payload["consigneeZip"], "48201")

    def test_country_normalized_to_iso(self):
        payload = format_client_json(_make_shipment())
        self.assertEqual(payload["shipperCountry"],   "US")
        self.assertEqual(payload["consigneeCountry"], "US")

    def test_dry_van_equipment(self):
        payload = format_client_json(_make_shipment(equipment="Dry Van"))
        self.assertEqual(payload["shipmentMode"],  "Truckload")
        self.assertEqual(payload["equipmentType"], "Van")

    def test_ltl_equipment(self):
        payload = format_client_json(_make_shipment(equipment="LTL"))
        self.assertEqual(payload["shipmentMode"],  "LTL")
        self.assertEqual(payload["equipmentType"], "NotSpecified")

    def test_palletized_flag_set_when_pallets_present(self):
        payload = format_client_json(_make_shipment())
        self.assertTrue(payload["isPalletized"])

    def test_palletized_false_when_no_pallets(self):
        items = [ShipmentItem(description="Crates", weight=2000, pieces=5)]
        s = _make_shipment(items=items)
        payload = format_client_json(s)
        self.assertFalse(payload["isPalletized"])

    def test_status_booked_for_shipment_tender(self):
        payload = format_client_json(_make_shipment(email_type="shipment_tender"))
        self.assertEqual(payload["shipmentStatus"], "Booked")

    def test_status_quoted_for_other_type(self):
        payload = format_client_json(_make_shipment(email_type="quote_request"))
        self.assertEqual(payload["shipmentStatus"], "Quoted")

    def test_customer_id_passed_through(self):
        payload = format_client_json(_make_shipment(), customer_id=4098014)
        self.assertEqual(payload["customerId"], 4098014)

    def test_customer_id_none_when_not_provided(self):
        payload = format_client_json(_make_shipment())
        self.assertIsNone(payload["customerId"])

    def test_placeholder_item_when_no_items(self):
        s = _make_shipment(items=[])
        payload = format_client_json(s)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["productDescription"], "General Freight")

    def test_test_flag_is_false(self):
        payload = format_client_json(_make_shipment())
        self.assertFalse(payload["test"])

    def test_dates_iso_format(self):
        d = datetime(2025, 7, 10)
        payload = format_client_json(_make_shipment(pickup_date=d))
        self.assertTrue(payload["pickupDate"].startswith("2025-07-10"))

    def test_item_weight_and_pieces(self):
        items = [ShipmentItem(description="Steel", weight=10000, pieces=20, pallets=0)]
        payload = format_client_json(_make_shipment(items=items))
        item = payload["items"][0]
        self.assertEqual(item["weight"], 10000)
        self.assertEqual(item["pieces"], 20)

    def test_item_fallback_description(self):
        items = [ShipmentItem(weight=1000, pieces=1)]
        payload = format_client_json(_make_shipment(items=items))
        self.assertEqual(payload["items"][0]["productDescription"], "General Freight")


if __name__ == "__main__":
    unittest.main()
