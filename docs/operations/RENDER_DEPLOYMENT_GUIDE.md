# Aptlyst AI — Manual Render Deployment & Billing Operations Guide

> [!IMPORTANT]
> This guide documents manual Render Dashboard configuration for Aptlyst AI's backend API and Razorpay background workers. It does **not** authorize or perform any staging or production deployment, credential rotation, or database migration.

---

## 1. Deployment Topology Overview

Aptlyst AI runs as **three independent native Python services on Render**, sharing a single Supabase PostgreSQL 16 database and decoupled from the Vercel-hosted Next.js frontend:

1. **`aptlyst-api` (Render Web Service):** Serves FastAPI HTTP endpoints, Supabase JWT auth verification, Razorpay checkout initiation/verification, and fast HMAC-verified webhook ingress (`/api/v1/billing/razorpay/webhook`).
2. **`aptlyst-razorpay-webhook-worker` (Render Background Worker):** Independently consumes verified events from `billing_webhook_events` using PostgreSQL `FOR UPDATE SKIP LOCKED` and lease fencing. Makes zero external Razorpay HTTP calls.
3. **`aptlyst-razorpay-recovery-worker` (Render Background Worker):** Independently claims stalled/abandoned attempts from `billing_checkout_attempts` (`recovery_required`) and performs read-only Razorpay subscription inquiries strictly outside database locks.

No Dockerfiles, Compose manifests, or `render.yaml` blueprints are used; each service uses Render's native Python runtime.

---

## 2. Service Specifications (Manual Render Dashboard)

Configure all three services in the **same geographical Render region** as your Supabase PostgreSQL project to minimize database round-trip latency. Pin a consistent Python version across all three services (CI validates **Python 3.11**; **Python 3.12+** is also supported).

### Service 1 — FastAPI Web Service (`aptlyst-api`)
- **Service Type:** Render Web Service (Python)
- **Root Directory:** `apps/api`
- **Build Command:**
  ```bash
  pip install --upgrade pip && pip install -r requirements.txt
  ```
- **Start Command:**
  ```bash
  uvicorn app.main:app --host 0.0.0.0 --port $PORT
  ```
  *(Note: `$PORT` is injected automatically by Render; `gunicorn` is not installed in `requirements.txt`, so `uvicorn` is the verified ASGI entrypoint.)*
- **Health Check Path:** `/health` (HTTP 200 `{"status": "ok"}`)
- **Manual DB Readiness Check:** `GET /health/db` (executes `SELECT 1`; returns HTTP 200 `{"status": "ok", "database": "connected"}` or HTTP 503)
- **Initial Instance Count:** `1`
- **Scaling Constraints:**
  - If scaling Web Service instances `> 1`, configure `RATE_LIMIT_STORAGE=redis` and `REDIS_URL` so rate limits are shared across instances instead of process-local memory.
  - Ensure `CORS_ALLOWED_ORIGINS` lists exact Vercel origins (e.g., `https://your-domain.example`) without wildcards (`*`).

### Service 2 — Razorpay Webhook Background Worker (`aptlyst-razorpay-webhook-worker`)
- **Service Type:** Render Background Worker (Python)
- **Root Directory:** `apps/api`
- **Build Command:**
  ```bash
  pip install --upgrade pip && pip install -r requirements.txt
  ```
- **Start Command:**
  ```bash
  python -m app.workers.razorpay_webhook_worker --poll-interval 2.0 --batch-size 10 --log-level INFO
  ```
- **Health Check Path:** Not applicable (Render Background Workers have no HTTP listener; monitor via stdout logs and `python -m app.workers.billing_health`).
- **Initial Instance Count:** `1`
- **Shutdown Grace Period:** Minimum `30s` (single-item database transactions execute without external HTTP calls; interrupted items roll back cleanly and reclaim after the `60s` lease).
- **Scaling Constraints:** Safe for `1..N` instances via PostgreSQL `FOR UPDATE SKIP LOCKED`, bounded by Supabase Session Pooler connection limits.

### Service 3 — Razorpay Checkout Recovery Background Worker (`aptlyst-razorpay-recovery-worker`)
- **Service Type:** Render Background Worker (Python)
- **Root Directory:** `apps/api`
- **Build Command:**
  ```bash
  pip install --upgrade pip && pip install -r requirements.txt
  ```
- **Start Command:**
  ```bash
  python -m app.workers.razorpay_checkout_recovery_worker --poll-interval 2.0 --batch-size 10 --lease-duration 60 --log-level INFO
  ```
