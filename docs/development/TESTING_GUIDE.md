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

### 2.1 Post-Merge CI Verified Baseline (`main` @ `6872f47`, [Run `46048131091`](https://github.com/Sahibnarulaofficial/Aptlyst-AI/actions/runs/46048131091))
- **Total Passing Tests in CI**: **`1,322` passed**, `0` skipped, `0` failed, `0` errors
  - **PostgreSQL 16 Integration (`REQUIRE_POSTGRES_TESTS=1`)**: **`106` tests** across 9 live PostgreSQL test suites (including provenance immutability, checkout/recovery concurrency, heartbeat fencing, and `pg_api_integration` tests).
  - **Advisory Lock Unit Suite (`tests/test_advisory_lock.py`)**: **`18` tests**
  - **Focused Offline / Non-PG Regression Suite**: **`1,198` tests**

### 2.2 Running Offline / Non-PostgreSQL Tests Locally
Navigate to `apps/api/` and exclude tests marked `pg_api_integration`:

```bash
# In apps/api with .venv activated (offline/mock/SQLite tests without PostgreSQL):
pytest -m "not pg_api_integration" -q

# Enforce fail-closed PG test guard behavior check:
pytest tests/test_postgres_test_guard.py -q

# Run specific unit modules:
pytest tests/test_billing_api.py tests/test_ai_generation.py -q
```

> [!NOTE]
> Setting `REQUIRE_POSTGRES_TESTS=1` without `POSTGRES_TEST_URL` fails closed via `tests/postgres_api_test_helper.py` (`test_postgres_test_guard.py`). When running offline locally without PostgreSQL, use `-m "not pg_api_integration"` so live PostgreSQL API tests are cleanly excluded rather than stalling or touching `.env` / `DATABASE_URL`.

### 2.3 Running PostgreSQL Integration Tests Locally
If you have a dedicated, disposable local PostgreSQL 16 test database running:

```powershell
# PowerShell (never point POSTGRES_TEST_URL at staging, production, or .env DATABASE_URL):
$env:REQUIRE_POSTGRES_TESTS="1"
$env:POSTGRES_TEST_URL="postgresql+asyncpg://aptlyst_ci_user:postgres_ci_password@localhost:5432/aptlyst_ci_test"

# Run the 14 provenance integration tests (does NOT carry the pg_api_integration marker):
pytest tests/test_postgres_provenance_integration.py

# Run the 21 PostgreSQL API integration tests (organizations, chatbots, conversations, public_chatbots):
pytest -m pg_api_integration
```

> [!NOTE]
> `-m pg_api_integration` selects only the **21** PostgreSQL API integration tests. The full **106**-test PostgreSQL 16 suite spans **9** CI modules and is orchestrated step-by-step in [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml).

### 2.4 CI Pipeline Configuration
The CI workflow in `.github/workflows/ci.yml` spins up an isolated PostgreSQL 16 service container (`POSTGRES_DB: aptlyst_ci_test`, `POSTGRES_USER: aptlyst_ci_user`) and executes:
1. Advisory-lock & focused offline regression suites (`-m "not pg_api_integration"`).
2. All PostgreSQL 16 integration suites with `REQUIRE_POSTGRES_TESTS=1` against the CI service container.

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
