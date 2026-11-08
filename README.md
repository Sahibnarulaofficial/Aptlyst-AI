# Aptlyst AI

**Aptlyst AI** is a production-grade, multi-tenant conversational AI platform. It empowers businesses to create, customize, embed, and analyze intelligent conversational AI agents trained on proprietary knowledge bases, complete with lead capture, multi-tenant organization management, and a provider-neutral subscription billing engine.

---

## 1. Product Capabilities

- **Custom AI Chatbots**: Configure custom prompt instructions, temperature, themes, brand avatars, and conversational guardrails.
- **RAG Knowledge Base**: Upload and index documents (PDFs, Markdown, raw text) for accurate, grounded similarity search and conversational retrieval.
- **Embeddable Chat Widget**: Lightweight, isolated embeddable widget (`/embed`) with real-time Server-Sent Events (SSE) token streaming for external customer websites.
- **Lead Capture & Analytics**: Collect visitor contact information dynamically during chat sessions and view conversation trends.
- **Multi-Tenant Organizations**: Team membership roles (`owner`, `admin`, `member`), organization switching, and scoped data isolation powered by Supabase Auth SSR.
- **Provider-Neutral Billing Engine**: Decoupled entitlement architecture supporting Razorpay Subscriptions (domestic INR e-mandates) and historical Stripe records.

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
├── AGENTS.md                      # Next.js agent instruction block
├── CLAUDE.md                      # Agent tool instructions
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
│       │   └── workers/           # Asynchronous queue workers
│       ├── alembic/               # Database migration scripts
│       └── tests/                 # Pytest unit & integration test suites
│
└── docs/                          # Comprehensive Documentation
    ├── architecture/              # Canonical living architecture specs
    ├── development/               # Setup, testing, and workflow guides
    ├── decisions/                 # Formal Architecture Decision Records (ADR-001 - 005)
    ├── audits/                    # Documentation audit deliverables
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

### Run Backend Unit Tests:
```bash
cd apps/api
pytest -q
```
*Current test metrics: 634 passed, 0 failed.*

### Run PostgreSQL CI Integration Tests:
```bash
cd apps/api
# Requires POSTGRES_TEST_URL pointing to a running PostgreSQL 16 instance
pytest tests/test_postgres_provenance_integration.py
```

### Run Frontend Tests:
```bash
npm test
```
*Current test metrics: 4 passed, 0 failed.*

---

## 7. Current Project Status

- **Active Milestone**: **Phase 5.2D — Step 5.2D.3.3A.2 (PostgreSQL CI Validation)**.
- **Status Summary**: Core chatbot, RAG, multi-tenancy, and frontend dashboards are fully implemented. The provider-neutral billing database schema and provenance migration (`e2f3a4b5c6d1`) are authored and awaiting live PostgreSQL service container execution in GitHub Actions before Step 5.2D.3.3B (Razorpay payment settlement handlers) begins.
- For complete details, see [`PROJECT_STATUS.md`](PROJECT_STATUS.md) and [`BILLING_ROADMAP.md`](BILLING_ROADMAP.md).
