"""
Reply merger — merges newly extracted data from a customer reply into an
existing (incomplete) Shipment without overwriting values already present.

Used by the follow-up orchestrator after each round of customer replies to
build up a complete shipment incrementally.
"""
from __future__ import annotations

from typing import List

from src.models.shipment import LocationInfo, Shipment
from src.utils.logger import get_logger

logger = get_logger(__name__)


class ReplyMerger:
    """
    Merges a reply extraction into an existing partial Shipment.

    Rules:
    - Only populate a field in ``existing`` when it is currently null / empty.
    - Never overwrite a value that was already extracted.
    - After merging, recalculate ``missing_required_fields`` from the merged state.
    """

    # Fields treated as "warning only" — not blocking for shipment creation
    _WARNING_ONLY_FIELDS = {"items"}

    def merge(self, existing: Shipment, reply: Shipment) -> Shipment:
        """
        Fill missing required (and nice-to-have) fields from ``reply`` into
        ``existing``.

        Args:
            existing: The partial shipment from the original extraction.
            reply:    The shipment extracted from the customer's reply email.

        Returns:
            Updated ``existing`` with newly provided fields filled in and
            ``missing_required_fields`` refreshed.
        """
        rf_e = existing.required_fields
        rf_r = reply.required_fields
        ntf_e = existing.nice_to_have_fields
        ntf_r = reply.nice_to_have_fields

        # ── Required fields ────────────────────────────────────────────────
        if not rf_e.customer_name and rf_r.customer_name:
            rf_e.customer_name = rf_r.customer_name
            logger.debug("Merged: customerName")

        # Pickup location
        if not rf_e.pickup_location and rf_r.pickup_location:
            rf_e.pickup_location = rf_r.pickup_location
            logger.debug("Merged: pickupLocation (full)")
        elif rf_e.pickup_location and rf_r.pickup_location:
            self._merge_location(rf_e.pickup_location, rf_r.pickup_location)

        # Drop location
        if not rf_e.drop_location and rf_r.drop_location:
            rf_e.drop_location = rf_r.drop_location
            logger.debug("Merged: dropLocation (full)")
        elif rf_e.drop_location and rf_r.drop_location:
            self._merge_location(rf_e.drop_location, rf_r.drop_location)

        if not rf_e.pickup_date and rf_r.pickup_date:
            rf_e.pickup_date = rf_r.pickup_date
            logger.debug("Merged: pickupDate")

        if not rf_e.pickup_window and rf_r.pickup_window:
            rf_e.pickup_window = rf_r.pickup_window

        if not rf_e.delivery_date and rf_r.delivery_date:
            rf_e.delivery_date = rf_r.delivery_date
            logger.debug("Merged: deliveryDate")

        if not rf_e.equipment_mode and rf_r.equipment_mode:
            rf_e.equipment_mode = rf_r.equipment_mode
            logger.debug("Merged: equipmentMode")

        if not rf_e.items and rf_r.items:
            rf_e.items = rf_r.items
            logger.debug("Merged: items (%d lines)", len(rf_r.items))

        if not rf_e.total_weight and rf_r.total_weight:
            rf_e.total_weight = rf_r.total_weight

        if not rf_e.accessorials and rf_r.accessorials:
            rf_e.accessorials = rf_r.accessorials

        # ── Nice-to-have fields ───────────────────────────────────────────
        if not ntf_e.pickup_contact and ntf_r.pickup_contact:
            ntf_e.pickup_contact = ntf_r.pickup_contact
        if not ntf_e.drop_contact and ntf_r.drop_contact:
            ntf_e.drop_contact = ntf_r.drop_contact
        if not ntf_e.special_instructions and ntf_r.special_instructions:
            ntf_e.special_instructions = ntf_r.special_instructions
        if not ntf_e.notes and ntf_r.notes:
            ntf_e.notes = ntf_r.notes
        if not ntf_e.carrier and ntf_r.carrier:
            ntf_e.carrier = ntf_r.carrier
        if not ntf_e.shipment_id and ntf_r.shipment_id:
            ntf_e.shipment_id = ntf_r.shipment_id
        if not ntf_e.order_number and ntf_r.order_number:
            ntf_e.order_number = ntf_r.order_number
        if not ntf_e.purchase_order and ntf_r.purchase_order:
            ntf_e.purchase_order = ntf_r.purchase_order
        if not ntf_e.tracking_number and ntf_r.tracking_number:
            ntf_e.tracking_number = ntf_r.tracking_number

        # Merge reference numbers (union, no duplicates)
        if ntf_r.reference_numbers:
            existing_refs = set(ntf_e.reference_numbers)
            for ref in ntf_r.reference_numbers:
                if ref not in existing_refs:
                    ntf_e.reference_numbers.append(ref)
                    existing_refs.add(ref)

        # ── Recalculate missing fields ────────────────────────────────────
        existing.missing_required_fields = self._compute_missing(existing)

        logger.info(
            "Reply merge complete — %d field(s) still missing: %s",
            len(existing.missing_required_fields),
            existing.missing_required_fields,
        )
        return existing

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_location(existing_loc: LocationInfo, reply_loc: LocationInfo) -> None:
        """Fill null sub-fields inside a LocationInfo from a reply LocationInfo."""
        if not existing_loc.name and reply_loc.name:
            existing_loc.name = reply_loc.name

        if not existing_loc.address and reply_loc.address:
            existing_loc.address = reply_loc.address
            return

        if existing_loc.address and reply_loc.address:
            ea = existing_loc.address
            ra = reply_loc.address
            if not ea.street and ra.street:
                ea.street = ra.street
            if not ea.city and ra.city:
                ea.city = ra.city
            if not ea.state and ra.state:
                ea.state = ra.state
            if not ea.zip_code and ra.zip_code:
                ea.zip_code = ra.zip_code
            if not ea.country and ra.country:
                ea.country = ra.country

    @staticmethod
    def _compute_missing(shipment: Shipment) -> List[str]:
        """
        Re-evaluate which required fields are absent after the merge.
        Mirrors the validation logic in streamlit_app._validate_zip_fields()
        and _has_blocking_missing_fields().
        """
        missing: List[str] = []
        rf = shipment.required_fields

        if not rf.customer_name:
            missing.append("customerName")

        # Pickup location: need at minimum a name OR (city + state) OR zip
        pickup_ok = (
            rf.pickup_location
            and (
                rf.pickup_location.name
                or (
                    rf.pickup_location.address
                    and (rf.pickup_location.address.city or rf.pickup_location.address.zip_code)
                )
            )
        )
        if not pickup_ok:
            missing.append("pickupLocation")

        # Drop location: same minimum
        drop_ok = (
            rf.drop_location
            and (
                rf.drop_location.name
                or (
                    rf.drop_location.address
                    and (rf.drop_location.address.city or rf.drop_location.address.zip_code)
                )
            )
        )
        if not drop_ok:
            missing.append("dropLocation")

        if not rf.pickup_date and not rf.pickup_window:
            missing.append("pickupDate")

        if not rf.equipment_mode:
            missing.append("equipmentMode")

        if not rf.items:
            missing.append("items")

        # Zip code completeness checks
        if (
            rf.pickup_location
            and rf.pickup_location.address
            and not rf.pickup_location.address.zip_code
        ):
            missing.append("shipperZip")

        if (
            rf.drop_location
            and rf.drop_location.address
            and not rf.drop_location.address.zip_code
        ):
            missing.append("consigneeZip")

        return missing