- **Health Check Path:** Not applicable (monitor via stdout logs and `python -m app.workers.billing_health`).
- **Initial Instance Count:** `1`
- **Shutdown Grace Period Requirement:** **Verify in the Render Dashboard and configure `>= 60` seconds if supported by your Render service tier** (default Render `SIGTERM` wait is typically ~30s).
  - *Justification:* While the worker claims 1 attempt at a time and stops claiming new items immediately upon `SIGTERM`, Case B recovery (unknown subscription ID) performs up to 3 sequential paginated Razorpay HTTP inquiries with a `15.0s` timeout each (`45s` worst-case). A `30s` `SIGKILL` cutoff cannot guarantee uninterrupted completion of a worst-case 45s inquiry; however, interrupted inquiries make zero DB mutations, expire after 60s, and are reclaimed automatically (capped at `MAX_EXPIRED_LEASE_RECOVERIES = 5`).
- **Scaling Constraints:** Safe for `1..N` instances via `FOR UPDATE SKIP LOCKED` and UUID fencing tokens (`recovery_claim_id`), subject to Razorpay API rate limits (HTTP 429).

---

## 3. Environment-Variable Least-Privilege Matrix

All processes preserve Aptlyst AI's **single canonical Razorpay merchant architecture**:
- **Production (`ENVIRONMENT=production`):** Strictly `RAZORPAY_ENVIRONMENT=live`, `rzp_live_*` keys, non-prohibited live `RAZORPAY_MERCHANT_ACCOUNT_ID`, and production Supabase `DATABASE_URL`.
- **Staging / Non-Production (`ENVIRONMENT=staging`):** Strictly `RAZORPAY_ENVIRONMENT=test`, `rzp_test_*` keys, test `RAZORPAY_MERCHANT_ACCOUNT_ID`, and staging Supabase `DATABASE_URL`.
- Never share production credentials with staging, development, or CI. Never place real secrets in source control.

| Variable Name | API Web Service | Webhook Worker | Recovery Worker | Controlled Migration | Notes / Validation |
|---|---|---|---|---|---|
| `ENVIRONMENT` | **Required** | **Required** | **Required** | **Required** | `production` or `staging` (fails closed if invalid) |
| `APTLYST_PROCESS_ROLE` | Optional (`api`) | Optional (`webhook_worker`) | Optional (`recovery_worker`) | Optional (`migration`) | Auto-set by each entrypoint; can be set explicitly |
| `DATABASE_URL` | **Required** | **Required** | **Required** | **Required** | Supabase Session Pooler port `5432` (NOT `6543`); uses `NullPool` |
| `CORS_ALLOWED_ORIGINS` | **Required** | Not needed | Not needed | Not needed | Comma-separated origins, e.g. `https://your-domain.example` (no `*`) |
| `SUPABASE_URL` | **Required** | Not needed | Not needed | Not needed | Used for JWKS/JWT verification in API |
| `SUPABASE_JWT_ALGORITHM` / `AUDIENCE` | **Required** | Not needed | Not needed | Not needed | Defaults `ES256` / `authenticated` |
| `SUPABASE_JWT_SECRET` | Required if HS256 | Not needed | Not needed | Not needed | Legacy symmetric JWT verification only |
| `VISITOR_SESSION_SECRET` | **Required (prod)** | **NO** | **NO** | **NO** | API-only (`>= 32` chars in prod); not required by workers |
| `LLM_PROVIDER` / `LLM_*` | **Required** | Not needed | Not needed | Not needed | Mock LLM prohibited in production |
| `RATE_LIMIT_*` / `REDIS_URL` | As configured | Not needed | Not needed | Not needed | Required only if `RATE_LIMIT_STORAGE=redis` |
| `DEFAULT_FREE_MONTHLY_TOKEN_QUOTA` | Recommended | Not needed | Not needed | Not needed | Positive integer for Free tier quotas |
| `RAZORPAY_ENABLED` | **Required** (`true`) | **Required** (`true`) | **Required** (`true`) | Not needed | Enables Razorpay billing paths |
| `RAZORPAY_ENVIRONMENT` | **Required** | **Required** | **Required** | Not needed | `live` (prod) or `test` (staging) |
| `RAZORPAY_MERCHANT_ACCOUNT_ID` | **Required** | **Required** | **Required** | Not needed | Canonical single merchant account ID (no `default` in prod) |
| `RAZORPAY_KEY_ID` | **Required** | **NO** | **Required** | Not needed | `rzp_live_...` (prod) / `rzp_test_...` (staging) |
| `RAZORPAY_KEY_SECRET` | **Required** | **NO** | **Required** | Not needed | Secret API credential |
| `RAZORPAY_WEBHOOK_SECRET` | **Required** | **NO** | **NO** | Not needed | API webhook ingress HMAC verification only |
| `RAZORPAY_PRO_PLAN_ID` | **Required** | **NO** | **Required** | Not needed | Must start with `plan_` |
| `RAZORPAY_PLAN_CURRENCY` | **Required** | **NO** | **Required** | Not needed | 3-letter ISO currency code (e.g. `USD`, `INR`) |
| `RAZORPAY_WEBHOOK_WORKER_*` | Not needed | Optional | Not needed | Not needed | `_POLL_INTERVAL=2.0`, `_BATCH_SIZE=10`, optional `_ID` |
| `RAZORPAY_RECOVERY_WORKER_*` | Not needed | Not needed | Optional | Not needed | `_POLL_INTERVAL=2.0`, `_BATCH_SIZE=10`, optional `_ID` |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | Not needed | Optional (`30.0`) | Optional (`30.0`) | Not needed | Throttles heartbeat DB upserts |

