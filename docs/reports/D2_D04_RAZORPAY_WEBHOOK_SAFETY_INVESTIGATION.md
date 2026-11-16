# Aptlyst AI — D-04 Razorpay Webhook Safety Investigation Report

**Investigation Date:** 2026-11-10  
**Investigation Mode:** Strictly Read-Only Code, Architecture, Test & Repository Verification  
**Authoritative Baseline Worktree:** [`D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16)  
**Baseline Branch / Verified HEAD:** `validation/d2-stage1-customer-pg16` @ `497852e19360d85787107dbae6921519074f0773`  
**Parallel Dirty Reference Worktree (Preserved Untouched):** [`D:/Projects/V1/Aptlyst-AI`](file:///D:/Projects/V1/Aptlyst-AI) (`main` @ `027ab7ad3ee8a02fb20823a4f743e29b03c0f248`, uncommitted changes)  
**Stage 2 Authorization Status:** **BLOCKED — NOT AUTHORIZED**  
**Production Exposure Status:** **`PRODUCTION EXPOSURE NOT VERIFIED`**

---

## 1. Executive Summary

Independent read-only verification against committed source code on the accepted D-2 Stage 1 branch (`validation/d2-stage1-customer-pg16` @ `497852e`) **confirms all three reported Razorpay webhook worker safety defects** before any Stage 2 settlement work can safely proceed.

| Finding ID | Investigated Defect | Classification (Stage 1 `497852e`) | Severity | Stage 2 Impact |
|---|---|---|---|---|
| **D04-01** | `RazorpayWebhookWorker.process_one()` rolls back handler database writes while marking webhook events `'processed'` | **`CONFIRMED`** | **Critical (P0)** | Silent permanent data loss of every `Subscription`, `BillingCheckoutAttempt`, and `PaymentReconciliation` write executed by worker handlers on real PostgreSQL |
| **D04-02** | Worker catches exception classes from the wrong module, bypassing permanent-error quarantine and dropping sanitization | **`CONFIRMED`** | **High (P1)** | All `handler_base` permanent/conflict/validation/scope errors bypass `PERMANENT_FAILURE` quarantine, trigger 5-attempt retry loops, lose staged reconciliations on rollback, and expose raw exceptions in logs |
| **D04-03** | `handle_subscription_activated()` resurrects already-`expired` / `failed` checkout attempts (and nuances across agreement & entitlement states) | **`CONFIRMED`** *(Checkout Attempt resurrection)* / **`NOT REPRODUCIBLE`** *(Canceled `Subscription` resurrection & Pro quota grant)* | **High (P1)** | Rewrites `expired`/`failed`/`creating`/`recovery_required`/elapsed `open` `BillingCheckoutAttempt` rows to `'complete'`, bypassing fail-closed expiration & failure checks in `verify_razorpay_checkout_callback()` |

### Key Conclusions

1. **Silent Webhook Data Corruption on Stage 1 (`497852e`):** None of the draft remediations reported on dirty `main` (`Step 5.2D.3.3B.1B` / `1C`) exist on the committed Stage 1 branch `497852e`. Executing `RazorpayWebhookWorker` against real PostgreSQL on `497852e` rolls back every domain write while irreversibly marking `BillingWebhookEvent.status = 'processed'`.
2. **Test Suite Blind Spot Masked All Three Defects:** Neither `test_razorpay_webhook_worker.py` nor `test_razorpay_agreement_handlers.py` runs in `.github/workflows/ci.yml`. Both suites run exclusively against in-memory mock `AsyncSession` doubles with no-op handlers, direct outcome-helper calls, and ignored SQL `WHERE` predicates.
3. **Dirty `main` Drafts Fix Symptoms Partially While Leaving Structural Flaws:** Uncommitted patches in [`D:/Projects/V1/Aptlyst-AI`](file:///D:/Projects/V1/Aptlyst-AI) add a separate `handler_db.commit()` (`Commit A`), dual-hierarchy catches, and an activation status guard, **but remain unmerged, untested against PostgreSQL 16, and structurally flawed** (Split Commit A / Commit B crash window, wiped `PaymentReconciliation` rows on conflict rollback, `handle_subscription_authenticated` elapsed-`open` bypass, wall-clock drift, and lock-coupling hazards).
4. **Operational Exposure:** Tracked repository code shows no auto-started worker daemon and defaults `razorpay_enabled = False`, **but** runtime environment config and host orchestration lie outside Git; therefore: **`PRODUCTION EXPOSURE NOT VERIFIED`**.

---

## 2. Repository & Git-State Verification

Read-only Git verification across all registered worktrees (`git worktree list --porcelain`, `git status --short`, `git rev-parse HEAD`, `git diff --stat`) established the following exact workspace topology:

| Worktree Path | Branch | Verified `HEAD` Commit | Tracked State | Notes |
|---|---|---|---|---|
| [`D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16) | `validation/d2-stage1-customer-pg16` | `497852e19360d85787107dbae6921519074f0773` | **Clean** | Accepted D-2 Stage 1 baseline (`STEP_5_2D_3_3B_1E_Z1..Z8_*.md`, [`D2_STAGE1_FORMAL_CLOSURE_REPORT.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_STAGE1_FORMAL_CLOSURE_REPORT.md), [`D2_STAGE2_READINESS_AUDIT.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_STAGE2_READINESS_AUDIT.md) untracked) |
| [`D:/Projects/V1/Aptlyst-AI`](file:///D:/Projects/V1/Aptlyst-AI) | `main` | `027ab7ad3ee8a02fb20823a4f743e29b03c0f248` | **Dirty (`13` modified tracked files, `+13773 / -712`, `35` untracked files)** | Parallel scratch/draft workspace; contains unmerged `checkout.py` (`+1114`), `razorpay_webhook_worker.py` (`100` diff lines), `agreement_handlers.py` (`76` diff lines), pre-Stage-1 `customer.py`/`ci.yml` edits, and step reports; **preserved completely untouched** |
| [`D:/Projects/V1/Aptlyst-AI-pg16-validation`](file:///D:/Projects/V1/Aptlyst-AI-pg16-validation) | `validation/postgres16-ci-hardening` | `c932cc480f82b2eea11c7616bf9290e255cffd31` | **Clean** | Pre-Stage-1 PostgreSQL 16 CI hardening base (`c932cc4`) |

- **Alembic Schema Head:** `14` linear migrations terminating at `20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py` (`e2f3a4b5c6d1`), including `BillingWebhookEvent` lease columns/index (`d1e2f3a4b5c6`) and `BillingCheckoutAttempt` / `BillingPayment` composite FK and immutability triggers (`e2f3a4b5c6d1`).
- **Baseline Discipline:** Throughout Sections 3–5, committed behavior on `validation/d2-stage1-customer-pg16` (`497852e`) is treated as the authoritative codebase state. Uncommitted edits on dirty `main` are analyzed separately in Section 6 and are **never** treated as accepted implementation.

---

## 3. Detailed Investigation — D04-01: Transaction & Session Boundary Analysis

### 3.1 Classification: `CONFIRMED`

On committed Stage 1 (`497852e`), [`RazorpayWebhookWorker.process_one()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L156-L261) executes domain handlers inside an `AsyncSession` (`handler_db`) that **never calls `commit()`**, causing PostgreSQL to silently roll back all flushed handler mutations upon context-manager exit, and immediately opens a third `AsyncSession` (`outcome_db`) that commits `BillingWebhookEvent.status = 'processed'`.

### 3.2 Session & Engine Configuration (`db/session.py`)

By default (`razorpay_webhook_worker.py#L92`), `RazorpayWebhookWorker` uses `AsyncSessionLocal` ([`session.py#L17-L28`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/db/session.py#L17-L28)):
- Engine uses `poolclass=pool.NullPool` (disabling pooled connection reuse across sessions).
- `AsyncSessionLocal` uses `expire_on_commit=False` and SQLAlchemy's default `autocommit=False` (`autobegin=True`).
- Every `async with self.session_factory() as session:` block begins a database transaction (`BEGIN`) upon first SQL statement and **destroys all uncommitted `FLUSH`ed changes with an implicit `ROLLBACK` on `__aexit__`** unless `await session.commit()` is explicitly invoked beforehand.

### 3.3 Exact Session-by-Session Timeline of `RazorpayWebhookWorker.process_one()` (`497852e`)

```text
Session 1 — claim_one()  [razorpay_webhook_worker.py#L141-L154 -> webhook_engine.py#L216-L322]
  ├─ Opens AsyncSession #1 (db)
  ├─ Executes: SELECT ... FROM billing_webhook_events WHERE ... FOR UPDATE SKIP LOCKED
  ├─ Stamps: lease_token = uuid4(), locked_by = worker_id, lease_expires_at = now + 60s
  ├─ Commits: await db.commit() [webhook_engine.py#L297] -> Releases claim row lock cleanly
  └─ Returns immutable ClaimedWebhookEvent; exits Session #1
        │
        ▼
Session 2 — handler_db  [razorpay_webhook_worker.py#L178-L209 -> agreement_handlers.py]
  ├─ Opens AsyncSession #2 (handler_db)
  ├─ Calls await handler(claimed, handler_db)  [#L182]
  │    ├─ Inside agreement_handlers.py (e.g., handle_subscription_activated):
  │    │    1. assert_valid_worker_lease() -> SELECT ... FROM billing_webhook_events FOR UPDATE  [handler_base.py#L973-L977]
  │    │    2. acquire_razorpay_advisory_lock(db, "subscription", sub_id) -> pg_advisory_xact_lock
  │    │    3. resolve_tenant_organization() -> queries BillingCustomerProvision / Subscription
  │    │    4. SELECT ... FROM subscriptions FOR UPDATE -> creates/mutates Subscription
  │    │    5. SELECT ... FROM billing_checkout_attempts FOR UPDATE -> mutates BillingCheckoutAttempt / PaymentReconciliation
  │    │    6. Calls await db.flush() ONLY [#L324, #L565, #L690, #L794] (or reconciliation flush at handler_base.py#L886, #L911)
  │    │    └─ ZERO handlers call await db.commit()!
  ├─ ❌ BUG: process_one() NEVER calls await handler_db.commit() [#L178-L186]
  ├─ Exits `async with self.session_factory() as handler_db:` normally
  │    └─ AsyncSession.__aexit__ closes Session #2 without COMMIT -> PostgreSQL ROLLS BACK all DML!
  └─ Sets outcome = ProcessingOutcome.SUCCESS; err_to_record = None [#L185-L186]
        │
        ▼
Session 3 — outcome_db  [razorpay_webhook_worker.py#L212-L261 -> webhook_engine.py#L330-L450]
  ├─ Opens AsyncSession #3 (outcome_db)
  ├─ Calls record_razorpay_webhook_outcome(..., outcome=ProcessingOutcome.SUCCESS)
  ├─ Executes: SELECT ... FROM billing_webhook_events WHERE id = :id FOR UPDATE [#L363-L367]
  ├─ Validates (lease_token, locked_by, lease_expires_at >= now_utc) -> SUCCEEDS
  ├─ Mutates: event.status = WebhookProcessingStatus.processed, processed_at = now_utc, clears lease fields
  └─ Commits: await db.commit() [webhook_engine.py#L438] -> Marks event PROCESSED permanently!
```

### 3.4 Consequences on Real PostgreSQL

1. **Silent Permanent Domain Data Loss:** An incoming `subscription.authenticated` or `subscription.activated` webhook returns HTTP `200` at ingress (`webhook_ingress.py#L358`), is claimed by the worker, runs the full handler (`Subscription` creation/activation, `BillingCheckoutAttempt` updates, `PaymentReconciliation` staging), **rolls back all domain tables completely**, and is permanently committed as `BillingWebhookEvent.status = 'processed'`. Neither automatic polling retries nor duplicate webhook deliveries (blocked at ingress by `uq_billing_webhook_provider_event` returning `"status": "duplicate"`) will ever re-run the event.
2. **Cascading Downstream Breakage Across Dependent Events:** Because `subscription.authenticated` committed nothing, no `Subscription` row exists when `subscription.updated` or `subscription.completed` arrives later (triggering `unknown_subscription_updated` / `unknown_subscription_completed` reconciliations at `agreement_handlers.py#L628-L648`, `#L751-L771` — which also roll back inside `handler_db`!). Even worse, `handle_subscription_completed()` omits `customer_id` in `resolve_tenant_organization()` (`#L728-L735`), meaning once the prior `Subscription` creation silently rolled back, `resolve_tenant_organization()` raises `RazorpayWebhookReconciliationConflictError("unbound_provider_customer")` with zero `Subscription` row to anchor on.

### 3.5 Deeper Transaction & Concern-Separation Flaws

Beyond the omitted `await handler_db.commit()`, `RazorpayWebhookWorker` exhibits four architectural separation defects (especially when contrasted with [`StripeWebhookWorker`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/stripe_webhook_worker.py#L114-L168) + [`execute_claimed_webhook_event()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/stripe_webhook_engine.py#L524-L678)):

1. **Split Handler Commit vs. Outcome Commit Window (Two-Session vs. Single-Transaction-with-Savepoint):**
   - `StripeWebhookWorker` intentionally separates transactions only across *external I/O* vs *DB commit* or runs `claim -> begin_nested() savepoint -> outcome commit` inside one ACID transaction.
   - Razorpay's Session 2 (`handler_db`) and Session 3 (`outcome_db`) separate **domain DB mutations** from **webhook event status update** into two non-atomic transactions (`Commit A` then `Commit B`). Even after adding `handler_db.commit()`, crashing or losing lease ownership between Commit A and Commit B persists domain changes while leaving `BillingWebhookEvent` `pending`/`failed`, forcing full handler replay once the lease expires.
   - Because current agreement handlers (`agreement_handlers.py`) make **zero external HTTP calls**, splitting Session 2 and Session 3 creates a needless Commit A / Commit B crash window rather than isolating network latency. *(Only if external HTTP calls are performed during webhook processing must HTTP execute outside DB transactions — in a separate pre-persistence phase, analogous to [`get_or_create_razorpay_customer()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/customer.py#L1968-L2040).)*
2. **Reconciliation Records (`PaymentReconciliation`) Destroyed on Failure Rollback:**
   - When `handle_subscription_authenticated()` or `handle_subscription_activated()` detects `ambiguous_checkout_attempt` or `checkout_attempt_tenant_mismatch` (`#L221-L250, #L285-L314, #L490-L519, #L529-L558`), it calls `stage_or_update_payment_reconciliation()` *inside `handler_db`* and immediately raises `RazorpayWebhookReconciliationConflictError`.
   - Meanwhile, `resolve_tenant_organization()` (`handler_base.py#L746-L810`) raises `RazorpayWebhookReconciliationConflictError` (`conflicting_tenant_records`, `metadata_tenant_mismatch`, `unbound_provider_customer`) without even staging a `PaymentReconciliation` row.
   - On rollback of `handler_db`, every handler-staged `PaymentReconciliation` row is lost, leaving no row in `payment_reconciliations`! Contrast [`stripe_webhook_engine.py#L587-L608`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/stripe_webhook_engine.py#L587-L608), which executes handlers inside `db.begin_nested()` and persists the `reconciliation_record` in the *outer* outcome transaction after savepoint rollback.
3. **Worker-Level Lease Fencing Delegated Inside Handlers (`assert_valid_worker_lease()`):**
   - `process_one()` on Stage 1 never calls `assert_valid_worker_lease()` in `handler_db` before dispatching or before committing; it delegates fencing solely to each handler (`agreement_handlers.py#L97-L103`, `#L353-L359`, `#L592-L598`, `#L716-L722`). Any future handler that omits `assert_valid_worker_lease()`, or any `asyncio.sleep`/DB stall between step 1 and commit, is unfenced on `handler_db`.
   - Conversely, calling `assert_valid_worker_lease()` inside handlers uses default wall-clock `now=datetime.now(timezone.utc)` (`handler_base.py#L971`) instead of `worker.clock`, and acquires `BillingWebhookEvent FOR UPDATE` ahead of `L0B`/`L2`/`L3` inside Session 2 — holding that event row lock for the full duration of domain SQL work and causing `BillingWebhookEvent` lock coupling if any path ever inverted order.
4. **Concurrency Risk Inside `stage_or_update_payment_reconciliation()` (`handler_base.py#L844-L912`):**
   - Performs `SELECT ... FOR UPDATE` + Python insert without PostgreSQL `INSERT ... ON CONFLICT (provider, merchant_account_id, environment, case_key) DO UPDATE`. Concurrent events targeting different `sub_id`s that map to the same `case_key` (or outer conflict persistence without `L0` serialization) can raise `IntegrityError` (`23505`).

---

## 4. Detailed Investigation — D04-02: Exception Hierarchy & Worker Catch-Block Analysis

### 4.1 Classification: `CONFIRMED`

[`razorpay_webhook_worker.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L43-L56) imports permanent and retryable exceptions exclusively from `webhook_engine.py`, whereas [`handler_base.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/handler_base.py#L73-L131) and [`agreement_handlers.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/agreement_handlers.py#L53) raise a completely separate inheritance hierarchy. As a result, `except RazorpayWebhookPermanentError:` in `process_one()` (`#L203-L205`) is unreachable dead code for all real agreement handlers.

### 4.2 Exact Exception Class Trees (Directly Verified)

```text
Exception
 ├── RazorpayWebhookEngineError                         [webhook_engine.py#L135]
 │    ├── webhook_engine.RazorpayWebhookRetryableError  [webhook_engine.py#L139]  <── Caught by worker #L206
 │    ├── webhook_engine.RazorpayWebhookPermanentError  [webhook_engine.py#L143]  <── Caught by worker #L203 (DEAD FOR HANDLERS)
 │    └── webhook_engine.StaleLeaseError                [webhook_engine.py#L147]  <── SHARED (imported at handler_base.py#L62)
 │
 └── RazorpayWebhookHandlerError                        [handler_base.py#L73]     (NO relation to RazorpayWebhookEngineError!)
      ├── handler_base.RazorpayWebhookRetryableError    [handler_base.py#L77]     ──> Falls into `Exception` at worker #L206
      └── handler_base.RazorpayWebhookPermanentError    [handler_base.py#L84]     ──> Falls into `Exception` at worker #L206!
           ├── RazorpayWebhookReconciliationConflictError [handler_base.py#L91]  ──> Misclassified as RETRYABLE_FAILURE!
           ├── RazorpayPayloadValidationError             [handler_base.py#L121] ──> Misclassified as RETRYABLE_FAILURE!
           ├── RazorpayScopeMismatchError                 [handler_base.py#L125] ──> Misclassified as RETRYABLE_FAILURE!
           └── RazorpayMissingEntityIdError               [handler_base.py#L129] ──> Misclassified as RETRYABLE_FAILURE!
```

### 4.3 Worker Catch-Block Trace (`razorpay_webhook_worker.py#L177-L261`)

| Exception Raised by Handler / Runtime | Expected Classification (`ADR` / Engine Design) | Actual Matched Clause in `process_one()` (`497852e`) | Actual Database Outcome |
|---|---|---|---|
| `StaleLeaseError` (shared `webhook_engine` class) | Abort without touching event status/attempts | `#L187: except StaleLeaseError as exc:` | Returns `ProcessResult(success=False, status=failed)`; skips Session 3 (`outcome_db`) |
| `webhook_engine.RazorpayWebhookPermanentError` *(synthetic / direct engine raise only)* | `PERMANENT_FAILURE` -> `quarantined` on attempt `1` | `#L203: except RazorpayWebhookPermanentError as exc:` | Increments `attempts = 1`, sets `status = 'quarantined'` |
| `handler_base.RazorpayWebhookPermanentError` (`RazorpayPayloadValidationError`, `RazorpayScopeMismatchError`, `RazorpayMissingEntityIdError`, `RazorpayWebhookReconciliationConflictError`) | **`PERMANENT_FAILURE` -> immediate `quarantined` on attempt `1`** | **`#L206: except (RazorpayWebhookRetryableError, Exception) as exc:`** (`#L203` missed!) | **MISCLASSIFIED as `RETRYABLE_FAILURE`**: sets `status = 'failed'`, `next_retry_at = now + 5s`, retries up to `5` attempts (`5s -> 15s -> 45s -> 120s`, delaying quarantine by `185s` across 5 worker cycles) |
| `handler_base.RazorpayWebhookRetryableError` | `RETRYABLE_FAILURE` -> `failed` with backoff | `#L206: except (RazorpayWebhookRetryableError, Exception) as exc:` | Only works accidentally via `Exception` fallback |
| Unknown runtime / DB `SQLAlchemyError` / `DBAPIError` / `P0001` inside `handler_db` | Transient DB glitches retry; permanent SQL constraint/trigger violations should be classified/quarantined | `#L206: except (RazorpayWebhookRetryableError, Exception) as exc:` | All `Exception` subclasses become `RETRYABLE_FAILURE` (`attempts += 1`, `failed` until 5th failure); permanent programming/trigger errors spin 5 times |
| Unexpected `Exception` inside `outcome_db` (`#L212-L261`) | Roll back `outcome_db`, sanitize logs, never falsely claim processed | `#L254: except Exception as exc:` | Logs **unsanitized** `exc` with `exc_info=True` (`#L255-L260`); relies on `__aexit__` rollback; returns `None` (event remains leased until `lease_expires_at`) |
| `Exception` inside `claim_one()` (`#L142-L154`) or `start()` (`#L327-L328`) | Sanitized error logging, return `None` / backoff | `#L152` / `#L327` | Logs **raw `exc`** without `sanitize_error_message()` |

### 4.4 `asyncio.CancelledError` vs. Database Exceptions

- **Python Version Context:** Verified CI runtime is Python `3.11` ([`.github/workflows/ci.yml#L43`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/.github/workflows/ci.yml#L43)), where `asyncio.CancelledError` inherits from `BaseException` (not `Exception`).
- **`asyncio.CancelledError` Path:**
  - Not caught by `except (RazorpayWebhookRetryableError, Exception)` (`#L206`) or `except Exception` (`#L152, #L254`).
  - Exiting `async with self.session_factory() as handler_db:` triggers `AsyncSession.__aexit__` cleanup (rolling back uncommitted `handler_db`), propagates `CancelledError` cleanly out of `process_one()`, skips `outcome_db` entirely, and is caught in `start()` (`#L325: except asyncio.CancelledError: break`) for clean loop exit.
  - Because `outcome_db` is skipped, `BillingWebhookEvent` stays `pending`/`failed` with `attempts` unchanged and lease `(lease_token, locked_by, lease_expires_at)` intact until 60s expiry recovery (`claim_next_razorpay_webhook_event`).
  - *Gap:* Unlike Stage 1 `customer.py` (which underwent `PG-CUST-17` live asyncpg `FOR UPDATE` cancellation teardown testing), `RazorpayWebhookWorker.process_one()` has **zero live PG16 cancellation teardown verification** to prove blocked `asyncpg` connections exit cleanly without leaving idle-in-transaction backends.
- **Database Errors (`OperationalError`, `IntegrityError`, `SerializationFailure`, `DeadlockDetected`, `DBAPIError`):**
  - All inherit from `Exception`.
  - In `handler_db`, they are uniformly treated as `RETRYABLE_FAILURE` (`#L206`), meaning deterministic permanent failures (`NotNullViolationError`, `IntegrityError`, PL/pgSQL `P0001` trigger errors) are needlessly retried 5 times rather than inspected via PostgreSQL `SQLSTATE`.
  - In `outcome_db`, commit/update DB failures hit `#L254` and return `None`, leaving the row leased until `lease_expires_at` expires (or if Commit A had succeeded in a multi-session model, stranding Commit A without Commit B).

---

## 5. Detailed Investigation — D04-03: Terminal Subscription & Checkout Attempt State Analysis

### 5.1 Classification: `CONFIRMED` *(Checkout Attempt Resurrection)*

Detailed verification separates the three distinct domain entities touched (or intentionally untouched) by [`handle_subscription_activated()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/agreement_handlers.py#L333-L566):

1. **`BillingCheckoutAttempt` Resurrection — `CONFIRMED` (`#L560-L563`):** `handle_subscription_activated()` uses a negative exclusion guard (`if attempt.status != CheckoutAttemptStatus.complete:`) that **unconditionally overwrites `expired`, `failed`, `creating`, `recovery_required`, and elapsed `open` attempts to `'complete'`** whenever the subscription is newly created or in any non-terminal, non-`past_due` state.
2. **`Subscription` Terminal (`canceled`) Resurrection — `NOT REPRODUCIBLE` (`#L412-L418`):** When `Subscription.status` is already `'canceled'` (or `'completed'`), `is_terminal_subscription()` returns `True` and `handle_subscription_activated()` returns early at `#L418` **before** Step 5 — refusing both `Subscription` reactivation and `BillingCheckoutAttempt` mutation. However, critical edge cases remain (missing `subscription.cancelled` handler; `SubscriptionStatus` enum lacks `'completed'`; `incomplete_expired -> active` is permitted; early returns skip checkout attempt reconciliation).
3. **Paid Pro Quota / Entitlement Elevation — `NOT REPRODUCIBLE` (`#L463`, `accounting.py#L258-L274`):** `handle_subscription_activated()` strictly sets `plan_tier = "free"` on new rows, never upgrades `plan_tier`, and creates zero `OrganizationBillingPeriod` rows. Because [`reserve_usage()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/accounting.py#L258-L274) authorizes paid usage only when `OrganizationBillingPeriod.plan_tier != "free"` **and** `Subscription.status IN ('active', 'past_due')`, activation alone grants zero paid tokens.

### 5.2 Exhaustive State Matrix on Stage 1 (`497852e`)

#### A. `Subscription` Agreement & Entitlement Transitions (`handle_subscription_activated()`)

| Pre-Lock `Subscription` State | Stale Event (`created_at <= last_webhook_event_at`)? | Resulting `Subscription.status` | Resulting `plan_tier` / Periods | Reaches Step 5 (`BillingCheckoutAttempt`)? | Nuances / Edge-Case Risks Verified in Code |
|---|---|---|---|---|---|
| **`None`** *(not yet created locally)* | N/A | **`active`** | `plan_tier = "free"`, `0` periods | **Yes** | Occurs on out-of-order delivery or after Stage 1 D04-01 rollback of `authenticated` |
| **`incomplete`** | No | **`active`** | Preserved (`"free"`), `0` periods | **Yes** | Standard mandate activation flow |
| **`incomplete_expired`** | No | **`active`** | Preserved, `0` periods | **Yes** | Permitted by `#L437-L442` (`is_terminal_subscription` excludes `incomplete_expired`) |
| **`paused`** | No | **`active`** | Preserved, `0` periods | **Yes** | Permitted by `#L437-L442` |
| **`active`** | No | **`active`** *(period dates & `plan_id` refreshed)* | Preserved, `0` periods | **Yes** | Re-enters Step 5 unconditionally |
| **`past_due`** | No | **`past_due`** *(updates period dates only; preserves `past_due_since`)* | Preserved, `0` periods | **No (`return` `#L434`)** | Correctly refuses curing grace period, **but** returns before Step 5 |
| **`canceled`** *(or string `'completed'`)* | No | **`canceled`** *(unchanged)* | Preserved, `0` periods | **No (`return` `#L418`)** | Note: `SubscriptionStatus` enum (`subscription.py#L33-L43`) has only `'canceled'` (single `l`), no `'completed'`; **and** Razorpay `subscription.cancelled` webhook is unhandled (`UNKNOWN_EVENT` quarantine at `#L803-L815`), so cancelled subs remain `active`/`incomplete` locally! |
| **Any status** | Yes (`is_stale_event`) | Unchanged | Unchanged | **No (`return` `#L409`)** | Caveat: if payload omits `created_at`, `handler_base.py#L634` substitutes `datetime.now(timezone.utc)`, defeating monotonicity! |

#### B. `BillingCheckoutAttempt` Transitions (`agreement_handlers.py#L560-L563`) When Step 5 Executes

```python
# Committed Stage 1 (497852e) — agreement_handlers.py lines 560-563:
if attempt.status != CheckoutAttemptStatus.complete:
    attempt.status = CheckoutAttemptStatus.complete
    if sub_entity.customer_id and not attempt.provider_customer_id:
        attempt.provider_customer_id = sub_entity.customer_id
```

| Pre-Lock `BillingCheckoutAttempt.status` | `handle_subscription_authenticated()` (`#L316-L320`) | `handle_subscription_activated()` on Stage 1 (`#L560-L563`) | Safety / Integrity Violation |
|---|---|---|---|
| **`creating`** | -> **`payment_pending`** *(ignores missing lease/recovery)* | -> **`complete`** *(OVERWRITTEN)* | Prematurely completes an unverified creation lease, vacating the active uniqueness slot before gateway creation reconciliation finishes |
| **`recovery_required`** | Unchanged | -> **`complete`** *(OVERWRITTEN)* | Destroys ambiguous-creation quarantine without running `recover_razorpay_checkout_attempt()` |
| **`open`** *(unexpired: `now < session_expires_at`)* | -> **`payment_pending`** | -> **`complete`** | Expected transition for active mandate completion prior to expiry |
| **`open`** *(elapsed: `now >= session_expires_at`)* | -> **`payment_pending`** *(FAILS to check expiry!)* | -> **`complete`** *(OVERWRITTEN — never marks `'expired'`)* | Allows stale timed-out checkout session to bypass expiration in both handlers |
| **`payment_pending`** | Unchanged | -> **`complete`** | Expected transition when subscription activates |
| **`expired`** | Unchanged | -> **`complete`** *(RESURRECTED!)* | Violates `ADR-002` history immutability; retains past `session_expires_at` on a `'complete'` row and re-opens checkout callback payment binding! |
| **`failed`** | Unchanged | -> **`complete`** *(RESURRECTED!)* | Overwrites terminal payment/recovery failure back to `'complete'` with `provider_payment_id = None` (and leaves failure `error_message` or wipes it later on verify)! |
| **`complete`** | Unchanged | Unchanged (`#L560` skips) | Idempotent no-op |

### 5.3 Cross-Module Bypass Into `verify_razorpay_checkout_callback()` (`checkout.py`)

Once D04-01 is fixed so `handler_db` commits, D04-03 creates an immediate cross-module state-machine bypass in [`checkout.py#L1328-L1339, #L1894-L2016`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/checkout.py#L1894-L2016):

1. Suppose a `BillingCheckoutAttempt` transitions to **`expired`** (timeout) or **`failed`** (gateway rejection), or is **`open`** with `session_expires_at` already elapsed (`now_utc >= session_expires_at`).
2. A delayed `subscription.activated` webhook arrives and flips `attempt.status` from `expired` / `failed` / stale-`open` -> **`complete`** (leaving `provider_payment_id = None`).
3. When the client/delayed flow invokes [`verify_razorpay_checkout_callback()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/checkout.py#L1894-L2005):
   - `_is_attempt_session_expired(locked_attempt, now_lock)` (`#L1328-L1339`) returns **`False`** because it only considers `status == expired` or `status == open` (`complete` bypasses `#L1894` even though `session_expires_at` is in the past!).
   - The terminal-failure guard (`locked_attempt.status in (failed, creating, recovery_required)` at `#L1946-L1955`) is **bypassed** because `status` was overwritten to `complete`!
   - Execution falls through directly to `#L1979-L2005` (`elif locked_attempt.status == CheckoutAttemptStatus.complete:`): if `payment_status == "captured"`, it **binds `locked_attempt.provider_payment_id = pay_id` and clears `error_message = None`** instead of failing closed (`409`) and routing delayed capture to `delayed_capture_after_checkout_expiration` (`#L1900-L1933`).

---

## 6. Committed Stage 1 (`497852e`) vs. Uncommitted `main` (`027ab7a` + Dirty Drafts)

Inspecting [`D:/Projects/V1/Aptlyst-AI`](file:///D:/Projects/V1/Aptlyst-AI) (`main`, uncommitted `13` modified files) confirms that two local draft fixes (`Step 5.2D.3.3B.1B` and `1C`) were attempted there earlier **prior** to D-2 Stage 1 branching, were **never ported** into `validation/d2-stage1-customer-pg16`, and **still retain unresolved structural flaws**.

| Comparison Dimension | Committed Stage 1 (`validation/d2-stage1-customer-pg16` @ `497852e`) | Dirty `main` Drafts (`Step 1B` & `1C` @ `027ab7a` uncommitted) | Remaining Structural Flaws Even on Dirty `main` |
|---|---|---|---|
| **D04-01 (`handler_db` commit)** | **Absent:** never calls `handler_db.commit()`; all handler writes roll back silently (`#L178-L186`) | Calls `await handler_db.commit()` ("Commit A", `#L224`) + explicit `rollback()` (`#L227`), plus pre-handler & pre-commit `assert_valid_worker_lease()` (`#L198, #L215`) | **(1)** Commit A (`handler_db`) and Commit B (`outcome_db`) remain two non-atomic PG transactions (crash window persists); **(2)** On conflict exception, `handler_db.rollback()` deletes handler-staged `PaymentReconciliation` rows (`STEP_5_2D_3_3B_1B_WEBHOOK_TRANSACTION_REPORT.md#L212`); **(3)** `agreement_handlers` still locks `BillingWebhookEvent FOR UPDATE` with wall-clock `now` instead of `worker.clock`; **(4)** Zero `L0A` org advisory locks & zero `lock_timeout`; **(5)** `start()` busy-poll bug on partial batch / repeated `StaleLeaseError` |
| **D04-02 (Exception catch mismatch)** | **Absent:** catches only `webhook_engine.RazorpayWebhookPermanentError` (`#L203`); logs raw `exc` (`#L153, #L255, #L328`) | Aliases `handler_base` permanent/retryable errors (`#L45-L46`), catches both tuples (`#L250, #L259`), applies `sanitize_error_message()` in worker logs (`#L255, #L264, #L325`) | **(1)** Dual disjoint hierarchies remain un-unified in modules; **(2)** Conflict exceptions still do not trigger outer `PaymentReconciliation` persistence; **(3)** Permanent DB `SQLSTATE` errors (e.g. `P0001` triggers / `IntegrityError`) still map to 5-attempt `RETRYABLE_FAILURE` |
| **D04-03 (Attempt resurrection)** | **Absent:** `if attempt.status != complete:` (`#L560-L563`) overwrites `expired`/`failed`/`creating`/`recovery_required`/elapsed `open`; omits `populate_existing=True` | Adds `_is_open_attempt_session_expired()` (`#L334-L348`), `.execution_options(populate_existing=True)` (`#L403, #L499`), explicit guards preserving `expired`/`failed`/`creating`/`recovery_required` and marking elapsed `open` -> `expired` (`#L581-L634`) | **(1)** **`handle_subscription_authenticated()` (`#L316-L320`) still promotes elapsed `open` -> `payment_pending`** without checking `session_expires_at` (a subsequent `activated` sees `payment_pending` and marks `complete`!) and omits `populate_existing=True`; **(2)** Uses wall clock `datetime.now(timezone.utc)` (`#L581`) without `now` injection/DB time; **(3)** `past_due` branch (`#L441-L454`) returns before Step 5; **(4)** Missing `subscription.cancelled` handler |
| **Verification Basis** | `test_razorpay_webhook_worker.py` (`23` tests) & `test_razorpay_agreement_handlers.py` (`18` tests) — mocked, excluded from CI | Appends `test_24`–`test_34` (`+1513` lines) & `10` handler tests (`+1411` lines) — **all mocked** (`SharedRazorpayTransactionalStore`, `ConcurrentAgreementCheckoutStore`), **zero PG16 tests**, excluded from CI | Zero live PostgreSQL `16` proof of `SKIP LOCKED`, `COMMIT`/`ROLLBACK` transaction visibility, `pg_advisory_xact_lock`, unique/partial indexes, `P0001` triggers, or asyncpg cancellation cleanup |

> [!WARNING]
> Dirty `main` (`D:/Projects/V1/Aptlyst-AI`) also carries pre-Stage-1 diverged versions of `checkout.py` (`+1114`), `customer.py` (`+2113`), `.github/workflows/ci.yml` (`+184`), `billing.py`, and `session.py`. Nothing from dirty `main` should be cherry-picked blindly without reconciling against the validated `validation/d2-stage1-customer-pg16` (`497852e`) lineage.

---

## 7. Existing Test Coverage & Blind-Spot Gap Analysis

*Per strictly read-only constraints, `pytest` was not executed during this investigation.* Static inspection of all `50` backend test modules in `apps/api/tests/` and [`.github/workflows/ci.yml`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/.github/workflows/ci.yml#L181-L250) establishes how CI missed all three D-04 bugs.

### 7.1 CI vs. Local Test Coverage Inventory (Stage 1 `497852e`)

| Test Module | Test Functions (`def test_`) | Execution Mode | Executed in GitHub Actions CI (`ci.yml`)? | What Is Verified vs. Critical Unverified Gaps & Blind Spots |
|---|---:|---|:---:|---|
| [`test_postgres_provenance_integration.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_postgres_provenance_integration.py) | `14` | **Real PostgreSQL 16** | **Yes** (`#L189`, fail-closed JUnit gate) | Verifies Alembic schema backfill, provenance FKs, check constraints, and `P0001` immutability triggers. **Gap:** Covers zero webhook worker or agreement handler execution. |
| [`test_postgres_customer_integration.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_postgres_customer_integration.py) | `20` | **Real PostgreSQL 16** | **Yes** (`#L217`, fail-closed JUnit gate) | Verifies Stage 1 `get_or_create_razorpay_customer()` session ownership, locks, constraints, RLS, and PG16 cancellation (`PG-CUST-17`). **Gap:** Covers customer provisioning + checkout initiation only; zero webhook worker or verification settlement coverage. |
| `7` offline CI modules (`test_postgres_test_guard.py`, `test_payment_association_migration.py`, `test_provider_neutral_billing_models.py`, `test_provider_neutral_migration.py`, `test_razorpay_customer.py`, `test_razorpay_checkout.py`, `test_billing_api.py`) | `201` defs (`375` CI tests incl. parametrize) | **In-Memory Mock / Offline DDL** | **Yes** (`#L241-L249`) | Validates DDL strings, models, customer mocks, checkout mocks, API mocks. **Gap:** Neither webhook worker, agreement handlers, accounting, nor Stripe runs in CI (`41` of `50` test files excluded from CI). |
| [`test_razorpay_webhook_worker.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_razorpay_webhook_worker.py) | `23` *(Stage 1)* / `34` *(dirty `main`)* | **100% Mock (`RazorpayWorkerMockDbSession`)** | **No** | **Blind spots explaining D04-01 & D04-02 miss on Stage 1:** (1) `test_12` & `test_20` register empty no-op handlers (`pass`) so `handler_db` never mutates domain rows; (2) `test_6`–`test_10`, `test_13` bypass `process_one()` completely by calling `record_razorpay_webhook_outcome()` directly; (3) Mock tracks only `BillingWebhookEvent` (`SharedRazorpayEventRepository`), never checking domain commit counts or PostgreSQL rollback; (4) Never passes any `handler_base` exception into `process_one()`. *(Even dirty `main`'s `test_24`–`test_34` use `TransactionalWorkerMockDbSession`, not real PG16!)* |
| [`test_razorpay_agreement_handlers.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_razorpay_agreement_handlers.py) | `18` *(Stage 1)* / `28` defs / `30` cases *(dirty `main`)* | **100% Mock (`RazorpayAgreementMockDbSession`)** | **No** | **Blind spots explaining D04-01 & D04-03 miss on Stage 1:** (1) Tests call `handle_subscription_*` directly without `RazorpayWebhookWorker.process_one()`, and never assert `session.commits >= 1`; (2) `test_subscription_activated_refuses_resurrection_of_canceled_subscription` (`#L773-L818`) populates **no** `BillingCheckoutAttempt`, missing Step 5 entirely; (3) Zero Stage 1 tests invoke `handle_subscription_activated()` with `expired`, `failed`, stale `open`, `creating`, or `recovery_required` checkout attempts; (4) Stage 1 `RazorpayAgreementMockDbSession.execute()` (`#L200-L241`) ignores SQL `WHERE` filters entirely! *(Even dirty `main`'s new `ConcurrentAgreementCheckoutStore` simulates PG locks via `asyncio.Lock`.)* |
| [`test_razorpay_handler_foundation.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_razorpay_handler_foundation.py) | `21` | **100% Mock (`RazorpayHandlerMockDbSession`)** | **No** | Verifies `validate_razorpay_event_envelope`, `resolve_tenant_organization`, advisory key derivation, and `assert_valid_worker_lease` in isolation, **never checking** whether `RazorpayWebhookWorker` catches `RazorpayWebhookHandlerError` subclasses. |
| [`test_razorpay_webhook_ingestion.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_razorpay_webhook_ingestion.py) | `23` | **Mock (`InMemoryRazorpayDbSession`)** | **No** | Verifies `/api/v1/billing/razorpay/webhook` signature, payload size, and duplicate handling; does not execute downstream worker consumption on ingested rows. |

---

## 8. Operational Exposure Assessment

### **`PRODUCTION EXPOSURE NOT VERIFIED`**

Live production deployment status, environment variables, container orchestration, external schedulers, and active host processes are **outside repository control** and were not accessed under read-only restrictions. Therefore, live **`PRODUCTION EXPOSURE NOT VERIFIED`**.

### Repository-Level Exposure Evidence (Directly Verified in Tracked Files)

1. **Webhook Worker Not Embedded or Auto-Started by FastAPI:**
   - [`apps/api/app/main.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/main.py#L22-L44, #L110-L115) starts only rate-limiter validation and HTTP routers; it imports neither `RazorpayWebhookWorker` nor `StripeWebhookWorker`.
   - [`apps/api/app/workers/__init__.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/__init__.py) exports only `StripeWebhookWorker`.
   - Repository-wide `git grep -n "razorpay_webhook_worker"` and `git grep -n "handle_subscription_"` confirm **zero automated runtime callers** outside tests, documentation, and [`razorpay_webhook_worker.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L486-L519) (`python -m app.workers.razorpay_webhook_worker`).
2. **No Tracked Deployment / Daemon / Scheduler Manifests:**
   - Full `git ls-files` inspection confirms zero `Dockerfile`, `docker-compose*`, `Procfile`, `render.yaml`, `fly.toml`, Kubernetes manifests, systemd units, supervisor configs, or GitHub Actions scheduled cron jobs running the worker. [`DEVELOPMENT_SETUP.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/development/DEVELOPMENT_SETUP.md#L90-L97) documents only manual CLI invocation (`--once` / `--poll-interval 2.0`).
3. **Ingress Endpoint & Shared Database Coupling Exist in Code:**
   - FastAPI mounts `POST /api/v1/billing/razorpay/webhook` ([`api/billing.py#L845-L951`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/api/billing.py#L845-L951)) which shares `DATABASE_URL` (`AsyncSessionLocal`) with the worker queue `billing_webhook_events`.
   - Ingress is fail-closed by default in code (`settings.razorpay_enabled: bool = False`, [`config.py#L412`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/config.py#L412)), returning HTTP `503` unless `RAZORPAY_ENABLED=true` and secrets are supplied via runtime environment configuration.
   - **Risk Condition:** If any staging/production environment currently runs with `RAZORPAY_ENABLED=true` *and* an out-of-repo worker process (`python -m app.workers.razorpay_webhook_worker` on commit `497852e` or earlier), every processed Razorpay webhook is actively suffering silent D04-01 data loss.

---

## 9. Evidence Table (Committed Stage 1 `497852e` Unless Noted)

| Domain / Finding | File Path | Symbol / Scope | Lines | Verified Observation |
|---|---|---|---|---|
| **D04-01** | [`razorpay_webhook_worker.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L141-L261) | `RazorpayWebhookWorker.process_one()` | `#L141-L261` | Opens Session 1 (`claim_one()`), Session 2 (`handler_db`, `#L178-L186` — **never calls `commit()`**), Session 3 (`outcome_db`, `#L212-L261` — marks `processed`) |
| **D04-01** | [`session.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/db/session.py#L17-L28) | `engine`, `AsyncSessionLocal` | `#L17-L28` | `pool.NullPool`, `expire_on_commit=False`, `autocommit=False` — un-committed `handler_db` rolls back automatically on `__aexit__` |
| **D04-01** | [`agreement_handlers.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/agreement_handlers.py#L324-L794) | `handle_subscription_*()` | `#L324, #L565, #L690, #L794` | All 4 agreement handlers call `await db.flush()` only (or `#L648, #L771` early-return reconciliation flush); zero handlers commit |
| **D04-01** | [`webhook_engine.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/webhook_engine.py#L297-L440) | `claim_next_razorpay_webhook_event()`, `record_razorpay_webhook_outcome()` | `#L297, #L363-L438` | Session 1 commits claim lease (`#L297`); Session 3 locks `BillingWebhookEvent FOR UPDATE` (`#L363-L367`), sets `processed`, clears lease, commits (`#L438`) |
| **D04-01** | [`stripe_webhook_engine.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/stripe_webhook_engine.py#L577-L670) | `execute_claimed_webhook_event()` | `#L577-L670` | Contrasting reference pattern: single tx with `db.begin_nested()` savepoint + outer outcome commit + outer `reconciliation_record` persistence (`#L587-L608`) |
| **D04-02** | [`webhook_engine.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/webhook_engine.py#L135-L148) | `RazorpayWebhookEngineError` hierarchy | `#L135-L148` | Defines `RazorpayWebhookRetryableError`, `RazorpayWebhookPermanentError`, `StaleLeaseError` |
| **D04-02** | [`handler_base.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/handler_base.py#L60-L131) | `RazorpayWebhookHandlerError` hierarchy | `#L60-L131` | Imports only `StaleLeaseError` (`#L62`); defines unrelated `RazorpayWebhookRetryableError` (`#L77`) & `RazorpayWebhookPermanentError` (`#L84`) subclasses |
| **D04-02** | [`razorpay_webhook_worker.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/workers/razorpay_webhook_worker.py#L43-L56) | Worker exception imports & catches | `#L49-L52, #L187-L209, #L254-L261` | Imports only `webhook_engine` errors; `#L203` misses all `handler_base` permanent/conflict errors; `#L206` maps them to `RETRYABLE_FAILURE`; `#L153, #L255, #L328` log raw `exc` |
| **D04-03** | [`agreement_handlers.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/agreement_handlers.py#L400-L566) | `handle_subscription_activated()` | `#L412-L468, #L560-L563` | Protects `Subscription` `canceled` (`#L412-L418`), `past_due` (`#L421-L434`), keeps `plan_tier="free"` (`#L463`), **but `#L560-L563` (`!= complete`) overwrites `expired`/`failed`/`creating`/`recovery_required`/elapsed `open` attempts to `'complete'`** |
| **D04-03** | [`agreement_handlers.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/agreement_handlers.py#L316-L320) | `handle_subscription_authenticated()`, dispatcher registry | `#L316-L320, #L803-L815` | `authenticated` skips `expired`/`failed` but converts elapsed `open` -> `payment_pending` without expiry check; only 4 agreement events registered (missing `subscription.cancelled`) |
| **D04-03** | [`checkout.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/checkout.py#L1328-L1339) | `_is_attempt_session_expired()`, `verify_razorpay_checkout_callback()` | `#L1328-L1339, #L1894-L2005` | Bypassed when `handle_subscription_activated()` flips `expired`/`failed`/elapsed `open` -> `complete`; `#L1979-L2005` binds `provider_payment_id` onto resurrected `'complete'` rows |
| **D04-03** | [`accounting.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/accounting.py#L258-L274) | `reserve_usage()` | `#L258-L274` | Enforces `period.plan_tier != "free"` + `Subscription.status IN (active, past_due)`; confirms activation alone grants 0 paid tokens |
| **Dirty `main`** | [`main/razorpay_webhook_worker.py`](file:///D:/Projects/V1/Aptlyst-AI/apps/api/app/workers/razorpay_webhook_worker.py#L44-L327) | Draft `Step 1B` `process_one()` | `#L44-L48, #L190-L327` | Adds pre/post fencing, Commit A (`#L224`), rollback (`#L227`), dual-hierarchy catches (`#L250, #L259`), log sanitization; leaves Commit A/B split & wiped reconciliations |
| **Dirty `main`** | [`main/agreement_handlers.py`](file:///D:/Projects/V1/Aptlyst-AI/apps/api/app/services/billing/razorpay/agreement_handlers.py#L334-L634) | Draft `Step 1C` `_is_open_attempt_session_expired()`, `handle_subscription_activated()` | `#L334-L348, #L403, #L499, #L581-L634` | Preserves `expired`/`failed` & marks elapsed `open` -> `expired`; omits `authenticated` expiry check & uses wall-clock `datetime.now(timezone.utc)` |
| **Blind Spots** | [`.github/workflows/ci.yml`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/.github/workflows/ci.yml#L181-L250) | CI pytest jobs | `#L181-L250` | Runs `9` of `50` modules (`409` tests); excludes worker, agreement handler, foundation, and webhook ingestion suites |
| **Exposure** | [`main.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/main.py#L22-L115), [`config.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/config.py#L412-L420), [`billing.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/api/billing.py#L845-L951) | `lifespan`, `Settings.razorpay_enabled`, `handle_razorpay_webhook` | `#L22-L44`, `#L412`, `#L845-L951` | Worker not started by FastAPI; `razorpay_enabled = False` default; ingestion writes to shared PG `billing_webhook_events` |

---

## 10. Severity Assessment

| Item | Rating | Justification |
|---|---|---|
| **D04-01** | **Critical (P0)** | **Guaranteed silent billing corruption under any real execution:** Webhook queue drains and irreversibly marks events `'processed'` while rolling back all domain state transitions (`Subscription`, `BillingCheckoutAttempt`, `PaymentReconciliation`), violating durability, idempotency, and auditability (`ADR-002`). |
| **D04-02** | **High (P1)** | **Complete failure of worker error classification:** All permanent payload, merchant/env scope, missing-ID, and tenant reconciliation conflicts from production handlers fall through dead-code `except RazorpayWebhookPermanentError` and loop 5 times on retry queues, leaking raw exceptions in outcome/loop logs and losing staged reconciliations on rollback. |
| **D04-03** | **High (P1)** | **Cross-module state corruption & checkout safety bypass:** Once D04-01 commits domain mutations, `handle_subscription_activated()` resurrects `expired` and `failed` checkout attempts to `'complete'`, poisoning audit history (`ADR-002`) and opening a direct bypass through `verify_razorpay_checkout_callback()` (`#L1979-L2005`) that binds delayed captured payments instead of failing closed (`409`) and reconciling. *(Downgraded from P0 only because `Subscription.plan_tier` stays `'free'` and no `OrganizationBillingPeriod` is created.)* |
| **Combined Composite Risk** | **Critical Stage 2 Blocker** | Building Stage 2 payment settlement (`payment.captured`, `subscription.charged`, `OrganizationBillingPeriod` provisioning) atop `497852e`—or atop dirty `main`'s unvalidated drafts—would either drop paid entitlements silently (`D04-01`), thrash conflicting payments across retries without saving `PaymentReconciliation` records (`D04-02`), or bind settled payments onto expired/failed checkout sessions (`D04-03`). |

---

## 11. Recommended Correction Sequence (Read-Only Design Proposal)

Do **not** begin D-2 Stage 2 settlement work until these prerequisites are executed cleanly in order on a dedicated branch off `validation/d2-stage1-customer-pg16` (or after reconciling the repository cleanly):

1. **Unify Exception Hierarchies & PostgreSQL SQLSTATE Mapping (Fixes D04-02 Foundation):**
   - Make `handler_base.RazorpayWebhookHandlerError` inherit from `webhook_engine.RazorpayWebhookEngineError` (and unify or subclass `RazorpayWebhookRetryableError` / `RazorpayWebhookPermanentError` so a single canonical base class pair governs both engine and all Razorpay handlers).
   - Ensure `RazorpayWebhookReconciliationConflictError` carries deterministic conflict metadata (`case_key`, `reason`, `organization_id`, `subscription_id`, `invoice_id`, `payment_id`, `raw_event_id`, `diagnostic_payload`) suitable for outer-transaction persistence.
   - Add explicit `SQLSTATE` classification (e.g., retry `40001`/`40P01`/`55P03` transient lock/serialization failures vs. quarantine permanent `P0001` trigger / deterministic schema errors) and enforce `sanitize_error_message()` on every worker log (`claim_one()`, `process_one()`, `start()`).

2. **Redesign Worker Transaction Boundary to Eliminate Split-Brain Commit & Preserved Conflicts (Fixes D04-01 + Split-Window Risk):**
   - Preserve Session 1 (`claim_one()`: `FOR UPDATE SKIP LOCKED` -> commit claim lease -> close session cleanly).
   - Isolate external HTTP calls (if any future Stage 2 handler needs gateway queries) strictly **outside** database transactions (per `ADR-005` & D-2 Stage 1 pattern).
   - Combine post-HTTP DB persistence (**lease fencing + domain mutations + outcome status update + conflict reconciliation**) into **one atomic transaction** around a `begin_nested()` savepoint for handler execution (mirroring [`stripe_webhook_engine.py#L577-L670`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/stripe_webhook_engine.py#L577-L670) plus Razorpay's lease check):
     - Acquire locks strictly in `ADR-005` order (`L0A org advisory` / `L0B entity advisory` -> `BillingWebhookEvent FOR UPDATE` or validate lease via conditional `UPDATE ... WHERE lease_token = :token AND locked_by = :worker AND lease_expires_at >= now()` cleanly after savepoint / before domain updates so `BillingWebhookEvent` row locks never invert against `L0A`/`L0B`/`L1`/`L2`).
     - Run handler inside `async with db.begin_nested():` (handlers remain pure flush-only).
     - On `RazorpayWebhookReconciliationConflictError`: let savepoint roll back bad partial domain state cleanly, persist/upsert `PaymentReconciliation` **in the outer transaction** using `INSERT ... ON CONFLICT (provider, merchant_account_id, environment, case_key) DO UPDATE`, and atomically commit `status = 'quarantined'` + lease clear!
     - On `SUCCESS`: commit handler domain writes + `status = 'processed'` + lease clear in **one ACID PostgreSQL `COMMIT`**, eliminating the Commit A / Commit B crash window completely.
   - Pass worker `clock` cleanly into lease checks and fix `RazorpayWebhookWorker.start()` polling sleep behavior on partial/rejected batches.

3. **Harden Checkout Attempt & Subscription Agreement State Machines (Fixes D04-03 + Sibling Gaps):**
   - In `handle_subscription_activated()`: add `.execution_options(populate_existing=True)` on `FOR UPDATE` queries; add UTC expiry helper `_is_open_attempt_session_expired(attempt, now_utc=..., event_created_at=...)`; allow `-> complete` **strictly** for unexpired `open` and `payment_pending`; transition elapsed `open` -> `expired` without altering `session_expires_at`; refuse modification of `expired`, `failed`, `creating`, `recovery_required`, and idempotent `complete`.
   - In `handle_subscription_authenticated()`: apply the exact same `.execution_options(populate_existing=True)` and expiry guard on `open` (`now >= session_expires_at` or `event_created_at >= session_expires_at` -> transition `open -> expired` instead of promoting stale `open` -> `payment_pending`).
   - Add handler support for Razorpay `subscription.cancelled` (or explicit cancellation lifecycle design per `D-01` / `ADR-003`) and fix `validate_razorpay_event_envelope()` / monotonic guards when payloads lack `created_at`.

4. **Add Real PostgreSQL 16 Webhook Integration Tests & Wire Into CI (`ci.yml`):**
   - Build a `test_postgres_webhook_worker_integration.py` suite executing against disposable PostgreSQL 16 (following `postgres_test_guard.py` + `REQUIRE_POSTGRES_TESTS=1` fail-closed gate) testing: end-to-end `handler -> PG commit -> processed`; handler failure rollback + outer `PaymentReconciliation` durability + immediate quarantine; stale/expired lease fencing under real MVCC; concurrent `RazorpayWebhookWorker` + `verify_razorpay_checkout_callback()` race preserving `expired`/`failed` attempts; `asyncio.CancelledError` during blocked locks checking `pg_stat_activity`; and advisory lock serialization (`L0A`/`L0B`).
   - Add `test_razorpay_webhook_worker.py`, `test_razorpay_agreement_handlers.py`, `test_razorpay_handler_foundation.py`, `test_razorpay_webhook_ingestion.py`, and the new PG16 webhook suite into `.github/workflows/ci.yml`.

5. **Branch Governance Cleanup:**
   - Reconcile or archive stale dirty edits on [`D:/Projects/V1/Aptlyst-AI`](file:///D:/Projects/V1/Aptlyst-AI) cleanly against `validation/d2-stage1-customer-pg16` (`497852e`) so engineers and audits target a single unambiguous source of truth.

---

## 12. Explicit Conclusion

All three D-04 findings (**D04-01**, **D04-02**, and **D04-03** for checkout attempt resurrection) are **`CONFIRMED`** on committed Stage 1 (`validation/d2-stage1-customer-pg16` @ `497852e`). While `Subscription.status = 'canceled'` resurrection and unintended Pro token quota grants are **`NOT REPRODUCIBLE`** on Stage 1, and live deployment exposure cannot be determined from Git alone (**`PRODUCTION EXPOSURE NOT VERIFIED`**), running the Razorpay webhook worker in its current committed state will silently discard domain mutations while marking webhooks `'processed'`, misroute permanent conflicts into retries, and corrupt expired/failed checkout state once commits are enabled. **D-2 Stage 2 settlement work must remain blocked until Section 11's remediations are implemented and verified against real PostgreSQL 16 in CI.**
