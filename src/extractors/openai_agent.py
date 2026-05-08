"""
Azure OpenAI agent for prompts and JSON structuring
"""
import json
from typing import Optional
from openai import AzureOpenAI
from src.config import Config
from src.models.shipment import Shipment
from src.utils.logger import get_logger

logger = get_logger(__name__)


class OpenAIAgent:
    """Agent for interacting with Azure OpenAI"""

    def __init__(self):
        self.endpoint = Config.AZURE_OPENAI_ENDPOINT
        self.api_key = Config.AZURE_OPENAI_KEY
        self.api_version = Config.AZURE_OPENAI_API_VERSION
        self.deployment_name = Config.AZURE_OPENAI_DEPLOYMENT

        if not all([self.endpoint, self.api_key, self.deployment_name]):
            raise ValueError("Azure OpenAI endpoint, API key, and deployment name must be configured")

        self.client = AzureOpenAI(
            api_key=self.api_key,
            api_version=self.api_version,
            azure_endpoint=self.endpoint
        )

    def extract_email_envelope(self, email_body: str, all_attachment_texts: Optional[str] = None) -> dict:
        """
        Pass 1 — Extract envelope-level context from the email body + all attachment texts.

        Runs once per email before per-attachment processing. Combining all sources here
        ensures that pickup/drop location data found in one attachment (e.g. a BOL) is
        available as envelope context when processing sibling attachments (e.g. a picking list)
        that don't repeat that information.
        """
        logger.info("Pass 1: extracting email envelope context")
        prompt = self._create_envelope_prompt(email_body, all_attachment_texts)

        try:
            response = self.client.chat.completions.create(
                model=self.deployment_name,
                messages=[
                    {
                        "role": "system",
                        "content": "Extract shipment envelope context from the email body. Return valid JSON only."
                    },
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            content = response.choices[0].message.content
            return json.loads(content) if content else {}
        except Exception as e:
            logger.error(f"Error extracting email envelope: {e}")
            return {}

    def extract_shipment_data(self, attachment_text: str, envelope: Optional[dict] = None) -> Optional[Shipment]:
        """
        Pass 2 — Extract structured shipment data from a single attachment.

        The attachment is the primary source of truth. The envelope context
        (from Pass 1) is injected as a read-only reference — used only to fill
        fields that are completely absent from the attachment.
        """
        logger.info("Pass 2: extracting shipment data from attachment")

        envelope_context = json.dumps(envelope, indent=2) if envelope else None
        prompt = self._create_extraction_prompt(attachment_text, envelope_context)

        try:
            response = self.client.chat.completions.create(
                model=self.deployment_name,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert logistics document extraction system. Always respond with valid JSON."
                    },
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )

            content = response.choices[0].message.content
            if not content:
                logger.error("Empty response from OpenAI")
                return None

            data = json.loads(content)
            shipment = Shipment.model_validate(data)
            shipment = self._deduplicate_items(shipment)

            logger.info("Successfully extracted shipment data")
            return shipment

        except Exception as e:
            logger.error(f"Error extracting shipment data: {e}")
            return None
    
    def extract_body_shipments(self, body_text: str, envelope: Optional[dict] = None) -> list:
        """
        Extract one or more shipments from the email body when the body contains shipment
        data unrelated to the attachments. Returns a list of Shipment objects.
        """
        logger.info("Extracting body-only shipments")
        envelope_context = json.dumps(envelope, indent=2) if envelope else None
        envelope_note = ""
        if envelope_context:
            envelope_note = f"""
Note: The following envelope context was extracted from the attachments in this email.
The body shipments below are UNRELATED to those attachments — do NOT mix or merge them.
Use the envelope only as background context, never to fill body shipment fields.
{envelope_context}
---
"""
        prompt = f"""The email body below contains one or more shipment details that are unrelated to the email attachments.
Extract ALL shipments found in the body as a JSON array.
{envelope_note}
For each shipment follow the exact same schema used for standard shipment extraction.
If only one shipment is present, still return it as a single-element array.

Rules:
- Extract each distinct shipment as a separate object in the array
- For each shipment, populate requiredFields and niceToHaveFields exactly as defined below
- Flag any missing required fields in missingRequiredFields
- The body may be unstructured (plain text, table, list) — figure out shipment boundaries from context

Required fields (place in requiredFields):
- customerName, pickupLocation (name + address), dropLocation (name + address)
- pickupDate or pickupWindow, deliveryDate, equipmentMode
- items (description, quantity, unit, weight, pieces, pallets, dimensions), totalWeight, accessorials

Nice-to-have fields (place in niceToHaveFields):
- pickupContact, dropContact (name, phone, email)
- referenceNumbers, specialInstructions, temperatureRequirements, declaredValue
- notes, carrier, shipmentId, orderNumber, purchaseOrder, trackingNumber

Email Body:
---
{body_text}
---

Return JSON:
{{
  "shipments": [
    {{
      "emailType": "shipment_tender",
      "missingRequiredFields": [],
      "requiredFields": {{
        "customerName": null,
        "pickupLocation": {{"name": null, "address": {{"street": null, "city": null, "state": null, "zipCode": null, "country": null}}}},
        "dropLocation": {{"name": null, "address": {{"street": null, "city": null, "state": null, "zipCode": null, "country": null}}}},
        "pickupDate": null,
        "pickupWindow": null,
        "deliveryDate": null,
        "equipmentMode": null,
        "items": [],
        "totalWeight": null,
        "accessorials": []
      }},
      "niceToHaveFields": {{
        "pickupContact": null,
        "dropContact": null,
        "referenceNumbers": [],
        "specialInstructions": null,
        "temperatureRequirements": null,
        "declaredValue": null,
        "notes": null,
        "carrier": null,
        "shipmentId": null,
        "orderNumber": null,
        "purchaseOrder": null,
        "trackingNumber": null
      }}
    }}
  ]
}}

Return only valid JSON."""

        try:
            response = self.client.chat.completions.create(
                model=self.deployment_name,
                messages=[
                    {"role": "system", "content": "Extract shipment data from email body. Return valid JSON only."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            content = response.choices[0].message.content
            if not content:
                return []
            data = json.loads(content)
            shipments = []
            for item in data.get("shipments", []):
                try:
                    shipments.append(Shipment.model_validate(item))
                except Exception as e:
                    logger.error(f"Failed to parse body shipment: {e}")
            return shipments
        except Exception as e:
            logger.error(f"Error extracting body shipments: {e}")
            return []

    def extract_source_fields(self, text: str) -> dict:
        """
        Extract structured shipment fields from a single source (email body or one attachment).
        Used only for the per-source breakdown display — not part of the main extraction pipeline.
        """
        prompt = f"""Extract all available shipment-related fields from the text below.
Return a flat JSON object with only the fields that are actually present — omit anything not found.

Fields to look for:
- customerName, shipmentId, orderNumber, purchaseOrder, referenceNumbers
- pickupName, pickupStreet, pickupCity, pickupState, pickupZip
- dropName, dropStreet, dropCity, dropState, dropZip
- pickupDate, deliveryDate, pickupWindow
- equipmentMode, carrier, trackingNumber
- items (list of: description, quantity, unit, weight, pieces, pallets, dimensions) — extract EACH row as a separate item, never merge rows even if they share the same description
- totalWeight, accessorials
- pickupContactName, pickupContactPhone, pickupContactEmail
- dropContactName, dropContactPhone, dropContactEmail
- specialInstructions, temperatureRequirements, declaredValue, notes

Text:
---
{text}
---

Return only valid JSON. Omit fields that are not present in the text."""

        try:
            response = self.client.chat.completions.create(
                model=self.deployment_name,
                messages=[
                    {"role": "system", "content": "Extract shipment fields from the provided text. Return valid JSON only."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            content = response.choices[0].message.content
            return json.loads(content) if content else {}
        except Exception as e:
            logger.error(f"Error in per-source extraction: {e}")
            return {}

    def resolve_customer_id(self, sender_email: str, email_subject: str, customer_list: list) -> Optional[int]:
        """
        Use OpenAI to match the sender email to a customerId from the customer list.

        Passes only customerId + customerName to keep the prompt small.
        Returns the matched customerId as an integer, or None if no match found.
        """
        if not customer_list:
            return None

        customers_json = json.dumps(customer_list, indent=2)

        prompt = f"""You are a freight brokerage data mapping assistant.

Sender email: {sender_email}
Email subject: {email_subject}

Top matching customers retrieved from our system (pre-filtered by Azure AI Search):
{customers_json}

Task: Confirm which customer this email belongs to and return their customerId.

Rules:
- The customerId you return MUST come from the list above — never invent or guess one
- Only return a customerId if the sender email domain clearly matches the customer's email domain in the list
- If the list is empty, return null — do not guess
- If none of the customers in the list match the sender email domain, return null
- A wrong customerId is far worse than returning null — when in doubt always return null

Return JSON only:
{{"customerId": <integer or null>}}"""

        try:
            response = self.client.chat.completions.create(
                model=self.deployment_name,
                messages=[
                    {"role": "system", "content": "You match sender emails to customer IDs. Return valid JSON only."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            content = response.choices[0].message.content
            if not content:
                return None
            data = json.loads(content)
            result = data.get("customerId")
            return int(result) if result is not None else None
        except Exception as e:
            logger.error(f"Error resolving customer ID via OpenAI: {e}")
            return None

    def _deduplicate_items(self, shipment: Shipment) -> Shipment:
        """If TAG-level items exist, drop summary-level PO/SO line items."""
        items = shipment.required_fields.items
        if not items:
            return shipment

        tag_items = [i for i in items if i.description and "TAG#" in i.description.upper()]
        non_tag_items = [i for i in items if not (i.description and "TAG#" in i.description.upper())]

        if tag_items:
            logger.info(f"Deduplication: keeping {len(tag_items)} TAG items, dropping {len(non_tag_items)} summary-level items")
            shipment.required_fields.items = tag_items

        return shipment

    def _create_envelope_prompt(self, email_body: str, all_attachment_texts: Optional[str] = None) -> str:
        attachments_section = ""
        if all_attachment_texts:
            attachments_section = f"""
Attachments (all documents in this email — use to extract pickup/drop locations and key references):
---
{all_attachment_texts}
---
"""
        return f"""Extract envelope-level shipment context from this email and its attachments.

Rules:
- pickupName/Street/City/State/Zip: Look across the email body AND all attachments for the
  shipper, origin, or "Ship From" location. One attachment may have this while others don't.
- dropName/Street/City/State/Zip: Same — look across all sources for the ship-to/destination.
  PRIORITY ORDER for drop location (highest to lowest):
  1. Explicit routing statements in the email body such as "[origin] to [destination]" or
     "[city] to [city], [address]" written by a logistics coordinator or freight broker —
     these directly state the shipment route and are the most reliable source.
  2. "Ship To" / "Deliver To" / "Destination" fields in attached documents.
  3. Contact signature addresses are the LOWEST priority — use them only if no routing
     statement or labeled destination field exists anywhere in the email or attachments.
- pickupDate: Look across all attachments AND the email subject line for pickup date signals.
  Check subject line for shorthand like "PU 3/5" or "PU 03/05" — these mean Pickup on that date.
  Also check for fields explicitly labeled "Ship Date", "Pickup Date", "Ready Date", or "Due Date".
  Do NOT use a document header date, print date, or "DATE:" timestamp — those are generation dates.
  If only month and day are given (e.g. "3/5", "PU 3/5", "March 5"), assume the current year 2026
  and return the full ISO date (e.g. 2026-03-05T00:00:00.000Z).
  Also check the email subject line for pickup date shorthand like "PU 3/5" or "PU 03/05" —
  these mean Pickup on that date. If only month and day are given (e.g. "3/5", "PU 3/5"),
  assume the current year 2026 and return the full ISO date (e.g. 2026-03-05T00:00:00.000Z).
- deliveryDate: Look across all attachments for a field explicitly labeled "Delivery Date",
  "Required Date", or "Deliver By".
  NOTE: On a Picking List document, the "Delivery Date" field refers to when the goods must
  be ready for carrier pickup — treat it as the pickupDate, not deliveryDate, unless a
  separate explicit delivery/drop date is also present.
- equipmentMode: Look for "SHIP VIA" fields on Sales Orders and shipping documents — this field
  always indicates the transportation method. Also check email subject lines and body text for
  equipment type mentions. Map to standard values: Dry Van, Flatbed, Reefer, Step Deck, LTL,
  Customer Truck, Box Truck, Straight Truck.
- referenceNumbers: Include only IDs that are NOT already captured in shipmentId, orderNumber,
  or purchaseOrder. If no additional IDs exist beyond those three fields, return []. Do NOT
  include weights, totals, item counts, or tag numbers.
- Do NOT use email sender signature address blocks as pickup or drop locations.
- contacts: Named individuals with phone/email from the email body.
- bodyRelatedToAttachments: Determine if the email body shipment content is related to the
  attached documents. Use this priority order:
  1. SHIPMENT ID MATCH — if the same shipment ID, order number, or PO number appears in both
     the body and an attachment, set to true.
  2. EXPLICIT REFERENCE — if the body text explicitly references the attachments (e.g.
     "see attached", "pickup details for the attached load", "as per the BOL attached",
     "load details are in the attachment"), set to true.
  3. If neither condition is met and the body contains shipment details that do not correspond
     to any attachment content, set to false.
  If the body has no shipment content at all (just a greeting, signature, or forwarding note),
  set to true (no separate extraction needed).

Email Body:
---
{email_body}
---
{attachments_section}
Return JSON:
{{
  "shipmentId": null,
  "orderNumber": null,
  "purchaseOrder": null,
  "referenceNumbers": [],
  "customerName": null,
  "carrier": null,
  "pickupName": null,
  "pickupStreet": null,
  "pickupCity": null,
  "pickupState": null,
  "pickupZip": null,
  "dropName": null,
  "dropStreet": null,
  "dropCity": null,
  "dropState": null,
  "dropZip": null,
  "equipmentMode": null,
  "items": [
    {{
      "description": "string",
      "quantity": "number or null",
      "unit": "string or null",
      "weight": "number or null",
      "dimensions": "string or null"
    }}
  ],
  "pickupDate": null,
  "deliveryDate": null,
  "totalWeight": null,
  "contacts": [],
  "specialInstructions": null,
  "notes": null,
  "bodyRelatedToAttachments": true,
  "bodyRelationReason": "shipment_id_match | explicit_reference | unrelated | no_body_shipment_content"
}}

Return only valid JSON."""

    def _create_extraction_prompt(self, attachment_text: str, envelope_context: Optional[str] = None) -> str:
        envelope_section = ""
        if envelope_context:
            envelope_section = f"""
ENVELOPE CONTEXT (read-only — extracted from email body and all attachments in this email):
All attachments in this email belong to the same shipment. Use the envelope to fill any fields
absent from this specific attachment. Rules:
- pickupLocation and dropLocation: if not found in this attachment, always use the envelope
  values — sibling attachments share the same origin and destination.
- pickupDate and deliveryDate: if not found in this attachment, use the envelope values —
  one sibling attachment (e.g. a BOL) may have the date while another (e.g. a Picking List)
  does not. Do NOT add pickupDate or deliveryDate to missingRequiredFields if the envelope
  provides them.
- equipmentMode: if not found in this attachment, use the envelope value.
- referenceNumbers, customerName, carrier: use envelope values if absent from attachment.
- Never override a value that IS present in this attachment with the envelope value.
- items: NEVER use envelope items. Items must come exclusively from this attachment's content.

{envelope_context}

---
"""
        return f"""You are a logistics document understanding system. Extract structured shipment data from the attachment below.
{envelope_section}

STEP 1 — CLASSIFY THE EMAIL
Determine which single category best describes this email:
- "shipment_tender": A confirmed load tender or booking request ready to create a new shipment
- "shipment_quote": A quote request or rate inquiry — part of the shipment lifecycle but requires pricing before a shipment can be created
- "tracking_request": A request for the status or location of an existing shipment
- "status_update": A notification about an active shipment in progress — e.g. shipment assigned, carrier confirmation, pickup confirmed, in-transit update. CarrierPoint notification emails always fall here (see rule below).
- "spam": Irrelevant or non-operational content — includes traditional spam/phishing, newsletters, marketing emails, vendor promotions, and internal emails with no shipment intent
- "other": Invoices, bid award notices (e.g. "Bid Award Notice", "Auction Load", "Carrier Action Required" from platforms like BestTransport, DAT, or similar), and anything that does not fit the categories above

CarrierPoint rule: If the email is from notifications@carrierpoint.com, is forwarded from that address, or contains CarrierPoint branding ("CarrierPoint" logo or the phrase "you have been selected to carry on CarrierPoint"), classify it as "status_update".
Bid award rule: Emails with subjects or bodies containing "Bid Award", "Auction Load", or "Carrier Action Required" from freight auction platforms are NOT status updates — classify them as "other".

STEP 2 — EXTRACT BASED ON TYPE

[shipment_tender]
Extract all available fields into requiredFields and niceToHaveFields.
After extraction, identify which required fields are still missing and list their keys
in missingRequiredFields.

Required fields (place in requiredFields):
- customerName: Company or person the shipment is being created for
- pickupLocation: name + address only — at minimum city + state, or a zip code
- dropLocation: name + address only — at minimum city + state, or a zip code
- pickupDate OR pickupWindow: When the pickup should occur
- equipmentMode: Transport type (e.g. Dry Van, Flatbed, Reefer, LTL, Customer Truck)
- items: At least one commodity entry with a weight value

Nice-to-have fields (place in niceToHaveFields):
- pickupContact / dropContact: contact name, phone, email for each location
- referenceNumbers, specialInstructions, temperatureRequirements, declaredValue
- notes, carrier, shipmentId, orderNumber, purchaseOrder, trackingNumber

[shipment_quote]
Extract all available fields into requiredFields and niceToHaveFields (same structure as shipment_tender).
Set missingRequiredFields to [] — do not flag missing fields for quote requests.

[tracking_request]
Extract whatever reference identifiers are present (shipment IDs, order numbers, dates).
Set missingRequiredFields to [].

[status_update]
Extract whatever shipment reference data is available (shipment IDs, stops, dates, carrier).
Set missingRequiredFields to [].

[spam]
Return only: {{"emailType": "spam"}}
Do not extract any other fields.

[other]
Extract whatever shipment-related data is available.
Set missingRequiredFields to [].

FIELD EXTRACTION GUIDELINES:

Origin (Pickup Location):
- Look for: Ship From, Pickup, Origin, Warehouse, Outside Yard
- If "Ship From" and "Vendor" are separate, use "Ship From" as the physical pickup address
- Do NOT use the email sender's signature address as the pickup location
- Only extract addresses explicitly labeled as pickup/origin in the shipping documents
- Leave street as null if no explicit pickup address is found in the documents

Destination (Delivery Location):
- Look for: Ship To, Delivery, Destination, Deliver To
- PRIORITY ORDER (highest to lowest):
  1. Explicit routing statements in the email body such as "[origin] to [destination]" or
     "[city] to [city, address]" written by a logistics coordinator or broker — use the
     destination city/address from that statement as the drop location.
  2. Labeled "Ship To" / "Deliver To" / "Destination" fields in attached documents.
  3. Contact signature addresses — use ONLY if no routing statement or labeled field exists.

Contacts:
- Place pickup location contact details in niceToHaveFields.pickupContact (name, phone, email)
- Place drop location contact details in niceToHaveFields.dropContact (name, phone, email)
- Only use persons explicitly listed as a contact FOR that specific location in the document
- A person listed as "Printed by", "Prepared by", "Created by", or any similar administrative
  or print role is NOT a location contact — never populate contact fields from these
- NEVER infer, guess, or hallucinate phone numbers or email addresses. Only include
  phone and email if they appear verbatim in the document for that location

Carrier:
- Use only the carrier name explicitly written in the document
- Do NOT expand a SCAC code into a full company name — if only a SCAC code is given,
  use it as the carrier value (e.g. "ECGI", not "Ecogistics")

Dates:
- Pickup date: Look for Pickup Date, Ship Date, Ready Date, Due Date explicitly labeled in the attachment.
- NEVER use a document's header date, print date, creation timestamp, or any "DATE:" field at
  the top of a document as the pickup date — those are document generation dates, not ship dates.
- CRITICAL — Picking List documents always contain TWO dates. You must distinguish them:
    1. The date near "Printed by [Name]" or at the document header (e.g. "Date 03/03/2026" next
       to "Printed by Karla Pruett") — this is the print/generation date. NEVER use it as pickupDate.
    2. The "Delivery Date" field lower in the document body (e.g. "Delivery Date 03/05/2026") —
       on a Picking List this IS the pickup/ship date. Always extract this as pickupDate.
- The "Ship Date" column must contain an actual date value; if blank, treat as absent and set pickupDate to null.
- If no pickup date is found in the attachment, set pickupDate to null — add "pickupDate" to missingRequiredFields.
- NEVER default to today's date or the current date under any circumstances.
- If a date is given as only month and day (e.g. "3/5", "PU 3/5", "March 5"), assume year 2026 and return full ISO date (e.g. 2026-03-05T00:00:00.000Z).
- Delivery date: Look for Delivery Date, Required Date, Deliver By in BOL and Sales Order documents (not Picking Lists).

Items (Commodities):
- If TAG-level items exist (with TAG#), use ONLY those — do not also add PO/SO summary lines
- If no TAG items exist, use PO, Sales Order, BOL, or Picking List named line items
- In a Purchase Order, the "Qty" column often represents weight in lbs (e.g. "6,700" under Qty with
  "0.52" rate means 6,700 lbs) — treat this as the item weight and extract it accordingly
- In a Sales Order or BOL, the "Weight" column is the actual weight — use it directly
- For Picking Lists: extract items with descriptions and quantities even if per-item weights are
  absent — use the document's total weight at the shipment level; do NOT flag items as missing
  just because individual line weights are not listed
- Extract items ONLY from this attachment's own content — never copy or inherit items from
  the envelope context or any sibling attachment. Each attachment produces its own item list.
- For documents with a TAG# column (spreadsheets, pick tickets, ship notices): extract EACH
  row as its own separate item. Include the TAG# in the description field (e.g. "TAG# 4517453
  HR 0.134GA 72.75x68.25"). Do NOT aggregate, group, or combine rows even if they share the
  same dimensions or gauge.
- CRITICAL — NEVER merge or combine multiple line items into a single item, even if they share
  the same product description, grade, gauge, or any other attribute. Each distinct row or line
  in the document is a separate item and must be extracted as its own entry. Do NOT sum quantities,
  pieces, or weights across rows. The number of items in your output must equal the number of
  distinct line rows in the document.
- NEVER include page totals, order totals, subtotals, or grand totals as line items
- Never include both TAG and PO/SO lines — pick the most granular source
- Capture: description, quantity, unit, weight (if available), pieces, pallets, dimensions

Order Numbers:
- orderNumber: Use the document's own order/SO number (e.g. Sales Order 218712 → "218712")
- purchaseOrder: Use the customer PO number (e.g. Cust PO# 11502 → "11502")
- referenceNumbers: Only IDs that are NOT already in shipmentId, orderNumber, or purchaseOrder.
  If shipmentId="277633", orderNumber="218712", purchaseOrder="11502", then referenceNumbers
  must NOT contain "277633", "218712", or "11502". If no additional IDs exist, return [].
- Do NOT include weights, totals, item counts, or TAG numbers in referenceNumbers.

Equipment / Mode:
- The "SHIP VIA" field on Sales Orders and shipping documents directly indicates the transportation
  method — always map it to equipmentMode (e.g. "CUSTOMER TRUCK" → "Customer Truck", "FLATBED" →
  "Flatbed", "LTL" → "LTL").
- Also look for: equipment type, trailer type, mode of transport in the email subject and body.
- Common values: Dry Van, Flatbed, Reefer, Step Deck, LTL, Intermodal, Customer Truck, Box Truck, Straight Truck

Accessorials:
- Liftgate, inside delivery, appointment required, team driver, hazmat, etc.

JSON Schema:
{{
  "emailType": "shipment_tender | shipment_quote | tracking_request | status_update | spam | other",
  "missingRequiredFields": ["keys of required fields that are missing — empty array if all present"],
  "requiredFields": {{
    "customerName": "string or null",
    "pickupLocation": {{
      "name": "string or null",
      "address": {{
        "street": "string or null",
        "city": "string or null",
        "state": "string or null",
        "zipCode": "string or null",
        "country": "string or null"
      }}
    }},
    "dropLocation": {{
      "name": "string or null",
      "address": {{
        "street": "string or null",
        "city": "string or null",
        "state": "string or null",
        "zipCode": "string or null",
        "country": "string or null"
      }}
    }},
    "pickupDate": "ISO datetime string or null",
    "pickupWindow": "string or null",
    "deliveryDate": "ISO datetime string or null",
    "equipmentMode": "string or null",
    "items": [
      {{
        "description": "string",
        "quantity": "number or null",
        "unit": "string or null",
        "weight": "number or null",
        "pieces": "number or null",
        "pallets": "number or null",
        "dimensions": "string or null"
      }}
    ],
    "totalWeight": "number or null — total shipment weight in lbs if explicitly stated",
    "accessorials": ["list of accessorial services"]
  }},
  "niceToHaveFields": {{
    "pickupContact": {{
      "name": "string or null",
      "phone": "string or null",
      "email": "string or null"
    }},
    "dropContact": {{
      "name": "string or null",
      "phone": "string or null",
      "email": "string or null"
    }},
    "referenceNumbers": ["additional reference numbers if any"],
    "specialInstructions": "string or null",
    "temperatureRequirements": "string or null",
    "declaredValue": "string or null",
    "notes": "string or null",
    "carrier": "string or null",
    "shipmentId": "string or null",
    "orderNumber": "string or null",
    "purchaseOrder": "string or null",
    "trackingNumber": "string or null"
  }},
}}

Attachment Content:
---
{attachment_text}
---

Return only valid JSON matching the schema above."""