> [!TIP]
> For single-instance Render Background Workers (`Initial Instance Count = 1`), set a stable `RAZORPAY_WEBHOOK_WORKER_ID` (e.g. `aptlyst-webhook-worker-1`) and `RAZORPAY_RECOVERY_WORKER_ID` (e.g. `aptlyst-recovery-worker-1`) so crash restarts cleanly fence and take over stale heartbeat rows via `instance_token` instead of leaving orphaned `status='running'` rows. Do **not** share one `worker_id` across multiple concurrently running instances.

---

## 4. Controlled Database Migration Procedure

**Rule:** Never run `alembic upgrade head` inside the Web Service `Start Command` or inside Background Worker startups. Concurrent startups across services/instances must never race on Alembic migrations.

### Recommended Execution Procedure
Execute migrations as a **single controlled release step** before rolling new Web/Worker code:
1. Confirm target environment (`APTLYST_PROCESS_ROLE=migration`, `ENVIRONMENT=<staging|production>`, `DATABASE_URL=<target port 5432 session pooler or direct connection>`).
2. For staging migrations, run the read-only staging migration preflight gate from repository root before applying any DDL:
   ```bash
   python scripts/check_staging_migration_gate.py
   ```
3. Inspect current head and pending revisions from `apps/api`:
   ```bash
   python -m alembic current
   python -m alembic heads
   ```
4. Execute the migration once (either via Render Web Service **Pre-Deploy Command** `alembic upgrade head` OR a single-run Render Shell / one-off job):
   ```bash
   python -m alembic upgrade head
   ```
5. Verify revision matches expected head (`a4b5c6d7e8f3` adding `billing_worker_heartbeats` on top of `f3a4b5c6d7e2`).

