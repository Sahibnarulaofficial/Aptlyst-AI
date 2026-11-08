# Aptlyst AI — System Architecture

## 1. System Overview

Aptlyst AI is a multi-tenant AI chatbot platform designed to allow businesses to create, customize, embed, and analyze intelligent conversational AI agents trained on their proprietary knowledge base.

The system is architected as a decoupled, multi-tier web application:
- **Frontend & Admin Web Application**: Built with Next.js 16 (App Router), React 19, and Tailwind CSS v4. Serves the customer dashboard, chatbot configurator, analytics, lead management, and embeddable widget script.
- **Backend API Service**: Built with FastAPI (Python 3.12+), SQLAlchemy 2.0, and Alembic. Handles business logic, organization scoping, chat orchestration, embeddings/RAG pipeline, and the billing engine.
- **Authentication & User Management**: Powered by Supabase Auth (SSR sessions, JWT tokens, and OAuth providers).
- **Primary Database**: PostgreSQL (version 16 recommended) with transactional Row-Level Security (RLS) conventions, ACID guarantees, and database triggers.
- **Background Worker & Task Queue**: Custom resilient worker infrastructure with lease ownership and dead-letter queue for asynchronous webhook ingestion and event processing.

```
+-----------------------------------------------------------------------------------+
|                                 Client Layer                                      |
|  +--------------------------------+          +---------------------------------+  |
|  |     Next.js Web Dashboard      |          |     Embeddable Chat Widget      |  |
|  |   (React 19, Tailwind CSS v4)  |          |      (External Websites)        |  |
|  +---------------+----------------+          +----------------+----------------+  |
+------------------|--------------------------------------------|-------------------+
                   | HTTPS / REST / SSE                         |
                   v                                            v
+-----------------------------------------------------------------------------------+
|                               Backend Services                                    |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                            FastAPI Application                              |  |
|  |  +----------------------+ +---------------------+ +----------------------+  |  |
|  |  | Chatbots & RAG Engine| |  Tenant & Org Scoping| | Provider-Neutral    |  |  |
|  |  | (Knowledge / Leads)  | |  (Members / Settings)| | Billing Foundation   |  |  |
|  |  +----------------------+ +---------------------+ +----------+-----------+  |  |
|  +--------------------------------------------------------------|--------------+  |
|                                                                 |                 |
|  +--------------------------------------------------------------v--------------+  |
|  |                    Background Webhook Workers & CLI                         |  |
|  |  +------------------------------------+ +--------------------------------+  |  |
|  |  | Razorpay Webhook Ingestion Worker   | | Payment Binding Sweeper        |  |  |
|  |  | (Lease-Based Queue, Exponential BO)| | (Designed - Step 5.2D.3.3B)    |  |  |
|  |  +------------------------------------+ +--------------------------------+  |  |
|  +-----------------------------------------------------------------------------+  |
+-----------------------------------|-----------------------------------------------+
                                    | SQLAlchemy 2.0
                                    v
+-----------------------------------------------------------------------------------+
|                               Persistence Layer                                   |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  |                        PostgreSQL 16 Database                               |  |
|  |  - Organizations & Billing Periods (Entitlements)                           |  |
|  |  - Chatbots, Knowledge Bases, Embeddings, Conversations, Leads              |  |
|  |  - Billing Ledgers: Customers, Checkout Attempts, Invoices, Payments        |  |
|  |  - Ingestion Queue: Billing Webhook Events with Lease Ownership             |  |
|  |  - Immutability Triggers & Monotonic Lock Primitives                        |  |
|  +-----------------------------------------------------------------------------+  |
+-----------------------------------------------------------------------------------+
```

---

## 2. Key Subsystems

### 2.1 Multi-Tenancy Model
All data access in Aptlyst AI is strictly partitioned by **Organization (`organization_id`)**. 
- Every user belongs to one or more organizations via membership roles (`owner`, `admin`, `member`).
- Backend endpoints automatically validate organization membership using JWT claims extracted from the Supabase session.
- Database queries scope all chatbot, knowledge base, conversation, and billing rows to the active `organization_id`.

### 2.2 Conversational AI & RAG Engine
- Users upload documents (PDF, Markdown, text, URLs) to a chatbot's knowledge base.
- Text is chunked, embedded, and indexed for similarity search.
- When an end-user chats via the embedded widget, the system retrieves relevant context chunks, constructs a prompt with strict guardrails, and streams the completion back to the client via Server-Sent Events (SSE).

### 2.3 Provider-Neutral Billing Engine
Aptlyst AI separates payment gateway mechanics from organizational entitlements:
- Historical billing functionality used **Stripe**.
- The primary gateway for active and future subscriptions is **Razorpay**.
- The billing domain is unified through provider-neutral database models (`BillingCustomer`, `BillingCheckoutAttempt`, `BillingInvoice`, `BillingPayment`, `OrganizationBillingPeriod`).
- Entitlements (token quotas and plan tiers) are decoupled from gateway subscription objects, ensuring customer access remains intact through cancellation until the purchased billing period expires.

---

## 3. Technology Stack Reference

| Layer | Component | Technology / Library | Version | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Frontend** | Framework | Next.js (App Router) | 16.3.7 | Server-side rendering, routing, static optimization |
| | UI Library | React | 19.2.8 | Declarative component UI |
| | Styling | Tailwind CSS | 4.x | Utility-first styling with `@tailwindcss/postcss` |
| | Auth Client | `@supabase/ssr` / `@supabase/supabase-js` | 0.5.2 / 2.118.0 | Session tokens, cookie management, OAuth |
| | Icons | Lucide React | 1.48.0 | UI iconography |
| **Backend** | Framework | FastAPI | 0.115+ | High-performance async REST API |
| | Language | Python | 3.12+ | Core backend runtime |
| | ORM | SQLAlchemy | 2.0+ | Typed object-relational mapping |
| | Migrations | Alembic | 1.13+ | Database schema version control |
| | Validation | Pydantic v2 | 2.9+ | Request/response schema validation |
| | DB Driver | psycopg2-binary / asyncpg | 2.9+ | PostgreSQL wire protocol drivers |
| **Infra / DB** | Database | PostgreSQL | 16.x | Primary relational store, constraints, triggers |
| | Gateway | Razorpay / Stripe | Subscriptions API | Payment processing & automated recurring e-mandates |
| | CI / CD | GitHub Actions | Ubuntu + Postgres 16 | Continuous integration and migration validation |

---

## 4. Current Implementation Status

To maintain documentation integrity, the platform subsystems are categorized below according to their real repository implementation state:

- **Frontend Core & Chatbot Management**: *Implemented and verified.*
- **FastAPI Foundation & Organization Scoping**: *Implemented and verified (634 passing API tests).*
- **Historical Stripe Billing**: *Implemented and verified historically; maintained for legacy audit.*
- **Provider-Neutral Schema & Migrations**: *Implemented in code (`e2f3a4b5c6d1`), but PostgreSQL CI execution remains unverified pending container run.*
- **Razorpay Webhook Ingestion & Worker**: *Implemented and locally unit-tested; pending live sandbox validation.*
- **Razorpay Settlement Handlers & Payment Binding Sweeper**: *Designed in detail (Steps 5.2D.3.2.1-14); implementation NOT yet started (Step 5.2D.3.3B).*
