# Shepherd AI POC

An AI-powered load tender processing system that reads freight emails, extracts shipment data from attachments, matches the customer, and outputs structured JSON ready for the Hyperion TMS API.

---

## What It Does

1. **Reads emails** (.eml files) from Azure Blob Storage or uploaded directly via the UI
2. **OCR / document parsing** — extracts text from PDF attachments (Picking Lists, BOLs, Sales Orders) using Azure Content Understanding
3. **AI extraction** — uses Azure OpenAI (GPT) to extract structured shipment fields from the text
4. **Customer matching** — identifies the customer ID using Azure AI Search (vector search) + Hyperion TMS contacts
5. **Outputs client JSON** — produces a Hyperion TMS-compatible JSON payload ready to create a shipment

---

## Architecture Overview

```
Email (.eml)
    │
    ▼
EML Parser          ← extracts body, From/To, attachments
    │
    ▼
Azure Content       ← OCR: converts PDF attachments → text
Understanding
    │
    ▼
OpenAI Agent        ← 3-pass extraction:
    │                    Pass 1: Email envelope (pickup/drop, dates, equipment)
    │                    Pass 2: Per-attachment shipment data
    │                    Pass 3: Per-source breakdown (display only)
    │
    ▼
Azure AI Search     ← vector search on Hyperion customer contacts
    │                    → broker domain mapping (hardcoded)
    │                    → vector similarity search for unknown senders
    │
    ▼
Hyperion TMS API    ← resolves customerId
    │
    ▼
Client JSON         ← final structured output (shipment payload)
```

---

## Key Components

### `streamlit_app.py`
Main UI application. Handles the end-to-end flow:
- File upload or Azure Blob Storage selection
- Orchestrates EML parsing → OCR → AI extraction → customer matching → JSON output
- Displays normalized extraction, per-source breakdown, validation flags, and final client JSON

### `src/extractors/openai_agent.py`
All Azure OpenAI interactions:
- `extract_email_envelope()` — Pass 1: global context (pickup/drop locations, dates, equipment) from the email body + all attachments combined
- `extract_shipment_data()` — Pass 2: per-attachment structured extraction using the envelope as read-only context
- `extract_source_fields()` — Pass 3: simple per-source breakdown used for the UI display panel
- `resolve_customer_id()` — confirms which customer a vector-search result belongs to (used only for non-broker senders)

### `src/services/search_client.py`
Azure AI Search integration for customer matching:
- Embeds Hyperion customer contacts using `text-embedding-3-small` and indexes them in Azure AI Search
- **Broker domain mapping** (hardcoded): known broker email domains are mapped directly to a customer name, bypassing vector search entirely
- **Vector search**: for unknown senders, embeds the sender domain and finds the top matching customers by cosine similarity (threshold: 0.70)
- Re-indexes contacts automatically when the Hyperion API returns new/changed data (MD5 fingerprint check)

**Hardcoded broker mappings:**
| Broker Domain | Customer |
|---|---|
| ecogistics.org | Disney Corporate |
| kloeckner.com | ABC - Alen |
| nucorskyline.com | Denon |

### `src/services/hyperion_client.py`
Hyperion TMS API client:
- OAuth 2.0 Client Credentials token management (auto-refreshes before expiry)
- Fetches customer contacts list (cached per token lifecycle)

### `src/readers/eml_parser.py`
Parses `.eml` files: extracts subject, From, To, plain text body, HTML body, and saves attachments to disk.

### `src/extractors/content_understanding.py`
Azure Content Understanding (Document Intelligence) client for OCR — converts PDF attachments to text.

### `src/models/shipment.py`
Pydantic data models for the internal shipment representation (`Shipment`, `ShipmentItem`, `RequiredFields`, `NiceToHaveFields`).

### `src/models/client_format.py`
Serializes the internal `Shipment` model to the Hyperion TMS client API JSON format (field renaming, equipment mode mapping, time window calculation, dimension parsing).

---

## Customer ID Resolution Flow

```
Email received
    │
    ├─ Is sender or receiver domain a known broker? ──► YES ─► Keyword search by customer name
    │   (ecogistics.org, kloeckner.com, nucorskyline.com)          │
    │                                                               ▼
    │                                                     Exact Python name match
    │                                                               │
    │                                                               ▼
    │                                                     Use customerId directly
    │                                                     (no OpenAI needed)
    │
    └─ Unknown sender ──► Vector search by sender domain (top 3, score ≥ 0.70)
                                    │
                                    ▼
                          Pass matches to OpenAI
                          → confirms which customer matches sender domain
                          → returns null if no confident match
```

