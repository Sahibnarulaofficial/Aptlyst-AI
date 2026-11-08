# Aptlyst AI — Documentation Portal

Welcome to the central documentation portal for **Aptlyst AI**. This directory organizes the architecture, developer workflows, formal technical decisions, and historical records of the project.

---

## 1. Directory Overview

```text
docs/
├── architecture/                  # Canonical Living Architecture
│   ├── SYSTEM_ARCHITECTURE.md     # High-level platform design, client/server tiers
│   ├── BACKEND_ARCHITECTURE.md    # FastAPI, models, concurrency, worker architecture
│   ├── FRONTEND_ARCHITECTURE.md   # Next.js 16 App Router, React 19, embed widget
│   └── BILLING_ARCHITECTURE.md    # Provider-neutral billing, ledgers, invariants
│
├── development/                   # Developer Guides & Workflows
│   ├── DEVELOPMENT_SETUP.md       # Onboarding, environment vars, running locally
│   ├── TESTING_GUIDE.md           # Pytest, PostgreSQL CI, frontend tests
│   └── DEVELOPMENT_ROADMAP.md     # Engineering phases and milestone progression
│
├── decisions/                     # Architecture Decision Records (ADRs)
│   ├── ADR-001-BILLING-PROVIDER-MIGRATION.md
│   ├── ADR-002-FULL-PAYMENT-SETTLEMENT.md
│   ├── ADR-003-SUBSCRIPTION-ENTITLEMENT-SEPARATION.md
│   ├── ADR-004-PAYMENT-PROVENANCE.md
│   └── ADR-005-BILLING-LOCK-ORDERING.md
│
├── audits/                        # Formal Audit Deliverables
│   └── DOCUMENTATION_AUDIT_REPORT.md # Inventory and restructuring audit report
│
└── archive/                       # Historical Phase Reports (Immutable Audit Trail)
    ├── README.md                  # Master archive index (29 historical reports)
    ├── phase_5_2d_foundation/     # Steps 5.2D.0.2 to 5.2D.2.3
    ├── phase_5_2d_lifecycle_design/ # Steps 5.2D.3A to 5.2D.3.1.1
    ├── phase_5_2d_agreement_handlers/ # Steps 5.2D.3.2 to 5.2D.3.2.14
    └── phase_5_2d_schema_migration/ # Steps 5.2D.3.3A to 5.2D.3.3A.2
```

---

## 2. Quick Navigation by Role

### For Developers Starting Out
1. Follow [`docs/development/DEVELOPMENT_SETUP.md`](development/DEVELOPMENT_SETUP.md) to configure your local environment.
2. Read [`docs/architecture/SYSTEM_ARCHITECTURE.md`](architecture/SYSTEM_ARCHITECTURE.md) for a complete system walkthrough.
3. Review [`docs/development/TESTING_GUIDE.md`](development/TESTING_GUIDE.md) to run and write unit and integration tests.

### For Billing & Database Engineers
1. Read [`docs/architecture/BILLING_ARCHITECTURE.md`](architecture/BILLING_ARCHITECTURE.md) to understand provider-neutral modeling.
2. Review the formal [Architecture Decision Records](decisions/) covering:
   - [ADR-001: Provider Migration](decisions/ADR-001-BILLING-PROVIDER-MIGRATION.md)
   - [ADR-002: Full Payment Settlement](decisions/ADR-002-FULL-PAYMENT-SETTLEMENT.md)
   - [ADR-003: Option A Decoupling](decisions/ADR-003-SUBSCRIPTION-ENTITLEMENT-SEPARATION.md)
   - [ADR-004: Payment Provenance & Triggers](decisions/ADR-004-PAYMENT-PROVENANCE.md)
   - [ADR-005: Billing Lock Ordering](decisions/ADR-005-BILLING-LOCK-ORDERING.md)
3. Track the live billing implementation sequence in [`BILLING_ROADMAP.md`](../BILLING_ROADMAP.md).

### For Codebase Auditors
- Review the historical audit trail of 29 step reports in [`docs/archive/README.md`](archive/README.md).
- Inspect [`docs/audits/DOCUMENTATION_AUDIT_REPORT.md`](audits/DOCUMENTATION_AUDIT_REPORT.md) for the documentation restructuring audit.
