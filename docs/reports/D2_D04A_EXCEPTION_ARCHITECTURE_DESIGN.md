# Aptlyst AI — D-04A: Razorpay Webhook Exception Hierarchy, Error Classification & Transaction Reconciliation Design

**Document ID:** D-04A (`D2_D04A_EXCEPTION_ARCHITECTURE_DESIGN.md`)  
**Design Mode:** **DESIGN ONLY** *(zero source, test, model, migration, CI, or Git mutations performed)*  
**Target Baseline Worktree:** [`D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16)  
**Target Branch / Verified HEAD:** `validation/d2-stage1-customer-pg16` @ `497852e19360d85787107dbae6921519074f0773`  
**Parallel Dirty Reference Worktree (Preserved Untouched):** [`D:/Projects/V1/Aptlyst-AI`](file:///D:/Projects/V1/Aptlyst-AI) (`main` @ `027ab7ad3ee8a02fb20823a4f743e29b03c0f248`)  
**Input Investigation:** [`D2_D04_RAZORPAY_WEBHOOK_SAFETY_INVESTIGATION.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04_RAZORPAY_WEBHOOK_SAFETY_INVESTIGATION.md)  
**Design Verdict:** **`DESIGN READY FOR IMPLEMENTATION`**

---

## 1. Executive Summary

### 1.1 Purpose & Scope
Before Aptlyst AI authorizes D-2 Stage 2 Razorpay payment settlement, [`RazorpayWebhookWorker`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L64-L342) and the Razorpay webhook subsystem require a deterministic, fail-closed exception architecture and atomic persistence boundary. This design document provides a complete implementation-ready specification resolving the D-04 webhook worker exception, reconciliation durability, cancellation, SQLSTATE classification, and redaction defects on committed Stage 1 (`497852e`).

*(Out of scope for D-04A: commercial Stage 2 policies `P-01`–`P-04`, D04-03 checkout attempt state-machine business rules in `handle_subscription_activated()` / `handle_subscription_authenticated()`, and any modification to Stripe business logic.)*

### 1.2 Core Architectural Decisions

