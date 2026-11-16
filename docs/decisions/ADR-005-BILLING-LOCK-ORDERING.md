# ADR-005: Billing Concurrency Control and Monotonic Lock Ordering

## Status
**Accepted** (Architectural Concurrency Model Approved; Provider-Neutral Advisory Lock Module Implemented via BLK-03; Monotonic Lock Hierarchy Defined)

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
     ```python
     key = derive_advisory_lock_key("organization", organization_id)
     acquire_advisory_lock(session, "organization", organization_id)
     ```
   - Uses a deterministic signed 64-bit big-endian integer derived from `SHA-256("organization:<canonical_uuid>")[:8]`.
   - This advisory lock is independent of table row existence, completely closing the concurrency void for organizations with zero billing periods.
2. **Monotonic 5-Level Lock Hierarchy**:
   All database locks must be acquired in a strictly ascending order across all code paths:
   - **Level 0A**: Organization Advisory Lock (`derive_advisory_lock_key("organization", org_id)`)
   - **Level 0B**: Entity Advisory Lock (`derive_advisory_lock_key("subscription", sub_id)` or `derive_advisory_lock_key("payment", pay_id)`)
     - *Multi-Entity Monotonic Ascending Integer Sort Ordering*: When acquiring multiple Level 0B entity locks within a single transaction, the distinct 64-bit signed integer keys must be sorted and acquired in strictly ascending numerical order ($K_1 < K_2 < \dots < K_n$). This eliminates circular-wait deadlocks among sibling resources.
     - *Logical Deduplication & Collision Handling*: Identical entity pairs are deduplicated prior to acquisition. In the astronomically rare event ($p \approx 2^{-64}$) that two distinct logical resources hash to the same 64-bit integer, a structured warning is emitted and the single lock is acquired safely without self-deadlock.
   - **Level 1**: Billing Period Row Lock (`organization_billing_periods` `FOR UPDATE`)
   - **Level 2**: Subscription Row Lock (`subscriptions` `FOR UPDATE`)
   - **Level 3**: Auxiliary Row Locks (`billing_invoices`, `billing_payments`, `billing_checkout_attempts` `FOR UPDATE`)
   Acquiring a lock at a lower level while holding a lock at a higher level is strictly prohibited.
3. **Strict Network Isolation Outside Database Transactions**:
   - All external HTTP calls to Razorpay, Stripe, or third-party APIs must execute **outside** database transactions.
   - Database locks are only acquired when reading state to prepare payloads or committing settlement state to the database.

---

## Implementation State Breakdown

### 1. Implemented Functionality (Verified in Code — BLK-03)
- **Canonical Provider-Neutral Advisory Lock Module**:
  - Implemented in `apps/api/app/services/billing/advisory_lock.py` (`derive_advisory_lock_key`, `acquire_advisory_lock`, `try_acquire_advisory_lock`, `acquire_multi_entity_advisory_locks`).
  - Generates signed 64-bit big-endian integers via `struct.unpack(">q", sha256_prefix[:8])[0]`.
  - Whitelists namespaces: `{"organization", "user", "subscription", "payment", "invoice", "customer"}`.
  - Normalizes UUID namespaces (`organization`, `user`) to canonical hyphenated lowercase UUID strings.
  - Preserves exact raw provider-identifier semantics for external gateways (Razorpay, Stripe) ensuring 100% hash parity with historical records.
- **Backward-Compatible Razorpay Facade**:
  - `apps/api/app/services/billing/razorpay/lock.py` preserves exact public API (`derive_razorpay_advisory_lock_key`, `acquire_razorpay_advisory_lock`, `try_acquire_razorpay_advisory_lock`, `ALLOWED_ENTITY_TYPES`), delegating to `advisory_lock.py` without breaking changes.
- **Transitional Dual-Locking in Organization API**:
  - `apps/api/app/api/organizations.py` implements transitional dual-locking during organization creation:
    1. Legacy 31-bit modulo lock: `pg_advisory_xact_lock(int(current_user.id.int % 2147483647))` (held for rolling-deployment backward compatibility).
    2. Canonical 64-bit user lock: `acquire_advisory_lock(db, "user", current_user.id)`.
