# Aptlyst AI — Phase 5.2D Historical Billing Archive

This directory serves as the permanent, immutable audit trail for the architectural decisions, concurrency audits, and implementation milestones of **Phase 5.2D: Provider-Neutral Billing Foundation and Razorpay Integration**.

> [!IMPORTANT]
> **Preservation Notice**: Documents in this archive are historical records. They must not be modified, overwritten, or deleted. Canonical, living documentation derived from these findings is maintained in [`docs/architecture/`](../architecture/), [`docs/decisions/`](../decisions/), and [`docs/development/`](../development/).

---

## 1. Archive Structure & Chronological Progression

Phase 5.2D progressed through four major technical phases, organized here into dedicated directories:

```text
docs/archive/
├── phase_5_2d_foundation/          # Steps 5.2D.0.2 to 5.2D.2.3: Webhook ingestion, worker architecture, and CLI tooling
├── phase_5_2d_lifecycle_design/    # Steps 5.2D.3A to 5.2D.3.1.1: Subscription state machines and shared handler design
├── phase_5_2d_agreement_handlers/  # Steps 5.2D.3.2 to 5.2D.3.2.14: Concurrency proofs, Option A decoupling, and advisory locking
└── phase_5_2d_schema_migration/    # Steps 5.2D.3.3A to 5.2D.3.3A.2: Composite FK constraints, provenance triggers, and CI setup
```

---

## 2. Master Archive Catalog (29 Reports)

The following table indexes all 29 historical reports, mapping their original filenames to their archived locations along with their milestone focus:

### 2.1 Phase 5.2D Foundation (`phase_5_2d_foundation/`)

| Original Filename | Archived Path | Focus / Scope | Historical Outcome |
| :--- | :--- | :--- | :--- |
| `STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md` | [`phase_5_2d_foundation/STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md`](phase_5_2d_foundation/STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md) | Webhook payload ingestion, signature validation, idempotency | Verified payload integrity and raw signature verification models. |
| `STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md` | [`phase_5_2d_foundation/STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md`](phase_5_2d_foundation/STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md) | Invariant alignment between Stripe legacy and Razorpay models | Formalized provider abstraction boundaries and event lease ownership. |
| `STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md` | [`phase_5_2d_foundation/STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md`](phase_5_2d_foundation/STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md) | Implementation of `/api/v1/billing/razorpay/webhook` endpoint | Delivered constant-time HMAC validation and raw payload persistence. |
| `STEP_5_2D_2_WEBHOOK_WORKER_IMPLEMENTATION_REPORT.md` | [`phase_5_2d_foundation/STEP_5_2D_2_WEBHOOK_WORKER_IMPLEMENTATION_REPORT.md`](phase_5_2d_foundation/STEP_5_2D_2_WEBHOOK_WORKER_IMPLEMENTATION_REPORT.md) | Background webhook worker implementation | Delivered lease ownership, exponential backoff, and dead-letter queue. |
| `STEP_5_2D_2_1_WEBHOOK_WORKER_FINAL_AUDIT.md` | [`phase_5_2d_foundation/STEP_5_2D_2_1_WEBHOOK_WORKER_FINAL_AUDIT.md`](phase_5_2d_foundation/STEP_5_2D_2_1_WEBHOOK_WORKER_FINAL_AUDIT.md) | Concurrency audit of worker claiming mechanism | Identified lease renewal race conditions; resolved in 5.2D.2.2. |
| `STEP_5_2D_2_2_WEBHOOK_WORKER_CLI_REPORT.md` | [`phase_5_2d_foundation/STEP_5_2D_2_2_WEBHOOK_WORKER_CLI_REPORT.md`](phase_5_2d_foundation/STEP_5_2D_2_2_WEBHOOK_WORKER_CLI_REPORT.md) | CLI interface for worker execution (`run_razorpay_webhook_worker`) | Delivered command-line runner, graceful shutdown, and batch processing. |
| `STEP_5_2D_2_3_WORKER_LIFECYCLE_FINAL_AUDIT.md` | [`phase_5_2d_foundation/STEP_5_2D_2_3_WORKER_LIFECYCLE_FINAL_AUDIT.md`](phase_5_2d_foundation/STEP_5_2D_2_3_WORKER_LIFECYCLE_FINAL_AUDIT.md) | Final lifecycle and poison-pill audit of worker | Validated crash-safety and lease expiration recovery. |

### 2.2 Phase 5.2D Lifecycle Design (`phase_5_2d_lifecycle_design/`)

