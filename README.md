# Aptlyst AI

**Aptlyst AI** is a production-grade, multi-tenant conversational AI platform. It empowers businesses to create, customize, embed, and analyze intelligent conversational AI agents trained on proprietary knowledge bases, complete with lead capture, multi-tenant organization management, and a provider-neutral subscription billing engine.

---

## 1. Product Capabilities & Implementation Boundary

- **Implemented Core Foundation**:
  - **Custom AI Chatbots**: Authenticated chatbot CRUD, system prompt configuration, temperature settings, and dashboard shell.
  - **Visitor Conversation API & Widget Preview**: Unauthenticated `/embed/[id]` branding/welcome preview and `/api/v1/public/chatbots/*` conversation/message APIs with signed visitor sessions, rate limiting, token reservation, and Phase 1 `LLM_PROVIDER` (`disabled` or `mock`).
  - **Multi-Tenant Organizations**: Team membership roles (`owner`, `admin`, `member`), organization switching, and tenant isolation backed by Supabase Auth SSR.
  - **Billing Infrastructure Foundation (Frozen in Stage 2L)**: Provider-neutral billing models, webhook queue & recovery workers, advisory locking, and health tooling merged through `6872f47` (kept disabled via `RAZORPAY_ENABLED=false` during chatbot product development).
- **Planned / Next Chatbot Product Work**:
  - **RAG Knowledge Base (`/knowledge`)**, **Live LLM Providers & SSE Widget Streaming (`/embed/[id]`)**, and **Conversations / Leads / Analytics / Integrations / Settings UIs** (currently placeholder routes).
  - **Final Razorpay Settlement & Billing UI Cutover**: `subscription.charged`/`payment.captured`/`payment.failed`, Pro quota rollover, `BLK-04` sweeper, and frontend cutover from legacy Stripe routes remain deferred until after chatbot product development.

---

## 2. Technology Stack