### Version Compatibility & Safe Rollback
- **Forward Compatibility:** Migrations `f3a4b5c6d7e2` and `a4b5c6d7e8f3` are additive/non-breaking for existing rows; if Pre-Deploy runs before new instances start, running instances continue cleanly while the schema updates.
- **Application Rollback:** Roll back the Render Web Service and both Background Workers to the prior commit in the Render Dashboard **without** running `alembic downgrade`.
- **Schema Downgrade Safety:** Only downgrade `f3a4b5c6d7e2` after stopping **both** background workers and waiting `> 60` seconds for active leases to expire (`f3a4b5c6d7e2`'s `downgrade()` acquires an `ACCESS EXCLUSIVE` lock and aborts if unexpired recovery claims exist).

---

## 5. Worker Heartbeats, Queue Monitoring, Alerting & Operational Hardening

### API Database Health Endpoint (`GET /health/db`)
- Executes `SELECT 1` against PostgreSQL.
- On success: returns HTTP `200` with `{"status": "ok", "database": "connected"}`.
- On database failure: returns HTTP `503` with a **generic, redacted response** (`{"status": "error", "database": "unavailable"}`). Never exposes SQLAlchemy tracebacks, connection strings, credentials, or internal hostnames to HTTP clients; sanitized error details are emitted only to server-side logs.

### Worker Ownership-Loss & Heartbeat Retention
Both `razorpay_webhook_worker` and `razorpay_checkout_recovery_worker` persist throttled liveness and progress heartbeats to `billing_worker_heartbeats` (`last_heartbeat_at`, `last_processed_at`, `last_error_at`, `processed_count`, `error_count`) with an isolated `2.0s` write timeout:
- **Ownership-Loss Claim Halt:** If a worker's `instance_token` fencing check fails (`ownership_lost=True`, e.g., another instance superseded its stale heartbeat or shared the same `worker_id`), the worker immediately stops claiming new queue items, exits its polling loop, and skips writing a `"stopped"` heartbeat so it cannot overwrite the active replacement's row. Already in-flight item processing continues to be guarded by UUID lease fencing.
- **Heartbeat Retention Cleanup:** On startup (with a `3.0s` non-fatal timeout), workers prune historical rows via `cleanup_stale_worker_heartbeats` using `SELECT ... FOR UPDATE SKIP LOCKED` in bounded batches:
  - Cleanly `stopped` rows older than `WORKER_HEARTBEAT_STOPPED_RETENTION_SECONDS` (default `604800`s / 7 days).
  - Non-stopped abandoned rows older than `WORKER_HEARTBEAT_ABANDONED_RETENTION_SECONDS` (default `2592000`s / 30 days).
  - Active/recent rows are protected by a mandatory safety floor (`>= max(stale_takeover_seconds * 2, 300)` seconds) and exclusion of the caller's own `worker_id`.

### Inspection & Scheduled Alerting CLI (`billing_health`)
Run read-only operational checks and optional edge-triggered alerting from an external scheduler or Render Shell (`apps/api` root directory, requiring only `ENVIRONMENT` and `DATABASE_URL`):

```bash
# Human-readable health report
python -m app.workers.billing_health --environment test

# Structured JSON output exiting non-zero when alerts trigger
python -m app.workers.billing_health --environment live --json --fail-on-alert

# Scheduled alert evaluation + opt-in sink delivery with private local deduplication file
python -m app.workers.billing_health --environment live --json --fail-on-alert --notify --state-file /var/lib/aptlyst/alert-state.json
```

**CLI Exit Codes for External Schedulers:**
- `0`: System healthy, OR alerts active without `--fail-on-alert` (and delivery succeeded if `--notify`).
- `1`: One or more alert conditions active under `--fail-on-alert`, **or** database connectivity / monitoring evaluation failed (`MONITORING_EVALUATION_FAILED`).
- `2`: `--notify` delivery failed (timeout, network error, non-2xx webhook response) or notification configuration was invalid.

**Alert Notification & Deduplication Configuration:**
- **Opt-In Webhook Sink:** Disabled by default (`BILLING_HEALTH_ALERT_WEBHOOK_ENABLED=false`). To enable external HTTP JSON alerts, set `BILLING_HEALTH_ALERT_WEBHOOK_ENABLED=true`, `BILLING_HEALTH_ALERT_WEBHOOK_URL=https://...`, optional `BILLING_HEALTH_ALERT_WEBHOOK_BEARER_TOKEN`, and `BILLING_HEALTH_ALERT_WEBHOOK_TIMEOUT_SECONDS` (default `5.0`, max `30.0`). Never pass tokens on CLI flags or commit URLs/secrets.
- **Deduplication Strategy (Single Non-Overlapping Scheduler & At-Least-Once Delivery):** With `--state-file` / `BILLING_HEALTH_ALERT_STATE_FILE`, alerts trigger on edge transitions (`firing` on newly active alert code, `escalated` on `warning -> critical`, and `resolved` when cleared). Resolved incidents are removed from active state so any future recurrence immediately re-alerts. Notification state updates **only after** HTTP `2xx` delivery succeeds (at-least-once delivery semantics: request timeouts or `5xx` responses after receiver acceptance trigger retry on the next run and may produce duplicate notifications). `FileAlertStateStore` uses atomic single-process file replacement without cross-process file locking; initial deployment MUST run a single non-overlapping scheduler and store the state file in a private dedicated directory (`chmod 0700`, e.g. `/var/lib/aptlyst/alert-state.json`), never in shared `/var/tmp` or `/tmp`.
- **Persistence Limitation (Proposed Future Configuration):** Local state files provide durable deduplication only when a single scheduler retains private local/persistent disk. Ephemeral stateless runners or concurrent multi-runner schedules (such as a future Render Cron Job without persistent disk, not yet configured) either discard local files or risk duplicate alerts; cross-runner stateless deduplication would require a future separately approved database schema migration (`billing_alert_states`). Never assume a Render Cron service is already provisioned in the Render Dashboard until explicitly created and verified.

### Monitored Alert Conditions & Default Thresholds
1. **`STALE_WORKER_HEARTBEAT` (`BILLING_HEALTH_HEARTBEAT_STALE_SECONDS=90`):** Active worker row has not reported a heartbeat in `> 90s`.
2. **`NO_ACTIVE_WEBHOOK_WORKER` / `NO_ACTIVE_RECOVERY_WORKER`:** Zero non-stale running workers registered for the target environment.
3. **`WORKER_ERRORING_NO_PROGRESS` / `WEBHOOK_WORKER_STALLED_OR_BACKLOGGED` / `RECOVERY_WORKER_STALLED_OR_BACKLOGGED`:** Flags alive workers whose queue backlog exceeds threshold or errors outpace progress.
4. **`WEBHOOK_BACKLOG_COUNT_EXCEEDED` (`BILLING_HEALTH_WEBHOOK_BACKLOG_COUNT_THRESHOLD=50`):** Unprocessed (`pending + failed`) webhook events meet or exceed configured count threshold (escalates to `critical` at `>= 2x` threshold).
5. **`WEBHOOK_BACKLOG_AGE_EXCEEDED` (`BILLING_HEALTH_WEBHOOK_BACKLOG_AGE_SECONDS=300`):** Oldest unprocessed (`pending` or `failed`) webhook older than 5 minutes.
6. **`REPEATED_WEBHOOK_FAILURES` (`BILLING_HEALTH_WEBHOOK_FAILED_COUNT_THRESHOLD=5`) & `QUARANTINED_WEBHOOK_EVENTS`:** Elevated retrying failures (`failed`) or any `quarantined` webhook events.
7. **`WEBHOOK_EXPIRED_LEASES` / `RECOVERY_EXPIRED_CLAIMS` / `REPEATED_EXPIRED_LEASES_OR_QUARANTINE` (`BILLING_HEALTH_REPEATED_LEASE_EXPIRY_THRESHOLD=2`):** Repeated lease/claim expirations, crash anomalies, or exhausted recovery attempts (`next_recovery_retry_at IS NULL`).
8. **`RECOVERY_OVERDUE_BACKLOG` (`BILLING_HEALTH_RECOVERY_OVERDUE_AGE_SECONDS=300`):** Overdue `recovery_required` attempt older than 5 minutes past `next_recovery_retry_at`.
9. **`PENDING_RECONCILIATION_CASES` (`BILLING_HEALTH_RECONCILIATION_BACKLOG_AGE_SECONDS=3600`):** Unresolved `payment_reconciliations` rows awaiting operator review.
10. **`MONITORING_EVALUATION_FAILED`:** Database connectivity or health snapshot query failure during a `billing_health` run (payload contains sanitized error diagnostics only).

### Incident Response Quick Reference
- **Stale / Superseded Worker (`STALE_WORKER_HEARTBEAT`, `NO_ACTIVE_*_WORKER`, `ownership_lost` logs):** Inspect worker service health and logs in Render, verify database pooler reachability and unique `RAZORPAY_*_WORKER_ID` per instance (never share one ID across multiple live processes), and restart the stopped worker instance.
- **Webhook Backlog / Quarantined Events (`WEBHOOK_BACKLOG_*`, `QUARANTINED_WEBHOOK_EVENTS`):** Verify `razorpay_webhook_worker` liveness and inspect quarantined events read-only via `python -m app.workers.billing_reconciliation list-quarantined-webhooks --environment <env>`. Investigate linked `payment_reconciliations` before taking manual operator action.
- **Exhausted Recovery / Pending Reconciliations (`RECOVERY_OVERDUE_*`, `REPEATED_EXPIRED_LEASES_OR_QUARANTINE`, `PENDING_RECONCILIATION_CASES`):** Inspect exhausted attempts via `python -m app.workers.billing_reconciliation list-exhausted-recoveries --environment <env>` and open cases via `python -m app.workers.billing_reconciliation list --environment <env>`, resolving verified cases cleanly with `resolve --confirm`.

---

## 6. Post-Deploy Manual Verification Checklist (Staging First)

1. Confirm `GET /health` returns HTTP `200` and `GET /health/db` returns HTTP `200`.
2. Confirm both worker Render logs show startup with `environment=test` (staging) and zero config validation errors.
3. Run `python -m app.workers.billing_health --environment test` and confirm both `razorpay_webhook` and `razorpay_checkout_recovery` appear as non-stale `running` workers.
4. Trigger a test-mode Razorpay checkout and verify webhook ingestion -> `processed` state progression and dashboard CORS access from staging Vercel origin.
5. Restart each worker manually from the Render Dashboard and confirm graceful `SIGTERM` shutdown logs (`stopped cleanly`) and clean `billing_worker_heartbeats` takeover/registration upon restart.
6. Production rollout remains **blocked** until staging verification passes and explicit production authorization is granted.