| Original Filename | Archived Path | Focus / Scope | Historical Outcome |
| :--- | :--- | :--- | :--- |
| `STEP_5_2D_3A_SUBSCRIPTION_LIFECYCLE_DESIGN.md` | [`phase_5_2d_lifecycle_design/STEP_5_2D_3A_SUBSCRIPTION_LIFECYCLE_DESIGN.md`](phase_5_2d_lifecycle_design/STEP_5_2D_3A_SUBSCRIPTION_LIFECYCLE_DESIGN.md) | Comprehensive state machine for Razorpay subscriptions | Defined valid states: `created`, `authenticated`, `active`, `pending`, `halted`, `canceled`, `completed`, `expired`. |
| `STEP_5_2D_3A_1_DESIGN_CORRECTION_REPORT.md` | [`phase_5_2d_lifecycle_design/STEP_5_2D_3A_1_DESIGN_CORRECTION_REPORT.md`](phase_5_2d_lifecycle_design/STEP_5_2D_3A_1_DESIGN_CORRECTION_REPORT.md) | Correction of resurrection flaws in lifecycle transitions | Established the fundamental *Refusal of Resurrection* invariant. |
| `STEP_5_2D_3_1_SHARED_HANDLER_FOUNDATION_REPORT.md` | [`phase_5_2d_lifecycle_design/STEP_5_2D_3_1_SHARED_HANDLER_FOUNDATION_REPORT.md`](phase_5_2d_lifecycle_design/STEP_5_2D_3_1_SHARED_HANDLER_FOUNDATION_REPORT.md) | Shared settlement handler abstraction and base dispatchers | Implemented provider-agnostic handler foundations and signature contracts. |
| `STEP_5_2D_3_1_1_SHARED_HANDLER_FINAL_AUDIT.md` | [`phase_5_2d_lifecycle_design/STEP_5_2D_3_1_1_SHARED_HANDLER_FINAL_AUDIT.md`](phase_5_2d_lifecycle_design/STEP_5_2D_3_1_1_SHARED_HANDLER_FINAL_AUDIT.md) | Audit of shared handler interfaces and error boundaries | Verified exception isolation and deterministic retry codes. |

### 2.3 Phase 5.2D Agreement Handlers & Concurrency (`phase_5_2d_agreement_handlers/`)

| Original Filename | Archived Path | Focus / Scope | Historical Outcome |
| :--- | :--- | :--- | :--- |
| `STEP_5_2D_3_2_AGREEMENT_HANDLERS_REPORT.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_AGREEMENT_HANDLERS_REPORT.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_AGREEMENT_HANDLERS_REPORT.md) | Implementation of agreement handlers (`subscription.authenticated`, etc.) | Initial handler drafting; flagged for concurrency review. |
| `STEP_5_2D_3_2_1_AGREEMENT_HANDLERS_FINAL_AUDIT.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_1_AGREEMENT_HANDLERS_FINAL_AUDIT.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_1_AGREEMENT_HANDLERS_FINAL_AUDIT.md) | Audit of agreement handler edge cases | Highlighted race conditions in parallel webhook and checkout verification. |
| `STEP_5_2D_3_2_2_AGREEMENT_HANDLER_CORRECTIONS_REPORT.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_2_AGREEMENT_HANDLER_CORRECTIONS_REPORT.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_2_AGREEMENT_HANDLER_CORRECTIONS_REPORT.md) | Technical corrections to subscription status updates | Hardened status transition guards against stale event replays. |
| `STEP_5_2D_3_2_3_OPTION_A_DESIGN_VALIDATION.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_3_OPTION_A_DESIGN_VALIDATION.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_3_OPTION_A_DESIGN_VALIDATION.md) | Architectural comparison of subscription coupling models | Validated Option A: strictly decoupling entitlements from agreements. |
| `STEP_5_2D_3_2_4_OPTION_A_FINAL_CONCURRENCY_DESIGN.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_4_OPTION_A_FINAL_CONCURRENCY_DESIGN.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_4_OPTION_A_FINAL_CONCURRENCY_DESIGN.md) | Concurrency model for decoupled entitlement periods | Designed transaction isolation and row locking for period provisioning. |
| `STEP_5_2D_3_2_5_SETTLEMENT_CONCURRENCY_FINAL_AUDIT.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_5_SETTLEMENT_CONCURRENCY_FINAL_AUDIT.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_5_SETTLEMENT_CONCURRENCY_FINAL_AUDIT.md) | Audit of concurrent settlement vs. user checkout | Proved need for global organization-level serialization. |
| `STEP_5_2D_3_2_6_TERMINAL_AGREEMENT_DELAYED_PAYMENT_DESIGN.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_6_TERMINAL_AGREEMENT_DELAYED_PAYMENT_DESIGN.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_6_TERMINAL_AGREEMENT_DELAYED_PAYMENT_DESIGN.md) | Handling delayed payments for canceled/completed subscriptions | Proved delayed payments settle into invoices without resurrecting agreements. |
| `STEP_5_2D_3_2_7_ENTITLEMENT_ASSOCIATION_RESUBSCRIPTION_DESIGN.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_7_ENTITLEMENT_ASSOCIATION_RESUBSCRIPTION_DESIGN.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_7_ENTITLEMENT_ASSOCIATION_RESUBSCRIPTION_DESIGN.md) | Resubscription entitlement chaining | Designed queued entitlement periods (`P2`) starting after active period (`P1.period_end`). |
| `STEP_5_2D_3_2_8_DESIGN_CORRECTIONS.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_8_DESIGN_CORRECTIONS.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_8_DESIGN_CORRECTIONS.md) | Corrections to invoice interval matching and payment linkage | Standardized `billing_invoices` period linkage and billing cycle boundaries. |
| `STEP_5_2D_3_2_9_FINAL_BILLING_POLICY_REVIEW.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_9_FINAL_BILLING_POLICY_REVIEW.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_9_FINAL_BILLING_POLICY_REVIEW.md) | Review of period reset vs. conservative retention | Discovered PostgreSQL window function defect in migration SQL; rejected immediate resets. |
| `STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md) | Fix for PostgreSQL distinct window aggregate; Level 0 lock | Formulated Level 0 advisory lock `pg_advisory_xact_lock` for zero-period void. |
| `STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_11_FINAL_RACE_AND_MIGRATION_AUDIT.md) | Three-layer race defense audit | Formulated advisory locking, checkout attempt scoping, and period chain verification. |
| `STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_12_PAYMENT_ATTEMPT_LIFECYCLE_AUDIT.md) | Monotonic 5-level lock hierarchy; composite foreign keys | Proved monotonic locking order and composite FK with null-bypass check constraint. |
| `STEP_5_2D_3_2_13_FINAL_PAYMENT_ASSOCIATION_VERIFICATION.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_13_FINAL_PAYMENT_ASSOCIATION_VERIFICATION.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_13_FINAL_PAYMENT_ASSOCIATION_VERIFICATION.md) | Verification of webhook-before-checkout edge case | Verified payment staging state `pending_attempt_binding` and deterministic retry budget. |
| `STEP_5_2D_3_2_14_PAYMENT_ASSOCIATION_HARDENING.md` | [`phase_5_2d_agreement_handlers/STEP_5_2D_3_2_14_PAYMENT_ASSOCIATION_HARDENING.md`](phase_5_2d_agreement_handlers/STEP_5_2D_3_2_14_PAYMENT_ASSOCIATION_HARDENING.md) | Hardening recovery; *Refusal to Rewrite History* | Established that late captures do not alter `expired` checkout attempt status. |