- **Frontend**: [Next.js 16](https://nextjs.org) (App Router), [React 19](https://react.dev), [Tailwind CSS v4](https://tailwindcss.com), [Lucide React](https://lucide.dev)
- **Backend API**: [FastAPI](https://fastapi.tiangolo.com) (Python 3.12+), [SQLAlchemy 2.0](https://www.sqlalchemy.org), [Pydantic v2](https://docs.pydantic.dev), [Alembic](https://alembic.sqlalchemy.org)
- **Authentication**: [Supabase Auth SSR](https://supabase.com/docs/guides/auth/server-side) (JWT tokens, session cookies)
- **Database**: [PostgreSQL 16](https://www.postgresql.org) (Advisory locks, procedural triggers, composite foreign keys)
- **Payment Providers**: [Razorpay Subscriptions](https://razorpay.com/docs/payments/subscriptions/) (Active) & [Stripe](https://stripe.com) (Historical)
- **Continuous Integration**: GitHub Actions with isolated PostgreSQL service container

---

## 3. High-Level Architecture

```text
[Browser / Client] ─────────▶ [Next.js Web Dashboard / Embed Widget]
                                    │
                                    │ HTTPS / REST / SSE (Supabase JWT)
                                    ▼
                             [FastAPI Backend]
                             ├── Auth & Org Context Scoping
                             ├── Chatbot Orchestration & RAG Pipeline
                             ├── Provider-Neutral Billing Layer
                             └── Webhook Ingestion & Worker Queue
                                    │
                                    │ SQLAlchemy 2.0
                                    ▼
                         [PostgreSQL 16 Database]
                         ├── Multi-Tenant Organizations
                         ├── Chatbots, Knowledge Bases, Messages
                         └── Invoices, Payments, Billing Periods
```

For detailed architecture specifications, consult:
- [`docs/architecture/SYSTEM_ARCHITECTURE.md`](docs/architecture/SYSTEM_ARCHITECTURE.md)
- [`docs/architecture/BACKEND_ARCHITECTURE.md`](docs/architecture/BACKEND_ARCHITECTURE.md)
- [`docs/architecture/FRONTEND_ARCHITECTURE.md`](docs/architecture/FRONTEND_ARCHITECTURE.md)
- [`docs/architecture/BILLING_ARCHITECTURE.md`](docs/architecture/BILLING_ARCHITECTURE.md)

---

## 4. Repository Structure

```text
Aptlyst-AI/
├── README.md                      # This project overview
├── AGENTS.md                      # Repository engineering contract
├── GEMINI.md                      # Agent execution rules
├── PROJECT_STATUS.md              # Live status dashboard & active gates
├── TECHNICAL_DECISIONS.md         # Index of formal Architecture Decision Records
├── BILLING_ROADMAP.md             # Authoritative billing implementation tracker
│
├── app/                           # Next.js App Router (Dashboard & Widget)
├── components/                    # React UI components & dialogs
├── lib/                           # Supabase SSR client & frontend context
├── package.json                   # Node.js dependencies & scripts
│
├── apps/
│   └── api/                       # FastAPI Backend Service
│       ├── app/                   # Backend application source code
│       │   ├── api/               # API endpoint routers
│       │   ├── models/            # SQLAlchemy declarative models
│       │   ├── services/          # Core business services
│       │   └── workers/           # Webhook, recovery, health & reconciliation workers
│       ├── alembic/               # Database migration scripts (head: a4b5c6d7e8f3)
│       └── tests/                 # Pytest unit & PostgreSQL 16 integration suites
│
└── docs/                          # Comprehensive Documentation
    ├── architecture/              # Canonical living architecture specs
    ├── development/               # Setup, testing, and workflow guides
    ├── decisions/                 # Formal Architecture Decision Records (ADR-001 – ADR-006)
    ├── operations/                # Stage 2L Billing Handoff/Freeze & Render Deployment Guide
    ├── audits/                    # Documentation audit deliverables
    ├── reports/                   # Stage implementation & CI validation reports
    └── archive/                   # Immutable audit trail of historical reports
```

---

## 5. Quick Start

### 5.1 Backend Setup (`apps/api`)
```bash
# 1. Navigate to backend directory
cd apps/api

# 2. Create and activate virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your PostgreSQL and Supabase credentials

# 5. Run database migrations
alembic upgrade head

# 6. Start the FastAPI development server
uvicorn app.main:app --reload --port 8000
```
Swagger UI available at `http://localhost:8000/docs`.

### 5.2 Frontend Setup (Repository Root)
```bash
# 1. In repository root, install Node dependencies
npm install

# 2. Configure frontend environment in .env.local:
# NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
# NEXT_PUBLIC_SUPABASE_ANON_KEY=your-anon-key-here
# NEXT_PUBLIC_API_URL=http://localhost:8000

# 3. Start Next.js development server
npm run dev
```
Dashboard available at `http://localhost:3000`.

---

## 6. Testing

### Run Offline Backend Tests (No PostgreSQL Required):
```bash
cd apps/api
pytest -m "not pg_api_integration" -q
```

### Run PostgreSQL API Integration Tests (`21` tests; full `106`-test suite via CI):
```bash
cd apps/api
# Requires REQUIRE_POSTGRES_TESTS=1 and a dedicated test POSTGRES_TEST_URL.
# Note: `-m pg_api_integration` selects only the 21 API integration tests, NOT all 106 PG tests.
REQUIRE_POSTGRES_TESTS=1 pytest -m pg_api_integration
```
*Full 106-test PostgreSQL 16 suite (9 CI modules) is orchestrated by [`.github/workflows/ci.yml`](.github/workflows/ci.yml). Post-merge CI baseline on `main` (`6872f47`, [Run `46048131091`](https://github.com/Sahibnarulaofficial/Aptlyst-AI/actions/runs/46048131091)): `1,322` passed (`106` PostgreSQL 16 integration + `18` advisory-lock + `1,198` regression), `0` skipped, `0` failed.*

### Run Frontend Tests:
```bash
npm test
```

---

## 7. Current Project Status

- **Active Phase**: **Stage 2L — Billing Handoff & Freeze (Chatbot Product Development Active)** (`main` @ [`6872f47`](https://github.com/Sahibnarulaofficial/Aptlyst-AI/commit/6872f471df201faeeae62decfb76552cdc28d084)).
- **Status Summary**: Stage 1–2K billing infrastructure (provider-neutral schema through `a4b5c6d7e8f3`, checkout recovery worker, advisory lock unification, heartbeats/monitoring, and PostgreSQL 16 CI isolation) is merged and green in CI. Paid Razorpay billing (`RAZORPAY_ENABLED=false`) is **frozen and NOT production-ready**; engineering focus is now on Chatbot Product Development (RAG, live LLM/SSE streaming, dashboard UI modules) before the Final Razorpay Integration & Cutover Phase.
- For complete details, see [`docs/operations/BILLING_HANDOFF_AND_FREEZE.md`](docs/operations/BILLING_HANDOFF_AND_FREEZE.md), [`PROJECT_STATUS.md`](PROJECT_STATUS.md), and [`BILLING_ROADMAP.md`](BILLING_ROADMAP.md).