---

## Azure Services Used

| Service | Purpose | Resource Name |
|---|---|---|
| Azure OpenAI | GPT extraction + embeddings | shepherdai-quadrant-openai |
| Azure Content Understanding | OCR / document text extraction | shepherdai-quadrant-foundry |
| Azure Blob Storage | Email storage + output JSON | shepherdaiquadstorage |
| Azure AI Search | Vector search on customer contacts | shepherdaisearch (Standard tier) |
| Hyperion TMS API | Customer contacts + shipment creation | 3pl.hyperiontms.com |

---

## Environment Variables

Create a `.env` file in the `backend/` directory with the following:

```env
# Azure OpenAI
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_OPENAI_KEY=<key>
AZURE_OPENAI_DEPLOYMENT=<gpt-deployment-name>
AZURE_OPENAI_API_VERSION=2024-02-15-preview
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small

# Azure Content Understanding (OCR)
AZURE_CONTENT_UNDERSTANDING_ENDPOINT=https://<your-resource>.services.ai.azure.com/
AZURE_CONTENT_UNDERSTANDING_KEY=<key>

# Azure Blob Storage
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...
AZURE_STORAGE_CONTAINER_NAME=sample-mails
AZURE_STORAGE_OUTPUT_CONTAINER=output-json

# Azure AI Search
AZURE_SEARCH_ENDPOINT=https://<your-search-resource>.search.windows.net
AZURE_SEARCH_KEY=<key>
AZURE_SEARCH_INDEX_NAME=customer-contacts

# Hyperion TMS (credentials are pre-configured, override if needed)
HYPERION_CLIENT_ID=<client-id>
HYPERION_CLIENT_SECRET=<client-secret>
```

---

## Azure AI Search Index Schema

Index name: `customer-contacts`

| Field | Type | Attributes |
|---|---|---|
| id | Edm.String | Key, Retrievable |
| customerId | Edm.Int32 | Retrievable |
| customerName | Edm.String | Searchable, Retrievable |
| email | Edm.String | Retrievable |
| emailDomain | Edm.String | Retrievable |
| contentVector | Collection(Edm.Single) | Vector (1536 dims, HNSW, cosine) |

The vector field uses the `text-embedding-3-small` model (1536 dimensions). Each contact is embedded as `"{customerName} {emailDomain}"`.

---

## Installation & Running

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
streamlit run streamlit_app.py
```

---

## Document Types Supported

| Document | Key Data Extracted |
|---|---|
| Bill of Lading (BOL) | Pickup/drop locations, shipment ID, items, carrier |
| Picking List | Items (individual line rows), delivery date (= pickup date), shipper info |
| Sales Order | Order number, PO number, items, ship-via (equipment mode) |
| Email body | Routing context, reference numbers, contacts, pickup date shorthand (e.g. "PU 3/5") |

---

## Important Extraction Rules

- **Items**: Every distinct line row in a document is extracted as a separate item — rows are never merged even if they share the same description
- **Picking List dates**: The "Date" near "Printed by [Name]" is the print date and is never used as pickup date. The "Delivery Date" field lower in the document is the actual pickup/ship date
- **Partial dates**: "PU 3/5" or "3/5" in the subject line → interpreted as March 5, 2026 (full ISO date)
- **Broker emails**: Broker domain detected in either the From or To field triggers the hardcoded mapping, bypassing vector search
- **Customer ID safety**: OpenAI returns `null` if no confident domain match is found — a wrong ID is treated as worse than no ID

---

## Output JSON Format

The final client JSON is structured for the Hyperion TMS API:

```json
{
  "customerId": 12345,
  "shipmentMode": "ftl",
  "equipmentType": "Flatbed",
  "shipperName": "Kloeckner Metals Corp - YRK",
  "shipperAddress": "420 Memory Ln",
  "shipperCity": "York",
  "shipperState": "PA",
  "shipperZip": "17402",
  "consigneeName": "...",
  "pickupOpen": "2026-03-05T08:00:00.000Z",
  "pickupClose": "2026-03-05T16:00:00.000Z",
  "items": [
    {
      "productDescription": "Coil Galvanized 18 GaNo Grade",
      "pieces": 4808,
      "weight": 55,
      "packaging": "truckloads"
    }
  ],
  "totalWeight": 41179,
  "specialInstructions": "Tarp Required RFC #: 82851812; Chains & Straps"
}
```
