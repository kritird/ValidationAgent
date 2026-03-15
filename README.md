# Production Validator
### Automated Production Launch Validation System for Visa Payment Features

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Component Reference](#component-reference)
4. [LangGraph Agent Workflow](#langgraph-agent-workflow)
5. [API Reference](#api-reference)
6. [Email Notifications](#email-notifications)
7. [Configuration](#configuration)
8. [Hadoop MCP Integration](#hadoop-mcp-integration)
9. [Frontend UI Guide](#frontend-ui-guide)
10. [Deployment](#deployment)
11. [Extending the System](#extending-the-system)
12. [Troubleshooting](#troubleshooting)

---

## Overview

**Production Validator** is an AI-powered system for automating production launch validation of Visa payment product features. It eliminates manual log-trawling by using an LLM agent to:

1. Parse uploaded test plan documents (Excel) and technical letters (PDF)
2. Intelligently generate up to 5 targeted validation cases based on impacted UMF/ISO fields
3. Schedule or immediately trigger production log pulls via Hadoop MCP
4. Analyze retrieved transaction logs against expected field values
5. Send structured pre/post-validation emails to a GDL distribution list
6. Support manual trigger overrides and retry with a fresh log window

The system is built around **LangGraph** for agent orchestration, **FastAPI** for the backend, and a vanilla HTML/CSS/JS frontend designed for payment operations teams.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    Web Frontend                       │
│          (HTML + CSS + JS · localhost:8000/ui)        │
└───────────────────────┬──────────────────────────────┘
                        │ HTTP / Multipart Form
┌───────────────────────▼──────────────────────────────┐
│                  FastAPI Backend                      │
│   /api/parse-documents  /api/setup  /api/trigger      │
│   /api/retry  /api/validations  /api/results          │
│                  APScheduler (cron)                   │
└───────────┬───────────────────────┬──────────────────┘
            │                       │
┌───────────▼──────────┐   ┌───────▼──────────────────┐
│   Document Parser    │   │   LangGraph Agent         │
│  (Claude API)        │   │   (StateGraph Workflow)   │
│  - Excel → Text      │   │   ┌──────────────────┐   │
│  - PDF → Text        │   │   │ validate_setup   │   │
│  - Extract UMF cases │   │   │ send_setup_emails│   │
└──────────────────────┘   │   │ pull_logs        │   │
                           │   │ run_validation   │   │
┌──────────────────────┐   │   │ send_results     │   │
│   In-Memory Store    │   │   │ error_handler    │   │
│  (→ PostgreSQL prod) │   │   └──────────────────┘   │
└──────────────────────┘   └───────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │     Hadoop MCP Connector        │
                    │  - UMF field-based log pull     │
                    │  - Time-windowed (≤5 min)       │
                    │  - Multi-system (VIPA/B/C/I)    │
                    └─────────────────────────────────┘
```

### Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Agent Orchestration | LangGraph 1.x | Stateful multi-node workflow |
| LLM | Claude (claude-sonnet-4-20250514) | Document parsing, validation analysis |
| API | FastAPI + Uvicorn | REST backend, file uploads |
| Scheduling | APScheduler (AsyncIOScheduler) | Timed auto-trigger |
| Document Parsing | openpyxl + PyMuPDF | Excel and PDF text extraction |
| Email | aiosmtplib (mock → SMTP) | GDL notifications |
| Frontend | Vanilla HTML/CSS/JS | Operations UI |
| Data Store | In-memory dict (→ PostgreSQL) | Validation state persistence |

---

## Component Reference

### `backend/models/schemas.py`

Pydantic models for all data structures.

| Model | Description |
|---|---|
| `ValidationSetup` | Full configuration for one validation run |
| `ValidationCase` | Single test case with UMF fields and expected outcomes |
| `UMFField` | One field to check: UMF number, ISO field, expected value |
| `LogPullRequest` | Parameters sent to Hadoop MCP for a log pull |
| `TransactionLog` | One transaction record returned from Hadoop |
| `ValidationResult` | Result for a single validation case |
| `OverallValidationResult` | Aggregated result across all cases |

Key enums:

- `SystemType`: `VIPA | VIPB | VIPC | VIPI`
- `ReleaseType`: `release | regular`
- `ValidationStatus`: `pending | scheduled | running | passed | failed | partial`

---

### `backend/tools/document_parser.py`

Parses uploaded documents and uses Claude to extract structured validation cases.

```python
parse_excel_test_plan(file_path: str) -> Tuple[str, List[dict]]
```
- Opens the Excel file with `openpyxl`
- Reads all sheets, up to 50 rows per sheet
- Returns a text representation (for Claude) plus raw row data

```python
parse_pdf_tech_letter(file_path: str) -> str
```
- Opens PDF with PyMuPDF (`fitz`)
- Extracts full text per page

```python
extract_validation_cases(
    excel_text: str,
    pdf_text: Optional[str],
    custom_notes: Optional[str],
    feature_number: str
) -> List[ValidationCase]
```
- Combines document text into a structured prompt for Claude
- Claude returns JSON: up to 5 validation cases with UMF fields, ISO fields, expected values
- Hard-capped at 5 cases on return

**Extending:** Swap the Claude call for your own logic or add a second pass that checks against a UMF reference dictionary.

---

### `backend/tools/hadoop_mcp.py`

Provides the Hadoop log-pull interface. Currently a **mock implementation**.

```python
class HadoopLogPullMCP:
    async def connect(self)
    async def execute_log_pull(request: LogPullRequest) -> List[TransactionLog]
    async def disconnect(self)
```

`execute_log_pull` takes:
- `umf_numbers`: list of UMF field numbers (e.g. `[46, 62, 124]`)
- `systems`: list of `SystemType` values
- `start_time` / `end_time`: datetime window (max 5 minutes recommended)
- `feature_number`: for log tagging

**To connect to real Hadoop MCP**, replace the body of `execute_log_pull` with:

```python
response = await mcp_client.call_tool(
    "pull_transaction_logs",
    {
        "umf_numbers": request.umf_numbers,
        "systems": [s.value for s in request.systems],
        "start_time": request.start_time.isoformat(),
        "end_time": request.end_time.isoformat(),
        "feature_number": request.feature_number,
        "max_records": 1000
    }
)
return [TransactionLog(**log) for log in response["logs"]]
```

---

### `backend/tools/email_service.py`

Four email functions, each building an HTML email and sending via SMTP (mock):

| Function | Trigger | Content |
|---|---|---|
| `send_validation_setup_confirmation` | After finalising setup | Full config summary, all cases, schedule info |
| `send_manual_trigger_notification` | When no launch time given | Manual trigger button with deep link |
| `send_pre_validation_email` | Immediately before log pull | "Validation starting" notification |
| `send_post_validation_email` | After validation completes | Full results, per-case breakdown, retry button if failed |

**To connect real SMTP**, replace `_send_email` with:

```python
import aiosmtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

async def _send_email(to: str, subject: str, html: str):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg['To'] = to
    msg.attach(MIMEText(html, 'html'))
    await aiosmtplib.send(
        msg,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        username=SMTP_USER,
        password=SMTP_PASS,
        use_tls=True
    )
```

---

### `backend/utils/store.py`

In-memory key-value store for validation state.

```python
save_validation(setup: ValidationSetup)
get_validation(validation_id: str) -> Optional[ValidationSetup]
list_validations() -> list
update_validation_status(validation_id: str, status: str)
save_result(result: OverallValidationResult)
get_result(validation_id: str) -> Optional[dict]
get_attempt_count(validation_id: str) -> int
```

**For production**, replace with a PostgreSQL-backed store using SQLAlchemy or asyncpg. The interface is identical — swap only the implementation inside each function.

---

## LangGraph Agent Workflow

The validation agent is a compiled `StateGraph` with the following topology:

```
START
  │
  ▼
validate_setup ──(error)──► error_handler ──► END
  │
  ▼ (valid)
send_setup_emails ──(no launch_time)──► END  ← waits for manual trigger
  │
  ▼ (launch_time given OR manually triggered)
pull_logs ──(error)──► error_handler ──► END
  │
  ▼ (logs retrieved)
run_validation
  │
  ▼
send_results
  │
  ▼
END
```

### State Schema (`ValidationState`)

```python
class ValidationState(TypedDict):
    validation_id: str          # Unique ID for this run
    setup: dict                 # Serialized ValidationSetup
    logs: List[dict]            # Transaction logs from Hadoop
    case_results: List[dict]    # Per-case validation results
    overall_result: Optional[dict]  # Aggregated result
    attempt_number: int         # 1 on first run, increments on retry
    status: str                 # Current phase status string
    error: Optional[str]        # Error message if any node fails
    phase: str                  # Lifecycle phase
```

### Node Descriptions

**`validate_setup`**
Checks all required fields are present. Returns `error` status if validation config is incomplete, blocking downstream nodes.

**`send_setup_emails`**
- Always sends setup confirmation to GDL
- If no `launch_time`: sends manual trigger email with deep-link URL, then routes to `END` (graph pauses)
- If `launch_time` present: routes to `pull_logs`

**`pull_logs`**
- Collects all unique UMF numbers from all validation cases
- Constructs a 5-minute log pull window (offset by attempt number on retry)
- Calls `hadoop_mcp.execute_log_pull()`
- Sends pre-validation email to GDL

**`run_validation`**
- Formats all cases and a sample of logs into a structured Claude prompt
- Claude returns per-case pass/fail with matched/failed field details
- Maps Claude output to `ValidationResult` objects

**`send_results`**
- Reconstructs typed result objects from state dicts
- Calls `send_post_validation_email` with retry URL
- If failed/partial, email includes a retry button

**`error_handler`**
Logs the error and sets `phase = "error"`. In production, this should also fire an alert email.

---

### Two-Phase Execution

The workflow splits into two callable phases:

```python
# Phase 1: Setup — parses docs, sends emails, schedules
await run_validation_setup_phase(setup: ValidationSetup) -> dict

# Phase 2: Execute — pulls logs, runs validation, sends results
await run_validation_execution_phase(
    validation_id: str,
    setup: ValidationSetup,
    attempt: int = 1
) -> dict
```

Phase 2 is invoked either:
- Automatically by APScheduler 10 minutes after `launch_time`
- Manually via `POST /api/trigger/{validation_id}`
- On retry via `POST /api/retry/{validation_id}`

---

## API Reference

Base URL: `http://localhost:8000`

### `POST /api/parse-documents`

Parse documents and preview validation cases before full setup.

**Form fields:**
| Field | Type | Required | Description |
|---|---|---|---|
| `test_plan` | File (.xlsx) | Yes | Excel test plan matrix |
| `tech_letter` | File (.pdf) | No | Tech letter PDF |
| `feature_number` | string | Yes | Feature identifier |
| `custom_notes` | string | No | Additional validation instructions |

**Response:**
```json
{
  "validation_cases": [
    {
      "case_id": "VC001",
      "title": "UMF 124 Field Presence",
      "description": "...",
      "umf_fields": [
        {
          "umf_number": 124,
          "iso_field": "DE 124",
          "field_name": "Custom Field",
          "expected_value": "V01A001",
          "description": "..."
        }
      ],
      "expected_outcome": "Field present in all auth transactions",
      "source": "test_plan"
    }
  ],
  "count": 3
}
```

---

### `POST /api/setup`

Full validation setup. Parses documents, stores config, triggers setup phase.

**Form fields** (same as parse-documents, plus):
| Field | Type | Required | Description |
|---|---|---|---|
| `release_type` | string | Yes | `"release"` or `"regular"` |
| `launch_date` | string | Yes | `YYYY-MM-DD` |
| `launch_time` | string | No | `HH:MM` UTC — if omitted, manual trigger email sent |
| `systems` | JSON string | Yes | `["VIPA","VIPB"]` |
| `gdl_email` | string | Yes | Distribution list email |
| `custom_validation_notes` | string | No | Free text additional notes |
| `validation_cases_override` | JSON string | No | User-edited cases array (if provided, skips re-parsing) |

**Response:**
```json
{
  "validation_id": "VAL-VDP-2024-1234-A1B2C3D4",
  "setup": { ... },
  "message": "Validation configured successfully"
}
```

---

### `POST /api/trigger/{validation_id}`

Manually trigger validation execution. Can override scheduled time.

**Response:**
```json
{
  "message": "Validation triggered",
  "validation_id": "VAL-...",
  "attempt": 1
}
```

---

### `POST /api/retry/{validation_id}`

Retry a failed or partial validation with a fresh log window. The log window shifts by 5 minutes per attempt number.

**Response:**
```json
{
  "message": "Retry triggered",
  "validation_id": "VAL-...",
  "attempt": 2
}
```

Maximum 5 retry attempts enforced.

---

### `GET /api/validations`

List all validations (setup + status).

### `GET /api/validations/{validation_id}`

Get full setup details for one validation.

### `GET /api/results/{validation_id}`

Get validation results. Returns `{ "status": "...", "result": null }` if not yet run.

### `GET /api/status/{validation_id}`

Lightweight status poll: `{ "status": "running", "has_result": false, "attempt_number": 1 }`

### `GET /health`

Health check: `{ "status": "ok", "timestamp": "..." }`

---

## Email Notifications

The system sends exactly these emails in this order:

### 1. Setup Confirmation (always sent)
- **To:** GDL email
- **When:** Immediately after `POST /api/setup` succeeds
- **Subject:** `[PROD VALIDATOR] Validation Configured - {feature} | {date}`
- **Content:** Full config table, all validation cases with UMF field tables, schedule/trigger info

### 2. Manual Trigger Request (when no launch time)
- **To:** GDL email
- **When:** Only when `launch_time` is omitted
- **Subject:** `[ACTION REQUIRED] Trigger Validation - {feature} | {date}`
- **Content:** Deep-link button to `POST /api/trigger/{id}`, validation summary
- **Note:** Manual trigger can always override auto-schedule — even when a time was provided

### 3. Pre-Validation Notification (always sent)
- **To:** GDL email
- **When:** At the start of each validation execution (auto or manual)
- **Subject:** `[PROD VALIDATOR] Validation Starting - {feature}`
- **Content:** Systems, case count, timestamp of log pull start

### 4. Post-Validation Results (always sent)
- **To:** GDL email
- **When:** After each validation completes
- **Subject:** `[PROD VALIDATOR] ✅ PASSED / ❌ FAILED / ⚠️ PARTIAL - {feature} (Attempt #N)`
- **Content:** Per-case results with matched/failed fields, summary text
- **On failure:** Includes a **Retry** button linking to `POST /api/retry/{id}`

---

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and fill in values:

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...

# Application
PORT=8000
BASE_URL=http://your-server:8000

# Email (SMTP)
SMTP_HOST=smtp.company.com
SMTP_PORT=587
SMTP_USER=prodvalidator@company.com
SMTP_PASS=secret
FROM_EMAIL=prodvalidator@company.com

# Hadoop MCP
HADOOP_MCP_URL=hadoop-mcp://your-cluster/transaction-logs
```

### Scheduling Behavior

| Scenario | Behavior |
|---|---|
| `launch_time` provided, time is future | APScheduler fires `execute_validation()` 10 min after `launch_time` |
| `launch_time` provided, time already past | Execution triggered immediately as background task |
| No `launch_time` | No auto-schedule. Manual trigger email sent. Waits for `POST /api/trigger/{id}` |
| Manual trigger always | `POST /api/trigger/{id}` works at any time regardless of schedule |

---

## Hadoop MCP Integration

### Log Pull Parameters

The agent collects all unique UMF numbers from all validation cases and issues a single log pull:

```python
LogPullRequest(
    umf_numbers=[46, 62, 124, 48],   # All UMFs across all cases
    systems=[SystemType.VIPA, SystemType.VIPB],
    start_time=now - timedelta(minutes=5),
    end_time=now,
    feature_number="VDP-2024-1234"
)
```

**Time window:** Always ≤ 5 minutes. On retry attempt N, the window shifts back by `(N-1) × 5` minutes, pulling an earlier window that may have more complete data.

### Real MCP Server Setup

When your Hadoop MCP server is available, update `HadoopLogPullMCP.execute_log_pull`:

```python
async def execute_log_pull(self, request: LogPullRequest) -> List[TransactionLog]:
    from mcp import Client
    
    async with Client(self.mcp_server_url) as client:
        response = await client.call_tool(
            "pull_transaction_logs",
            {
                "umf_numbers": request.umf_numbers,
                "systems": [s.value for s in request.systems],
                "start_time": request.start_time.isoformat() + "Z",
                "end_time": request.end_time.isoformat() + "Z",
                "feature_number": request.feature_number,
                "max_records": 1000
            }
        )
    
    return [TransactionLog(**log) for log in response["logs"]]
```

The `TransactionLog` model expects:
```json
{
  "transaction_id": "TXN-00001",
  "timestamp": "2024-12-01T14:05:22Z",
  "system": "VIPA",
  "umf_fields": {
    "UMF_124": "V01A001",
    "UMF_62": "001AB",
    "UMF_1": "0200"
  }
}
```

---

## Frontend UI Guide

### New Validation Tab

A 3-step wizard:

**Step 1 — Configure**
- Feature number and release type (Release Product vs Regular Release)
- Launch date (required) and activation time (optional)
- System selector: click to toggle VIPA / VIPB / VIPC / VIPI
- File uploads: Excel test plan (required) + PDF tech letter (optional)
- GDL email and custom validation notes
- Click **Parse Documents & Preview** to invoke AI parsing

**Step 2 — Review Cases**
- AI-generated validation cases shown as cards
- Each card shows: case ID, title, UMF field tags, expected outcome
- Remove cases with the × button; add blank cases with + Add Case
- Click **View Full Summary & Confirm** to proceed

**Step 3 — Confirm & Submit**
- Read-only summary of all configuration
- Click **Submit Validation Setup** to create the validation and fire setup emails
- On success, automatically switches to Monitor tab

### Monitor Tab
- Lists all validations with current status badge
- **Trigger Now** button for scheduled/pending validations
- **Retry** button for failed/partial results
- **View Details** opens a modal with full result breakdown
- Auto-refreshes every 15 seconds

### History Tab
- Table view of all validations
- Sortable by feature number, date, status
- Details button opens the same modal

---

## Deployment

### Local Development

```bash
# 1. Clone and set up environment
cd production-validator
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY

# 2. Run
python run.py

# Access at http://localhost:8000/ui
```

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
EXPOSE 8000
CMD ["python", "run.py"]
```

```bash
docker build -t production-validator .
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e SMTP_HOST=smtp.company.com \
  production-validator
```

### Production Considerations

| Item | Recommendation |
|---|---|
| Data store | Replace `utils/store.py` with PostgreSQL + asyncpg |
| Email | Configure real SMTP credentials; consider SendGrid or AWS SES |
| Auth | Add JWT or SSO middleware — currently no auth |
| Secrets | Use Vault or AWS Secrets Manager for API keys |
| Logging | Add structured logging (structlog) with log levels |
| Monitoring | Instrument with Prometheus metrics on `/metrics` |
| State persistence | If server restarts, APScheduler jobs are lost — use persistent job store |

---

## Extending the System

### Adding a New System Type

1. Add to `SystemType` enum in `schemas.py`:
   ```python
   VIPD = "VIPD"
   ```
2. Add a button in the frontend `systems-grid` div:
   ```html
   <div class="system-btn" onclick="toggleSystem('VIPD', this)">VIPD</div>
   ```

### Adding a New Agent Node

Add to `validation_agent.py`:
```python
async def node_post_analysis_check(state: ValidationState) -> ValidationState:
    # Your logic here
    return {**state, "status": "post_checked"}

# In build_validation_graph():
graph.add_node("post_analysis_check", node_post_analysis_check)
graph.add_edge("run_validation", "post_analysis_check")
graph.add_edge("post_analysis_check", "send_results")
# Remove the direct run_validation → send_results edge
```

### Customising Validation Logic

The AI validation prompt in `node_run_validation` can be tuned. Key levers:

- **Pass criteria:** Edit the "A case PASSES if" section to be stricter (e.g. require all transactions, not some)
- **Field matching:** Add domain logic for known value formats (e.g. bitmask fields, length-prefixed values)
- **Log sampling:** Currently passes 20 logs to Claude — increase for higher confidence at the cost of tokens

### Adding a Database

Replace `backend/utils/store.py`:
```python
import asyncpg

async def save_validation(setup: ValidationSetup):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO validations VALUES ($1, $2)",
            setup.validation_id,
            setup.model_dump_json()
        )
```

---

## Troubleshooting

### "No validation cases generated"
- Check the Excel file has readable headers in row 1
- Ensure the test plan contains field/value columns Claude can interpret
- Add specific instructions in the custom notes field

### "Logs not found" in validation
- The mock MCP always returns logs — if using real MCP, verify the time window
- Check the UMF numbers extracted from cases match what your Hadoop schema uses
- Ensure `HADOOP_MCP_URL` is reachable from the server

### Emails not sending
- Confirm SMTP credentials in `.env`
- Check firewall rules allow outbound SMTP on the configured port
- In development, emails are printed to stdout — look for `[EMAIL MOCK]` output

### APScheduler not firing
- Validate `launch_time` is in `HH:MM` format (24h UTC)
- If the server restarts, the schedule is lost — re-trigger manually or add persistent job store
- Check the server clock is in UTC

### Graph node errors
- Set `LANGCHAIN_VERBOSE=true` for detailed LangGraph trace output
- Errors surface in `state["error"]` — check API response or stdout logs
- The `error_handler` node logs to stdout and terminates the graph

---

## Project Structure

```
production-validator/
├── run.py                          # Application entry point
├── requirements.txt
├── .env.example
├── frontend/
│   └── index.html                  # Single-page UI
├── backend/
│   ├── __init__.py
│   ├── models/
│   │   └── schemas.py              # All Pydantic data models
│   ├── tools/
│   │   ├── document_parser.py      # Excel + PDF parsing + AI case extraction
│   │   ├── hadoop_mcp.py           # Hadoop MCP log pull connector (mock)
│   │   └── email_service.py        # Email notification templates + sender
│   ├── agents/
│   │   └── validation_agent.py     # LangGraph StateGraph workflow
│   ├── api/
│   │   └── main.py                 # FastAPI routes + APScheduler
│   └── utils/
│       └── store.py                # In-memory validation state store
└── docs/
    └── README.md                   # This document
```
