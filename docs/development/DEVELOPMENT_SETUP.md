# Aptlyst AI — Development Setup Guide

## 1. Prerequisites

Before setting up the repository locally, ensure the following tools are installed:

- **Node.js**: Version 20.x or higher
- **npm**: Version 10.x or higher
- **Python**: Version 3.12 or higher
- **PostgreSQL**: Version 16.x (or access to a Supabase project)
- **Git**

---

## 2. Repository Structure Overview

```text
Aptlyst-AI/
├── app/                           # Next.js App Router (Frontend Dashboard & Widget)
├── components/                    # React UI components
├── lib/                           # Frontend utility libraries & Supabase client
├── package.json                   # Frontend dependencies & npm scripts
├── apps/
│   └── api/                       # FastAPI Backend Service
│       ├── app/                   # Backend application source code
│       ├── alembic/               # Database migrations
│       ├── requirements.txt       # Production dependencies
│       └── requirements-dev.txt   # Development & testing dependencies
└── docs/                          # Project documentation
```

---

## 3. Backend Setup (`apps/api`)

### 3.1 Virtual Environment & Dependencies
Open a terminal and navigate to the backend directory:

```bash
cd apps/api

# Create Python virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt
```

### 3.2 Environment Configuration
Copy the template configuration file:

```bash
cp .env.example .env
```

Edit `.env` to configure your local credentials:
- `DATABASE_URL`: Connection string for PostgreSQL (e.g. `postgresql+asyncpg://postgres:password@localhost:5432/aptlyst_dev` or Supabase pooler URL).
- `SUPABASE_URL` and `SUPABASE_ANON_KEY`: From your Supabase project dashboard.
- `SUPABASE_JWT_SECRET`: For JWT token verification.
- `ENVIRONMENT`: Set to `development`.
- `VISITOR_SESSION_SECRET`: Random 32+ character string (e.g. `python -c "import secrets; print(secrets.token_hex(32))"`).
- `RAZORPAY_ENABLED=false` & `STRIPE_ENABLED=false`: Keep disabled during the **Stage 2L Billing Freeze** ([handoff spec](../operations/BILLING_HANDOFF_AND_FREEZE.md)). Do not configure live keys in development.
- `LLM_PROVIDER` & `DEFAULT_FREE_MONTHLY_TOKEN_QUOTA`: Both default safely to disabled/fail-closed (`disabled` / `0`). For local chatbot testing without enabling paid billing, set `LLM_PROVIDER=mock` and a positive `DEFAULT_FREE_MONTHLY_TOKEN_QUOTA` (e.g., `100000`) in your local `.env`.

### 3.3 Database Migrations
Apply the Alembic migrations up to the latest head:

```bash
alembic upgrade head
```

> [!NOTE]
> The current migration chain head is `a4b5c6d7e8f3` (`add_billing_worker_heartbeats`, layered cleanly atop `f3a4b5c6d7e2` and `e2f3a4b5c6d1`). In a clean local PostgreSQL database, running `upgrade head` provisions all core tables, constraints, triggers, and billing structures.

### 3.4 Running the Backend Server
Start the development server with live reload:

```bash
uvicorn app.main:app --reload --port 8000
```
- API Base URL: `http://localhost:8000`
- Interactive Swagger UI: `http://localhost:8000/docs`
- Health check: `http://localhost:8000/health` (`GET /health/db` checks PostgreSQL connectivity)

### 3.5 Running Billing Workers (Only When Explicitly Testing Billing)
Under Stage 2L Billing Freeze, workers do not need to run for chatbot development. When authorized for billing tests:

```bash
python -m app.workers.razorpay_webhook_worker --once
python -m app.workers.razorpay_checkout_recovery_worker --once
```

---

## 4. Frontend Setup (Root Directory)

### 4.1 Install Node.js Dependencies
Navigate back to the repository root:

```bash
# In the repository root
npm install
```

### 4.2 Frontend Environment Configuration
Create or update `.env.local` at the repository root:

```ini
NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=your-anon-key-here
NEXT_PUBLIC_API_URL=http://localhost:8000
```

### 4.3 Running the Frontend Development Server
Start the Next.js development server:

```bash
npm run dev
```
Open [http://localhost:3000](http://localhost:3000) in your browser.

---

## 5. Development Verification Checklist

After launching both servers, verify your local environment:
1. Navigate to `http://localhost:3000/` and verify the landing page renders.
2. Sign up or log in via Supabase Auth to confirm session cookie handling.
3. Check `http://localhost:8000/health/db` to verify database connectivity.
4. Run the automated test suites as outlined in the [`TESTING_GUIDE.md`](TESTING_GUIDE.md).
