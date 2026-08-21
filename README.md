# Shepherd AI — AI-Powered Shipment Orchestration Platform

An Azure-hosted, event-driven platform that ingests freight emails via Microsoft Graph API, extracts structured shipment data using Azure OpenAI + Document Intelligence, matches customers via Azure AI Search, and automatically creates shipments in the Brokerware TMS — with intelligent follow-up, multi-mailbox support, human-in-the-loop review, and a Streamlit operator dashboard.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Project Structure](#project-structure)
3. [Azure Services](#azure-services)
4. [Prerequisites](#prerequisites)
5. [Local Setup](#local-setup)
6. [Environment Variables](#environment-variables)
7. [Running Locally](#running-locally)
8. [Running Tests](#running-tests)
9. [Azure Function Triggers](#azure-function-triggers)
10. [Extraction Pipeline](#extraction-pipeline)
11. [Shipment Creation Logic](#shipment-creation-logic)
12. [End-to-End Scenarios](#end-to-end-scenarios)
13. [Follow-Up System](#follow-up-system)
14. [Conversation State & Thread Tracking](#conversation-state--thread-tracking)
15. [Multi-Mailbox Support](#multi-mailbox-support)
16. [Human-in-the-Loop Review Queue](#human-in-the-loop-review-queue)
17. [Multi-Tenant Brokerware Setup](#multi-tenant-brokerware-setup)
18. [Customer ID Resolution](#customer-id-resolution)
18. [Deployment](#deployment)
19. [Webhook Registration](#webhook-registration)
20. [Security](#security)
21. [Operational Commands](#operational-commands)

---

## Architecture Overview

```
Microsoft 365 Mailbox(es)
         │
         │  Graph API change notification (webhook)
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Azure API Management (APIM)                  │
│  Subscription key validation · Rate limiting · Route prefix     │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│               Azure Function App (shepherdai-funcapp)           │
│                                                                 │
│  graph_webhook  ──► Azure Storage Queue (email-notifications)   │
│  (HTTP trigger)                    │                            │
│                                    ▼                            │
│  process_email ◄─────────── Queue trigger                       │
│  │                                                              │
│  │  1. Fetch full email + attachments via Graph API             │
│  │  2. OCR attachments (Azure Content Understanding)            │
│  │  3. Extract structured fields (Azure OpenAI — 2-pass)        │
│  │  4. Resolve customer (AI Search + Brokerware API + maps)     │
│  │  5. Create shipment in Brokerware TMS (if fields complete)   │
│  │  6. Persist ConversationState (Azure Blob Storage)           │
│  │  7. Send follow-up reply if required fields are missing      │
│  │                                                              │
│  send_reminder_followups ── Timer (configurable, default 24h)   │
│  renew_subscriptions     ── Timer (every 47 hours)              │
│  poll_inbox_fallback     ── Timer (every 2 minutes)             │
│  register_webhooks       ── HTTP (function-level auth)          │
│  health                  ── HTTP (anonymous)                    │
└─────────────────────────────────────────────────────────────────┘
         │                          │
         ▼                          ▼
Azure Blob Storage           PostgreSQL (Azure DB)
  · ConversationState          · email_records (audit)
  · Processed logs             · customer_retry_config
  · Dedup markers
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│           Streamlit Web App (ShepherdAI-Quad-WebApp)            │
│  Operator dashboard — process emails, review queue,             │
│  conversation history, health monitor, manual follow-up         │
└─────────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
backend/
├── function_app.py              # Azure Functions entry point (all triggers)
├── streamlit_app.py             # Streamlit operator dashboard
├── run_scheduler.py             # Standalone scheduler (non-Azure environments)
├── startup.sh                   # Web App startup command
├── host.json                    # Azure Functions host configuration
├── local.settings.json          # Local dev env vars (never commit secrets)
├── requirements.txt             # Python dependencies
│
├── src/
│   ├── config.py                # Centralised Config class (reads all env vars)
│   ├── secrets.py               # Azure Key Vault secret loader
│   ├── main.py                  # Shared orchestration logic
│   │
│   ├── extractors/
│   │   ├── openai_agent.py      # Azure OpenAI extraction (2-pass + Brokerware fallbacks)
│   │   ├── content_understanding.py   # OCR via Azure Document Intelligence
│   │   └── reply_merger.py      # Merges reply thread context; computes missing fields
│   │
│   ├── models/
│   │   ├── shipment.py          # Pydantic: Shipment, RequiredFields, ShipmentItem
│   │   ├── client_format.py     # Serialize Shipment → Brokerware API JSON payload
│   │   ├── conversation_state.py  # ConversationState blob model + lifecycle events
│   │   ├── graph_models.py      # MailMessage, GraphConfig
│   │   └── review_request.py    # HITL ReviewRequest model
│   │
│   ├── services/
│   │   ├── graph_client.py              # Microsoft Graph API client
│   │   ├── brokerware_client.py         # Brokerware TMS API (multi-tenant)
│   │   ├── search_client.py             # Azure AI Search — customer matching
│   │   ├── context_search_client.py     # RAG context retrieval
│   │   ├── conversation_tracker.py      # Load/save ConversationState blobs; mailbox-scoped list_active()
│   │   ├── followup_orchestrator.py     # Follow-up logic; handle_initial_extraction / handle_reply
│   │   ├── followup_email_generator.py  # Follow-up email HTML generation
│   │   ├── webhook_subscription_manager.py  # Graph subscription lifecycle
│   │   ├── health_monitor.py            # System health checks + alerting
│   │   ├── hitl_router.py               # HITL routing triggers and decisions
│   │   ├── review_queue.py              # Review queue (Blob-backed)
│   │   └── scheduler.py                 # APScheduler jobs
│   │
│   ├── db/
│   │   ├── database.py             # SQLAlchemy engine + session factory
│   │   ├── models.py               # ORM models: EmailRecord, CustomerRetryConfig
│   │   ├── email_repository.py     # DB writes for email audit log
│   │   └── retry_config_repository.py   # Per-customer retry limit lookup
│   │
│   ├── security/
│   │   ├── apim_guard.py           # APIM subscription key validation
│   │   ├── rbac.py                 # JWT RS256 role-based access control
│   │   ├── input_validator.py      # Email body + attachment size limits
│   │   ├── prompt_guard.py         # Prompt injection detection
│   │   └── audit.py                # Structured audit event logging
│   │
│   ├── readers/                    # EML / attachment parsers
│   └── utils/                      # Logger and shared utilities
│
├── scripts/
│   └── test_reminder.py         # Manual follow-up reminder test runner
│
└── tests/
    ├── test_timeout_followup.py
    ├── test_health_monitor.py
    ├── test_scheduler.py
    ├── test_brokerware_client.py
    ├── test_client_format.py
    ├── test_config.py
    └── test_retry_config_repository.py
```

---

## Tenant Configuration

| | Tenant 1 — Shepherd | Tenant 2 — ShepherdWest |
|---|---|---|
| **Portal** | shepherd.brokerware.io | shepherdwest.brokerware.io |
| **Brokerware Client ID** | 4097939 | 4098017 |
| **Mailboxes monitored** | Shepherd@3plsystems0.onmicrosoft.com<br>Shepherd1@3plsystems0.onmicrosoft.com | Shepherd2@3plsystems0.onmicrosoft.com |

Emails arriving at `Shepherd@3plsystems0.onmicrosoft.com` or `Shepherd1@3plsystems0.onmicrosoft.com` are processed under the **Shepherd** tenant and matched against Shepherd customer contacts.

Emails arriving at `Shepherd2@3plsystems0.onmicrosoft.com` are processed under the **ShepherdWest** tenant.

---

## Azure Services

| Service | Purpose | Resource Name |
|---|---|---|
| Azure Function App | Event-driven backend (all triggers) | `shepherdai-funcapp` |
| Azure App Service (Web App) | Streamlit dashboard | `ShepherdAI-Quad-WebApp` |
| Azure OpenAI | GPT extraction + embeddings | `shepherdai-quadrant-openai` |
| Azure Content Understanding | OCR / Document Intelligence | `shepherdai-quadrant-foundry` |
| Azure Blob Storage | Email state, logs, dedup | `shepherdaiquadstorage` |
| Azure AI Search | Vector search — customer matching | `shepherdaisearch` |
| Azure Database for PostgreSQL | Audit log + retry config | `shepherddb.postgres.database.azure.com` |
| Azure API Management | Gateway, subscription key, rate limiting | `shepherdai-apim` |
| Azure AD (Entra ID) | OAuth2 app registration (Graph + RBAC) | Tenant: `39f80b2d-...` |
| Azure Key Vault | Secrets at rest (production) | Optional — set `KEY_VAULT_URL` |
| Application Insights | Telemetry, structured audit logs | `shepherdai-appinsights` |
| Azure Firewall | Egress control for VNet-integrated apps | `shepherdai-firewall` |
| Azure NAT Gateway | Static outbound IP for App subnet | `shepherdai-nat-gateway` (IP: `20.112.81.67`) |

### Storage Containers

| Container | Purpose |
|---|---|
| `sample-mails` | Incoming `.eml` files (blob-tab processing) |
| `output-json` | Extracted shipment JSON output |
| `processed-logs` | ConversationState blobs, dedup markers, HITL review queue |

### Storage Queue

| Queue | Purpose |
|---|---|
| `email-notifications` | Decouples Graph webhook → email processor |

### PostgreSQL Tables

| Table | Purpose |
|---|---|
| `email_records` | Full audit log of every processed email |
| `customer_retry_config` | Per-customer maximum follow-up count |

### Azure AI Search Index

Index name: `customer-contacts`

| Field | Type | Notes |
|---|---|---|
| `id` | `Edm.String` | Key |
| `customerId` | `Edm.Int32` | Brokerware customer ID |
| `customerName` | `Edm.String` | Searchable |
| `email` | `Edm.String` | |
| `emailDomain` | `Edm.String` | |
| `contentVector` | `Collection(Edm.Single)` | 1536 dims, HNSW cosine — `text-embedding-3-small` |

---

## Prerequisites

- Python 3.11+
- [Azure Functions Core Tools v4](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local)
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli)
- Access to the Azure subscription (`ShepherdAI-Quadrant-rg` resource group)
- PostgreSQL client (optional — for direct DB inspection)

---

## Local Setup

```bash
# 1. Clone the repository
git clone <repo-url>
cd backend

# 2. Create and activate a virtual environment
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy example settings and fill in credentials
cp local.settings.json.example local.settings.json
# Edit local.settings.json with your values
```

---

## Environment Variables

All variables live in `local.settings.json` for local dev, or in Azure Function App / App Service **Configuration > Application settings** for production. Sensitive values should be stored in **Azure Key Vault** (set `KEY_VAULT_URL`).

### Azure OpenAI

| Variable | Description | Example |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | OpenAI resource URL | `https://shepherdai-quadrant-openai.openai.azure.com/` |
| `AZURE_OPENAI_KEY` | API key | |
| `AZURE_OPENAI_DEPLOYMENT` | Chat model deployment name | `gpt-5-chat` |
| `AZURE_OPENAI_API_VERSION` | API version | `2024-02-15-preview` |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | Embedding model name | `text-embedding-3-small` |

### Azure Content Understanding (OCR)

| Variable | Description |
|---|---|
| `AZURE_CONTENT_UNDERSTANDING_ENDPOINT` | AI Foundry endpoint |
| `AZURE_CONTENT_UNDERSTANDING_KEY` | API key |

### Azure Blob Storage

| Variable | Description | Default |
|---|---|---|
| `AZURE_STORAGE_CONNECTION_STRING` | Full connection string | |
| `AZURE_STORAGE_CONTAINER_NAME` | Incoming email container | `sample-mails` |
| `AZURE_STORAGE_OUTPUT_CONTAINER` | Extracted JSON output container | `output-json` |
| `AZURE_STORAGE_LOGS_CONTAINER` | Conversation state + logs | `processed-logs` |
| `AZURE_STORAGE_QUEUE_NAME` | Processing queue name | `email-notifications` |
| `AzureWebJobsStorage` | Same connection string (Functions runtime) | |

### Azure AI Search

| Variable | Description | Default |
|---|---|---|
| `AZURE_SEARCH_ENDPOINT` | Search service URL | |
| `AZURE_SEARCH_KEY` | Admin key | |
| `AZURE_SEARCH_INDEX_NAME` | Customer contacts index | `customer-contacts` |
| `AZURE_SEARCH_CONTEXT_INDEX_NAME` | RAG context index | `shipment-context` |

### Azure AD / Microsoft Graph

| Variable | Description |
|---|---|
| `AZURE_AD_TENANT_ID` | Azure AD tenant GUID |
| `AZURE_AD_CLIENT_ID` | App registration client ID |
| `AZURE_AD_CLIENT_SECRET` | App registration secret |
| `AZURE_AD_AUDIENCE` | JWT audience (defaults to `AZURE_AD_CLIENT_ID`) |
| `AZURE_AD_JWKS_CACHE_TTL` | JWKS public key cache TTL seconds (default `3600`) |
| `GRAPH_REDIRECT_URI` | OAuth redirect URI (local dev) | `http://localhost:8501` |
| `GRAPH_MAILBOX_USER_ID` | Primary mailbox UPN (single-inbox compat) |
| `GRAPH_MAILBOX_USER_IDS` | Comma-separated mailbox UPNs (multi-inbox) |
| `GRAPH_WEBHOOK_NOTIFICATION_URL` | Public HTTPS URL for Graph webhook delivery |
| `GRAPH_WEBHOOK_CLIENT_STATE` | Shared secret echoed in every notification | `shepherd-ai-webhook` |

### Brokerware TMS (Multi-Tenant)

| Variable | Description |
|---|---|
| `BROKERWARE_MAILBOX_TENANT_MAP` | JSON: mailbox UPN → tenant key |
| `BROKERWARE_CUSTOMER_EMAIL_MAP` | JSON: sender email → per-tenant `customerId` map (static override) |
| `BROKERWARE_<KEY>_BASE_URL` | TMS base URL per tenant |
| `BROKERWARE_<KEY>_CLIENT_ID` | OAuth client ID per tenant |
| `BROKERWARE_<KEY>_CLIENT_SECRET` | OAuth client secret per tenant |
| `BROKERWARE_<KEY>_BROKER_CLIENT_ID` | Broker client ID per tenant |
| `BROKERWARE_<KEY>_USERNAME` | TMS username per tenant |
| `BROKERWARE_<KEY>_PASSWORD` | TMS password per tenant |

Example tenant keys: `SHEPHERD`, `SHEPHERDWEST`

`BROKERWARE_CUSTOMER_EMAIL_MAP` maps sender emails to a per-tenant `customerId`. It is checked **before** the live Brokerware API and takes highest priority. Format:

```json
{
  "jack3pl@outlook.com":      {"shepherd": 4098014, "shepherdwest": 5001},
  "orders@acmecargo.com":     {"shepherd": 12345},
  "dispatch@fastfreight.com": {"shepherd": 67890}
}
```

> The outer key is the sender email (case-insensitive). The inner key is the Brokerware tenant key (lowercase). The value is the `customerId` integer.

### PostgreSQL

| Variable | Description | Example |
|---|---|---|
| `DATABASE_URL` | Full SQLAlchemy connection URL | `postgresql+psycopg2://user:pass@host:5432/db?sslmode=require` |
| `DB_POOL_SIZE` | Connection pool size | `5` |
| `DB_MAX_OVERFLOW` | Max overflow connections | `10` |
| `DB_POOL_TIMEOUT` | Pool checkout timeout seconds | `30` |
| `DB_POOL_RECYCLE` | Connection recycle interval seconds | `300` |

### Security

| Variable | Description | Default |
|---|---|---|
| `APIM_SUBSCRIPTION_KEY` | APIM subscription key (from Key Vault) | |
| `APIM_SUBSCRIPTION_KEY_HEADER` | Header name APIM uses | `Ocp-Apim-Subscription-Key` |
| `APIM_STRICT_ENFORCEMENT` | Return 503 if key missing (vs warning) | `false` |
| `RBAC_ADMIN_ROLES` | Azure AD app roles for admin endpoints | `WebhookAdmin` |
| `KEY_VAULT_URL` | Key Vault URI (enables KV secret loading) | |
| `MAX_EMAIL_BODY_CHARS` | Max email body size in characters | `500000` |
| `MAX_ATTACHMENT_BYTES` | Max single attachment size in bytes | `26214400` (25 MB) |

### Follow-Up Reminders

| Variable | Description | Default |
|---|---|---|
| `FOLLOWUP_TIMER_SCHEDULE` | Azure cron for reminder timer | `0 0 */24 * * *` (24 hours) |
| `FOLLOWUP_REMINDER_INTERVAL_MINUTES` | Staleness threshold before resending | `5` (dev) / `1440` (prod) |
| `FOLLOWUP_DEFAULT_MAX` | Default max reminders per conversation | `3` |
| `FOLLOWUP_MAX_BY_CUSTOMER` | JSON override: `{"customerId": maxCount}` | `{}` |

### Scheduling

| Variable | Description | Default |
|---|---|---|
| `POLL_INBOX_SCHEDULE` | Azure cron for inbox polling fallback | `0 */2 * * * *` (every 2 min) |
| `FOLLOWUP_TIMER_SCHEDULE` | Azure cron for follow-up reminders | `0 */1 * * * *` (every 1 min) |

### Alerting

| Variable | Description |
|---|---|
| `ALERT_WEBHOOK_URL` | Teams / Slack webhook URL for health alerts |
| `ALERT_EMAIL_TO` | Recipient email for alert emails |
| `ALERT_FROM_EMAIL` | Sender UPN (must have `Mail.Send` Graph permission) |

### Observability

| Variable | Description |
|---|---|
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Application Insights connection string |
| `FUNCTIONS_WORKER_RUNTIME` | Must be `python` for Azure Functions |

---

## Running Locally

### Option 1 — Azure Functions Core Tools (full pipeline)

```bash
# Install Azure Functions Core Tools v4
npm install -g azure-functions-core-tools@4 --unsafe-perm true

# Start the Function App locally
func start

# Available at http://localhost:7071:
#   GET/POST  http://localhost:7071/api/graph_webhook
#   POST      http://localhost:7071/api/register_webhooks?code=<host-key>
#   GET       http://localhost:7071/api/health
```

### Option 2 — Streamlit UI only

```bash
venv\Scripts\activate         # Windows
source venv/bin/activate      # macOS / Linux

streamlit run streamlit_app.py
# Access at http://localhost:8501
```

### Option 3 — Background Scheduler (non-Azure environments)

```bash
python run_scheduler.py
```

---

## Running Tests

```bash
# Activate virtual environment
venv\Scripts\activate

# Run all tests
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_timeout_followup.py -v

# Run with coverage
python -m pytest tests/ --cov=src --cov-report=term-missing

# Manual reminder test script
python scripts/test_reminder.py --list
python scripts/test_reminder.py --dry-run
python scripts/test_reminder.py --send
python scripts/test_reminder.py --send --minutes 1
python scripts/test_reminder.py --send --conv-id <conversation_id>
```

### Test Files

| File | What It Tests |
|---|---|
| `test_timeout_followup.py` | `FollowupOrchestrator.send_timeout_followup()` — 17 test cases |
| `test_health_monitor.py` | Health check logic and alert triggering |
| `test_scheduler.py` | APScheduler job registration and execution |
| `test_brokerware_client.py` | Brokerware API client (multi-tenant routing) |
| `test_client_format.py` | Shipment → Brokerware JSON serialization |
| `test_config.py` | Config class environment variable loading |
| `test_retry_config_repository.py` | PostgreSQL retry config lookup |

---

## Azure Function Triggers

| Function | Trigger Type | Route / Schedule | Auth Level |
|---|---|---|---|
| `graph_webhook` | HTTP | `GET/POST /api/graph_webhook` | Anonymous |
| `process_email` | Queue | `email-notifications` queue | — |
| `send_reminder_followups` | Timer | `FOLLOWUP_TIMER_SCHEDULE` (default: every 24h) | — |
| `renew_subscriptions` | Timer | Every 47 hours | — |
| `poll_inbox_fallback` | Timer | `POLL_INBOX_SCHEDULE` (default: every 2 min) | — |
| `register_webhooks` | HTTP | `POST /api/register_webhooks` | Function key |
| `health` | HTTP | `GET /api/health` | Anonymous |

### Function Descriptions

**`graph_webhook`** — Receives Microsoft Graph change notifications when new emails arrive.
- `GET`: Returns `validationToken` for subscription handshake.
- `POST`: Validates `clientState`, checks for dedup marker, enqueues `message_id` to `email-notifications`.

**`process_email`** — Core pipeline. Dequeues a message ID and runs the full extraction, customer resolution, Brokerware submission, and follow-up flow.

**`send_reminder_followups`** — Finds all `awaiting_reply` conversations stale beyond the threshold and sends another follow-up reply in the original email thread.

**`renew_subscriptions`** — Renews Graph API subscriptions (max 72-hour lifetime) before they expire.

**`poll_inbox_fallback`** — Polls inbox every 2 minutes as a fallback against dropped webhook notifications.

**`register_webhooks`** — One-shot admin endpoint to create Graph subscriptions for all configured mailboxes.

**`health`** — Returns JSON health status of all subsystems (Graph, Blob, Queue, Brokerware, PostgreSQL).

---

## Extraction Pipeline

### Supported Document Types

| Document | Key Data Extracted |
|---|---|
| Bill of Lading (BOL) | Pickup/drop locations, shipment ID, items, carrier |
| Rate Confirmation | Load details, pickup/delivery dates, equipment, weight |
| Picking List | Line items, delivery date, shipper info |
| Sales Order | Order number, PO number, items, ship-via (equipment mode) |
| Brokerware-format JSON | All shipment fields including `shipperZip`, `consigneeZip`, `customerId` |
| Email body | Routing context, reference numbers, contacts, pickup date |

### 2-Pass Extraction Flow

```
Email body + all attachment text
         │
         ▼  Pass 1 — Envelope Extraction
Azure OpenAI
  → pickup / drop locations and dates
  → equipment type and mode
  → reference numbers
  → customer context
         │
         ├──────────────────────────────────────────────┐
         │                                              │
         ▼  Pass 2 — Detailed Shipment (per attachment) │
Azure OpenAI (per attachment + envelope as read-only)   │
  → line items (pieces, weight, description)            │
  → shipper / consignee address details                 │
  → special instructions                                │
         │                                              │
         ▼  Brokerware ZIP Fallback                     │
_apply_brokerware_fallback()                            │
  → parses Brokerware-format JSON from raw text         │
  → fills pickup zip_code from shipperZip if null       │
  → fills drop zip_code from consigneeZip if null       │
  → re-runs _compute_missing() after filling            │
         │                                              │
         ▼  Missing Field Validation                    │
ReplyMerger._compute_missing()                         ◄┘
  → checks all blocking required fields
  → populates missing_required_fields list
         │
         ▼
Customer ID Resolution (see next section)
         │
         ▼
Brokerware TMS
  → Create shipment if all required fields present
  → Return shipment ID
```

### Required Fields

All of the following must be present to create a Brokerware shipment:

| Field | Notes |
|---|---|
| `customerName` | The freight customer name |
| `pickupLocation.address` | Full address including zip code |
| `dropLocation.address` | Full address including zip code |
| `pickupDate` | ISO date string |
| `equipmentMode` | e.g. Flatbed, Dry Van, Reefer |
| `items` | At least one shipment item (warning-only — won't block submission) |

---

## Shipment Creation Logic

The core rule is simple: **if all required fields are present, create the shipment immediately. If any are missing, send a follow-up email.**

### Decision Flow

```
Email received
      │
      ▼
Is it a tracked customer reply?
      │
   YES ──► handle_reply()
      │         │
      │         ├── All fields now complete? ──► Submit to Brokerware ✅
      │         └── Still missing fields?    ──► Send next follow-up 📧
      │
   NO ──► Extract from email body / attachments
              │
              ├── All required fields present?
              │         ├── YES ──► Submit to Brokerware immediately ✅
              │         └── NO  ──► Send follow-up email 📧
              │                     Save ConversationState = "awaiting_reply"
              │
              └── Conversation already "complete" in blob?
                        └── YES ──► Load merged shipment from blob
                                    Submit to Brokerware ✅
```

### What "All Fields Present" Means

`ReplyMerger._compute_missing()` checks the required fields listed above. Fields returned in `missing_required_fields` that are not marked `warning_only` are **blocking** — shipment creation is skipped until they are filled. The `items` field is `warning_only` (submission proceeds even without line items).

---

## End-to-End Scenarios

These are the real-world scenarios the system handles, with what the operator sees in the Streamlit dashboard.

---

### Scenario 1 — Fresh email, all fields present

**Example:** Forwarded rate confirmation with attachment. Sender is in Brokerware contacts.

| Step | What happens |
|---|---|
| Email arrives | Graph webhook fires, message queued |
| OCR + extraction | All required fields extracted (pickup, drop, ZIP, date, equipment) |
| Customer lookup | Sender email matched → `customerId` resolved |
| Result | `Shipment created in Brokerware TMS — ID: 10014` |

**Dashboard:** Shows green "Shipment created" banner with Brokerware ID. No follow-up sent.

---

### Scenario 2 — Fresh email, missing fields

**Example:** Email body only, no attachment. Pickup ZIP code missing.

| Step | What happens |
|---|---|
| Email arrives | Graph webhook fires, message queued |
| Extraction | Pickup ZIP not found in email body |
| `_compute_missing()` | Flags `shipperZip` as missing (blocking) |
| Auto follow-up | System sends reply to sender: "Please provide: Pickup ZIP code" |
| Conversation saved | `status = "awaiting_reply"`, `mailbox_user_id` set |

**Dashboard:** Shows "Follow-up email #1 sent successfully. Waiting for customer reply."

---

### Scenario 3 — Customer replies with missing information

**Example:** Customer sends reply: "Zip is 77077"

| Step | What happens |
|---|---|
| Reply arrives | Correlated to original thread via `conversationId` |
| `handle_reply()` | Merges reply data into existing partial shipment |
| `_compute_missing()` | No blocking fields remaining |
| Result | `Shipment created in Brokerware TMS — ID: 10015` |
| Conversation saved | `status = "complete"` |

**Dashboard:** Shows "Thread complete — all required fields gathered across the conversation. Creating shipment from the merged thread…" then Brokerware ID.

---

### Scenario 4 — Customer replies but fields still missing

**Example:** Customer replies but forgets to include delivery date.

| Step | What happens |
|---|---|
| Reply arrives | Merged into partial shipment |
| `_compute_missing()` | `deliveryDate` still missing |
| Auto follow-up | System sends follow-up #2: "Please also provide: Delivery date" |
| Conversation saved | `followup_count` incremented, still `awaiting_reply` |

**Dashboard:** Shows "Follow-up email #2 sent successfully."

---

### Scenario 5 — Email contains Brokerware-format JSON (Jack's scenario)

**Example:** Sender forwards a Brokerware JSON payload in the email body:
```json
{
  "customerId": 4098014,
  "shipperZip": "77077",
  "consigneeZip": "78626",
  "shipmentStatus": "Booked",
  ...
}
```

| Step | What happens |
|---|---|
| Sender not in contacts | `find_customer_matches()` returns no match |
| Brokerware JSON fallback | Body parsed → `customerId = 4098014` extracted |
| ZIP fallback | `shipperZip` → `pickup.address.zip_code = "77077"` |
| ZIP fallback | `consigneeZip` → `drop.address.zip_code = "78626"` |
| Result | `Shipment created in Brokerware TMS — ID: <id>` |

**Dashboard:** Shows "Customer ID resolved from Brokerware JSON in email body: `4098014`" then Brokerware ID.

> To avoid relying on the body fallback, add the sender to `BROKERWARE_CUSTOMER_EMAIL_MAP`:
> ```json
> {"jack3pl@outlook.com": {"shepherd": 4098014}}
> ```

---

### Scenario 6 — Sender not in Brokerware contacts, no JSON fallback

**Example:** New customer emails in for a shipment. Sender domain not indexed in AI Search.

| Step | What happens |
|---|---|
| Customer lookup | Static map: no match. Live API: no match. JSON fallback: no `customerId` in body |
| HITL trigger | `no_customer_match` → routed to Review Queue |
| Operator action | Opens Review Queue, enters Brokerware Customer ID, clicks Approve |
| Result | `Shipment created in Brokerware TMS — ID: <id>` |

**Dashboard (Review Queue):** Shows routing reason "Customer could not be matched", editable fields form, **Brokerware Customer ID** input, Approve / Reject buttons.

> After approving, add the sender to `BROKERWARE_CUSTOMER_EMAIL_MAP` so future emails from them are auto-resolved.

---

### Scenario 7 — Max follow-ups reached, no response

**Example:** System sent 3 follow-up emails. Customer never replied.

| Step | What happens |
|---|---|
| After follow-up #3 | `followup_count >= max_followups` |
| Status updated | `status = "max_retries_reached"` |
| HITL trigger | `max_retries` → routed to Review Queue |
| Operator action | Reviews extracted data, fills any missing fields, clicks Approve |
| Result | Shipment created or rejected |

**Dashboard:** Shows "Maximum follow-ups reached (3). This shipment requires human review." with "Route to Review Queue" button.

---

### Scenario 8 — Same email re-processed (conversation already complete)

**Example:** Operator processes an email that was already completed in a previous session. Conversation blob has `status = "complete"`.

| Step | What happens |
|---|---|
| Correlation | `get_active_state()` returns `None` (not "awaiting_reply") |
| Fresh email path | `submit_shipments = True` |
| If all fields extracted | Normal submission at `process_graph_email` |
| If LLM misses fields | Auto-followup loop calls `handle_initial_extraction` → early-return `action = "complete"` |
| Fallback | Loads merged shipment from blob (`_cs.partial_shipment`) → submits to Brokerware |

**Dashboard:** Shows "Conversation already complete — submitting merged shipment to Brokerware TMS…" then Brokerware ID.

---

### Scenario 9 — Two mailboxes, same sender sends multiple different loads

**Example:** `jack3pl@outlook.com` sends "CertainTeed Jonesburg" email to mailbox A, then sends "Nucor Yamato" email to mailbox B. Mailbox A is awaiting a reply for the Jonesburg thread.

**Without mailbox scoping (old behavior):**
- "Nucor Yamato" email correlated to the Jonesburg thread (60% sender confidence)
- Follow-up #2 sent to wrong thread
- Nucor Yamato's content never independently extracted → FAIL

**With mailbox scoping (current behavior):**
- Jonesburg thread has `mailbox_user_id = "mailboxA@domain.com"`
- `list_active(mailbox_upn="mailboxB@domain.com")` excludes Jonesburg thread
- Nucor Yamato email starts its own fresh conversation on mailbox B → PASS

**Key config:** `GRAPH_MAILBOX_USER_IDS` must list all mailbox UPNs. Each conversation stores its originating `mailbox_user_id` at creation time.

---

### Scenario 10 — HITL approve with customer ID for no-match

**Example:** "Wire Mesh Sales Jacksonville, FL" emails in. Not in contacts.

| Step | What happens |
|---|---|
| No match | Routed to Review Queue with `no_customer_match` |
| Operator opens queue | Sees "Customer could not be matched" flag |
| Operator enters | `Customer ID: 4098021` in the number input |
| Approve clicked | `_submit_to_brokerware(shipment, customer_id=4098021)` |
| Result | `Shipment created in Brokerware TMS — ID: 10021` |

---

## Follow-Up System

When required fields are missing, the system automatically sends a follow-up reply in the original email thread asking the customer to provide the missing information.

### How It Works

1. **Initial extraction** → `_compute_missing()` flags all missing fields upfront (including ZIP codes via the Brokerware ZIP fallback).

2. **Single follow-up per email** → Even if an email has multiple attachments, the system sends **one** follow-up reply containing the union of all missing fields across every attachment. It never sends one reply per attachment.

3. **`ConversationState` saved** to Blob with `status = "awaiting_reply"`, `missing_fields` list, and `mailbox_user_id` (for mailbox scoping).

4. **Re-processing guard** → If the same original email is re-queued (e.g. the inbox poller picks it up again), `handle_initial_extraction` checks the existing state first:
   - Status `complete` → skip, submit merged shipment from blob.
   - Status `awaiting_reply` with followup already sent → skip, show existing status.
   - Status `max_retries_reached` → skip, show error.

5. **Customer replies** → Incoming reply is correlated to the original thread by conversation ID, reference IDs, subject similarity, or sender. The reply data is merged into the existing partial shipment. If all fields are now present, the shipment is submitted to Brokerware.

6. **Reminder timer** (`send_reminder_followups`) → Fires on `FOLLOWUP_TIMER_SCHEDULE`. Finds all `awaiting_reply` conversations stale longer than `FOLLOWUP_REMINDER_INTERVAL_MINUTES` and sends another follow-up reply.

7. **Max retries** → When `followup_count >= max_followups`, status changes to `max_retries_reached` and the conversation is routed to the HITL Review Queue.

### Per-Customer Follow-Up Limits

Limits are resolved in priority order:

1. `customer_retry_config` table in PostgreSQL (per `customerId`)
2. `FOLLOWUP_MAX_BY_CUSTOMER` env var JSON: `{"customerId": maxCount}`
3. `FOLLOWUP_DEFAULT_MAX` global default (default: `3`)

---

## Conversation State & Thread Tracking

Every email thread the system sends a follow-up for has a `ConversationState` blob saved to `processed-logs/conversations/<conversation_id>.json`.

### ConversationState Fields

| Field | Description |
|---|---|
| `conversation_id` | Graph `conversationId` of the email thread |
| `status` | `processing` → `awaiting_reply` → `complete` \| `max_retries_reached` |
| `partial_shipment` | The current best-merged Shipment (updated after each reply) |
| `missing_fields` | List of field names still missing |
| `followup_count` | Number of follow-up emails sent so far |
| `max_followups` | Limit for this conversation (from per-customer config) |
| `customer_id` | Resolved Brokerware `customerId` (set at conversation creation) |
| `mailbox_user_id` | The mailbox UPN that received the original email (mailbox scoping) |
| `sender_email` | Original sender |
| `subject` | Original email subject |

### Lifecycle Events

Events are appended to `ConversationState.events` for full audit traceability:

- `email_classified` — email type identified (tender, quote, spam)
- `extraction_complete` — OpenAI extraction finished
- `customer_id_resolved` — Brokerware customer resolved
- `followup_generated` — follow-up email body generated
- `followup_sent` — follow-up reply sent via Graph
- `data_merged` — customer reply merged into partial shipment
- `validation_passed` — all required fields now present
- `conversation_complete` — shipment ready / submitted
- `max_retries_reached` — follow-up limit hit

### Reply Correlation Strategies (in priority order)

1. **Conversation ID** — exact match on Graph `conversationId`
2. **Reference ID** — load/PO/BOL number referenced in both emails
3. **Subject similarity** — normalized subject line fuzzy match
4. **Sender + subject** — same sender email + similar subject

Only conversations with `status == "awaiting_reply"` and matching `mailbox_user_id` are candidates for correlation (see [Multi-Mailbox Support](#multi-mailbox-support)).

---

## Multi-Mailbox Support

Shepherd AI supports multiple Microsoft 365 mailboxes simultaneously. Each mailbox is independently tracked so that follow-up conversations from one mailbox never interfere with another.

### Configuration

```bash
# Comma-separated list of mailbox UPNs to monitor
GRAPH_MAILBOX_USER_IDS=shepherd@3plsystems0.onmicrosoft.com,shepherd2@3plsystems0.onmicrosoft.com
```

### Mailbox Scoping

When a `ConversationState` is created, the `mailbox_user_id` field is set to the UPN of the mailbox that received the original email. When `list_active()` is called during reply correlation, it filters to only return conversations from the **same mailbox**:

- Conversations with a known `mailbox_user_id` that **differs** from the current mailbox are excluded.
- Conversations with no `mailbox_user_id` (legacy states created before this feature) are **always included** for backwards compatibility.

This prevents a common bug where multiple emails from the same sender arriving at different mailboxes get mis-correlated into the wrong thread.

---

## Human-in-the-Loop Review Queue

When automated processing cannot complete a shipment, the case is routed to the HITL Review Queue where an operator can review and approve it.

### Routing Triggers (in priority order)

| Trigger | Condition |
|---|---|
| `max_retries` | Customer did not respond after N follow-up attempts |
| `send_error` | Follow-up email could not be sent (Graph API failure) |
| `no_customer_match` | No Brokerware customer matched to the sender email/domain |
| `low_confidence` | Extraction confidence below threshold with missing fields |

### Review Queue Workflow

1. Shipment is routed to the queue and saved as a `ReviewRequest` blob.
2. Operator opens the **Review Queue** panel in the Streamlit dashboard.
3. For each pending review, the operator sees:
   - Routing reason and flags
   - Extracted shipment data (read-only reference)
   - Editable form for any missing fields
   - **Brokerware Customer ID** input (shown for `no_customer_match` cases)
   - Reviewer notes field
4. Operator clicks **Approve & Complete**:
   - Reviewer-supplied field values are merged into the shipment
   - Shipment is submitted to Brokerware TMS immediately
   - `ConversationState` is updated to `status = "complete"`
5. Operator clicks **Reject** to discard the review.

### Customer ID for No-Match Cases

For `no_customer_match` reviews, the Review Queue form shows a **Brokerware Customer ID** number input. The operator enters the correct `customerId`, which is used for the Brokerware submission. After approval, this ID can be added to `BROKERWARE_CUSTOMER_EMAIL_MAP` to prevent the same sender triggering a manual review in the future.

---

## Customer ID Resolution

Customer ID resolution happens in this priority order for every incoming email:

### 1. Static Email Map (highest priority)

`BROKERWARE_CUSTOMER_EMAIL_MAP` env var is checked first. If the sender email matches an entry, the `customerId` for the current tenant is used directly — no API call made.

```json
{"jack3pl@outlook.com": {"shepherd": 4098014}}
```

### 2. Live Brokerware Contacts API

If the sender is not in the static map, the system queries the Brokerware `CustomerContacts` API:
- Tries exact email match first
- Falls back to domain-based matching if a single customer owns the domain
- Result is cached in memory for the session

### 3. Brokerware JSON in Email Body (fallback)

If both the static map and live API return no match, and the email body contains a Brokerware-format JSON block with a `customerId` field, that value is used:

```json
{"customerId": 4098014, "shipperZip": "77077", "consigneeZip": "78626", ...}
```

### 4. No match → HITL

If no customer ID can be resolved for a `shipment_tender` email, the case is routed to the HITL Review Queue with reason `no_customer_match`.

---

## Multi-Tenant Brokerware Setup

Shepherd AI routes Brokerware API calls to the correct tenant based on which mailbox received the email.

### Tenant Configuration

| | Tenant 1 — Shepherd | Tenant 2 — ShepherdWest |
|---|---|---|
| **Portal** | shepherd.brokerware.io | shepherdwest.brokerware.io |
| **Brokerware Client ID** | 4097939 | 4098017 |
| **Mailboxes monitored** | Shepherd@3plsystems0.onmicrosoft.com<br>Shepherd1@3plsystems0.onmicrosoft.com | Shepherd2@3plsystems0.onmicrosoft.com |

Emails arriving at `Shepherd@3plsystems0.onmicrosoft.com` or `Shepherd1@3plsystems0.onmicrosoft.com` are processed under the **Shepherd** tenant and matched against Shepherd customer contacts.

Emails arriving at `Shepherd2@3plsystems0.onmicrosoft.com` are processed under the **ShepherdWest** tenant.

### Configuration

Set `BROKERWARE_MAILBOX_TENANT_MAP` to map lowercase mailbox UPNs to tenant keys:

```json
{
  "shepherd@3plsystems0.onmicrosoft.com":  "shepherd",
  "shepherd1@3plsystems0.onmicrosoft.com": "shepherd",
  "shepherd2@3plsystems0.onmicrosoft.com": "shepherdwest"
}
```

For each unique tenant key, provide these env vars (key uppercased as infix):

```bash
# Tenant: shepherd
BROKERWARE_SHEPHERD_BASE_URL="https://shepherd.brokerware.io"
BROKERWARE_SHEPHERD_CLIENT_ID="<oauth-client-id>"
BROKERWARE_SHEPHERD_CLIENT_SECRET="<oauth-client-secret>"
BROKERWARE_SHEPHERD_BROKER_CLIENT_ID="<broker-client-id>"
BROKERWARE_SHEPHERD_USERNAME="shepherd@3plsystems.com"
BROKERWARE_SHEPHERD_PASSWORD="<password>"

# Tenant: shepherdwest
BROKERWARE_SHEPHERDWEST_BASE_URL="https://shepherdwest.brokerware.io"
BROKERWARE_SHEPHERDWEST_CLIENT_ID="<oauth-client-id>"
BROKERWARE_SHEPHERDWEST_CLIENT_SECRET="<oauth-client-secret>"
BROKERWARE_SHEPHERDWEST_BROKER_CLIENT_ID="<broker-client-id>"
BROKERWARE_SHEPHERDWEST_USERNAME="shepherdwest@3plsystems.com"
BROKERWARE_SHEPHERDWEST_PASSWORD="<password>"
```

`BrokerwareClient` selects the correct tenant credentials based on the `mailbox_upn` of the incoming email.

---

## Deployment

### Prerequisites

```bash
az login
az account set --subscription "<subscription-id-or-name>"
```

### Deploy Function App

```bash
# From the backend/ directory
func azure functionapp publish shepherdai-funcapp --python

# Verify
az functionapp show \
  --name shepherdai-funcapp \
  --resource-group ShepherdAI-Quadrant-rg \
  --query "state" -o tsv
```

### Deploy Streamlit Web App

```bash
# Create deployment zip (cross-platform — works on Windows and Linux)
python -c "
import zipfile, os, pathlib
skip = {'.git','venv','__pycache__','output','.pytest_cache'}
with zipfile.ZipFile('deploy_webapp.zip','w',zipfile.ZIP_DEFLATED) as z:
    for p in pathlib.Path('.').rglob('*'):
        if any(s in p.parts for s in skip): continue
        if p.suffix == '.pyc': continue
        if p.is_file(): z.write(p)
print('Done')
"

# Deploy to App Service
az webapp deploy \
  --name ShepherdAI-Quad-WebApp \
  --resource-group ShepherdAI-Quadrant-rg \
  --src-path deploy_webapp.zip \
  --type zip \
  --async true

# Verify
az webapp show \
  --name ShepherdAI-Quad-WebApp \
  --resource-group ShepherdAI-Quadrant-rg \
  --query "state" -o tsv

# Tail live logs
az webapp log tail \
  --name ShepherdAI-Quad-WebApp \
  --resource-group ShepherdAI-Quadrant-rg
```

> **Note:** Delete `deploy_webapp.zip` after deployment — do not commit it to git.

The Web App uses `startup.sh`:
```bash
python -m streamlit run streamlit_app.py --server.port 8000 --server.address 0.0.0.0
```

### Configure App Settings (Production)

```bash
# Function App
az functionapp config appsettings set \
  --name shepherdai-funcapp \
  --resource-group ShepherdAI-Quadrant-rg \
  --settings \
    AZURE_OPENAI_ENDPOINT="https://shepherdai-quadrant-openai.openai.azure.com/" \
    AZURE_OPENAI_DEPLOYMENT="gpt-5-chat" \
    AZURE_OPENAI_API_VERSION="2024-02-15-preview" \
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT="text-embedding-3-small" \
    AZURE_SEARCH_INDEX_NAME="customer-contacts" \
    GRAPH_WEBHOOK_NOTIFICATION_URL="https://shepherdai-apim.azure-api.net/functions/graph_webhook" \
    GRAPH_WEBHOOK_CLIENT_STATE="shepherd-ai-webhook" \
    FOLLOWUP_TIMER_SCHEDULE="0 0 */24 * * *" \
    FOLLOWUP_REMINDER_INTERVAL_MINUTES="1440" \
    FOLLOWUP_DEFAULT_MAX="3" \
    APIM_STRICT_ENFORCEMENT="true"

# Web App
az webapp config appsettings set \
  --name ShepherdAI-Quad-WebApp \
  --resource-group ShepherdAI-Quadrant-rg \
  --settings \
    WEBSITES_PORT="8000" \
    AZURE_OPENAI_DEPLOYMENT="gpt-5-chat"
```

---

## Webhook Registration

After deploying the Function App for the first time (or after rotating secrets), register Graph subscriptions for all configured mailboxes:

```bash
FUNC_KEY=$(az functionapp keys list \
  --name shepherdai-funcapp \
  --resource-group ShepherdAI-Quadrant-rg \
  --query "functionKeys.default" -o tsv)

curl -X POST \
  "https://shepherdai-funcapp.azurewebsites.net/api/register_webhooks?code=${FUNC_KEY}" \
  -H "Content-Type: application/json"
```

The function creates one Graph subscription per mailbox listed in `GRAPH_MAILBOX_USER_IDS`. Subscriptions expire after 72 hours and are renewed automatically by `renew_subscriptions`.

---

## Security

### APIM Subscription Key

All inbound requests pass through Azure API Management. APIM injects the subscription key; `apim_guard.py` validates it using constant-time comparison.

- Set `APIM_SUBSCRIPTION_KEY` (from Key Vault: `apim-subscription-key`)
- Set `APIM_STRICT_ENFORCEMENT=true` in production to block traffic when the key is absent

### JWT / RBAC

Admin endpoints (e.g. `register_webhooks`) require a valid Azure AD JWT (RS256) with the `WebhookAdmin` app role.

- `RBAC_ADMIN_ROLES` — comma-separated list of allowed roles (default: `WebhookAdmin`)
- JWKS public keys are fetched from Azure AD and cached for `AZURE_AD_JWKS_CACHE_TTL` seconds

### Azure Key Vault Integration

Set `KEY_VAULT_URL` to enable automatic secret loading via `DefaultAzureCredential` (Managed Identity in Azure, CLI credentials locally):

```bash
KEY_VAULT_URL=https://shepherd-ai-kv.vault.azure.net/
```

Secrets use hyphens in Key Vault (e.g. `azure-openai-key`); `secrets.py` maps them to environment variables.

### Input Validation

- `MAX_EMAIL_BODY_CHARS` — rejects email bodies exceeding limit (default 500,000 chars)
- `MAX_ATTACHMENT_BYTES` — rejects attachments exceeding limit (default 25 MB)
- `prompt_guard.py` — detects and blocks prompt injection in email content

### Audit Logging

All security events, extraction results, and follow-up actions are logged as structured JSON to Application Insights via `audit.py`.

---

## Operational Commands

### Check Resource Status

```bash
# Function App state
az functionapp show \
  --name shepherdai-funcapp \
  --resource-group ShepherdAI-Quadrant-rg \
  --query "state" -o tsv

# Web App state
az webapp show \
  --name ShepherdAI-Quad-WebApp \
  --resource-group ShepherdAI-Quadrant-rg \
  --query "state" -o tsv
```

### Live Logs

```bash
# Function App
func azure functionapp logstream shepherdai-funcapp

# Web App
az webapp log tail \
  --name ShepherdAI-Quad-WebApp \
  --resource-group ShepherdAI-Quadrant-rg
```

### Restart Services

```bash
az functionapp restart \
  --name shepherdai-funcapp \
  --resource-group ShepherdAI-Quadrant-rg

az webapp restart \
  --name ShepherdAI-Quad-WebApp \
  --resource-group ShepherdAI-Quadrant-rg
```

### Health Check

```bash
# Direct
curl https://shepherdai-funcapp.azurewebsites.net/api/health

# Via APIM
curl https://shepherdai-apim.azure-api.net/functions/health \
  -H "Ocp-Apim-Subscription-Key: <key>"
```

### Git Operations

```bash
git branch
git push origin chinmaya
git log --oneline -10
```

### PostgreSQL

```bash
# Connect
psql "postgresql://shephardai:<password>@shepherddb.postgres.database.azure.com:5432/postgres?sslmode=require"

# Email audit log
SELECT id, sender_email, subject, status, created_at FROM email_records ORDER BY created_at DESC LIMIT 20;

# Per-customer retry config
SELECT * FROM customer_retry_config;

# Update retry limit
UPDATE customer_retry_config SET retry_count = 5 WHERE client_id = '<id>';
```

### Azure Blob Storage

```bash
# List ConversationState blobs
az storage blob list \
  --container-name processed-logs \
  --connection-string "<conn-str>" \
  --prefix "conversations/" \
  --output table

# Download a specific conversation
az storage blob download \
  --container-name processed-logs \
  --name "conversations/<conversation_id>.json" \
  --file conversation.json \
  --connection-string "<conn-str>"
```

### Subscription Management

```bash
# View active subscriptions via health endpoint
curl https://shepherdai-funcapp.azurewebsites.net/api/health | python -m json.tool

# Subscriptions auto-renew every 47 hours via renew_subscriptions timer
```

### Azure Firewall Egress Rules

The Web App runs in `snet-app` (`10.0.0.0/24`) with all egress routed through `shepherdai-firewall`. Any new external FQDN the app needs to reach must be explicitly allowed.

**Step 1** — Export current firewall policy rules:
```bash
az rest --method GET \
  --url "https://management.azure.com/subscriptions/<sub-id>/resourceGroups/ShepherdAI-Quadrant-rg/providers/Microsoft.Network/firewallPolicies/shepherdai-firewall-policy/ruleCollectionGroups/DefaultApplicationRuleCollectionGroup?api-version=2023-11-01" \
  --output json > fw_current.json
```

**Step 2** — Edit `fw_current.json`, add a new entry under `Allow-ShepherdAI-Outbound`:
```json
{
  "name": "allow-<service>",
  "ruleType": "ApplicationRule",
  "sourceAddresses": ["10.0.0.0/24"],
  "targetFqdns": ["your-service.example.com"],
  "protocols": [{"protocolType": "Https", "port": 443}],
  "terminateTLS": false
}
```

**Step 3** — Apply the update:
```bash
az rest --method PUT \
  --url "https://management.azure.com/subscriptions/<sub-id>/resourceGroups/ShepherdAI-Quadrant-rg/providers/Microsoft.Network/firewallPolicies/shepherdai-firewall-policy/ruleCollectionGroups/DefaultApplicationRuleCollectionGroup?api-version=2023-11-01" \
  --body @fw_update.json
```

> **Important:** Source must always be `10.0.0.0/24` (the `snet-app` subnet). An incorrect CIDR causes silent default-deny, manifesting as an `SSLEOFError` during TLS handshake.