- **Entity Advisory Locking in Stripe Handlers**:
  - Implemented in `apps/api/app/services/billing/stripe_invoice_paid_handler.py` and `stripe_invoice_payment_failed_handler.py`.
- **Row-Level Lock Sequencing**:
  - Implemented in agreement handlers (`OrganizationBillingPeriod` locked before `Subscription`).

### 2. Lock Key Derivation Contracts

| Domain / Namespace | Input Format | Derivation Formula | Range |
| :--- | :--- | :--- | :--- |
| **Razorpay Entity** | `subscription`, `payment`, etc. | `SHA-256("razorpay:<clean_type>:<clean_id>")[:8]` | Signed 64-bit Big-Endian |
| **Stripe Invoice** | `invoice` | `SHA-256("stripe_invoice:<raw_invoice_id>")[:8]` | Signed 64-bit Big-Endian |
| **Organization** | Canonical UUID | `SHA-256("organization:<normalized_uuid>")[:8]` | Signed 64-bit Big-Endian |
| **User** | Canonical UUID | `SHA-256("user:<normalized_uuid>")[:8]` | Signed 64-bit Big-Endian |
| **Legacy User (31-bit)** | User UUID | `user.id.int % 2147483647` | Signed 31-bit Positive Integer |

### 3. Validation & Concurrency Assurance
- Unit test suite in `apps/api/tests/test_advisory_lock.py` validates golden hash values, signed 64-bit boundary constraints, multi-entity ascending sort, collision handling, and dual-lock sequencing.
- Agreement handlers validated in `tests/test_razorpay_agreement_handlers.py` and `tests/test_razorpay_handler_foundation.py`.
- Live PostgreSQL 16 CI container validation confirmed via D-04A Stage 5 closure (PR #2 merge commit `6d11ec53dab47c4159f56c9b119762ced146033a`).

### 4. Rejected Approaches
- **Row-Only Locking (`SELECT ... FOR UPDATE`)**: Discarded because an organization with zero active billing periods has no row to lock, creating a concurrency race void.
- **Unsorted Multi-Entity Locking**: Discarded because acquiring multiple entity locks in arbitrary order risks circular-wait deadlocks under concurrent events.
- **Network Calls Under Lock**: Discarded because third-party API latency while holding database transactions exhausts database connection pools.
- **Premature Removal of Legacy 31-Bit Lock**: Discarded to ensure safe rolling upgrades across multiple deployment nodes without mutual exclusion gaps.

---

## Consequences

### Positive
- Mathematically eliminates database deadlocks across all billing execution paths.
- Guarantees serialized execution per organization, preventing duplicate period creation or concurrent attempt collisions.
- Unifies advisory lock derivation across Razorpay, Stripe, and internal multi-tenant domains into a single testable primitive.
- Eliminates transaction connection starvation by ensuring HTTP latency never delays database locks.

### Negative / Trade-offs
- Operations on the same organization are serialized, slightly limiting parallel throughput for high-concurrency actions on a single tenant (acceptable since an individual organization rarely performs multiple simultaneous checkouts).
- Advisory locks require PostgreSQL (`pg_advisory_xact_lock`), requiring integration tests to run against a real PostgreSQL instance rather than SQLite.

---

## Relevant Implementation References
- `apps/api/app/services/billing/advisory_lock.py` (`acquire_advisory_lock`, `acquire_multi_entity_advisory_locks`)
- `apps/api/app/services/billing/razorpay/lock.py` (`acquire_razorpay_advisory_lock` facade)
- `apps/api/app/api/organizations.py` (`create_organization` dual-lock)
- `apps/api/app/services/billing/razorpay/agreement_handlers.py`
- `apps/api/tests/test_advisory_lock.py`
- `apps/api/tests/test_razorpay_handler_foundation.py`
- `apps/api/tests/test_postgres_provenance_integration.py`

## Supporting Historical Reports
- [`STEP_5_2D_3_2_5_SETTLEMENT_CONCURRENCY_FINAL_AUDIT.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_5_SETTLEMENT_CONCURRENCY_FINAL_AUDIT.md)
- [`STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md)
- [`STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md)
- [`STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md)
