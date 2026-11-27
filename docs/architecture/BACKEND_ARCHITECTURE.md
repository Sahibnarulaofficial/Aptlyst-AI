# Aptlyst AI — Backend Architecture

## 1. Overview

The backend service is located in `apps/api/`. It is a high-performance REST API built with **FastAPI**, **SQLAlchemy 2.0 (async/declarative)**, and **Pydantic v2**, backed by a **PostgreSQL** relational database.

It provides tenant management, AI chatbot orchestration, RAG embeddings and retrieval, conversation tracking, and provider-neutral subscription billing.

---

## 2. Directory Layout & Module Responsibilities

```text
apps/api/
├── alembic/                       # Database migrations
│   ├── versions/                  # Migration scripts (linear Alembic chain)
│   └── env.py                     # Alembic migration environment
├── app/
│   ├── main.py                    # FastAPI application initialization & middleware
│   ├── config.py                  # Pydantic Settings configuration & env validation
│   ├── api/                       # API route definitions and controllers
│   │   ├── billing.py             # Billing endpoints (checkout, portal, webhooks)
│   │   └── ...                    # Chatbot, conversation, and organization routes
│   ├── auth/                      # Authentication & Supabase token verification
│   ├── db/                        # Database session lifecycle & connection pool
│   ├── models/                    # SQLAlchemy 2.0 declarative database models
│   │   ├── organization.py        # Organizations, members, billing periods
│   │   ├── chatbot.py             # Chatbots & knowledge base configuration
│   │   ├── conversation.py        # Chat sessions, messages, and captured leads
│   │   ├── generation.py          # AI prompt generation records
│   │   ├── generation_attempt.py  # Generation retry & token accounting
│   │   ├── billing.py             # Provider-neutral billing ledger models
│   │   ├── payment.py             # BillingPayment model & composite FK constraints
│   │   └── subscription.py        # Gateway subscriptions & legacy Stripe tables
│   ├── schemas/                   # Pydantic v2 request/response schemas
│   ├── security/                  # Cryptographic utilities & signature checks
│   ├── services/                  # Business logic services
│   │   └── billing/               # Stripe legacy handlers & Razorpay services
│   └── workers/                   # Background workers, health & operator CLIs
│       ├── razorpay_webhook_worker.py
│       ├── razorpay_checkout_recovery_worker.py
│       ├── billing_health.py
│       └── billing_reconciliation.py
├── tests/                         # Pytest suite (1,322 CI tests passing on Run 46048131091)
├── requirements.txt               # Production dependencies
└── requirements-dev.txt           # Testing and development tooling
```

---

## 3. Data Models & Entity Relationships

### 3.1 Organization & Multi-Tenancy
The top-level tenant is `Organization`. All entities link directly or transitively to an `organization_id`:
- `OrganizationMember`: Maps Supabase user IDs to organizations with assigned roles (`owner`, `admin`, `member`).
- `OrganizationBillingPeriod`: The authoritative source of customer entitlement (tier, token limits, start/end timestamps).

### 3.2 Chatbot & Knowledge Base
- `Chatbot`: Defines bot settings (system instructions, temperature, model name, active theme).
- `Conversation`: Captures user chat sessions, linking messages and leads to the parent bot and tenant.
- `Message`: Individual exchanges (user query, assistant response, token consumption metrics).
- `Lead`: Contact details captured by conversational forms during user interactions.

### 3.3 Provider-Neutral Billing Models
- `BillingCustomer`: Decouples organization identity from third-party gateway customer identifiers (`stripe_customer_id`, `razorpay_customer_id`).
- `BillingCheckoutAttempt`: Ephemeral checkout session state (`creating`, `open`, `completed`, `expired`).
- `BillingInvoice`: Financial accounting record representing issued invoices across all payment gateways.
- `BillingPayment`: Transactional payment records linked via a 6-column composite foreign key to the originating checkout attempt.
- `BillingWebhookEvent`: Asynchronous webhook queue with lease ownership, payload storage, and retry counters.

---

## 4. Database Concurrency & Locking Architecture

To guarantee absolute consistency across multi-threaded operations and asynchronous webhook arrivals, the backend architecture defines two concurrency tiers:

### 4.1 Implemented Concurrency Controls (Verified Through Stage 2K / `6872f47`)
1. **Unified Advisory Locking (`apps/api/app/services/billing/advisory_lock.py`)**:
   - Canonical provider-neutral advisory lock module with backward-compatible facade in `apps/api/app/services/billing/razorpay/lock.py`.
   - Enforces ascending signed 64-bit integer ordering for multi-entity locks (`acquire_multi_entity_advisory_locks`) and transitional dual-locking in `apps/api/app/api/organizations.py`.
2. **Worker Lease & Heartbeat Fencing (`f3a4b5c6d7e2`, `a4b5c6d7e8f3`)**:
   - `SELECT ... FOR UPDATE SKIP LOCKED` claiming, UUID `lease_token` / `recovery_claim_id` fencing, and `instance_token` ownership-loss guards in `razorpay_webhook_worker.py` and `razorpay_checkout_recovery_worker.py`.
3. **Database-Level Immutability Triggers (`e2f3a4b5c6d1`)**:
   - PostgreSQL procedural triggers prevent post-settlement reassignment of `subscription_id`, `checkout_attempt_id`, or `provider_payment_id` (CI-verified on PostgreSQL 16).
4. **Network Call Isolation**:
   - External Razorpay HTTP calls execute strictly outside database transactions and locks.

### 4.2 Deferred Settlement Concurrency & Reconciliation (Final Razorpay Phase)
- Monotonic 5-level organization-to-auxiliary settlement locking (`L0A(org) -> L0B(entity) -> L1(period) -> L2(sub) -> L3(auxiliary)`) inside upcoming `subscription.charged` / `payment.captured` / `payment.failed` settlement handlers and the `BLK-04` payment-binding sweeper remains deferred under the [Stage 2L Billing Freeze](../operations/BILLING_HANDOFF_AND_FREEZE.md).

---

## 5. Webhook Ingestion & Worker Infrastructure

Webhook handling is split into two asynchronous stages:

```
[Gateway Webhook]
       │
       ▼ (1) Fast HTTP POST
[FastAPI Ingestion Endpoint] ──(Validates HMAC signature)──▶ [Store raw in `billing_webhook_events`]
                                                            (Status: 'pending', lease: NULL)
       │
       ▼ (2) Background Polling / Claiming
[Webhook Worker Runner] ─────(Acquires lease lock)────────▶ [Dispatches to Event Handler]
                                                            (Updates event status, retries on error)
```

- **Immediate Ingestion**: The API verifies the raw cryptographic HMAC signature in constant time and immediately commits the event to `billing_webhook_events` with status `'pending'`, returning `HTTP 200 OK` in under 50ms.
- **Worker Execution**: `razorpay_webhook_worker.py` runs as an independent daemon or CLI process. It claims pending events using transactional lease locks (`lease_token`, `lease_expires_at`), dispatches to the registered handler, and implements exponential backoff on retries.
