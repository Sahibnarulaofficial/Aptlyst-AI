# Aptlyst AI — Technical Decisions Index

This document serves as the central index of **Architecture Decision Records (ADRs)** for the Aptlyst AI platform.

Each record documents a significant architectural decision, including its context, alternatives considered, decision rationale, and engineering consequences.

---

## Architecture Decision Records (ADRs)

| ADR ID | Title | Status | Date | Summary |
| :--- | :--- | :---: | :---: | :--- |
| [**ADR-001**](docs/decisions/ADR-001-BILLING-PROVIDER-MIGRATION.md) | **Billing Provider Migration and Provider-Neutral Foundation** | **Accepted** | 2026-11-06 | Architectural transition from Stripe to Razorpay Subscriptions (INR/RBI e-mandate support) via provider-neutral models (`BillingCustomer`, `BillingCheckoutAttempt`, `BillingInvoice`, `BillingPayment`, `BillingWebhookEvent`). Foundation and webhook worker implemented; settlement handlers pending Step 5.2D.3.3B. |
| [**ADR-002**](docs/decisions/ADR-002-FULL-PAYMENT-SETTLEMENT.md) | **Full Payment Settlement Policy and Immutability Invariants** | **Accepted** | 2026-11-08 | Enforces the *Conservative Core Billing Policy* (excising immediate resets; unexpired paid quotas run to `period_end`; resubscriptions queue future periods). Enforces *Refusal of Resurrection* (terminal states are permanent) and *Refusal to Rewrite History* (late captures do not alter `expired` checkout attempt status). Terminal guards implemented; period rollover pending Step 5.2D.3.3B. |
| [**ADR-003**](docs/decisions/ADR-003-SUBSCRIPTION-ENTITLEMENT-SEPARATION.md) | **Subscription and Entitlement Separation (Option A Decoupling)** | **Accepted** | 2026-11-08 | Strictly decouples recurring payment contracts (`Subscription`) from tenant entitlements (`OrganizationBillingPeriod`). Canceling an auto-renewal stops future charges but leaves active paid quota intact until `period_end`. Schema separation implemented; provisioning handlers pending Step 5.2D.3.3B. |
| [**ADR-004**](docs/decisions/ADR-004-PAYMENT-PROVENANCE.md) | **Payment Association Provenance, Composite Foreign Keys, and Conflict Gates** | **Accepted** | 2026-11-08 | Closes the PostgreSQL composite foreign key `MATCH SIMPLE` null-bypass vulnerability using `ck_billing_payments_checkout_attempt_complete`. Enforces PostgreSQL immutability triggers on provenance columns and unified cross-provider candidate conflict detection during migration `e2f3a4b5c6d1`. Implemented in code; PostgreSQL CI execution pending Step 5.2D.3.3A.2. |
| [**ADR-005**](docs/decisions/ADR-005-BILLING-LOCK-ORDERING.md) | **Billing Concurrency Control and Monotonic Lock Ordering** | **Accepted** | 2026-11-08 | Architectural design closing the zero-period concurrency void using Level 0 Organization Advisory Locking (`pg_advisory_xact_lock`) and monotonic 5-level lock acquisition order across transactions to prevent deadlocks. Entity-level advisory locks implemented; organization-level lock hierarchy pending Step 5.2D.3.3B. |

---

## Decision Lifecycle & Guidelines

- **Proposed**: Under review; architectural designs undergoing audit and concurrency proofs.
- **Accepted**: Formally approved and backed by repository implementation or mathematical proofs.
- **Superseded**: Replaced by a subsequent ADR.
- **Rejected**: Evaluated and discarded due to identified failure modes or anti-patterns.
