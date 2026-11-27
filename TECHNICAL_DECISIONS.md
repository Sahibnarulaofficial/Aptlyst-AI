# Aptlyst AI — Technical Decisions Index

This document serves as the central index of **Architecture Decision Records (ADRs)** for the Aptlyst AI platform.

Each record documents a significant architectural decision, including its context, alternatives considered, decision rationale, and engineering consequences.

---

## Architecture Decision Records (ADRs)

| ADR ID | Title | Status | Date | Summary |
| :--- | :--- | :---: | :---: | :--- |
| [**ADR-001**](docs/decisions/ADR-001-BILLING-PROVIDER-MIGRATION.md) | **Billing Provider Migration and Provider-Neutral Foundation** | **Accepted** | 2026-11-06 | Architectural transition from Stripe to Razorpay Subscriptions (INR/RBI e-mandate support) via provider-neutral models (`BillingCustomer`, `BillingCheckoutAttempt`, `BillingInvoice`, `BillingPayment`, `BillingWebhookEvent`). Infrastructure through Stage 2K is merged (`6872f47`) and frozen under [Stage 2L Billing Freeze](docs/operations/BILLING_HANDOFF_AND_FREEZE.md); payment settlement handlers and live Razorpay integration await the Final Razorpay Phase after product development. |
| [**ADR-002**](docs/decisions/ADR-002-FULL-PAYMENT-SETTLEMENT.md) | **Full Payment Settlement Policy and Immutability Invariants** | **Accepted** | 2026-11-08 | Enforces the *Conservative Core Billing Policy* (excising immediate resets; unexpired paid quotas run to `period_end`; resubscriptions queue future periods), *Refusal of Resurrection* (terminal states are permanent), and *Refusal to Rewrite History* (late captures do not alter `expired` checkout attempt status). Terminal guards and Stage 2K infrastructure merged (`6872f47`); settlement handlers and period rollover remain deferred to the Final Razorpay Phase under [Stage 2L Billing Freeze](docs/operations/BILLING_HANDOFF_AND_FREEZE.md). |
| [**ADR-003**](docs/decisions/ADR-003-SUBSCRIPTION-ENTITLEMENT-SEPARATION.md) | **Subscription and Entitlement Separation (Option A Decoupling)** | **Accepted** | 2026-11-08 | Strictly decouples recurring payment contracts (`Subscription`) from tenant entitlements (`OrganizationBillingPeriod`). Canceling an auto-renewal stops future charges but leaves active paid quota intact until `period_end`. Schema separation and Stage 2K infrastructure merged (`6872f47`); Pro provisioning/settlement handlers remain deferred to the Final Razorpay Phase under [Stage 2L Billing Freeze](docs/operations/BILLING_HANDOFF_AND_FREEZE.md). |
| [**ADR-004**](docs/decisions/ADR-004-PAYMENT-PROVENANCE.md) | **Payment Association Provenance, Composite Foreign Keys, and Conflict Gates** | **Accepted** | 2026-11-08 | Closes the PostgreSQL composite foreign key `MATCH SIMPLE` null-bypass vulnerability using `ck_billing_payments_checkout_attempt_complete`. Enforces PostgreSQL immutability triggers on provenance columns and cross-provider candidate conflict detection (`e2f3a4b5c6d1`). Verified on PostgreSQL 16 CI (`6872f47`). |
| [**ADR-005**](docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md) | **Billing Concurrency Control and Monotonic Lock Ordering** | **Accepted** | 2026-11-08 | Enforces canonical provider-neutral advisory locking (`advisory_lock.py`, level-0 organization/entity ordering `L0A -> L0B -> L1 -> L2 -> L3`) and multi-entity ascending sort order to prevent deadlocks across concurrent webhook and checkout transactions. Lock primitives and tests merged (`6872f47`); settlement handlers await Final Razorpay Phase. |
| [**ADR-006**](docs/decisions/ADR-006-CHECKOUT-EXCLUSIVITY-AND-RECOVERY.md) | **Organization-Wide Checkout Exclusivity, Recovery Lifecycle, and Reconciliation Policy** | **Accepted (Option A)** | 2026-11-17 | Product Owner authorized Option A (production billing databases are live-only; test billing runs in isolated dev/staging/CI DBs; rationale in [Decision Brief](docs/decisions/ADR-006-TEST-LIVE-SUBSCRIPTION-DECISION-BRIEF.md)). Environment-scoped checkout uniqueness (`f3a4b5c6d7e2`), checkout recovery worker (`razorpay_checkout_recovery_worker.py`), and Option A safeguards merged (`6872f47`); remaining settlement/sweeper scope governed by [Stage 2L Billing Freeze](docs/operations/BILLING_HANDOFF_AND_FREEZE.md). |

---

## Decision Lifecycle & Guidelines

- **Proposed**: Under review; architectural designs undergoing audit and concurrency proofs.
- **Accepted**: Formally approved and backed by repository implementation or mathematical proofs.
- **Superseded**: Replaced by a subsequent ADR.
- **Rejected**: Evaluated and discarded due to identified failure modes or anti-patterns.
