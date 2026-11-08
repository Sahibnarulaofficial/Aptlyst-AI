# Aptlyst AI — Testing Guide & Quality Assurance

## 1. Testing Philosophy & Separation of Concerns

Aptlyst AI enforces a strict separation between **fast, isolated unit/mock tests** and **live database integration tests**:

1. **Unit & Fast Mock Tests (SQLite / Mock DB)**:
   - Run locally without external dependencies.
   - Use in-memory SQLite engines and mocked HTTP clients.
   - Execute in under 15 seconds across the full test suite.
2. **PostgreSQL-Specific Integration Tests**:
   - Validate PostgreSQL-specific features: advisory locks (`pg_advisory_xact_lock`), procedural database triggers, composite foreign key null-bypass constraints (`ck_billing_payments_checkout_attempt_complete`), and Alembic transactional conflict abort gates.
   - Require a live PostgreSQL 16 instance.
   - Configured to run automatically in GitHub Actions CI via an isolated service container.

---

## 2. Backend Testing (`apps/api/tests`)

### 2.1 Test Execution Metrics
- **Total Passing Local Unit Tests**: **634 tests**
- **Authored PostgreSQL Integration Tests**: **14 tests** (in `tests/test_postgres_provenance_integration.py`, skipped locally when `POSTGRES_TEST_URL` is absent)

### 2.2 Running Unit Tests Locally
Navigate to `apps/api/` and invoke pytest:

```bash
# In apps/api with .venv activated:
pytest

# Run tests with short summary output:
pytest -q

# Run specific test modules:
pytest tests/test_billing_api.py
pytest tests/test_payment_association_migration.py
pytest tests/test_razorpay_webhook_worker.py
```

### 2.3 Running PostgreSQL Integration Tests Locally
If you have a local PostgreSQL 16 instance running on `localhost:5432`:

```bash
# Set POSTGRES_TEST_URL environment variable:
# PowerShell:
$env:POSTGRES_TEST_URL="postgresql://postgres:postgres@localhost:5432/aptlyst_test"
pytest tests/test_postgres_provenance_integration.py

# Linux / macOS:
export POSTGRES_TEST_URL="postgresql://postgres:postgres@localhost:5432/aptlyst_test"
pytest tests/test_postgres_provenance_integration.py
```

### 2.4 CI Pipeline Configuration
The CI workflow is defined in `.github/workflows/ci.yml`. It spins up an ephemeral PostgreSQL 16 service container:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    env:
      POSTGRES_DB: aptlyst_test
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres_ci_password
    ports:
      - 5432:5432
    options: >-
      --health-cmd pg_isready
      --health-interval 10s
      --health-timeout 5s
      --health-retries 5
```

The CI job runs:
1. `pytest` for unit and migration mock tests.
2. `pytest tests/test_postgres_provenance_integration.py` against the PostgreSQL service container with `POSTGRES_TEST_URL` set.

---

## 3. Frontend Testing

The frontend test suite uses the Node.js native test runner with TypeScript type stripping:

```bash
# In repository root:
npm test
```

### Coverage Areas:
- **`tests/redirect.test.ts`**: Tests authentication routing, cookie evaluation, and protected route redirect logic.
- **`tests/api.test.ts`**: Tests client-side backend API client error handling, HTTP status parsing, and retry handling.
- **`tests/organization.test.ts`**: Tests multi-tenant organization context switching, local storage persistence, and member state.
- **`tests/chatbots.test.ts`**: Tests chatbot configuration validation, temperature ranges, and knowledge base associations.

---

## 4. Writing New Tests: Best Practices

1. **Do Not Depend on Network**:
   Never perform real HTTP calls to Stripe, Razorpay, or OpenAI in tests. Use `unittest.mock`, `pytest-mock`, or `respx` to mock external API responses.
2. **Respect Lock Monotonicity**:
   When writing tests that touch database transactions, ensure locks are acquired according to [ADR-005](../decisions/ADR-005-BILLING-LOCK-ORDERING.md) to prevent deadlock flakes in CI.
3. **Database Isolation**:
   Always clean up created test data using transactional rollbacks or fixture teardowns.
