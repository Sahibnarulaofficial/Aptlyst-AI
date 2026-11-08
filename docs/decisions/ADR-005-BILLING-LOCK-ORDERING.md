# ADR-005: Billing Concurrency Control and Monotonic Lock Ordering

## Status
**Accepted** (Architectural Concurrency Model Approved; Entity Advisory Locks Implemented; Global Organization-Level Lock Hierarchy Pending Step 5.2D.3.3B)

## Context
Payment processing systems face severe concurrency challenges when multiple events arrive simultaneously for the same tenant. In Aptlyst AI, potential concurrent paths include:
1. A user completing a checkout verification flow on the frontend (`/verify`).
2. An asynchronous Razorpay webhook (`payment.captured` or `subscription.charged`) arriving at the ingestion endpoint.
3. A background sweeper or reconciliation worker evaluating expired attempts.
4. An out-of-band subscription cancellation or update from the payment gateway.

If transactions acquire row-level locks on different tables in differing orders (e.g., Transaction A locks `subscriptions` then `organization_billing_periods`, while Transaction B locks `organization_billing_periods` then `subscriptions`), database deadlocks occur.

Furthermore, when an organization signs up for the first time or resubscribes after being on the free tier, it may have **zero active billing periods**. In this scenario, executing `SELECT ... FOR UPDATE` on `organization_billing_periods` returns zero rows and acquires **no lock**, allowing concurrent checkouts to race and create duplicate periods.

Finally, executing external HTTP network calls (e.g. calling Razorpay API) while holding open database transactions causes connection pool exhaustion and transaction bloat.

---

## Accepted Architectural Decisions
1. **Level 0 Organization Advisory Lock**:
   - Every transaction that provisions entitlements, creates checkouts, or settles payments must acquire a PostgreSQL transaction-scoped advisory lock on the organization:
     ```sql
     SELECT pg_advisory_xact_lock(hashtext('org:' || :organization_id));
     ```
   - This advisory lock is independent of table row existence, completely closing the concurrency void for organizations with zero billing periods.
2. **Monotonic 5-Level Lock Hierarchy**:
   All database locks must be acquired in a strictly ascending order across all code paths:
   - **Level 0A**: Organization Advisory Lock (`hashtext('org:' || org_id)`)
   - **Level 0B**: Entity Advisory Lock (`hashtext('subscription:' || sub_id)` or `hashtext('payment:' || pay_id)`)
   - **Level 1**: Billing Period Row Lock (`organization_billing_periods` `FOR UPDATE`)
   - **Level 2**: Subscription Row Lock (`subscriptions` `FOR UPDATE`)
   - **Level 3**: Auxiliary Row Locks (`billing_invoices`, `billing_payments`, `billing_checkout_attempts` `FOR UPDATE`)
   Acquiring a lock at a lower level while holding a lock at a higher level is strictly prohibited.
3. **Strict Network Isolation Outside Database Transactions**:
   - All external HTTP calls to Razorpay, Stripe, or third-party APIs must execute **outside** database transactions.
   - Database locks are only acquired when reading state to prepare payloads or committing settlement state to the database.

---

## Implementation State Breakdown

### 1. Implemented Functionality (Verified in Code)
- **Entity-Level Transaction-Scoped Advisory Locking**:
  - Implemented in `apps/api/app/services/billing/razorpay/lock.py` (`acquire_razorpay_advisory_lock` and `try_acquire_razorpay_advisory_lock`).
  - Derives deterministic signed 64-bit integer keys using `SHA-256("razorpay:<clean_type>:<clean_id>")[:8]`.
  - Whitelists entity types: `{"subscription", "payment", "invoice", "customer"}`.
  - Used in `apps/api/app/services/billing/razorpay/agreement_handlers.py` (locking on `subscription`).
- **Invoice Advisory Locking in Stripe Handlers**:
  - Implemented in `apps/api/app/services/billing/stripe_invoice_paid_handler.py` and `stripe_invoice_payment_failed_handler.py`.
- **Row-Level Lock Sequencing**:
  - Implemented in existing agreement handlers (`OrganizationBillingPeriod` locked before `Subscription`).

### 2. Proposed Implementation (Pending Step 5.2D.3.3B)
- **Level 0A Organization Advisory Locking**:
  - Expanding `lock.py` to support `organization` advisory locking (`pg_advisory_xact_lock(hashtext('org:' || org_id))`).
  - Enforcing Level 0A locking in checkout initialization and upcoming payment settlement handlers.
- **Unified 5-Level Lock Ordering**:
  - Full enforcement across `subscription.authenticated`, `subscription.charged`, delayed payment handlers, and the payment-binding sweeper.

### 3. Outstanding Validation
- Live concurrent execution testing in PostgreSQL CI to verify deadlock freedom under parallel checkout verification and webhook delivery.

### 4. Rejected Approaches
- **Row-Only Locking (`SELECT ... FOR UPDATE`)**: Discarded because an organization with zero active billing periods has no row to lock, creating a concurrency race void.
- **Network Calls Under Lock**: Discarded because third-party API latency while holding database transactions exhausts database connection pools.

---

## Consequences

### Positive
- Mathematically eliminates database deadlocks across all billing execution paths.
- Guarantees serialized execution per organization, preventing duplicate period creation or concurrent attempt collisions.
- Eliminates transaction connection starvation by ensuring HTTP latency never delays database locks.

### Negative / Trade-offs
- Operations on the same organization are serialized, slightly limiting parallel throughput for high-concurrency actions on a single tenant (acceptable since an individual organization rarely performs multiple simultaneous checkouts).
- Advisory locks require PostgreSQL (`pg_advisory_xact_lock`), requiring integration tests to run against a real PostgreSQL instance rather than SQLite.

---

## Relevant Implementation References
- `apps/api/app/services/billing/razorpay/lock.py` (`acquire_razorpay_advisory_lock`)
- `apps/api/app/services/billing/razorpay/agreement_handlers.py`
- `apps/api/tests/test_razorpay_handler_foundation.py`
- `apps/api/tests/test_postgres_provenance_integration.py`

## Supporting Historical Reports
- [`STEP_5_2D_3_2_5_SETTLEMENT_CONCURRENCY_FINAL_AUDIT.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_5_SETTLEMENT_CONCURRENCY_FINAL_AUDIT.md)
- [`STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md)
- [`STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md)
- [`STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md)