### 2.4 Phase 5.2D Schema Migration (`phase_5_2d_schema_migration/`)

| Original Filename | Archived Path | Focus / Scope | Historical Outcome |
| :--- | :--- | :--- | :--- |
| `STEP_5_2D_3_3A_SCHEMA_MIGRATION_REPORT.md` | [`phase_5_2d_schema_migration/STEP_5_2D_3_3A_SCHEMA_MIGRATION_REPORT.md`](phase_5_2d_schema_migration/STEP_5_2D_3_3A_SCHEMA_MIGRATION_REPORT.md) | Implementation of migration `e2f3a4b5c6d1` | Authored migration DDL for payment association, triggers, and composite keys. |
| `STEP_5_2D_3_3A_1_PROVENANCE_VALIDATION_REPORT.md` | [`phase_5_2d_schema_migration/STEP_5_2D_3_3A_1_PROVENANCE_VALIDATION_REPORT.md`](phase_5_2d_schema_migration/STEP_5_2D_3_3A_1_PROVENANCE_VALIDATION_REPORT.md) | Unified candidate provenance conflict detection | Replaced disjoint Stripe/Neutral backfills with unified cross-provider validation gate. |
| `STEP_5_2D_3_3A_2_POSTGRESQL_CI_VALIDATION_REPORT.md` | [`phase_5_2d_schema_migration/STEP_5_2D_3_3A_2_POSTGRESQL_CI_VALIDATION_REPORT.md`](phase_5_2d_schema_migration/STEP_5_2D_3_3A_2_POSTGRESQL_CI_VALIDATION_REPORT.md) | Isolated PostgreSQL service container in GitHub Actions | Authored `.github/workflows/ci.yml` to execute 14 integration tests against live PostgreSQL. |

---

## 3. Relationship to Canonical Living Documentation

Key architectural decisions from these reports have been synthesized into canonical living documents:

- **General System Architecture**: [`docs/architecture/SYSTEM_ARCHITECTURE.md`](../architecture/SYSTEM_ARCHITECTURE.md)
- **Billing Engine Architecture**: [`docs/architecture/BILLING_ARCHITECTURE.md`](../architecture/BILLING_ARCHITECTURE.md)
- **Testing & PostgreSQL CI Guide**: [`docs/development/TESTING_GUIDE.md`](../development/TESTING_GUIDE.md)
- **Architecture Decision Records**: [`docs/decisions/`](../decisions/)
- **Live Billing Implementation Roadmap**: [`BILLING_ROADMAP.md`](../../BILLING_ROADMAP.md)