1. **Unify Exception Hierarchies Under One Provider-Neutral + Razorpay Taxonomy:**
   - Introduce a thin provider-neutral base module (`apps/api/app/services/billing/webhook_errors.py`, sibling under [`apps/api/app/services/billing/`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/__init__.py)) defining `BillingWebhookError`, `BillingWebhookRetryableError`, `BillingWebhookPermanentError`, `BillingWebhookLeaseError`, shared PostgreSQL `SQLSTATE` classification, and unified secret/PII redaction.
   - Make **`RazorpayWebhookEngineError`** (in [`webhook_engine.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/webhook_engine.py#L135)) the single canonical Razorpay processing base class, with `RazorpayWebhookRetryableError`, `RazorpayWebhookPermanentError`, and `StaleLeaseError` defined **once** and imported by [`handler_base.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/handler_base.py#L60-L131), [`agreement_handlers.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/agreement_handlers.py#L53), [`razorpay/__init__.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/__init__.py#L60-L119), and [`razorpay_webhook_worker.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L43-L56). Alias `RazorpayWebhookHandlerError = RazorpayWebhookEngineError` for backwards compatibility and preserve `webhook_ingress.RazorpayWebhookError` strictly for HTTP ingress.
2. **Eliminate Split-Session Commit Loss & Conflict Reconciliation Wipes Without Adding Commit A / Commit B:**
   - Retain **Phase 1 Claim (`claim_one()`)**: short `SELECT ... FOR UPDATE SKIP LOCKED` committing `(lease_token, locked_by, lease_expires_at)` and releasing the claim connection immediately.
   - Reserve **Phase 2 Pre-Transaction External I/O** strictly *outside* DB transactions per [`ADR-005`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md#L36-L38) (no-op for today's 4 agreement handlers; mandatory gateway isolation for future Stage 2 HTTP enrichment).
   - Merge handler domain execution, lease fencing, outer `PaymentReconciliation` upsert, and `BillingWebhookEvent` outcome recording into **Phase 3: One Atomic Persistence Transaction (`work_db`)** wrapping handler execution in `async with work_db.begin_nested():` (PostgreSQL `SAVEPOINT`), mirroring [`execute_claimed_webhook_event()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/stripe_webhook_engine.py#L524-L677) plus Razorpay lease fencing and atomic `INSERT ... ON CONFLICT (provider, merchant_account_id, environment, case_key) DO UPDATE`.
3. **Prevent `23503` Foreign-Key Crashes During Outer Reconciliation Persistence:**
   - On conflict savepoint rollback, child ORM objects created inside the rolled-back savepoint (`sub.id`, newly staged `invoice.id`, `payment.id`) do not exist in PostgreSQL. Outer reconciliation persistence MUST persist **only pre-committed safe foreign keys** (`raw_event_id = claimed.event_id`, verified committed `organization_id` else `None`) and keep transient/uncommitted IDs inside `diagnostic_payload`.
4. **Explicit Classification of Database (`SQLSTATE`), Client HTTP, Internal, and Cancellation Paths:**
   - Transient DB concurrency/connection SQLSTATEs (`40001`, `40P01`, `55P03`, `57P*`, `53300`, `08*`) and idempotent gateway transient errors (`429`, `5xx`, pre-dispatch network) retry cleanly up to `MAX_WEBHOOK_ATTEMPTS` (`5`, backoff `5s/15s/45s/120s`).
   - Permanent DB violations (`P0001` PL/pgSQL immutability triggers, `23502`, `23503`, `23514`, `22*`, `42*`), client config/4xx rejections (`RazorpayAuthError`, `RazorpayBillingDisabledError`, `RazorpayConfigurationError`, HTTP `400/404/422`), validation errors, and reconciliation conflicts quarantine on attempt `1`.
   - Python 3.11 `asyncio.CancelledError` (`BaseException`) is never swallowed into retry/quarantine counters, rolls back open DB sockets cleanly without leaving `idle in transaction` backends, preserves `attempts`, and releases ownership cleanly via natural lease expiry (or optional bounded cooperative release only on clean non-cancelled shutdown drain).
5. **Comprehensive Secret, DSN, SQL-Parameter & PII Redaction + PG16 Integration Gate:**
   - Replace partial `_SECRET_PATTERNS` with a unified sanitizer scrubbing `Basic` auth, PostgreSQL DSNs, libpq keywords, SQLAlchemy `[parameters: ...]` blocks, `rzp_*`/`whsec_*`/`sk_*`/`rk_*`/`pk_*`, signatures, PANs (UUID-safe), emails, and phones; ban raw `exc_info=True` and unredacted `ctx.raw_payload` persistence; add multi-connection real PostgreSQL 16 integration verification (`PG-WRK-01..14`) wired into [`.github/workflows/ci.yml`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/.github/workflows/ci.yml).

### 1.3 Design Verdict
**`DESIGN READY FOR IMPLEMENTATION`** — The exception architecture, worker decision tree, transaction/reconciliation boundary, sanitization rules, and PostgreSQL 16 verification suite are completely specified and have zero commercial policy dependencies (`P-01`–`P-04`).

---

## 2. Current Architecture Findings (Independently Verified on Stage 1 `497852e`)

All findings below were independently re-verified against committed source files in [`D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16) and extend [`D2_D04_RAZORPAY_WEBHOOK_SAFETY_INVESTIGATION.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04_RAZORPAY_WEBHOOK_SAFETY_INVESTIGATION.md) with additional verified package/FK/sanitization nuances.

### 2.1 Dual Exception Hierarchy & Package Export Masking (`D04-02`)

1. **Disjoint Inheritance Trees:**
   - [`webhook_engine.py#L135-L148`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/webhook_engine.py#L135-L148) defines `RazorpayWebhookEngineError` with `RazorpayWebhookRetryableError`, `RazorpayWebhookPermanentError`, and `StaleLeaseError`.
   - [`handler_base.py#L60-L131`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/handler_base.py#L60-L131) imports **only** `StaleLeaseError` (`#L62`) from `webhook_engine.py`, then defines a completely unrelated hierarchy rooted at `RazorpayWebhookHandlerError(Exception)` (`#L73`) declaring identically named `RazorpayWebhookRetryableError` (`#L77`) and `RazorpayWebhookPermanentError` (`#L84`), plus four subclasses (`RazorpayWebhookReconciliationConflictError`, `RazorpayPayloadValidationError`, `RazorpayScopeMismatchError`, `RazorpayMissingEntityIdError`).
2. **Package Re-Export Shadowing Bug (Newly Verified):**
   - [`razorpay/__init__.py#L68-L95`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/__init__.py#L68-L95) imports `RazorpayWebhookPermanentError` and `RazorpayWebhookRetryableError` from `webhook_engine` (`#L69-L70`), while importing `RazorpayPayloadValidationError`, `RazorpayScopeMismatchError`, `RazorpayMissingEntityIdError`, and `RazorpayWebhookReconciliationConflictError` from `handler_base` (`#L89-L95`) — **never re-exporting `handler_base`'s base pair**. Consequently, `issubclass(razorpay.RazorpayPayloadValidationError, razorpay.RazorpayWebhookPermanentError)` evaluates to **`False`** at runtime!
3. **Worker Catch Failure:**
   - [`razorpay_webhook_worker.py#L49-L52, #L203-L208`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L49-L52) imports `webhook_engine`'s classes. For all production agreement handlers (`agreement_handlers.py`), `except RazorpayWebhookPermanentError:` (`#L203-L205`) is **unreachable dead code**; every permanent/validation/scope/conflict exception falls into `except (RazorpayWebhookRetryableError, Exception):` (`#L206-L208`) and triggers 5-attempt exponential retry loops (`5s -> 15s -> 45s -> 120s`, delaying quarantine by `185s`).
4. **Separate HTTP Ingress & Gateway Client Hierarchies (Verified Context):**
   - [`webhook_ingress.py#L58-L80`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/webhook_ingress.py#L58-L80) defines `RazorpayWebhookError` (`PayloadTooLarge`, `SignatureVerification`, `PayloadMalformed`, `MerchantMismatch`, `PersistenceError`) used solely at HTTP ingestion [`api/billing.py#L845-L951`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/api/billing.py#L845-L951). Note the naming collision hazard: the symbol `RazorpayWebhookError` already belongs to ingress in [`razorpay/__init__.py#L47`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/__init__.py#L47).
   - [`client.py#L23-L82`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/client.py#L23-L82) defines `RazorpayClientError` (`BillingDisabled`, `Configuration`, `Auth`, `RateLimit`, `API`, `Server5xx`, `Network(is_pre_dispatch)`). Neither worker nor engine currently classifies any `RazorpayClientError` subclass.

### 2.2 Transaction Ownership, Reconciliation Loss, FK Crash Risk & Lock Ordering (`D04-01`)

1. **Silent Rollback of Domain Writes & Non-Raising Reconciliations:**
   - [`RazorpayWebhookWorker.process_one()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L178-L237) opens `handler_db`, runs `await handler(...)` (which only calls `await db.flush()` at [`agreement_handlers.py#L324, #L565, #L690, #L794`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/agreement_handlers.py#L324)), never calls `await handler_db.commit()`, exits the context manager (`__aexit__` auto-rolls back PostgreSQL state under `AsyncSessionLocal` [`session.py#L17-L28`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/db/session.py#L17-L28)), and commits `status = 'processed'` in a third session `outcome_db`.
   - This also destroys non-raising `PaymentReconciliation` cases (`missing_checkout_attempt` `#L256-L277`, `unknown_subscription_updated` `#L633-L650`, `unknown_subscription_completed` `#L756-L773`).
2. **Conflict Reconciliation Wiped on Exception Rollback:**
   - Handlers call `stage_or_update_payment_reconciliation()` *inside* `handler_db` before raising `RazorpayWebhookReconciliationConflictError` (`#L221-L250`, `#L285-L315`, `#L490-L519`, `#L529-L558`), while [`resolve_tenant_organization()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/handler_base.py#L746-L810) raises `RazorpayWebhookReconciliationConflictError` (`conflicting_tenant_records`, `metadata_tenant_mismatch`, `unbound_provider_customer`) without staging any record. When `handler_db` rolls back, zero `PaymentReconciliation` rows survive.
3. **Transient Foreign-Key Crash Trap (`SQLSTATE 23503`):**
   - [`PaymentReconciliation`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L680-L710) enforces `ON DELETE RESTRICT` foreign keys to `organizations.id`, `subscriptions.id`, `billing_invoices.id`, `billing_payments.id`, and `billing_webhook_events.id`.
   - `agreement_handlers.py` passes **uncommitted, transaction-local** `subscription_id=sub.id` (`#L229, #L264, #L293, #L498, #L537`, where `sub` was added via `db.add(sub)` in that same transaction at `#L199` / `#L468`), and omits `raw_event_id` on `RazorpayWebhookReconciliationConflictError` (`#L240-L250`, `#L297-L315`, `#L509-L519`, `#L547-L558`). Persisting an uncommitted `sub.id` outside the rolled-back savepoint fails in PostgreSQL with `ForeignKeyViolationError` (`23503`).
4. **Non-Atomic `stage_or_update_payment_reconciliation()` (`handler_base.py#L844-L912`):**
   - Uses `SELECT ... FOR UPDATE` (without `populate_existing=True`) followed by `db.add()`. Two concurrent workers processing distinct events hitting the same `case_key` without a shared `L0A`/`L0B` lock (e.g., unbound customer or cross-org conflict) both select `None` and collide on `uq_payment_reconciliations_case_key` (`SQLSTATE 23505`).
5. **Dirty `main` Split-Commit Flaw & Lock-Ordering Hazards:**
   - Uncommitted `Step 1B` on [`D:/Projects/V1/Aptlyst-AI`](file:///D:/Projects/V1/Aptlyst-AI) adds `handler_db.commit()` ("Commit A") followed by `outcome_db` ("Commit B"), creating a split-brain crash window between domain persistence and queue completion, losing conflict reconciliations on `handler_db.rollback()`, omitting `lock_timeout`, acquiring `BillingWebhookEvent FOR UPDATE` inside domain execution using wall clock `datetime.now(timezone.utc)` (`handler_base.py#L971`) rather than `worker.clock`, and risking `L0B -> L0A` inversion if future handlers add `L0A(org_id)` after `acquire_razorpay_advisory_lock(db, "subscription", sub_id)`.

### 2.3 Missing DB SQLSTATE Classification, Leakage Gaps & Test Blind Spots

1. **Zero `SQLSTATE` Classification:** Neither `webhook_engine.py` nor `razorpay_webhook_worker.py` inspects `pgcode`/`sqlstate` (compare `_extract_db_error_details()` in [`customer.py#L380-L431`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/customer.py#L380-L431)). Consequently, PL/pgSQL immutability trigger failures (`SQLSTATE P0001` defined in migration [`e2f3a4b5c6d1#L343-L433`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py#L343-L433)) and deterministic constraint bugs (`23502`, `23503`, `23514`) spin through 5 retries instead of quarantining cleanly.
2. **Incomplete Sanitization & Unsafe Logging:**
   - [`webhook_engine.py#L155-L172`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/webhook_engine.py#L155-L172) `sanitize_error_message()` misses `Basic` HTTP credentials (caught only in [`client.py#L89`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/client.py#L89)), PostgreSQL URI passwords & `password=` DSN params (caught only in [`customer.py#L300-L343`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/customer.py#L300-L343)), SQLAlchemy `[parameters: ...]` JSON payload dumps, webhook signatures, `rk_`/`pk_` keys, emails, and phones.
   - `razorpay_webhook_worker.py` logs unsanitized `exc` at `#L153`, `#L189`, `#L240`, `#L255` (with `exc_info=True`), and `#L328` (with `exc_info=True`).
   - `agreement_handlers.py#L644, #L767` dumps unredacted `ctx.raw_payload` directly into `PaymentReconciliation.diagnostic_payload`.
3. **CI & Test Blind Spots:**
   - `test_razorpay_webhook_worker.py`, `test_razorpay_handler_foundation.py`, and `test_razorpay_agreement_handlers.py` use in-memory mock DB classes and are excluded from [`.github/workflows/ci.yml#L181-L250`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/.github/workflows/ci.yml#L181-L250). No live PostgreSQL 16 test exercises `RazorpayWebhookWorker` persistence, `CancelledError` teardown, or `PaymentReconciliation` conflict durability.

---

## 3. Proposed Exception Hierarchy & 8-Category Classification

### 3.1 Target Python Class Hierarchy

Create a new provider-neutral module `apps/api/app/services/billing/webhook_errors.py` (sibling to [`billing/__init__.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/__init__.py)) for shared provider-neutral bases, and unify [`webhook_engine.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/webhook_engine.py) + [`handler_base.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/handler_base.py) onto **one canonical Razorpay runtime exception tree** (preserving `webhook_ingress.RazorpayWebhookError` untouched for HTTP ingress):

```text
BaseException
 ├── asyncio.CancelledError                                   # NEVER caught in process_one(); propagates cleanly after TX rollback
 └── Exception
      ├── RazorpayWebhookError                                # [EXISTING in webhook_ingress.py#L58 — HTTP ingress ONLY]
      │
      └── BillingWebhookError                                 # [NEW in billing/webhook_errors.py — provider-neutral base]
           ├── BillingWebhookRetryableError                   # Transient errors eligible for bounded retry
           ├── BillingWebhookPermanentError                   # Non-retryable errors -> immediate quarantine
           │    ├── BillingWebhookValidationError
           │    ├── BillingWebhookScopeMismatchError
           │    ├── BillingWebhookConflictError
           │    └── BillingWebhookUnknownEventError
           ├── BillingWebhookLeaseError                       # Concurrency ownership abort (NOT retry/quarantine)
           │
           └── RazorpayWebhookEngineError(BillingWebhookError)# [CANONICAL RAZORPAY BASE in webhook_engine.py]
                │  (alias: RazorpayWebhookHandlerError = RazorpayWebhookEngineError)
                │
                ├── StaleLeaseError(RazorpayWebhookEngineError, BillingWebhookLeaseError)
                │    # Lease expired, stolen, deleted, or superseded; aborts without status/attempt mutation
                │
                ├── RazorpayWebhookRetryableError(RazorpayWebhookEngineError, BillingWebhookRetryableError)
                │    ├── RazorpayDatabaseRetryableError       # SQLSTATE 40001, 40P01, 55P03, 57P*, 53300, 08* / transient pool drops
                │    ├── RazorpayProviderTransientError       # HTTP 429, idempotent GET 5xx, pre-dispatch network errors, asyncio.TimeoutError
                │    └── RazorpayTransientRaceRetryError      # Optional bounded race guard (e.g., concurrent customer provisioning pending)
                │
                └── RazorpayWebhookPermanentError(RazorpayWebhookEngineError, BillingWebhookPermanentError)
                     ├── RazorpayPayloadValidationError(RazorpayWebhookPermanentError, BillingWebhookValidationError)
                     │    └── RazorpayMissingEntityIdError    # Missing/invalid sub_*, pay_*, inv_* identifier
                     ├── RazorpayScopeMismatchError(RazorpayWebhookPermanentError, BillingWebhookScopeMismatchError)
                     ├── RazorpayUnverifiedEventError         # Defense-in-depth if signature_verified is False at worker
                     ├── RazorpayUnknownEventError(RazorpayWebhookPermanentError, BillingWebhookUnknownEventError)
                     ├── RazorpayBusinessRejectionError       # Permanent non-conflict business state rejection
                     ├── RazorpayDatabasePermanentError       # SQLSTATE P0001 triggers, 23502, 23503, 23514, 22*, 42*
                     ├── RazorpayProviderPermanentError       # HTTP 400/401/404/422, RazorpayConfigurationError, BillingDisabled, ambiguous non-idempotent POST
                     └── RazorpayWebhookReconciliationConflictError(RazorpayWebhookPermanentError, BillingWebhookConflictError)
                          # Carries deterministic conflict_spec for atomic outer PaymentReconciliation upsert
```

> [!IMPORTANT]
> **Elimination of `handler_base.py` Duplicate Classes & `razorpay/__init__.py` Shadowing:**
> [`handler_base.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/handler_base.py#L60-L131) **MUST delete** its independent definitions of `RazorpayWebhookRetryableError` (`#L77`) and `RazorpayWebhookPermanentError` (`#L84`) and instead **import and re-export** the exact canonical classes from `webhook_engine.py`. Likewise, set `RazorpayWebhookHandlerError = RazorpayWebhookEngineError` as an alias so every existing `from ...handler_base import ...` import remains 100% source-compatible while `issubclass(RazorpayPayloadValidationError, RazorpayWebhookPermanentError)` and `issubclass(RazorpayWebhookReconciliationConflictError, RazorpayWebhookPermanentError)` become unconditionally `True` across all modules and [`razorpay/__init__.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/__init__.py).

### 3.2 Canonical Structured Attributes on Exceptions

All `RazorpayWebhookEngineError` subclasses share optional structured non-secret metadata attributes (never storing raw secrets/PII in `str(exc)`):

- `failure_category: str` *(stable machine-readable classification slug for telemetry/quarantine)*
- `error_code: str | None` *(e.g., SQLSTATE `'40001'`, HTTP `'429'`, or internal code)*
- `is_retryable: bool` *(class property derived from inheritance: `Retryable -> True`, `Permanent -> False`, `StaleLease -> False`)*

`RazorpayWebhookReconciliationConflictError` extends its constructor (`handler_base.py#L97-L118`) with strict FK-safety metadata:

| Field | Type | Required | FK / Rollback Safety Contract |
|---|---|---|---|
| `case_key` | `str` | Yes | Deterministic non-null key `<= 255` chars (bounded with SHA-256 suffix via `_build_reconciliation_case_key()` pattern if long) |
| `reason` | `str` | Yes | Short normalized slug `<= 64` chars (`payment_reconciliations.reason`) |
| `organization_id` | `uuid.UUID \| None` | Optional | Set ONLY when backed by a **pre-existing committed** `Organization` row (else `None`, placing candidate UUID inside `diagnostic_payload`) |
| `committed_subscription_id` *(alias `subscription_id`)* | `uuid.UUID \| None` | Optional | **MUST be `None`** if the `Subscription` was inserted inside the aborted savepoint; only pre-existing committed UUID allowed |
| `committed_invoice_id` *(alias `invoice_id`)* | `uuid.UUID \| None` | Optional | Must be pre-committed UUID or `None` |
| `committed_payment_id` *(alias `payment_id`)* | `uuid.UUID \| None` | Optional | Must be pre-committed UUID or `None` |
| `raw_event_id` | `uuid.UUID \| None` | Default `claimed.event_id` | Outer worker injects `claimed.event_id` automatically if omitted by handler |
| `diagnostic_payload` | `dict[str, Any]` | Optional | Sanitized structured conflict context (**zero raw credentials/emails/phones/full payloads**) |

### 3.3 Mandatory 8-Category Classification Matrix

> [!NOTE]
> **Persisted DB Enum vs. Logical Lease Disposition (`[processed | failed | quarantined | released]`):**  
> PostgreSQL enum `webhook_processing_status` ([`subscription.py#L46-L52`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/subscription.py#L46-L52)) defines exactly four committed row statuses: `pending`, `processed`, `failed`, and `quarantined`. There is no separate `'released'` SQL enum value; rather, for **Stale Lease** and **Process Cancellation**, the worker returns a logical **`LEASE_RELEASED` / aborted** outcome without mutating DB `status` or incrementing `attempts`, relinquishing ownership cleanly (either immediately if unowned/released cleanly on cooperative shutdown or upon `lease_expires_at` expiry).

| # | Category | Canonical Exception Type(s) | Operational Meaning | Retryable? | Event DB Status & Lease Disposition | Reconciliation Required? | Manual Review Required? |
|---|---|---|---|---|---|---|---|
| **1** | **Permanent business rejection** | `RazorpayWebhookReconciliationConflictError`, `RazorpayBusinessRejectionError`, `RazorpayUnknownEventError`, non-conflict business state rejections | Authoritative state conflict (`metadata_tenant_mismatch`, `conflicting_tenant_records`, `unbound_provider_customer`, `checkout_attempt_tenant_mismatch`, `ambiguous_checkout_attempt`), unregistered event type, or deterministic rule violation that retries cannot cure | **No** *(immediate fail-closed quarantine on current attempt)* | **`quarantined`** (`attempts += 1`, `quarantined_at = now`, `quarantine_reason` set, lease cleared) | **Yes** for `RazorpayWebhookReconciliationConflictError` (`PaymentReconciliation` upserted atomically in outer TX); **No** for deterministic `UnknownEvent` / pure business rejections | **Yes** *(reconciliation `pending_review` and/or quarantined event queue)* |
| **2** | **Validation failure** | `RazorpayPayloadValidationError`, `RazorpayMissingEntityIdError`, `RazorpayScopeMismatchError`, `RazorpayUnverifiedEventError` | Malformed JSON schema, invalid/non-positive timestamp/amount/currency, missing `sub_*`/`pay_*`/`inv_*`, wrong `(provider, merchant_account_id, environment)`, or `signature_verified == False` | **No** | **`quarantined`** (`attempts += 1`, `quarantined_at = now`, `quarantine_reason` set, lease cleared) | **No** *(no domain ledger ambiguity before entity binding; `ScopeMismatch` emits ops alert/metric; future money-bearing mismatch may optionally raise conflict instead)* | **Yes** *(engineering/ops triage of malformed or misrouted event)* |
| **3** | **Stale lease / ownership failure** | `StaleLeaseError` | Held `(lease_token, locked_by)` no longer matches DB row, `lease_expires_at < now`, or event row was removed/reclaimed by another worker | **No** *(this worker MUST abort immediately; current owner/next claim handles execution)* | **Logical `released` / aborted** — DB `status`, `attempts`, `next_retry_at`, and current owner's lease remain **untouched** (`ProcessResult.status` reflects non-mutating failure/aborted state) | **No** *(winner worker handles domain & reconciliation)* | **No** *(logged as warning / fencing metric; investigate only if chronic lease expiry spike)* |
| **4** | **Idempotent duplicate event** | *(Ingress `uq_billing_webhook_provider_event`)*; DB-level `WebhookAlreadyProcessedError` *(or lease check on terminal row)*; Entity-level idempotent no-op / `is_stale_event()` / `is_terminal_subscription()` early return *(or optional `RazorpayIdempotentDuplicateNoop`)* | Exact duplicate delivery (`provider_event_id`), concurrent already-`processed` replay, or distinct event ID whose payload is chronologically stale or already applied | **No** | **`processed`** *(for valid entity-level duplicate/stale event: `processed_at = now`, error cleared, lease cleared)*; **Unchanged `processed`** if blocked at ingress or already terminal | **No** *(clean monotonic replay — note stale capture after expired checkout belongs to Cat. 1 conflict per `ADR-002` / `P-03`, not Cat. 4)* | **No** |
| **5** | **Recoverable DB failure** | `RazorpayDatabaseRetryableError` *(subclass of `RazorpayWebhookRetryableError`)* | Transient PostgreSQL SQLSTATE (`40001` serialization, `40P01` deadlock, `55P03` lock_timeout, `57P01..03` admin/crash shutdown, `53300` too many connections, `08*` connection reset) or transient `asyncpg`/pool disconnect. *(Contrast `P0001` trigger & `23502/23503/23514` -> `RazorpayDatabasePermanentError` -> Cat. 1/2 `quarantined`!)* | **Yes** *(up to `max_attempts = 5`; backoff `5s -> 15s -> 45s -> 120s`)* | **`failed`** for attempts `1..max_attempts-1` (`next_retry_at` set, lease cleared); **`quarantined`** when `attempts >= max_attempts` (`5`) | **No** on retries `1..4` (full TX rollback left zero partial state); **No** unless attempt `5` exhausted after post-dispatch external call | **No** on transient recovery; **Yes** if retry limit (`5`) exhausted into `quarantined` |
| **6** | **Provider / API transient failure** | `RazorpayProviderTransientError` *(subclass of `RazorpayWebhookRetryableError` wrapping `RazorpayRateLimitError` 429, idempotent GET `RazorpayServer5xxError` 5xx, pre-dispatch `RazorpayNetworkError`, `asyncio.TimeoutError`)*. *(Contrast `AuthError` 401, `BillingDisabled`, `Configuration`, 400/404/422 & post-dispatch ambiguous mutation -> `RazorpayProviderPermanentError`)* | External Razorpay API call in **Phase 2 (outside DB TX)** hits temporary throttling, 5xx gateway outage on idempotent read, or connect timeout before dispatch. *(If a future non-idempotent POST mutation hits post-dispatch timeout/5xx -> escalate to conflict/permanent rather than blind retry!)* | **Yes** for idempotent transient failures (up to `max_attempts = 5`); **No** for config/auth/4xx or post-dispatch ambiguous mutation | **`failed`** (`next_retry_at` backoff, lease cleared) until attempt `5` -> **`quarantined`** *(immediate `quarantined` for permanent/ambiguous provider failure)* | **Conditional:** **No** for pre-dispatch/read 5xx/429; **Yes** for ambiguous post-dispatch remote mutation (`PaymentReconciliation` staged in outer TX) | **Conditional:** **No** during recoverable retries `1..4`; **Yes** upon quarantine or ambiguous remote mutation |
| **7** | **Unexpected internal failure** | Unmapped `Exception` (`TypeError`, `KeyError`, `AttributeError`, `RuntimeError`, unhandled library bug) caught after all explicit classifiers | Software defect or unforeseen runtime failure not matched by permanent/validation/SQLSTATE/provider classifiers | **Bounded Yes** *(classified as `RETRYABLE_FAILURE` with distinct `failure_category="unexpected_internal_error"` up to `max_attempts = 5`, then quarantined)* — *see Section 11 for immediate-quarantine option on `TypeError`/`AttributeError`* | **`failed`** for attempts `1..max_attempts-1` (`attempts += 1`, sanitized `error_message`, `next_retry_at` backoff, lease cleared); **`quarantined`** on `5th` attempt | **No** *(savepoint/TX rollback preserves clean DB invariants; see Section 6.2 if Phase 3 commit fails)* | **Yes** *(emit high-severity internal-bug alert even on attempt 1; manual intervention required once `quarantined`)* |
| **8** | **Process cancellation** | `asyncio.CancelledError` (`BaseException` in Python 3.11) | Task cancellation, worker shutdown (`SIGTERM`/`SIGINT` escalation), event loop teardown, or lock-wait cancellation test (`PG-CUST-17` equivalent) | **No** *(worker exits cleanly; event remains claimable by surviving workers)* | **Logical `released` / aborted** — PG connection closed & TX rolled back immediately; DB `status` (`pending`/`failed`) and **`attempts` UNCHANGED**; lease clears naturally on `lease_expires_at` (`<= 60s`) without attempt penalty *(or cooperative short lease clear only on unblocked graceful drain)* | **No** *(full uncommitted state rolled back cleanly by PG)* | **No** |

> [!CAUTION]
> **Critical Separation — PostgreSQL Permanent Errors (`RazorpayDatabasePermanentError`):**  
> Never map `SQLSTATE P0001` (PL/pgSQL immutability triggers `trg_prevent_*` from [`e2f3a4b5c6d1`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py#L343-L433)), `23502` (`not_null_violation`), `23503` (`foreign_key_violation`), `23514` (`check_violation`), `22*` (`data_exception`), or `42*` (`syntax_or_access_rule`) to `RETRYABLE_FAILURE`. Those deterministically fail on every attempt and MUST classify directly into `RazorpayDatabasePermanentError` -> `PERMANENT_FAILURE` -> immediate `quarantined`.  
> For `23505` (`unique_violation`): when raised on an `ON CONFLICT`-exempt table without advisory lock serialization (unlikely once `L0A -> L0B` is enforced, except possible cross-module races), classify `23505` as bounded `RETRYABLE_FAILURE` **unless** raised on a known deterministic constraint without concurrent actor eligibility, preventing false quarantines under brief `READ COMMITTED` visibility races.

---

## 4. Worker Decision Tree

### 4.1 High-Level Processing Flowchart

```mermaid
flowchart TD
    Start["1. claim_one(): Session 1<br/>FOR UPDATE SKIP LOCKED -> Commit lease -> Close session"] --> QEmpty{"Event claimed?"}
    QEmpty -- "No" --> ReturnNone["Return None (sleep poll_interval)"]
    QEmpty -- "Yes (ClaimedWebhookEvent)" --> CheckHandler{"Handler registered<br/>for event_type?"}

    CheckHandler -- "No" --> OutcomeUnknown["Phase 3 Atomic TX: Lock PK FOR NO KEY UPDATE,<br/>Fence Lease -> UNKNOWN_EVENT -> quarantined -> COMMIT"]

    CheckHandler -- "Yes" --> Phase2["2. Optional Phase 2: External Razorpay HTTP<br/>(STRICTLY OUTSIDE DB TX / LOCKS)"]
    Phase2 -->|"Client Permanent/Ambiguous<br/>(401, Config, 4xx, Post-Dispatch POST 5xx/Net)"| PrepPerm["Prepare PERMANENT_FAILURE<br/>(+ optional Conflict spec)"]
    Phase2 -->|"Client Transient<br/>(429, GET 5xx, Pre-Dispatch Net)"| PrepRetry["Prepare RETRYABLE_FAILURE"]
    Phase2 -- "OK (or No HTTP needed)" --> Phase3["3. Phase 3: Single Atomic Persistence TX (work_db)<br/>SET LOCAL lock_timeout -> Acquire Level -1 PK FOR NO KEY UPDATE"]

    Phase3 --> Cancelled["asyncio.CancelledError<br/>(at ANY step!)"]
    Cancelled --> RollbackCancel["Immediate Session Rollback + Socket Close<br/>attempts UNCHANGED, lease expires naturally<br/>RE-RAISE CancelledError"]

    Phase3 --> FencePre{"Lease valid & unexpired?<br/>(token, worker, expires_at >= clock_now)"}
    FencePre -- "No" --> StaleAbort["Rollback TX -> Raise StaleLeaseError caught -> Return Lease-Lost Result<br/>(ZERO DB outcome/status mutation)"]

    FencePre -- "Yes" --> Savepoint["BEGIN NESTED SAVEPOINT<br/>Execute handler(ctx, work_db)<br/>(Locks strictly: L0A -> L0B -> L1..L6)"]

    Savepoint -- "Clean Return / Idempotent No-op" --> PreCommitFence{"Re-check lease_expires_at<br/>+ PK fence at commit?"}
    PreCommitFence -- "Expired/Stolen" --> StaleAbort
    PreCommitFence -- "Valid" --> CommitSuccess["Set status='processed', clear lease<br/>(Commits Handler Writes + Status Together)<br/>SINGLE ATOMIC COMMIT"]

    Savepoint -- "RazorpayWebhookReconciliationConflictError" --> RollbackSP1["Savepoint Rolled Back (Outer TX Alive!)"] --> UpsertRec["Outer TX: Atomic PG INSERT ... ON CONFLICT<br/>PaymentReconciliation (Committed FKs Only)"] --> CommitPerm1["Set status='quarantined', attempts += 1,<br/>sanitized error & reason, clear lease -> SINGLE ATOMIC COMMIT"]

    Savepoint -- "Other RazorpayWebhookPermanentError<br/>(Validation, Scope, MissingID, BizReject, P0001/23503 DB)" --> RollbackSP2["Savepoint Rolled Back (Outer TX Alive!)"] --> CommitPerm2["Set status='quarantined', attempts += 1,<br/>sanitized error & reason, clear lease -> SINGLE ATOMIC COMMIT"]

    Savepoint -- "StaleLeaseError" --> StaleAbort

    Savepoint -- "RazorpayWebhookRetryableError<br/>(DB 40001/40P01/55P03/08*, Transient Race)" --> RollbackSP3["Savepoint Rolled Back (Outer TX Alive!)"] --> RetryOrQ["attempts += 1;<br/>If attempts >= max_attempts -> 'quarantined'<br/>Else -> 'failed' + backoff next_retry_at;<br/>Clear lease -> SINGLE ATOMIC COMMIT"]

    Savepoint -- "Unexpected Exception<br/>(Inspect SQLSTATE -> Permanent or Retryable;<br/>else Unmapped Internal Bug)" --> RollbackSP4["Savepoint Rolled Back"] --> ClassifyExc{"SQLSTATE<br/>Permanent?"}
    ClassifyExc -- "Yes (P0001, 23502/03/14, 42*)" --> CommitPerm2
    ClassifyExc -- "No (40001/40P01/55P03/08* or Unmapped Bug)" --> RetryOrQ
```

### 4.2 Deterministic Exception Classification Algorithm (`process_one()`)

1. **Invariant 1 (`CancelledError` First & Untouchable):** Python 3.11 `asyncio.CancelledError` inherits from `BaseException`, **never** `Exception`. Neither `claim_one()`, nor `handler`, nor `process_one()` catches `BaseException`. `async with self.session_factory() as work_db:` executes SQLAlchemy's async exit cleanup (rolling back uncommitted work and closing the `asyncpg` socket), and `CancelledError` propagates directly up to [`RazorpayWebhookWorker.start()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L325-L326) (or test caller awaiting `process_one()`).
2. **Invariant 2 (`StaleLeaseError` Before Permanent/Retryable):** Check `except StaleLeaseError:` explicitly *before* `except RazorpayWebhookPermanentError:` or `except RazorpayWebhookRetryableError:` (even though `StaleLeaseError` does not inherit from either). Always roll back `work_db` completely and return `ProcessResult(success=False, status=WebhookProcessingStatus.failed, attempts=claimed.attempts, lease_lost=True)` without writing to `billing_webhook_events` or `payment_reconciliations`.
3. **Invariant 3 (`SQLSTATE` Classification Before Generic Catch):** When an unhandled exception escapes `async with work_db.begin_nested():`:
   - First check if `exc` is already `StaleLeaseError`, `RazorpayWebhookPermanentError`, or `RazorpayWebhookRetryableError`.
   - Else check `isinstance(exc, RazorpayClientError)` (if triggered inside future Phase 2 / handler helper) and translate cleanly into `RazorpayProviderPermanentError` vs `RazorpayProviderTransientError`.
   - Else run `classify_db_exception(exc)` inspecting the full `__cause__`/`__context__`/`orig` chain via `_extract_db_error_details()` semantics:
     - Known transient `SQLSTATE` (`40001`, `40P01`, `55P03`, `57P01`, `57P02`, `57P03`, `53300`, `08000`, `08003`, `08006`, `23505` if marked concurrency-safe) -> wrap as `RazorpayDatabaseRetryableError` (`failure_category="db_retryable"`).
     - Known permanent `SQLSTATE` (`P0001`, `23502`, `23503`, `23514`, `22*`, `42*`) -> wrap as `RazorpayDatabasePermanentError` (`failure_category="db_permanent"`).
     - Asyncpg/SQLAlchemy pool/interface disconnect without SQLSTATE -> `RazorpayDatabaseRetryableError` (`failure_category="db_connection_retryable"`).
   - Else (true unmapped `Exception` bug: `TypeError`, `KeyError`, `AttributeError`, `ValueError`, etc.) -> map to `ProcessingOutcome.RETRYABLE_FAILURE` with distinct `failure_category="unexpected_internal_error"` (log critical sanitized diagnostic + increment attempts up to `max_attempts` -> quarantine).
4. **Invariant 4 (Unknown Event Type / Unverified Defense-in-Depth):**
   - If `claimed.signature_verified` were ever `False` *(note `ClaimedWebhookEvent` adds `signature_verified: bool = True` or handler guard verifies DB column `event.signature_verified is True`)* -> immediately map to `RazorpayUnverifiedEventError` -> `PERMANENT_FAILURE` -> `quarantined` + security alert.
   - If `dispatcher.get_handler(claimed.event_type) is None` -> `ProcessingOutcome.UNKNOWN_EVENT` -> `attempts += 1`, `status = 'quarantined'`, `quarantine_reason = "Unknown event type: '<type>'"`, lease cleared, committed atomically.
5. **Invariant 5 (Outer Transaction Persistence Failure Safety):**
   - What if `work_db` fails *during* outer `PaymentReconciliation` upsert or final `await work_db.commit()` (e.g., network drop during `COMMIT`)?
   - `work_db.rollback()` guarantees **zero partial domain mutations and zero phantom `processed` markers**.
   - Optionally open one short isolated `fallback_outcome_db` session (strictly fenced by `(lease_token, locked_by, lease_expires_at >= now)`) to record sanitized `RETRYABLE_FAILURE` (`attempts += 1`, backoff `next_retry_at`, clear lease). If `fallback_outcome_db` also fails (PG down), suppress only after logging sanitized error: the DB row keeps its original `status` (`pending`/`failed`) and `attempts` intact, recovering cleanly once `lease_expires_at` (`<= 60s`) expires!
6. **Invariant 6 (Worker Clock Consistency & Polling Loop Fix):**
   - Pass `now = self._get_now()` into **all** claim, lease fencing, savepoint expiry context (`ctx.now_utc`), and outcome recording operations (eliminating wall-clock drift in `handler_base.assert_valid_worker_lease()`).
   - Fix `RazorpayWebhookWorker.start()` ([#L313-L324](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L313-L324)): sleep `poll_interval` when `len(results) < self.batch_size` (or if every returned result in a batch failed lease ownership / retry deferment), preventing CPU tight-loop spinning when `1 <= len(results) < batch_size`!

---

## 5. Event-State Transition Table

Every transition on `BillingWebhookEvent` respects `webhook_processing_status` (`pending`, `processed`, `failed`, `quarantined`) and enforces monotonic progression once terminal (`processed`, `quarantined`).

| Current `status` & Lease State | Event / Worker Condition | Next DB `status` | `attempts` Mutation | `next_retry_at` | `processed_at` / `quarantined_at` | `lease_token` / `locked_by` / `lease_expires_at` | Notes / Invariants |
|---|---|---|---|---|---|---|---|
| `pending` *(lease NULL or expired)* | `claim_next_razorpay_webhook_event()` | `pending` | Unchanged (`0` or current) | `None` (preserved) | Preserved (`None`) | **Stamped:** `(uuid4, worker_id, now + lease_sec)` | Committed in Session 1 before handler runs; lease recovery does **NOT** increment attempts (`0` for fresh event). |
| `failed` *(next_retry_at <= now; attempts < max; lease NULL or expired)* | `claim_next_razorpay_webhook_event()` | `failed` | Unchanged (`k`) | Preserved | Preserved | **Stamped:** `(uuid4, worker_id, now + lease_sec)` | Stamped under `FOR UPDATE SKIP LOCKED`. |
| `pending` or `failed` *(claimed by `W1`)* | Handler returns cleanly OR idempotent duplicate / stale entity no-op | **`processed`** | Unchanged | `None` | `processed_at = now`; `quarantined_at = None` | **Cleared (`None, None, None`)** | Commits atomically with domain writes in Phase 3 single `COMMIT`; clears `error_message = None`. |
| `pending` or `failed` *(claimed by `W1`)* | Category 1 or Category 2 (`Permanent`, `Validation`, `ScopeMismatch`, `MissingEntityId`, `ReconciliationConflict`, `P0001`/`23502`/`23503`, `UNKNOWN_EVENT`, permanent provider 4xx/config) | **`quarantined`** | **`attempts += 1`** | `None` | `processed_at = preserved/None`; **`quarantined_at = now`** | **Cleared (`None, None, None`)** | Immediate terminal quarantine on 1st permanent error (`attempts` becomes `k + 1`); savepoint rolled back; outer conflict `PaymentReconciliation` committed in same TX. |
| `pending` or `failed` *(claimed by `W1`, `attempts + 1 < max_attempts`)* | Category 5 (`DB Retryable`), Category 6 (`Provider Transient`), Category 7 (`Unexpected Internal Bug`) | **`failed`** | **`attempts += 1`** | **`now + backoff(attempts)`** (`5s, 15s, 45s, 120s`) | `processed_at = now` | **Cleared (`None, None, None`)** | `error_message = sanitize(...)`; savepoint rolled back (`0` domain mutations). |
| `pending` or `failed` *(claimed by `W1`, `attempts + 1 >= max_attempts`)* | Category 5 / 6 / 7 exhausts attempt `5` (`max_attempts`) | **`quarantined`** | **`attempts += 1`** (`= 5`) | `None` | `processed_at = now`; **`quarantined_at = now`** | **Cleared (`None, None, None`)** | `quarantine_reason = "Quarantined: exceeded max attempts (5/5)..."`; sanitized `error_message` preserved. |
| `pending` or `failed` *(claimed by `W1`, lease stolen/expired before Phase 3 commit)* | Category 3 (`StaleLeaseError` raised by `W1`) | **Unchanged** *(owned by `W2` or awaiting recovery)* | **Unchanged** | **Unchanged** | **Unchanged** | **Unchanged** (`W1` forbidden to touch row) | Logical result = `released` / aborted (`ProcessResult.success=False`); `W1` rolls back local TX completely. |
| `pending` or `failed` *(claimed by `W1`, task cancelled mid-flight)* | Category 8 (`asyncio.CancelledError`) | **Unchanged** (`pending` or `failed`) | **Unchanged** | **Unchanged** | **Unchanged** | Retained until `lease_expires_at` *(auto-releases without attempt penalty)* | Logical result = `released` after lease window (or optional short fenced clear only on cooperative shutdown drain). Zero `idle in transaction` connections. |
| `processed` or `quarantined` | Any incoming duplicate delivery / worker claim / stale write | **Terminal Immutable** | **Immutable** | `None` | **Immutable** | `None` | Ingress returns `200 duplicate`; worker claim query `WHERE status IN ('pending', 'failed')` ignores permanently; fencing rejects stale late writers. |

---

## 6. Reconciliation Transaction Requirements

### 6.1 Three-Phase Lifecycle & Exact Lock Ordering

To maintain compatibility with [`ADR-005`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md) and [`D2_STAGE2_READINESS_AUDIT.md` Section 4.2](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_STAGE2_READINESS_AUDIT.md#L130-L133), Razorpay webhook processing obeys:

1. **Phase 1 — Claim (`Session 1`, short transaction):**
   - Query `BillingWebhookEvent` (`WHERE ... FOR UPDATE SKIP LOCKED`), stamp `(lease_token, locked_by, lease_expires_at)`, `COMMIT`, close connection.
   - Zero `L0A..L7` locks touched.
2. **Phase 2 — External Network Calls (`NO DB Session`, outside all locks):**
   - Required by [`ADR-005`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md#L36-L38) before any DB write session opens if future Stage 2 handlers invoke `RazorpayClient`.
   - Zero DB connections open while HTTP sockets block.
3. **Phase 3 — Single Atomic Persistence Transaction (`Session 2: work_db`, `Level -1 ≺ L0A ≺ L0B ≺ L1A..L7`):**
   - **Pre-Handler Fencing (`Level -1`, bounded):**
     ```sql
     SET LOCAL lock_timeout = '5000';  -- bounded strictly below lease duration (e.g. 5s vs 60s)
     SELECT * FROM billing_webhook_events WHERE id = :event_id FOR NO KEY UPDATE;
     ```
     *Why `FOR NO KEY UPDATE` + `Single-Row Root-Node Invariant`:*
     - **Single-Row Root Rule:** Locks at most **1 PK row** in `billing_webhook_events`, holds **0 HTTP calls**, and never waits on domain tables (`checkout`/`accounting` NEVER lock `billing_webhook_events FOR UPDATE/NO KEY UPDATE`).
     - **`FOR NO KEY UPDATE` vs `FOR KEY SHARE`:** Foreign-key inserts into `billing_invoices(raw_event_id)`, `billing_payments(raw_event_id)`, and `payment_reconciliations(raw_event_id)` acquire `FOR KEY SHARE` on `billing_webhook_events(id)`. In PostgreSQL 16, `FOR NO KEY UPDATE` **does not conflict with `FOR KEY SHARE`**, eliminating spurious lock waits/deadlocks from concurrent FK child inserts referencing historical events.
     - Verify `(lease_token == claimed.lease_token and locked_by == claimed.locked_by and lease_expires_at >= clock_now)`. If false -> rollback & raise `StaleLeaseError`.
   - **Tenant Discovery & Monotonic `L0A -> L0B` Rule Inside Handler Savepoint (`async with work_db.begin_nested():`):**
     - **Important `L0B -> L0A` Deadlock Prevention:** Handlers currently call `acquire_razorpay_advisory_lock(db, "subscription", sub_id)` (`L0B`) **before** knowing `organization_id` (`L0A`). Once `L0A(org_id)` is enabled in Stage 2 (alongside checkout/verify which acquire `L0A -> L0B`), calling `L0A` *after* `L0B` would create an `L0B <-> L0A` deadlock cycle!
     - **Design Contract:** Perform a **non-locking** discovery `SELECT` (`NO FOR UPDATE`, `NO L0B` held yet) to resolve candidate `org_id` from `BillingCustomerProvision` / `Subscription`; acquire **`L0A(org_id)` first** (when `org_id` exists), then acquire **lexicographically sorted `L0B` entity keys** (`acquire_razorpay_advisory_lock` sorted by `(entity_type, entity_id)`), and then lock/re-verify `Organization`, `BillingCustomerProvision`, `OrganizationBillingPeriod`, `Subscription` (`ORDER BY id ASC`, `.execution_options(populate_existing=True)`), `BillingInvoice`, `BillingPayment`, and `BillingCheckoutAttempt` strictly monotonically (`L1A -> L7`).
   - **Mid-TX Expiry Guard + Atomic Commit:**
     - Before writing the final outcome onto the locked `BillingWebhookEvent` row, re-check `lease_expires_at >= worker._get_now()` (if handler blocked or stalled longer than lease budget, abort cleanly with `StaleLeaseError` and full rollback!).
     - Execute final conditional fencing check or update on the locked row, apply `processed` / `failed` / `quarantined` + clear `(lease_token, locked_by, lease_expires_at)`, and execute **ONE atomic `await work_db.commit()`**.

### 6.2 Atomic `PaymentReconciliation` Persistence & Foreign-Key Safety

1. **Relationship Among Domain Writes, `PaymentReconciliation`, `BillingWebhookEvent`, and Lease Ownership:**
   - **Happy-Path Domain Writes:** Committed together with `BillingWebhookEvent(status='processed', lease=NULL)` inside Phase 3's single `COMMIT`.
   - **Happy-Path Observational Reconciliations** *(e.g. `missing_checkout_attempt` `#L256`, `unknown_subscription_updated` `#L633`, `unknown_subscription_completed` `#L756`)*: Use the atomic `upsert_payment_reconciliation()` helper inside the savepoint/TX; committed atomically alongside `status='processed'`.
   - **Conflict-Path Reconciliations (`RazorpayWebhookReconciliationConflictError`):** Handler MUST **NOT** flush dirty domain mutations hoping they survive; better yet, handlers SHOULD raise `RazorpayWebhookReconciliationConflictError` cleanly without inline `stage_or_update_payment_reconciliation()`, letting the savepoint roll back ALL partial domain state (`ROLLBACK TO SAVEPOINT`), and letting the **outer Phase 3 transaction (`work_db`)** execute `upsert_payment_reconciliation()` right alongside `BillingWebhookEvent(status='quarantined', lease=NULL)` inside ONE atomic `COMMIT`.
2. **Eliminating Savepoint Foreign-Key Violations (`SQLSTATE 23503`):**
   - All child FKs on [`PaymentReconciliation`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L680-L710) (`organization_id`, `subscription_id`, `invoice_id`, `payment_id`, `raw_event_id`) have `ON DELETE RESTRICT`.
   - **Invariant Rule:** Any entity created *inside* the rolled-back savepoint (`sub = Subscription(...)` at `agreement_handlers.py#L208` or `#L468`) ceases to exist upon savepoint exit! Therefore, when the outer TX runs `upsert_payment_reconciliation()` after savepoint rollback:
     - `raw_event_id` = `claimed.event_id` *(always committed during ingress)*.
     - `organization_id` = committed `exc.organization_id` *(verify existence if derived from external metadata or soft reference; if absent in committed DB, set `None` and store candidate UUID text in `diagnostic_payload`)*.
     - `subscription_id`, `invoice_id`, `payment_id` = **`None`** unless explicitly proven to reference a **pre-existing committed row** (and store any prospective/uncommitted entity IDs inside `diagnostic_payload` as strings!).
3. **Atomic PostgreSQL `ON CONFLICT DO UPDATE` (Fixing `23505` Case-Key Collisions):**
   Replace the racy `SELECT FOR UPDATE` + `db.add()` in [`stage_or_update_payment_reconciliation()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/handler_base.py#L844-L912) with PostgreSQL native `INSERT ... ON CONFLICT`:
   - Conflict target: `("provider", "merchant_account_id", "environment", "case_key")` matching `uq_payment_reconciliations_case_key`.
   - JSONB merge on conflict:
     - Atomically append the new sanitized history item array onto `COALESCE(payment_reconciliations.diagnostic_payload->'history', '[]'::jsonb)` using JSONB concatenation (`||`) inside `jsonb_set(...)`, updating `'latest'` without dropping historical array entries.
     - Preserve existing non-null FKs cleanly: `organization_id = COALESCE(payment_reconciliations.organization_id, EXCLUDED.organization_id)` (and similarly for `subscription_id`, `invoice_id`, `payment_id`, `raw_event_id`).
     - **Status Re-Opening Rule:** If a previously `resolved` or `ignored` reconciliation case receives a *brand new* conflicting event for the same `case_key`, transition `status` back to `ReconciliationStatus.pending_review` (and record `"reopened_by_event_id"` in history) rather than hiding an active conflict under an old closed status.

### 6.3 Edge-Case Scenario Analysis

| Scenario | Exact System Behavior Under This Design | Result Guaranteed |
|---|---|---|
| **A. Savepoint Rollback on Handler Conflict** | Handler raises `RazorpayWebhookReconciliationConflictError`. `work_db.begin_nested()` issues `ROLLBACK TO SAVEPOINT`, discarding any flushed `Subscription` / `BillingCheckoutAttempt` edits. Outer `work_db` TX remains active and un-aborted, runs `ON CONFLICT` `PaymentReconciliation` upsert (with `subscription_id=None`, `raw_event_id=claimed.event_id`), marks `BillingWebhookEvent` `quarantined` (`attempts += 1`), clears lease, and executes single `COMMIT`. | **Zero leaked domain mutations; 100% durable `PaymentReconciliation`; immediate quarantine.** |
| **B. Outer TX Rollback (Outer DB Failure or `StaleLeaseError` on Final Fence)** | Outer commit fails or final `lease_token`/`lease_expires_at` check raises `StaleLeaseError`. Entire `work_db` rolls back (`ROLLBACK`), erasing domain writes, outer reconciliation row, and event status update atomically. On `StaleLeaseError`, exit without touching DB. On outer transient PG error, attempt strictly fenced `fallback_outcome_db` `RETRYABLE_FAILURE` (or rely on lease expiry `<= 60s`). | **Zero half-committed reconciliation/status split; stale worker never overwrites winner.** |
| **C. Unique-Key Conflicts on `uq_payment_reconciliations_case_key`** | Eliminated natively by `INSERT ... ON CONFLICT (provider, merchant_account_id, environment, case_key) DO UPDATE` even when two workers process distinct `provider_event_id`s that map to the same `case_key` simultaneously (e.g., two unbound events for `cust_123`). PG serializes row update on the unique index tuple lock and appends both history entries cleanly. | **Zero `23505` crash; full chronological diagnostic history preserved.** |
| **D. Repeated Webhook Delivery** | Same `provider_event_id`: blocked at `/razorpay/webhook` ingress by `uq_billing_webhook_provider_event` returning `200 {"status": "duplicate"}` without touching worker queue. Distinct `provider_event_id` for same discrepancy: quarantined again + history appended to existing `case_key` row via `ON CONFLICT DO UPDATE`. Distinct `provider_event_id` for already-applied entity state: handler returns idempotent no-op -> `processed` in single `COMMIT`. | **Idempotent deduplication across both transport and business layers.** |
| **E. Worker Crashes** | **Crash in Phase 1 (before claim commit):** PG rolls back claim; row stays `pending`/`failed`, immediately claimable. **Crash in Phase 2 or Phase 3 before `COMMIT`:** PG closes TCP socket, rolls back uncommitted TX, frees `L0A`/`L0B`/row locks; row keeps expired/orphaned lease with **`attempts` UNCHANGED**; recovered after `lease_expires_at`. **Crash right after Phase 3 `COMMIT`:** Domain state, optional reconciliation, `processed`/`quarantined` status, and `lease=NULL` are all durable together on PG WAL! | **No Commit A / Commit B split-brain window; no attempt inflation on crash.** |
| **F. Lease Expiry & Worker Takeover** | Worker A stalls; lease expires (`t > lease_expires_at`). Worker B claims same event (`attempts` unchanged, gets new `lease_token_B`). If A is not yet in Phase 3: A hits `Level -1` check -> `lease_token_A != lease_token_B` -> raises `StaleLeaseError` & aborts before domain SQL. If A already held `Level -1` row lock during stall (`t > lease_expires_at`): A's pre-commit `lease_expires_at >= now()` check fails -> raises `StaleLeaseError` & rolls back cleanly; `L0A`/`L0B` + row lock free immediately for B. | **Strict single-writer fencing; stalled worker never commits past lease expiry.** |
| **G. Duplicate / Conflicting Provider Events Concurrently** | Two workers claim distinct events (`E1`, `E2`) targeting `sub_X`. Each locks its own distinct `BillingWebhookEvent` PK (`Level -1`), performs non-locking tenant discovery, then serializes cleanly on `L0A(org) -> L0B(subscription:sub_X)`. `E1` commits first; `E2` wakes, reads committed state with `.execution_options(populate_existing=True)`, and deterministically either skips as stale/duplicate (`processed`) or raises `ReconciliationConflictError` (`quarantined` + `PaymentReconciliation`). | **Mathematical deadlock freedom; deterministic `READ COMMITTED` serialization.** |

---

## 7. Logging and Observability Requirements

### 7.1 Mandatory Structured Log Schema

Every log line emitted by `claim_one()`, `process_one()`, `start()`, `record_razorpay_webhook_outcome()`, and handler failure paths **MUST** bind/include structured contextual fields (via logger `extra={...}` + concise formatted summary string):

| Field | Required Always? | Value Contract |
|---|---|---|
| `worker_id` | Yes | Worker instance identifier (`worker_rzp_...`) |
| `event_id` | After claim | `str(claimed.event_id)` (`BillingWebhookEvent` UUID) |
| `provider` | After claim | `"razorpay"` |
| `merchant_account_id` | After claim | Scoped merchant account ID |
| `environment` | After claim | `"test"` \| `"live"` |
| `provider_event_id` | After claim | Verified gateway event ID / SHA-256 fallback |
| `event_type` | After claim | Normalized Razorpay event string (`subscription.activated`, etc.) |
| `organization_id` | When resolved *(else `None`)* | Authoritative `UUID` string only (captured as scalar `str`/`UUID`, never dereferencing expired ORM attributes) |
| `failure_category` | On failure / cancel / stale | Exact taxonomy slug: `none`, `permanent_business`, `validation`, `scope_mismatch`, `missing_entity_id`, `reconciliation_conflict`, `unknown_event`, `stale_lease`, `duplicate_noop`, `db_retryable`, `db_permanent`, `provider_transient`, `provider_permanent`, `unexpected_internal_error`, `cancelled` |
| `error_code` / `sqlstate` | When DB / HTTP error | PG SQLSTATE (e.g., `"40001"`, `"P0001"`) or HTTP status code (e.g., `"429"`); `"None"` otherwise |
| `constraint_name` | When IntegrityError | Non-secret DB constraint/index name (e.g., `uq_org_active_subscription`) |
| `attempt_number` | After claim | Current integer attempt (`claimed.attempts` pre-outcome; `updated.attempts` post-outcome) |
| `max_attempts` | After claim | Configured worker `max_attempts` (`5`) |
| `lease_status` | After claim | Enum string: `claimed`, `active`, `expired`, `superseded`, `cleared`, `retained_on_cancel` |
| `lease_token_prefix` | Optional safe correlation | First 8 chars of UUID (`str(token)[:8]`) ONLY for log correlation — **never log raw auth tokens** |
| `correlation_id` | Always per execution | `f"{claimed.event_id}:{claimed.attempts + 1}"` (or inbound trace/request ID) |
| `case_key` | When reconciled | Deterministic `PaymentReconciliation.case_key` |

### 7.2 Mandatory Sanitization, Redaction & Safe Diagnostics

1. **Unified Redaction Helper (`sanitize_billing_error_message()` in `billing/webhook_errors.py`, re-exported as `sanitize_error_message`):**
   Unify and strengthen the partial sanitizers in [`webhook_engine.py#L155-L172`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/webhook_engine.py#L155-L172), [`stripe_webhook_engine.py#L205-L223`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/stripe_webhook_engine.py#L205-L223), [`client.py#L84-L92`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/client.py#L84-L92), and [`customer.py#L300-L343`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/customer.py#L300-L343). Apply in exact priority order:
   - **Preserve UUIDs first or guard PAN matching** so standard `8-4-4-4-12` UUIDs are never mangled.
   - **Strip SQLAlchemy bound parameter dumps:** Replace `\[parameters:.*` (`re.DOTALL`) with `[parameters: REDACTED]` so bound JSON payloads (`BillingWebhookEvent.payload` containing customer emails, phone numbers, notes) inside `DBAPIError` strings never reach `BillingWebhookEvent.error_message` or logs!
   - **Strip PostgreSQL / URI / libpq Credentials:**
     - `postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^:/@\s]+:[^@\s]+@` -> `\1[REDACTED]@`
     - `\b(?:password|passfile)\s*=\s*('[^']*'|\"[^\"]*\"|[^\s,;]+)` -> `[REDACTED]`
   - **Strip Gateway API Keys, Webhooks Secrets & Auth Headers:**
     - `rzp_(?:test|live)_[0-9a-zA-Z]+`, `sk_(?:test|live)_[0-9a-zA-Z]+`, `rk_(?:test|live)_[0-9a-zA-Z]+`, `pk_(?:test|live)_[0-9a-zA-Z]+`, `whsec_[0-9a-zA-Z]+`
     - `Basic\s+[A-Za-z0-9+/=_-]+`, `Bearer\s+[A-Za-z0-9._~+/-]+=*`
     - Header/KV secrets: `(?i)(x-razorpay-signature|x-razorpay-secret|key_secret|webhook_secret|api_key|authorization|secret|token)\s*[:=]\s*("[^"]*"|'[^']*'|[^\s,;}]+)`
   - **Strip PCI & Customer PII:**
     - Email addresses (`[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`) -> `[REDACTED_EMAIL]`
     - Phone numbers (`(?<!\w)\+?[0-9][0-9\-\s()]{7,}[0-9]\b`, applied cleanly without matching UUID hex groups or 10-digit epoch timestamps!) -> `[REDACTED_PHONE]`
     - Payment card PANs (`\b(?:\d[ -]*?){13,19}\b` applied after stripping timestamps/UUIDs) -> `[REDACTED_PAN]`
   - **Whitespace Normalization & Length Cap:** Collapse whitespace and cap stored `error_message` / `quarantine_reason` at `<= 1024` chars.
2. **Ban Unsafe `exc_info=True` & Raw `ctx.raw_payload` Dumps:**
   - **Worker Logs:** NEVER emit raw `%s` exception objects (`logger.error("... %s", exc, exc_info=True)` at `#L153, #L259, #L328`!). Always pass `sanitize_billing_error_message(exc)` and `exception_type = type(exc).__name__`. For unexpected internal failures where stack location aids debugging, log ONLY sanitized file/line summary frames *(zero frame locals, zero raw exception SQL arguments)*.
   - **Reconciliation `diagnostic_payload`:** Refactor `agreement_handlers.py#L644, #L767` and `handler_base.py` conflict builders so `diagnostic_payload` stores ONLY an **allowlisted sanitized conflict summary** (`provider_event_id`, `event_type`, `provider_subscription_id`, `provider_payment_id`, `provider_invoice_id`, `provider_customer_id`, candidate `organization_id`, scalar status/timestamps, `reason`, `sanitized_error`) — **NEVER** dumping raw `ctx.raw_payload` verbatim (which carries customer `email`, `contact`, and arbitrary `notes`).

---

## 8. Stripe Compatibility Assessment (Zero Stripe Refactoring in D-04A)

### 8.1 Verified Structural Comparison

| Dimension | Stripe Subsystem (`stripe_webhook_engine.py`, `stripe_webhook_worker.py`) | Razorpay Subsystem (`webhook_engine.py`, `handler_base.py`, `razorpay_webhook_worker.py`) | Compatibility Verdict |
|---|---|---|---|
| **Event Table & Lock Model** | `StripeWebhookEvent` locked `FOR UPDATE` across entire single TX ([`claim_next_webhook_event()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/stripe_webhook_engine.py#L396)); zero `lease_token` column; PG closes lock on crash | `BillingWebhookEvent` two-phase lease (`lease_token`, `locked_by`, `lease_expires_at`); Phase 1 Claim TX + Phase 2 Outside-TX HTTP + Phase 3 Atomic Persistence TX | Keep **separate orchestration engines** (`Stripe` single-session vs `Razorpay` two-phase leased). |
| **Exception Classes** | `WebhookEngineError -> {WebhookRetryableError, WebhookPermanentError -> {WebhookReconciliationConflictError, WebhookUnknownEventError}, WebhookEnginePersistenceError}` ([`#L109-L150`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/stripe_webhook_engine.py#L109-L150)) | After D-04A: `RazorpayWebhookEngineError -> {RazorpayWebhookRetryableError, RazorpayWebhookPermanentError -> {..., RazorpayWebhookReconciliationConflictError, RazorpayUnknownEventError}, StaleLeaseError}` | **Compatible via Shared Provider-Neutral Base Classes.** |
| **Savepoint & Outer Reconciliation** | `execute_claimed_webhook_event()` already runs handler inside `db.begin_nested()` and persists `BillingReconciliation` in outer TX ([`#L577-L608`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/stripe_webhook_engine.py#L577-L608)) | Target D-04A Phase 3 adopts the **same `begin_nested()` savepoint pattern** + outer `PaymentReconciliation` atomic `ON CONFLICT` upsert | **Aligned architecture.** |
| **Known Legacy Stripe Gaps (Out of D-04A Scope)** | Holds `Level -1` without `lock_timeout`; `stripe_invoice_paid_handler.py#L202` passes `00000000-...` sentinel UUID; `record_webhook_outcome` uses unlocked `SELECT + add`; no SQLSTATE classifier | Fixed directly for Razorpay in D-04A | **Document only; DO NOT modify Stripe logic in D-04A.** |

### 8.2 Option Evaluation & Recommendation

- **Option A (Evaluated & Rejected — Separate Unrelated Hierarchies):** Keeping Razorpay exceptions completely disconnected from Stripe duplicates sanitizer regexes, duplicates SQLSTATE extraction logic (`customer.py` vs worker), and prevents shared billing telemetry or future unified webhook alerting.
- **Option B (Recommended — Provider-Neutral Core Base in `billing/webhook_errors.py` + Provider-Specific Subclasses):**
  1. **Shared cleanly across both providers (utilities + abstract bases only):**
     - Base hierarchy in `app/services/billing/webhook_errors.py`: `BillingWebhookError`, `BillingWebhookRetryableError`, `BillingWebhookPermanentError`, `BillingWebhookLeaseError`.
     - Shared PostgreSQL SQLSTATE classifier (`extract_db_error_details`, `classify_pg_sqlstate`) and unified `sanitize_billing_error_message()`.
  2. **Provider-specific (zero coupling of Stripe & Razorpay domain semantics):**
     - Razorpay keeps `RazorpayWebhookEngineError` / `StaleLeaseError` / `RazorpayWebhookReconciliationConflictError` (`PaymentReconciliation` case-key schema, leased two-phase claim) inheriting from the shared base pair (`class RazorpayWebhookPermanentError(RazorpayWebhookEngineError, BillingWebhookPermanentError)`).
     - **Stripe Coexistence Constraint (Non-Invasive):** `stripe_webhook_engine.py` and all Stripe handlers (`stripe_checkout_handler.py`, `stripe_subscription*.py`, `stripe_invoice_*.py`, `stripe_webhook_worker.py`) require **zero modifications** during D-04A execution! *(If desired in a later cleanup commit, making `WebhookEngineError` inherit from `BillingWebhookError` is a 3-line backwards-compatible opt-in that changes zero Stripe behavior and preserves all 322 existing Stripe tests.)*

---

## 9. Complete Verification & Test Matrix

Per D-04A design discipline, verification MUST span three tiers and wire all Razorpay suites + the new PostgreSQL 16 webhook suite into [`.github/workflows/ci.yml`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/.github/workflows/ci.yml) using [`tests/postgres_test_guard.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/postgres_test_guard.py) (`REQUIRE_POSTGRES_TESTS=1`, zero skips allowed).

**Multi-Session / Multi-Connection Concurrency Flag Legend:** `[MULTI-CONN]` identifies tests requiring 2+ independent `AsyncSession` instances (`async_sessionmaker` with `pool.NullPool`) and real PostgreSQL 16 MVCC / row locks / `pg_stat_activity` inspection.

### 9.1 Tier 1: Pure Unit Tests (Fast, Deterministic, Zero DB I/O)

| Test ID | Target Component | Scenario & Assertions |
|---|---|---|
| `UNIT-EXC-01` | `razorpay`, `webhook_engine`, `handler_base` | Assert class identity: `handler_base.RazorpayWebhookPermanentError is webhook_engine.RazorpayWebhookPermanentError`; `issubclass(...)` `True` across all leaf errors (`RazorpayPayloadValidationError`, `RazorpayScopeMismatchError`, `RazorpayMissingEntityIdError`, `RazorpayWebhookReconciliationConflictError`, `RazorpayDatabasePermanentError`, `RazorpayProviderPermanentError`, `RazorpayUnknownEventError`) and retryable errors (`RazorpayDatabaseRetryableError`, `RazorpayProviderTransientError`); `StaleLeaseError` is **not** a subclass of `Permanent` or `Retryable`; `webhook_ingress.RazorpayWebhookError` remains isolated for HTTP ingress. |
| `UNIT-SQL-01` | `classify_pg_sqlstate()` / DB classifier | Verify SQLSTATE chain extraction across nested `DBAPIError -> orig -> __cause__` wrapping `40001`, `40P01`, `55P03`, `57P01`, `53300`, `08006` -> `RazorpayDatabaseRetryableError`; and `P0001`, `23502`, `23503`, `23514`, `22P02`, `42501` -> `RazorpayDatabasePermanentError`; `23505` context handling. |
| `UNIT-API-01` | Client-to-worker error mapping | Verify `RazorpayRateLimitError` (429), idempotent GET `RazorpayServer5xxError`, pre-dispatch `RazorpayNetworkError`, `asyncio.TimeoutError` -> `RazorpayProviderTransientError`; `RazorpayAuthError`, `RazorpayBillingDisabledError`, `RazorpayConfigurationError`, 400/404/422 `RazorpayAPIError`, and post-dispatch ambiguous mutation -> `RazorpayProviderPermanentError`. |
| `UNIT-LOG-01` | `sanitize_billing_error_message()` | Verify complete redaction of `rzp_test_...`/`rzp_live_...`, `whsec_...`, `sk_...`/`rk_...`/`pk_...`, `Basic <b64>`, `Bearer <jwt>`, `postgresql+asyncpg://user:pass@host/db`, `password=...`, SQLAlchemy `[parameters: {...}]`, 16-digit PANs, emails, and phones — **while preserving** `event_id` UUIDs (`8-4-4-4-12`), `provider_event_id`, `SQLSTATE`, `constraint_name`, and `<= 1024` char truncation. |
| `UNIT-DIAG-01` | Reconciliation payload sanitizer | Verify `sanitize_conflict_diagnostic_payload()` strips emails, phones, auth keys, and raw unredacted payload blobs while preserving entity IDs and timeline details. |

### 9.2 Tier 2: Mock-Based Worker Integration Tests (AsyncSession Doubles + Worker Harness)

| Test ID | Target Component | Scenario & Assertions |
|---|---|---|
| `MOCK-WRK-01` | `RazorpayWebhookWorker.process_one()` + real agreement handlers | Happy path `subscription.authenticated` -> handler executes inside `begin_nested()`, single atomic `commit()` commits domain writes + `status='processed'` + clears lease; assert `ProcessResult.success is True`. |
| `MOCK-WRK-02` | `process_one()` + `handler_base` permanent exceptions | Handler raises each `RazorpayPayloadValidationError`, `RazorpayMissingEntityIdError`, `RazorpayScopeMismatchError`, `RazorpayBusinessRejectionError` -> savepoint rolled back, `PERMANENT_FAILURE`, immediate `status='quarantined'` on `attempts=1`, `next_retry_at=None`, lease cleared, `quarantine_reason` populated. |
| `MOCK-WRK-03` | `process_one()` + `RazorpayWebhookReconciliationConflictError` | Handler raises conflict (`metadata_tenant_mismatch`, `unbound_provider_customer`, `ambiguous_checkout_attempt`, `checkout_attempt_tenant_mismatch`) -> savepoint rolled back (0 domain writes), outer `ON CONFLICT` upserts `PaymentReconciliation` with `subscription_id=None`, `raw_event_id=claimed.event_id`, commits atomically with `status='quarantined'`. |
| `MOCK-WRK-04` | `process_one()` + Retryable / Unexpected / Backoff | Handler raises `RazorpayWebhookRetryableError` OR unmapped `RuntimeError`/`KeyError` -> savepoint rolled back, `attempts` increments `1..4` setting `status='failed'` with exact `5s, 15s, 45s, 120s` `next_retry_at` via injected `worker.clock`, then attempt `5` transitions to `quarantined`; unexpected exception sets `failure_category="unexpected_internal_error"`. |
| `MOCK-WRK-05` | `process_one()` + Unknown Event Type | Unregistered `event_type` (`subscription.paused` before implementation) -> `UNKNOWN_EVENT`, `attempts += 1`, `status='quarantined'`, lease cleared, zero domain mutations. |
| `MOCK-WRK-06` | `process_one()` + `StaleLeaseError` & Clock Drift | Stale `lease_token`, wrong `locked_by`, or expired `lease_expires_at < worker.clock()` at either pre-savepoint check OR post-savepoint commit check -> rolls back cleanly, returns lease-lost result, zero mutations to `BillingWebhookEvent` or `PaymentReconciliation`. |
| `MOCK-WRK-07` | `process_one()` + `asyncio.CancelledError` | Handler raises `asyncio.CancelledError` -> `CancelledError` propagates out of `process_one()` (NOT caught into retry/quarantine), `attempts` unchanged, `status` unchanged, `work_db` rolled back. |
| `MOCK-WRK-08` | Worker Log Sanitization Capture (`caplog`) | Inject secrets (`rzp_live_SECRET123`, `Basic dXNlcjpwYXNz`, `postgres://u:secret@h/db`, `user@example.com`, `+919876543210`) inside exceptions failing `claim_one()`, `process_one()` handler, `process_one()` commit, and `start()` polling loop -> assert `caplog.text` contains **zero** unredacted secrets, **zero** raw trace frame locals (`exc_info=True` banned), and contains all mandatory structured fields (`event_id`, `provider_event_id`, `event_type`, `failure_category`, `lease_status`, `correlation_id`). |
| `MOCK-WRK-09` | `start()` Polling Loop Fix | Verify `start()` sleeps `poll_interval` when batch returns `1 <= results < batch_size` or stale-lease aborts, preventing busy CPU spin, and shuts down cleanly on `CancelledError`. |

### 9.3 Tier 3: Real PostgreSQL 16 Integration Tests (`apps/api/tests/test_postgres_webhook_worker_integration.py`)

Follows [`test_postgres_customer_integration.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_postgres_customer_integration.py#L145-L260) harness pattern (`REQUIRE_POSTGRES_TESTS=1` fail-closed, `NullPool`, `statement_cache_size=0`, reverse-FK cleanup, `pg_stat_activity` backend diagnostics).

| Test ID | Concurrency Flag | Real PG16 Scenario & Hard Assertions |
|---|---|---|
| `PG-WRK-01` | `[SINGLE-CONN + MULTI-READ]` | **End-to-End Handler Domain Persistence (Fixing D04-01):** Ingest verified `subscription.authenticated` and `subscription.activated` (with valid open attempt) -> run `worker.process_one()` -> verify from fresh PG16 session that `Subscription` and `BillingCheckoutAttempt` changes **committed durably** alongside `BillingWebhookEvent(status='processed', lease_token=None, locked_by=None, lease_expires_at=None)` — plus retry delivery at ingress returns `duplicate`. |
| `PG-WRK-02` | `[SINGLE-CONN + FRESH READ]` | **Validation & Permanent Business Error Immediate Quarantine (Fixing D04-02):** Ingest malformed payload (`RazorpayPayloadValidationError`), missing ID (`RazorpayMissingEntityIdError`), scope mismatch (`RazorpayScopeMismatchError`) -> `worker.process_one()` quarantines immediately (`attempts=1, status='quarantined'`, `quarantined_at` set, lease cleared) in 1 worker pass with sanitized `error_message`. |
| `PG-WRK-03` | `[SINGLE-CONN + SAVEPOINT ROLLBACK]` | **Reconciliation Durability + Uncommitted `sub.id` `23503` FK Proof:** Pre-create Organization + active conflicting `BillingCheckoutAttempt`, ingest `subscription.authenticated` / `activated` triggering `ambiguous_checkout_attempt` or `checkout_attempt_tenant_mismatch` (where handler adds a new `Subscription` inside savepoint before raising!) -> verify PG16 **savepoint rolled back cleanly (`0` `Subscription` rows committed!)**, outer `PaymentReconciliation` row **committed durably** with `subscription_id=None, organization_id=org.id, raw_event_id=event.id, status='pending_review'` (zero `23503` FK error!), and `BillingWebhookEvent` committed `status='quarantined'` in the **same single PG commit**. |
| `PG-WRK-04` | `[MULTI-CONN]` | **Concurrent Case-Key `ON CONFLICT DO UPDATE` Proof:** Spawn 2 worker sessions simultaneously processing distinct conflicting events (`E1`, `E2`) targeting identical deterministic `case_key` (e.g., unbound customer `cust_race` triggering `unbound_provider_customer`) -> zero `23505` errors raised, **exactly 1** `PaymentReconciliation` row exists on PG16, `diagnostic_payload['history']` contains **both** events chronologically, both events `quarantined`. Also verify reopening if existing case row was marked `resolved`. |
| `PG-WRK-05` | `[MULTI-CONN]` | **Split-Window Elimination & Outer Commit Failure Rollback:** Inject outer `work_db` failure at final `BillingWebhookEvent` persistence (e.g., transient `PG` trigger raising `40001` only on final event `UPDATE`!) -> verify PG16 **rolled back handler domain writes too** (`Subscription` count stays `0`, zero Commit A without Commit B drift!), fallback/retry leaves event reclaimable -> on next pass without `40001`, both domain writes AND `processed` commit together. |
| `PG-WRK-06` | `[SINGLE-CONN + PG TRIGGER]` | **PL/pgSQL `SQLSTATE P0001` Immutability Trigger Classification:** Execute handler path that attempts to reassign an immutable column guarded by migration [`e2f3a4b5c6d1`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py#L343-L433) (`billing_checkout_attempts.provider_payment_id` immutability trigger raising `P0001`) -> verify worker classifies `P0001` as `RazorpayDatabasePermanentError` -> **immediate `quarantined` on `attempts=1`** (NOT retried 5 times!), savepoint rolled back, `SQLSTATE=P0001` scrubbed into safe `error_message`. |
| `PG-WRK-07` | `[MULTI-CONN]` | **Recoverable PG `40001` / `55P03` `lock_timeout` Retry + Eventual Quarantine:** Session 1 holds conflicting row lock causing Worker Session 2 (`SET LOCAL lock_timeout`) to fail with `55P03` -> verify `status='failed'`, `attempts=1`, `next_retry_at = clock + 5s`, lease cleared, `0` leaked locks/transactions; release lock & advance clock -> Worker re-claims and succeeds (`status='processed'`, `attempts=1` prior -> final `processed`). Also test `5` consecutive `40001` triggers culminating cleanly in `status='quarantined'` at `attempts=5`. |
| `PG-WRK-08` | `[MULTI-CONN]` | **Two-Worker Lease Takeover & Stale Worker Rejection Under Real MVCC:** Worker A claims `E1` (`t=0`, expires `t=60`), pauses via `asyncio.Event` before Phase 3. Advance injected clock to `t=61`; Worker B re-claims `E1` (`attempts` still `0`, `lease_token_B != lease_token_A`) and commits domain state + `processed`. Release Worker A -> A executes Phase 3, hits `StaleLeaseError` on `Level -1` fencing, rolls back, leaves B's committed domain state & `BillingWebhookEvent(processed)` completely untouched. |
| `PG-WRK-09` | `[MULTI-CONN]` | **Mid-Savepoint Lease Expiry Fence:** Worker A claims (`expires=t+60`) at `t=0`, enters handler at `t=5`, injected clock advances past `t=65` before commit -> Phase 3 pre-commit expiry check raises `StaleLeaseError`, rolls back savepoint & TX completely -> domain writes discarded. |
| `PG-WRK-10` | `[MULTI-CONN]` | **Concurrent Conflicting Subscriptions Lock Order & Deadlock Freedom:** Simultaneously run `Worker 1` (`subscription.authenticated` / `activated`) and `Worker 2` (`subscription.updated` / parallel checkout or second conflicting event for same org/sub) over `10` concurrent randomized/interleaved asyncio runs -> assert **0 PostgreSQL deadlocks (`40P01`)**, monotonic `L0A -> L0B -> L3` serialization, deterministic conflict reconciliation. |
| `PG-WRK-11` | `[MULTI-CONN + PG_STAT_ACTIVITY]` | **Live PG16 `asyncio.CancelledError` During Blocked `FOR UPDATE` / Advisory Lock (PG-CUST-17 Pattern):** Locker session holds exclusive lock on target `Subscription` / `Organization` / `pg_advisory_xact_lock`; Worker `task = asyncio.create_task(worker.process_one())` blocks waiting on PG lock (verified via `pg_stat_activity` `wait_event_type = 'Lock'` and `pid` capture). Execute `task.cancel()`, assert `asyncio.CancelledError` propagates, rollback/close locker -> verify `pg_stat_activity` contains **zero lingering worker/locker PIDs** and **zero `idle in transaction` backends**; verify `BillingWebhookEvent` has original `status` & **`attempts` unchanged**, and after lease expiry clock advance a second worker claims and processes the event cleanly to `processed`. |
| `PG-WRK-12` | `[SINGLE-CONN + LOG ASSERT]` | **Live PG16 Secret & `DBAPIError [parameters: ...]` PII Sanitization:** Ingest payload containing customer email/phone and trigger an `IntegrityError` / `DBAPIError` containing SQL `[parameters: ...]` + simulated credentials -> verify committed `BillingWebhookEvent.error_message` and `PaymentReconciliation.diagnostic_payload` and worker `caplog` contain **zero emails, phones, keys, DSN passwords, or `[parameters: ...]` JSON dumps**. |
| `PG-WRK-13` | `[MULTI-CONN]` | **Provider-Neutral Idempotent Duplicate Event & Stale Event Replay:** Deliver identical `provider_event_id` concurrently twice at `/razorpay/webhook` (`200 pending` + `200 duplicate`), then deliver older-timestamp distinct `provider_event_id` to worker -> single execution, stale duplicate completes cleanly (`status='processed'`) without regressing timestamps or state. |
| `PG-WRK-14` | `[MULTI-CONN]` | **RLS & Role Isolation Sanity Check:** Verify `anon` and `authenticated` roles fail closed (`SQLSTATE 42501`) on `billing_webhook_events` and `payment_reconciliations`, mirroring `PG-CUST-19`. |

---

## 10. Implementation Sequence

Execute as small, verifiable commits on a clean feature branch branched directly from `validation/d2-stage1-customer-pg16` (`497852e`) — never cherry-picking unverified edits blindly from dirty `main`:

1. **Step 1 — Shared Exception Base, SQLSTATE Classifier & Unified Redaction (`billing/webhook_errors.py` + `customer.py` reuse):**
   - Create `app/services/billing/webhook_errors.py` with `BillingWebhookError` tree, `classify_pg_sqlstate()`, and `sanitize_billing_error_message()` (including `Basic`, PG DSN, SQLAlchemy `[parameters: ...]`, PCI/PII redaction).
   - Add `UNIT-SQL-01`, `UNIT-LOG-01`, `UNIT-API-01`, `UNIT-DIAG-01`.
2. **Step 2 — Razorpay Unified Exception Hierarchy & Package Exports (`webhook_engine.py`, `handler_base.py`, `razorpay/__init__.py`):**
   - Refactor `webhook_engine.py` exceptions to inherit from `webhook_errors.py` and add concrete subclasses (`RazorpayDatabaseRetryableError`, `RazorpayDatabasePermanentError`, `RazorpayProviderTransientError`, `RazorpayProviderPermanentError`, `RazorpayBusinessRejectionError`, `RazorpayUnverifiedEventError`, `RazorpayUnknownEventError`).
   - Remove duplicate base definitions in `handler_base.py`; re-export `webhook_engine` canonical classes; alias `RazorpayWebhookHandlerError = RazorpayWebhookEngineError`; preserve `webhook_ingress.RazorpayWebhookError`.
   - Fix `razorpay/__init__.py` exports and add `UNIT-EXC-01`.
3. **Step 3 — Atomic `PaymentReconciliation` Upsert Helper (`handler_base.py`):**
   - Replace racy `SELECT FOR UPDATE + INSERT` in `stage_or_update_payment_reconciliation()` with PostgreSQL `INSERT ... ON CONFLICT (provider, merchant_account_id, environment, case_key) DO UPDATE` + sanitized diagnostic builder (stripping raw `ctx.raw_payload`, preserving non-null FKs, handling `pending_review` reopening).
   - Remove uncommitted `sub.id` child FK arguments and inline conflict `stage_or_update_payment_reconciliation()` calls prior to `raise RazorpayWebhookReconciliationConflictError` across `agreement_handlers.py`.
4. **Step 4 — Phase 3 Single Atomic Transaction + Fencing + Classification in Engine & Worker (`webhook_engine.py`, `razorpay_webhook_worker.py`):**
   - Implement `execute_claimed_razorpay_webhook_event()` wrapping handler in `async with work_db.begin_nested():` savepoint + `Level -1` `FOR NO KEY UPDATE` + `SET LOCAL lock_timeout` + `worker.clock` consistency + outer conflict upsert + atomic status/lease-clear `COMMIT` + `classify_db_exception` / `classify_client_error`.
   - Wire `RazorpayWebhookWorker.process_one()` (keep `CancelledError` uncaught, log only sanitized structured telemetry without `exc_info=True`, export `RazorpayWebhookWorker` in `app/workers/__init__.py`, fix `start()` partial-batch polling sleep).
   - Enforce `.execution_options(populate_existing=True)` + non-locking tenant discovery before `L0A -> L0B` in `handler_base.py` / `agreement_handlers.py`.
5. **Step 5 — Complete Test Suite & CI Expansion (`apps/api/tests/*` + `.github/workflows/ci.yml`):**
   - Update `test_razorpay_webhook_worker.py`, `test_razorpay_handler_foundation.py`, `test_razorpay_agreement_handlers.py` with `MOCK-WRK-01..09`.
   - Implement `tests/test_postgres_webhook_worker_integration.py` (`PG-WRK-01..14`, `0` skips under `REQUIRE_POSTGRES_TESTS=1`).
   - Add all Razorpay webhook/handler suites + `test_postgres_webhook_worker_integration.py` to `.github/workflows/ci.yml` with fail-closed `assert_junit_zero_skips` gates.
   - *(Subsequent dependency before Stage 2 settlement: apply separate D04-03 fix blocking `expired`/`failed`/elapsed `open` checkout attempt resurrection in `handle_subscription_activated()` & `handle_subscription_authenticated()`, plus D-03 `lock.py` `L0A` organization & sorted multi-key `L0B` primitives.)*

---

## 11. Risks and Unresolved Decisions

### 11.1 Engineering Trade-Offs & Decisions Flagged Before Coding

| ID | Risk / Question | Options Evaluated | Recommended Default & Impact |
|---|---|---|---|
| **ED-01** | **Unmapped Unexpected `Exception` (`TypeError`, `KeyError`, `AttributeError` vs Transient OS/Network Glitch):** Quarantine on attempt `1` vs bounded `RETRYABLE_FAILURE` up to `5` attempts? | **(A)** Bounded retry up to `max_attempts=5` (matches Stripe & avoids dropping unforeseen transient driver errors). **(B)** Quarantine explicit pure-Python logic bugs (`TypeError`, `KeyError`, `AttributeError`, `AssertionError`, `ZeroDivisionError`) on attempt `1` while retrying unforeseen `OSError`/`TimeoutError`/`Exception`. | Recommend **(A)** initially (every deterministic SQLSTATE `P0001`/`23502`/`23503`/`23514`/`42*` & handler error ALREADY quarantines on attempt `1`), emitting high-priority `"unexpected_internal_error"` paging alert — confirm with backend owner if Option **(B)** fail-fast quarantine for Python `TypeError`/`KeyError` is preferred. |
| **ED-02** | **Out-of-Order Webhook Arrival Before Checkout Commit (`unbound_provider_customer` Race):** [`D2_STAGE2_READINESS_AUDIT.md` Section 6.1](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_STAGE2_READINESS_AUDIT.md#L173) notes an optional bounded immediate retry (`cap=1`) for out-of-order `unbound` arrival while customer provisioning or checkout attempt commit finishes concurrently. | **(A)** Immediate `quarantined` + `PaymentReconciliation` on `unbound_provider_customer`. **(B)** If an active `BillingCustomerProvision(status='pending')` or unexpired `BillingCheckoutAttempt(status='creating')` exists, raise `RazorpayTransientRaceRetryError` (`RETRYABLE_FAILURE` up to `cap=1..2` short `2s` backoff) before escalating to `RazorpayWebhookReconciliationConflictError`. | Recommend **(B)** once Stage 2 `L0A` + checkout attempt ownership lands (keeps transient webhook-beat-checkout-commit noise out of `pending_review` without hiding true orphan webhooks!). *(Requires checking `BillingCustomerProvision(pending)` / `BillingCheckoutAttempt(creating)` safely.)* |
| **ED-03** | **PostgreSQL `23505` (`unique_violation`) Retryable vs Permanent:** | Under `L0A -> L0B`, normal paths shouldn't hit `23505`; rare cross-path unique race (e.g. concurrent rollover) heals on retry, whereas deterministic duplicate bug repeats 5 times and quarantines. | Classify generic `23505` as bounded `RazorpayDatabaseRetryableError` (`attempts += 1`, quarantine at `5`), while eliminating `PaymentReconciliation` `23505` completely via `ON CONFLICT DO UPDATE`. |
| **ED-04** | **SQLAlchemy `postgresql.insert(...).on_conflict_do_update` in Unit Mocks:** | Offline in-memory session doubles cannot compile/execute PG dialect `ON CONFLICT DO UPDATE` ASTs automatically. | Keep `upsert_payment_reconciliation()` as an isolated async helper in `handler_base.py`; unit mocks can test against a shared store double or patch helper cleanly while `PG-WRK-*` verifies native PostgreSQL 16 SQL execution directly. |
| **ED-05** | **Cooperative Shutdown Immediate Lease Release vs Natural Expiry:** | Hard `task.cancel()` MUST NEVER block teardown on network/DB calls. On graceful `worker.stop()`, wait for active `process_one()` to finish normally; on mid-flight `CancelledError`, rollback + close socket + let `lease_expires_at` (`<= 60s`) expire cleanly. | Recommend **zero extra DB I/O inside `CancelledError` handler** (guaranteed zero shutdown deadlock & zero leaked sockets); keep `lease_duration_seconds` reasonable (`30s–60s`). |
| **ED-06** | **Sanitizer Phone Regex False-Positives on Numeric IDs / Timestamps:** | Aggressive digit matching could redact Unix epoch seconds (`1794300000`, 10 digits) or long order integers inside diagnostic messages. | Require `>= 13` digits for PAN matching, exclude UUIDs & pure 10-digit Unix epoch timestamps in `1500000000..2500000000`, and match phones only with country/prefix punctuation or explicit field names (`contact`, `phone`, `mobile`). |

### 11.2 Pre-Existing External Dependencies (Out of D-04A Scope)

- **D04-03 State-Machine Fix** (`handle_subscription_activated()` / `handle_subscription_authenticated()` expired/failed checkout attempt non-resurrection) MUST ship alongside or immediately after D-04A *before* production worker activation so persisted commits cannot mutate historical `expired`/`failed` attempts.
- **Commercial Policies [`P-01`–`P-04`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_STAGE2_READINESS_AUDIT.md#L106-L111)** (`payment_pending` TTL, abandoned subscription cancellation vs triage, late-capture policy, production Pro/Free token quotas) remain **product-gated blockers for D-2 Stage 2 settlement**, though they do **NOT** block D-04A exception/transaction/worker repair.

---

## 12. Explicit Acceptance Criteria

Before any D-04A implementation PR can be merged or accepted:

1. **Single Exception Hierarchy Verified:** `handler_base.py` and `webhook_engine.py` share identical `RazorpayWebhookPermanentError` / `RazorpayWebhookRetryableError` / `StaleLeaseError` class objects; `issubclass(cls, razorpay.RazorpayWebhookPermanentError)` is `True` for all permanent/validation/scope/conflict/DB-permanent subclasses; `StaleLeaseError` inherits from `RazorpayWebhookEngineError` only.
2. **Zero Split-Commit Data Loss & Zero Lost Conflict Reconciliations on PG16:** Real PostgreSQL 16 integration run proves `process_one()` commits domain updates + `status='processed'` atomically (`PG-WRK-01`), rolls back partial domain writes on savepoint conflict while committing outer `PaymentReconciliation` + `status='quarantined'` in the same transaction without `23503` FK errors (`PG-WRK-03`), and suffers zero `23505` collisions under concurrent `case_key` races (`PG-WRK-04`).
3. **Deterministic SQLSTATE & Provider Classification:** `P0001` trigger violations and `23502`/`23503`/`23514`/`401`/`400`/`404` quarantine on attempt `1`; `40001`/`40P01`/`55P03`/`08*` and `429`/`GET 5xx` retry cleanly (`5s/15s/45s/120s`) and quarantine on attempt `5`.
4. **Lease Fencing & Python 3.11 Cancellation Safety Proven on PG16:** Stale/expired leases abort with `StaleLeaseError` without touching DB rows (`PG-WRK-08`, `PG-WRK-09`); `task.cancel()` during blocked PG16 `FOR UPDATE` / advisory lock propagates `asyncio.CancelledError`, leaves `attempts` unchanged, leaves **0 lingering backend PIDs**, and leaves **0 `idle in transaction` connections** (`PG-WRK-11`).
5. **Zero Secret / PII Leakage:** Worker logs, `BillingWebhookEvent.error_message`, `quarantine_reason`, and `PaymentReconciliation.diagnostic_payload` scrub `rzp_*`, `whsec_*`, `sk_*`, `rk_*`, `pk_*`, `Basic`, `Bearer`, PG DSN passwords, SQLAlchemy `[parameters: ...]`, PANs, emails, and phones; zero unredacted `exc_info=True` or verbatim `ctx.raw_payload` dumps remain (`UNIT-LOG-01`, `MOCK-WRK-08`, `PG-WRK-12`).
6. **Stripe Zero Regression:** Existing Stripe tests and code execute cleanly with zero behavioral modifications to Stripe webhook ingestion, handlers, or worker logic.
7. **Automated CI Gate Enabled With Zero Skips:** [`.github/workflows/ci.yml`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/.github/workflows/ci.yml) executes all Razorpay unit/mock suites plus `test_postgres_webhook_worker_integration.py` under `REQUIRE_POSTGRES_TESTS=1` enforced by `assert_junit_zero_skips(...)`.

---

## Final Engineering Verdict

### **`DESIGN READY FOR IMPLEMENTATION`**
