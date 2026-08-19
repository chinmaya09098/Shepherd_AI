# Shepherd AI — AI-Powered Shipment Orchestration Platform

An Azure-hosted, event-driven platform that ingests freight emails via Microsoft Graph API, extracts structured shipment data using Azure OpenAI + Document Intelligence, matches customers via Azure AI Search, and creates shipments in the Brokerware TMS — all with automated follow-up and a Streamlit operator dashboard.

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
10. [Deployment](#deployment)
11. [Webhook Registration](#webhook-registration)
12. [Multi-Tenant Brokerware Setup](#multi-tenant-brokerware-setup)
13. [Follow-Up System](#follow-up-system)
14. [Security](#security)
15. [Operational Commands](#operational-commands)

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
│  │  1. Fetch full email via Graph API                           │
│  │  2. OCR attachments (Azure Content Understanding)            │
│  │  3. Extract fields (Azure OpenAI — 2-pass)                   │
│  │  4. Match customer (Azure AI Search RAG)                     │
│  │  5. Create shipment (Brokerware TMS API)                     │
│  │  6. Persist ConversationState (Azure Blob)                   │
│  │  7. Send follow-up reply if fields missing                   │
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
│  Operator dashboard — review queue, manual processing,          │
│  conversation history, health monitor                           │
└─────────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
backend/
├── function_app.py              # Azure Functions entry point (all triggers)
├── streamlit_app.py             # Streamlit operator dashboard
├── run_scheduler.py             # Standalone scheduler (non-Azure environments)
├── startup.sh                  # Web App startup command
├── host.json                   # Azure Functions host configuration
├── local.settings.json         # Local dev env vars (never commit secrets)
├── requirements.txt            # Python dependencies
│
├── src/
│   ├── config.py               # Centralised Config class (reads all env vars)
│   ├── secrets.py              # Azure Key Vault secret loader
│   ├── main.py                 # Shared orchestration logic
│   │
│   ├── extractors/
│   │   ├── openai_agent.py     # Azure OpenAI extraction (2-pass)
│   │   ├── content_understanding.py  # OCR via Document Intelligence
│   │   └── reply_merger.py     # Merges multi-reply thread context
│   │
│   ├── models/
│   │   ├── shipment.py         # Pydantic: Shipment, ShipmentItem
│   │   ├── client_format.py    # Serialize Shipment → Brokerware API JSON
│   │   ├── conversation_state.py  # ConversationState blob model
│   │   ├── graph_models.py     # GraphConfig, GraphMessage
│   │   └── review_request.py   # HITL review queue model
│   │
│   ├── services/
│   │   ├── graph_client.py         # Microsoft Graph API client
│   │   ├── brokerware_client.py    # Brokerware TMS API client (multi-tenant)
│   │   ├── search_client.py        # Azure AI Search (customer matching)
│   │   ├── context_search_client.py  # RAG context retrieval
│   │   ├── conversation_tracker.py   # Load/save ConversationState blobs
│   │   ├── followup_orchestrator.py  # Follow-up email logic
│   │   ├── followup_email_generator.py  # Follow-up email HTML generator
│   │   ├── webhook_subscription_manager.py  # Graph subscription lifecycle
│   │   ├── health_monitor.py       # System health checks + alerting
│   │   ├── hitl_router.py          # Human-in-the-loop review routing
│   │   ├── review_queue.py         # Review queue (Blob-backed)
│   │   ├── scheduler.py            # APScheduler jobs
│   │   └── hyperion_client.py      # Hyperion TMS (legacy)
│   │
│   ├── db/
│   │   ├── database.py             # SQLAlchemy engine + session factory
│   │   ├── models.py               # ORM models: EmailRecord, CustomerRetryConfig
│   │   ├── email_repository.py     # DB writes for email audit log
│   │   └── retry_config_repository.py  # Per-customer retry limit lookup
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
│   └── test_reminder.py        # Manual follow-up reminder test runner
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
| `sample-mails` | Incoming `.eml` files |
| `output-json` | Extracted shipment JSON output |
| `processed-logs` | ConversationState blobs + dedup markers |

### Storage Queue

| Queue | Purpose |
|---|---|
| `email-notifications` | Decouples webhook → processor |

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

All variables live in `local.settings.json` for local dev, or in Azure Function App / App Service **Configuration > Application settings** for production. In production, sensitive values should be stored in **Azure Key Vault** (set `KEY_VAULT_URL`).

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
| `BROKERWARE_CUSTOMER_EMAIL_MAP` | JSON: sender email → `customerId` (static fallback override) |
| `BROKERWARE_<KEY>_BASE_URL` | TMS base URL per tenant |
| `BROKERWARE_<KEY>_CLIENT_ID` | OAuth client ID per tenant |
| `BROKERWARE_<KEY>_CLIENT_SECRET` | OAuth client secret per tenant |
| `BROKERWARE_<KEY>_BROKER_CLIENT_ID` | Broker client ID per tenant |
| `BROKERWARE_<KEY>_USERNAME` | TMS username per tenant |
| `BROKERWARE_<KEY>_PASSWORD` | TMS password per tenant |

Example tenant keys: `SHEPHERD`, `SHEPHERDWEST`

`BROKERWARE_CUSTOMER_EMAIL_MAP` is a JSON string used as a static fallback when AI Search cannot resolve a customer from the sender email domain. Example:
```json
{"orders@acmecargo.com": 12345, "dispatch@fastfreight.com": 67890}
```

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

# The following triggers are available at http://localhost:7071:
#   GET/POST  http://localhost:7071/api/graph_webhook
#   POST      http://localhost:7071/api/register_webhooks?code=<host-key>
#   GET       http://localhost:7071/api/health
```

### Option 2 — Streamlit UI only

```bash
# Activate virtual environment first
venv\Scripts\activate         # Windows
source venv/bin/activate      # macOS / Linux

# Run Streamlit dashboard
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

# Run with coverage report
python -m pytest tests/ --cov=src --cov-report=term-missing

# Run the manual reminder test script
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

### Function descriptions

**`graph_webhook`** — Microsoft Graph sends change notifications here when new emails arrive.
- `GET`: Returns `validationToken` query param for subscription handshake.
- `POST`: Validates `clientState`, checks for dedup marker, enqueues `message_id` to `email-notifications`.

**`process_email`** — Core pipeline. Dequeues a message ID and runs:
1. Fetch full email + attachments via Graph API
2. OCR attachments with Azure Content Understanding
3. 2-pass extraction with Azure OpenAI
4. Customer lookup via Azure AI Search (vector) + Brokerware API
5. Create shipment in Brokerware TMS
6. Persist `ConversationState` to Blob Storage
7. Send follow-up reply email if required fields are missing

**`send_reminder_followups`** — Finds all `awaiting_reply` conversations stale beyond the threshold and sends another reminder reply in the original email thread.

**`renew_subscriptions`** — Renews Graph API subscriptions (max 72-hour lifetime) before they expire.

**`poll_inbox_fallback`** — Polls inbox every 2 minutes as a fallback against dropped webhook notifications. Uses a checkpoint blob to track the last-polled timestamp.

**`register_webhooks`** — One-shot admin endpoint to create Graph subscriptions for all configured mailboxes.

**`health`** — Returns JSON health status of all subsystems (Graph, Blob, Queue, Brokerware, PostgreSQL).

---

## Deployment

### Prerequisites

```bash
# Login to Azure
az login

# Set active subscription (if needed)
az account set --subscription "<subscription-id-or-name>"
```

### Deploy Function App

```bash
# From the backend/ directory
func azure functionapp publish shepherdai-funcapp --python

# Verify deployment
az functionapp show --name shepherdai-funcapp --resource-group ShepherdAI-Quadrant-rg --query "state" -o tsv
```

### Deploy Streamlit Web App

```bash
# Create deployment zip (Python — works on Windows and Linux)
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

# Verify deployment
az webapp show --name ShepherdAI-Quad-WebApp --resource-group ShepherdAI-Quadrant-rg --query "state" -o tsv

# Tail live logs
az webapp log tail --name ShepherdAI-Quad-WebApp --resource-group ShepherdAI-Quadrant-rg
```

> **Note:** Remove `deploy_webapp.zip` after deployment — it should not be committed to git.

The Web App uses `startup.sh` which runs:
```bash
python -m streamlit run streamlit_app.py --server.port 8000 --server.address 0.0.0.0
```

### Configure App Settings (Production)

```bash
# Function App — set all environment variables
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

# Web App — set environment variables
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
# Get the Function App host key
FUNC_KEY=$(az functionapp keys list \
  --name shepherdai-funcapp \
  --resource-group ShepherdAI-Quadrant-rg \
  --query "functionKeys.default" -o tsv)

# Register webhooks
curl -X POST \
  "https://shepherdai-funcapp.azurewebsites.net/api/register_webhooks?code=${FUNC_KEY}" \
  -H "Content-Type: application/json"
```

The function creates one Graph subscription per mailbox listed in `GRAPH_MAILBOX_USER_IDS`. Subscriptions expire after 72 hours and are renewed automatically by the `renew_subscriptions` timer trigger.

---

## Multi-Tenant Brokerware Setup

Shepherd AI supports multiple Brokerware tenants routed by the incoming mailbox UPN.

### Configuration

Set `BROKERWARE_MAILBOX_TENANT_MAP` to a JSON object mapping lowercase mailbox UPNs to tenant keys:

```json
{
  "shepherd@3plsystems0.onmicrosoft.com": "shepherd",
  "shepherd1@3plsystems0.onmicrosoft.com": "shepherd",
  "shepherd2@3plsystems0.onmicrosoft.com": "shepherdwest"
}
```

For each unique tenant key (e.g. `SHEPHERD`, `SHEPHERDWEST`), provide the following env vars using the key as the infix:

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

The `BrokerwareClient` automatically selects the correct tenant credentials based on the `mailbox_upn` of the incoming email.

---

## Follow-Up System

When required shipment fields are missing after the initial extraction, the system automatically sends follow-up reminder emails in the original thread.

### How It Works

1. **Initial email processed** → OpenAI extracts fields; `ReplyMerger._compute_missing()` is applied immediately so all missing fields (including ZIP codes) are flagged upfront.
2. **Single follow-up per email** → Even if an email has multiple attachments (multiple shipment records), the system sends **one** follow-up reply containing the union of missing fields from all attachments. It never sends one reply per attachment.
3. **`ConversationState` saved** to Blob with `status = "awaiting_reply"`, `missing_fields` populated.
4. **Re-processing guard** → If the same original email is processed again (e.g. a reply arrives and the inbox poller re-queues it), `handle_initial_extraction` checks existing conversation state first. If state is `complete`, `max_retries_reached`, or `awaiting_reply` with a follow-up already sent, it returns immediately without sending another follow-up or overwriting state.
5. **Customer replies** → `process_email` re-runs extraction, merges context from prior exchanges, re-attempts field extraction.
6. **Timer fires** (`send_reminder_followups`) → finds all `awaiting_reply` conversations stale longer than `FOLLOWUP_REMINDER_INTERVAL_MINUTES`.
7. **`FollowupOrchestrator.send_timeout_followup()`** sends a follow-up reply via `graph_client.send_reply()` in the same email thread.
8. **Max retries reached** → status changes to `max_retries_reached`, no further follow-ups.

### Per-Customer Follow-Up Limits

Limits are resolved in priority order:
1. `customer_retry_config` table in PostgreSQL (per `customerId`)
2. `FOLLOWUP_MAX_BY_CUSTOMER` env var JSON: `{"customerId": maxCount}`
3. `FOLLOWUP_DEFAULT_MAX` global default (default: `3`)

### Manual Testing

```bash
# List all active conversations
python scripts/test_reminder.py --list

# Preview which conversations would get a reminder (no emails sent)
python scripts/test_reminder.py --dry-run

# Send reminder emails to all stale conversations
python scripts/test_reminder.py --send

# Override stale threshold to 1 minute
python scripts/test_reminder.py --send --minutes 1

# Force-send a reminder to one specific conversation
python scripts/test_reminder.py --send --conv-id <conversation_id>
```

---

## Security

### APIM Subscription Key

All inbound requests pass through Azure API Management. APIM injects the subscription key in the `Ocp-Apim-Subscription-Key` header; the `apim_guard.py` module validates it using constant-time comparison.

- Set `APIM_SUBSCRIPTION_KEY` (from Key Vault: `apim-subscription-key`)
- Set `APIM_STRICT_ENFORCEMENT=true` in production to block traffic when the key is absent

### JWT / RBAC

Admin endpoints (e.g. `register_webhooks`) require a valid Azure AD JWT (RS256) with the `WebhookAdmin` app role.

- `RBAC_ADMIN_ROLES` — comma-separated list of allowed roles (default: `WebhookAdmin`)
- JWKS public keys are fetched from Azure AD and cached for `AZURE_AD_JWKS_CACHE_TTL` seconds (default 1 hour)

### Azure Key Vault Integration

Set `KEY_VAULT_URL` to enable automatic secret loading at startup via `DefaultAzureCredential` (Managed Identity in Azure, CLI credentials locally):

```bash
KEY_VAULT_URL=https://shepherd-ai-kv.vault.azure.net/
```

Secrets in Key Vault must use hyphens in their names (e.g. `azure-openai-key`); `secrets.py` maps them to environment variables.

### Input Validation

- `MAX_EMAIL_BODY_CHARS` — rejects email bodies exceeding limit (default 500 000 chars)
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
# Function App logs
func azure functionapp logstream shepherdai-funcapp

# Web App logs
az webapp log tail \
  --name ShepherdAI-Quad-WebApp \
  --resource-group ShepherdAI-Quadrant-rg
```

### Restart Services

```bash
# Restart Function App
az functionapp restart \
  --name shepherdai-funcapp \
  --resource-group ShepherdAI-Quadrant-rg

# Restart Web App
az webapp restart \
  --name ShepherdAI-Quad-WebApp \
  --resource-group ShepherdAI-Quadrant-rg
```

### Health Check

```bash
# Public health endpoint (anonymous)
curl https://shepherdai-funcapp.azurewebsites.net/api/health

# Via APIM
curl https://shepherdai-apim.azure-api.net/functions/health \
  -H "Ocp-Apim-Subscription-Key: <key>"
```

### Git Operations

```bash
# Current branch
git branch

# Push to origin
git push origin chinmaya

# View recent commits
git log --oneline -10
```

### PostgreSQL

```bash
# Connect via psql
psql "postgresql://shephardai:<password>@shepherddb.postgres.database.azure.com:5432/postgres?sslmode=require"

# Check email audit log
SELECT id, sender_email, subject, status, created_at FROM email_records ORDER BY created_at DESC LIMIT 20;

# Check per-customer retry config
SELECT * FROM customer_retry_config;

# Update retry limit for a customer
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

# Download a specific conversation state
az storage blob download \
  --container-name processed-logs \
  --name "conversations/<conversation_id>.json" \
  --file conversation.json \
  --connection-string "<conn-str>"
```

### Subscription Management

```bash
# List active Graph subscriptions (via health endpoint)
curl https://shepherdai-funcapp.azurewebsites.net/api/health | python -m json.tool

# Force-renew subscriptions (call the timer function manually via APIM)
# Or wait for the 47-hour timer to fire automatically
```

### Azure Firewall Egress Rules

The Web App runs in `snet-app` (`10.0.0.0/24`) with all egress routed through `shepherdai-firewall`. Any new external FQDN the app needs to reach must be explicitly allowed.

To add an egress rule for a new FQDN:

1. Export the current firewall policy rules:
```bash
az rest --method GET \
  --url "https://management.azure.com/subscriptions/<sub-id>/resourceGroups/ShepherdAI-Quadrant-rg/providers/Microsoft.Network/firewallPolicies/shepherdai-firewall-policy/ruleCollectionGroups/DefaultApplicationRuleCollectionGroup?api-version=2023-11-01" \
  --output json > fw_current.json
```

2. Edit `fw_current.json` — add a new entry under the `Allow-ShepherdAI-Outbound` rule collection:
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

3. Apply the update:
```bash
az rest --method PUT \
  --url "https://management.azure.com/subscriptions/<sub-id>/resourceGroups/ShepherdAI-Quadrant-rg/providers/Microsoft.Network/firewallPolicies/shepherdai-firewall-policy/ruleCollectionGroups/DefaultApplicationRuleCollectionGroup?api-version=2023-11-01" \
  --body @fw_update.json
```

> **Important:** The source must always be `10.0.0.0/24` (the `snet-app` subnet). Using any other CIDR will silently default-deny the traffic — which manifests as an `SSLEOFError` during the TLS handshake.

---

## Document Types Supported

| Document | Key Data Extracted |
|---|---|
| Bill of Lading (BOL) | Pickup/drop locations, shipment ID, items, carrier |
| Picking List | Line items, delivery date, shipper info |
| Sales Order | Order number, PO number, items, ship-via (equipment mode) |
| Email body | Routing context, reference numbers, contacts, pickup date |

---

## Extraction Pipeline (2-Pass)

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
         ├──────────────────────────────────────────────────┐
         │                                                  │
         ▼  Pass 2 — Detailed Shipment                      │
Azure OpenAI (per attachment + envelope as read-only)       │
  → line items (pieces, weight, description)                │
  → shipper / consignee address details                     │
  → special instructions                                    │
         │                                                  │
         ▼                                                  │
Customer Lookup ◄──────────────────────────────────────────┘
  → Azure AI Search (vector cosine similarity ≥ 0.70)
  → Brokerware API (/api/client/{id}/customer)
  → Resolve customerId
         │
         ▼
Brokerware TMS
  → Create shipment
  → Return shipment ID
```
