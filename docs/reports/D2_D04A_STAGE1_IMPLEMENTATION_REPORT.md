# Aptlyst AI — D-04A Stage 1: Shared Webhook Exception Foundation — Implementation Report

**Document ID:** `D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md`  
**Authorized Scope:** D-04A Stage 1 Corrections & F-01 Security Correction ONLY ([Correction Plan Rev 1.2](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A_STAGE1_CORRECTION_PLAN.md), [Post-Correction Review F-01](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A_STAGE1_POST_CORRECTION_REVIEW.md))  
**Implementation Worktree:** [`D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16)  
**Feature Branch:** `implementation/d04a-stage1-exception-foundation`  
**Verified Baseline Branch / HEAD:** `validation/d2-stage1-customer-pg16` @ `497852e19360d85787107dbae6921519074f0773`  
**Git Staging / Commit / Push State:** None staged, none committed, none pushed

---

## 1. Summary & Revision History

### Revision History

| Revision | Date | Scope of Update |
|---|---|---|
| **Rev 1.0** | 2026-11-10 | Initial D-04A Stage 1 implementation (shared exception hierarchy, cycle-safe SQLSTATE classifier, sanitizer, 88 unit tests). |
| **Rev 2.0** | 2026-11-11 | **Authorized Stage 1 Corrections Implemented (`RC-01` through `RC-05`, `L-01a/b`, `L-03`).** Implemented conservative 4-type Tier 1 programming-error classification, Tier 2 context-dependent exception handling (`NotImplementedError`), two-phase causal-first exception chain traversal with `__suppress_context__` respect, exact `23505` constraint buckets (moved cross-tenant provision to Bucket 3, removed prefix wildcards), hardened secret sanitizer and cycle-safe diagnostic dict parser, Class 53 (`53100`, `53200`, `53300`) `RETRYABLE_WITH_ALERT` classification, parameter-scrubbed `57014` statement timeout classifier, `23001` permanent mapping, innermost traceback frame extraction with host-path stripping, and expanded unit test suite from 88 to **158 passed offline tests** (`657 passed` total offline regression). |
| **Rev 2.1** | 2026-11-14 | **Authorized F-01 Security Correction Implemented.** Resolved finding `F-01` from [`D2_D04A_STAGE1_POST_CORRECTION_REVIEW.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A_STAGE1_POST_CORRECTION_REVIEW.md). Implemented boundary-aware compound secret-key regex pattern (`_SECRET_KEY_NAME_PATTERN`) supporting underscore-prefixed and compound keys (`razorpay_key_secret`, `my_webhook_secret`, `webhook_signature`, `api_secret`, `client_secret`, `access_token`, `refresh_token`, etc.) while strictly preserving non-secret identifiers (`idempotency_key`, `lease_token_prefix`, etc.) and safe-value exclusions (`signature_verified=False`, etc.). Added 5 regression test methods covering all 12 required scenarios. Unit test suite expanded to **163 passed offline tests** (`662 passed` total offline regression). |

All Stage 1 safety invariants and isolation boundaries were verified cleanly:
- **Zero tracked modifications:** `git diff --stat` and `git diff --cached --stat` are completely empty.
- **Strictly authorized files only:** Only [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py), [`test_billing_webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py), and this report were created or modified.
- **Zero circular dependencies:** [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py) imports strictly Python standard library modules (`__future__`, `collections`, `dataclasses`, `enum`, `pathlib`, `re`, `traceback`, `typing`) and passes standalone import verification with bare system Python.
- **Sibling worktrees untouched:** `D:\Projects\V1\Aptlyst-AI` on `main` @ `027ab7a` and `D:\Projects\V1\Aptlyst-AI-pg16-validation` on `validation/postgres16-ci-hardening` @ `c932cc4` remain untouched.
- **Full offline regression compatibility:** All `163` Stage 1 unit tests, `124` existing Razorpay & Stripe webhook tests, and `375` CI Step 13 offline billing/model/guard/migration tests pass (`662 passed, 0 skipped, 0 failed`).

---

## 2. Branch and Baseline Verification

| Check | Verified Value / State |
|---|---|
| **Target Worktree** | [`D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16) |
| **Original Baseline Branch** | `validation/d2-stage1-customer-pg16` @ `497852e19360d85787107dbae6921519074f0773` (verified unchanged) |
| **Implementation Branch** | `implementation/d04a-stage1-exception-foundation` @ `497852e19360d85787107dbae6921519074f0773` |
| **Tracked Diff (`git diff --name-only`)** | Empty (`0` modified tracked files; `git diff --cached --name-only` empty) |
| **Pre-existing Untracked Reports** | Preserved intact: `STEP_5_2D_3_3B_1E_Z1` through `Z8` `*_REPORT.md` and `docs/reports/` (`D2_STAGE1_FORMAL_CLOSURE_REPORT.md`, `D2_STAGE2_READINESS_AUDIT.md`, `D2_D04_RAZORPAY_WEBHOOK_SAFETY_INVESTIGATION.md`, `D2_D04A_EXCEPTION_ARCHITECTURE_DESIGN.md`, `D2_D04A1_IMPLEMENTATION_BLUEPRINT.md`, `D2_D04A_STAGE1_CORRECTION_PLAN.md`, `D2_D04A_STAGE1_POST_CORRECTION_REVIEW.md`) |
| **Main Worktree (`D:/Projects/V1/Aptlyst-AI`)** | Untouched on `main` @ `027ab7a` (`027ab7ad3ee8a02fb20823a4f743e29b03c0f248`) |
| **PG Hardening Worktree (`D:/Projects/V1/Aptlyst-AI-pg16-validation`)** | Untouched on `validation/postgres16-ci-hardening` @ `c932cc4` (`c932cc480f82b2eea11c7616bf9290e255cffd31`) |

---

## 3. Files Changed and Cryptographic Digests

| Action | File | Lines | SHA-256 | Purpose |
|---|---|---:|---|---|
| **Modified** | [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py) | `1542` | `9508C855AD423C2E8C816D12572C38D66E31F5CE9D8484F1BDD66964603C8033` | Shared exception hierarchy, cycle-safe SQLSTATE/constraint classifier, causal chain extractor, boundary-aware secret sanitizer (`F-01`), and ED-01 webhook exception classifier |
| **Modified** | [`test_billing_webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py) | `1696` | `592DABDA406903540569F8D05E2E9767CC01D4BA3D9BE2B00BF9B02624FF3645` | `163` offline unit tests (`0` DB/API calls) covering all Stage 1 requirements, corrections, and F-01 regression battery |
| **Updated** | [`D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md) | — | — | This implementation report addendum (Revision 2.1) |

---

## 4. Technical Implementation of Authorized Corrections

### 4.1 `RC-01` — Conservative ED-01 Programming-Error Classification (`H-01`)

Implemented declarative webhook exception classification without encoding retry counts or worker transitions:
- **`UNEQUIVOCAL_PROGRAMMING_ERROR_TYPES = (TypeError, AttributeError, NameError, UnboundLocalError)`:** Strictly restricted to four unequivocal deterministic Python code defects. Evaluated exclusively on the top-level exception instance via `is_unequivocal_programming_error(exc)`.
- **Tier 2 Context-Dependent Exceptions (`NotImplementedError`, `AssertionError`, `ZeroDivisionError`, `RecursionError`, `SyntaxError`, `IndentationError`):** Direct/unwrapped occurrences classify as `WebhookExceptionCategory.UNKNOWN_EXCEPTION` (`failure_category="unexpected_internal_error"`, `should_quarantine_immediately=False`, `should_retry=True`, `is_programming_error=False`, `requires_alert=True`), ensuring bounded queue retry + mandatory alerting rather than irreversible attempt-1 quarantine. Explicit wrapping by handlers (`BillingWebhookPermanentError` / `BillingWebhookUnknownEventError` -> `PERMANENT_ERROR`, `BillingWebhookRetryableError` -> `RETRYABLE_ERROR`) is strictly honored.
- **Two-Phase Causal-First Chain Traversal in `iter_db_exception_chain()`:**
  - *Phase 1 (Explicit Causal Subgraph):* Traverses `.orig` and `__cause__` edges via BFS with `visited: set[int]` deduplication, bounded by `max_nodes`. While traversing, non-suppressed `__context__` references are enqueued to a deferred queue only when `not bool(_safe_getattr(node, "__suppress_context__"))`.
  - *Phase 2 (Deferred Context Fallback):* Processes deferred `__context__` nodes within the remaining node budget only after explicit causal edges are exhausted, ensuring `raise ... from None` severs context cleanly and explicit causes take precedence.
- **5-Step Precedence in `classify_webhook_exception()`:**
  1. *Step 1:* Re-raise non-`Exception` `BaseException` (`asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`) uncaptured.
  2. *Step 2:* Authoritative `BillingWebhookError` hierarchy (`Lease` -> `STALE_LEASE`, `Permanent` -> `PERMANENT_ERROR`, `Retryable` -> `RETRYABLE_ERROR`, bare `BillingWebhookError` -> `UNKNOWN_EXCEPTION` with alert).
  3. *Step 3:* Primary active Tier 1 check on top-level `exc` ONLY (nested Tier 1 in causes/contexts cannot hijack outer wrappers; ambient DB context cannot mask a top-level code bug).
  4. *Step 4:* Non-override guard: ordinary non-DB exceptions without direct DB attributes or explicit causes retain `UNKNOWN_EXCEPTION` (`requires_alert=True`), preventing caught `except` DB contexts from overriding the failure.
  5. *Step 5:* Causal-first database/transport classification fallback.

### 4.2 `RC-02` — Constraint-Aware `23505` Buckets & Exact Precedence (`H-02`)

- **Prefix Wildcards Removed:** `UNIQUE_CONCURRENCY_RACE_PREFIXES = ()` retained as an empty tuple for import compatibility. Speculative future constraint names fall through safely to Bucket 4 `UNMAPPED_UNIQUE_VIOLATION` (`RETRYABLE_WITH_ALERT`).
- **Exact Set Membership:**
  - *Bucket 1 (`UNIQUE_CONVERGENCE_CONSTRAINTS`):* `uq_billing_invoices_provider_invoice`, `uq_billing_payments_provider_payment`, `uq_billing_webhook_provider_event`, `uq_payment_reconciliations_case_key`, `uq_stripe_invoices_invoice_id`.
  - *Bucket 2 (`UNIQUE_CONCURRENCY_RACE_EXACT`):* `uq_billing_customer_provisions_org_provider`, `uq_subscriptions_provider_subscription`, `uq_org_scoped_active_billing_checkout_attempt`, `uq_checkout_attempts_provider_sub`, `uq_checkout_attempts_provider_payment`, `uq_org_active_checkout_attempt`.
  - *Bucket 3 (`UNIQUE_PERMANENT_CONSTRAINTS`):* `uq_org_active_subscription`, `uq_subscriptions_provider_customer`, `uq_billing_payments_checkout_attempt`, `uq_billing_checkout_org_idempotency`, `uq_bca_composite_identity`, `uq_reconciliation_event_sub` (replaces phantom), **`uq_billing_customer_provisions_provider_customer`** (moved from Bucket 2!), `uq_org_active_billing_period`.
- **Precedence Order in `classify_unique_violation_constraint()`:** Bucket 3 (PERMANENT) is checked first, followed by Bucket 1, Bucket 2, prefixes, and Bucket 4.
- **Set Invariant:** Verified pairwise disjointness between Buckets 1, 2, and 3 via automated test.

### 4.3 `RC-03` — Sanitizer & Diagnostic Payload Hardening (`M-01`)

- **PostgreSQL URI Credentials:** `_PG_URI_CREDENTIAL_RE` updated to permit `/` in passwords after `:` while guarding authority bounds.
- **URI Query Secrets:** `_URI_QUERY_SECRET_RE` terminates cleanly at `&`, preserving subsequent query parameters.
- **Shield Tokens:** `_SHIELD_PATTERNS` extended to protect `acct_` and `rfnd_` entity IDs and 13-digit millisecond Unix epochs (`(?:1[5-9]\d{8}|2[0-4]\d{8})(?:\.\d+|\d{3})?`).
- **Diagnostic Dictionary Sanitizer:** `_is_sensitive_diagnostic_key()` checks token-split normalized components while preserving safe identifiers (`idempotency_key`, `signature_verified`, `lease_token_prefix`). `_sanitize_diag_value()` enforces active-path `seen_ids` cycle tracking and decrements `depth_left` across both dictionaries and lists/tuples.

### 4.4 `RC-04` — Conservative PostgreSQL SQLSTATE Retry Classification (`M-02`)

- **Class 53 Capacity Exhaustion (`ALERT_RETRYABLE_PG_SQLSTATES`):** `53100` (`disk_full`), `53200` (`out_of_memory`), and `53300` (`too_many_connections`) classified as `DbRetryDisposition.RETRYABLE_WITH_ALERT` (`failure_category="db_resource_exhausted"`, `should_retry=True`, `requires_alert=True`, `is_known=True`). `53300` was removed from silent `RETRYABLE_PG_SQLSTATES`.
- **Relational Integrity Violations:** `23001` (`restrict_violation`) added to `PERMANENT_PG_SQLSTATES` (`DbRetryDisposition.PERMANENT`, `should_retry=False`, `is_permanent=True`).
- **Configuration Ceilings:** `53400` (`configuration_limit_exceeded`) explicitly retained as `DbRetryDisposition.UNKNOWN` (`requires_alert=True`).
- **Context-Aware `57014` Handling:** Parameter-scrubbed inspection (`_is_statement_timeout_57014()`) maps server `statement timeout` messages to `RETRYABLE_WITH_ALERT` (`db_statement_timeout`, `should_retry=True`, `requires_alert=True`), while `user request` (operator cancel) and bare SQLSTATEs stay explicitly `UNKNOWN` (`requires_alert=True`).

### 4.5 `RC-05` — Innermost Traceback Frames & Path Safety (`M-03`)

- **Crash-Site Ordering:** `extract_safe_traceback_locations()` extracts the innermost frames using `limit=-effective_limit` with `MAX_TRACEBACK_FRAMES = 64`. Frame limits `<= 0` return `[]`.
- **Host Path Stripping:** `_sanitize_traceback_filename()` normalizes separators, preserves synthetic `<...>` pseudofiles, strips host roots before well-known anchors (`apps/api/`, `tests/`, `app/`, `alembic/`, `site-packages/`, `dist-packages/`), and defaults unrecognized absolute paths to basename only.

### 4.6 `L-01` through `L-03` Dispositions

- **`L-01a` (Mid-marker truncation):** In `sanitize_billing_error_message()`, truncation at `max_chars` steps back before an incomplete `[` token boundary.
- **`L-01b` (Empty/whitespace `failure_category`):** `BillingWebhookError.__init__()` normalizes whitespace-only strings to class defaults.
- **`L-01c` (Single-node `sanitized_message` scope):** Retained as-is (chained diagnostics belong to `extract_db_error_details()`).
- **`L-02` (`billing/__init__.py` eager import cleanup):** Formally deferred beyond Stage 1 to protect scope; `webhook_errors.py` remains 100% stdlib-only.
- **`L-03` (Unit-test coverage gaps):** Added unit tests for acyclic chains beyond `max_nodes`, non-mutation of `RazorpayClientError` / `RazorpayCustomerError`, empty `BillingWebhookError()`, and uppercase `UNIQUE CONSTRAINT` fallback regexes.

### 4.7 `F-01` — Boundary-Aware Compound Secret Key Sanitization (`M-01` Addendum)

- **Root Cause of Defect F-01:**  
  The original `_KV_HEADER_SECRET_RE` regex used an initial word boundary anchor `\b(...)` around an alternation of bare secret stems (`secret`, `signature`, `key_secret`, etc.). Because the underscore character `_` is classified as a word character (`\w`) in Python's regular expression engine, `\b` fails to match compound names where an underscore-separated prefix precedes the stem (e.g. in `razorpay_key_secret=val`, the transition from `_` to `k` is between two word characters and therefore not a word boundary). Consequently, compound secret names like `razorpay_key_secret=...` or `my_webhook_secret=...` were skipped in free-form strings, leaving the secret value unredacted.

- **Boundary-Aware Fix in `webhook_errors.py`:**  
  Implemented a robust, boundary-aware compound secret key descriptor:
  ```python
  _SECRET_KEY_NAME_PATTERN = (
      r"(?:[A-Za-z0-9]+[_-])*"
      r"(?:"
      r"secret|"
      r"signature|"
      r"api[_-]?key|"
      r"private[_-]?key|"
      r"secret[_-]?key|"
      r"(?:access|refresh|id|auth|bearer|session|csrf|jwt|api)[_-]token|"
      r"secret[_-]?token|"
      r"password|passfile|passphrase|"
      r"authorization"
      r")"
  )
  _KV_HEADER_SECRET_RE = re.compile(
      rf"\b({_SECRET_KEY_NAME_PATTERN})\b"
      r"([^\S\r\n]*[:=][^\S\r\n]*)"
      r"(?!(?:\s|True\b|False\b|true\b|false\b|None\b|null\b|\[REDACTED|(?:Basic|Bearer)[^\S\r\n]+\[REDACTED))"
      r"('(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"|(?:t=\d+,[^\S\r\n]*)?(?:v\d+=[0-9A-Za-z+/=]+(?:,[^\S\r\n]*)?)+|[^\s,;&}\]]+)",
      re.IGNORECASE,
  )
  ```
  - **Compound Name Support:** `(?:[A-Za-z0-9]+[_-])*` allows arbitrary underscore- or hyphen-separated prefixes before the sensitive token.
  - **Full Key Capture in Group 1:** The entire compound key name (e.g. `razorpay_key_secret`) is captured in `\1`, and the delimiter in `\2`. The replacement `\1\2[REDACTED]` cleanly preserves the original key name while redacting only the value.
  - **Preservation of Non-Secret Identifiers:** Key names such as `idempotency_key`, `lease_token_prefix`, `order_id`, `customer_id`, `case_key`, `max_tokens`, `token_count`, `routing_key`, `partition_key`, and `signature_algorithm` do not match the sensitive suffix stems and remain completely untouched.
  - **Safe-Value Exclusions Preserved:** Boolean flags (`signature_verified=False`, `signature_verified=True`, etc.), null markers (`None`, `null`), and pre-redacted markers (`[REDACTED]`, `Bearer [REDACTED_TOKEN]`) are protected by the negative lookahead.
  - **ReDoS Safety:** Each segment in `(?:[A-Za-z0-9]+[_-])*` requires a trailing delimiter character (`_` or `-`), eliminating catastrophic backtracking ambiguity (verified via pathological benchmark completing in `< 2ms`).
  - **Idempotency:** Repeated sanitization passes produce identical output.

---

## 5. Test Results and Verification Evidence

All test suites were executed offline using the project virtualenv interpreter from `D:\Projects\V1\Aptlyst-AI\apps\api\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider` in worktree [`D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api):

| Suite | Scope / Files Executed | Result | Runtime | Notes |
|---|---|---|---|---|
| **Stage 1 Unit Foundation** | [`tests/test_billing_webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py) | **`163 passed, 0 skipped, 0 failed`** | `1.78s` | 100% pass across hierarchy, sanitization, Buckets 1–4, SQLSTATE sets, 12-scenario ED-01 classification suite, 57014, Class 53, innermost tracebacks, edge cases, and all 5 F-01 regression test methods (12 required scenarios) |
| **Existing Razorpay & Stripe Webhook Tests** | `test_razorpay_handler_foundation.py`, `test_razorpay_agreement_handlers.py`, `test_razorpay_webhook_worker.py`, `test_razorpay_webhook_ingestion.py`, `test_razorpay_client.py`, `test_razorpay_config.py`, `test_stripe_webhook_engine.py` | **`124 passed, 0 skipped, 0 failed`** | `7.79s` | Full compatibility with existing Razorpay and Stripe test suites |
| **CI Step 13 Offline Billing/Migration/Model Regression Suite** | `test_postgres_test_guard.py`, `test_payment_association_migration.py`, `test_provider_neutral_billing_models.py`, `test_provider_neutral_migration.py`, `test_razorpay_customer.py`, `test_razorpay_checkout.py`, `test_billing_api.py` | **`375 passed, 0 skipped, 0 failed`** | `35.00s` | Full compatibility with CI Step 13 offline regression suite |
| **Combined Offline Total** | — | **`662 passed, 0 skipped, 0 failed`** | `44.57s` | Zero secret tokens leaked; zero regressions |

**Standalone Import Check:**
- Bare system Python `C:\Program Files\Python311\python.exe` imported `webhook_errors.py` via `importlib.util` with zero third-party packages installed, verifying all 37 symbols in `__all__` load cleanly without dependencies.

**AST Check:**
- AST walk confirmed strictly standard-library imports: `['__future__', 'collections', 'dataclasses', 'enum', 'pathlib', 're', 'traceback', 'typing']`.

---

## 6. Implementation and Test Status Matrix

| Finding / Item | Component | Implementation Status | Test Status | Notes |
|---|---|---|---|---|
| **RC-01** (`H-01`) | `UNEQUIVOCAL_PROGRAMMING_ERROR_TYPES` | Implemented | Tested & Passed | 4 types only: `TypeError`, `AttributeError`, `NameError`, `UnboundLocalError` |
| **RC-01** (`H-01`) | Tier 2 `NotImplementedError` handling | Implemented | Tested & Passed | Direct -> `UNKNOWN_EXCEPTION` (`requires_alert=True`); wrapped -> honors wrapper |
| **RC-01** (`H-01`) | Causal-first chain traversal & `__suppress_context__` | Implemented | Tested & Passed | BFS explicit `.orig`/`__cause__` before deferred `__context__` |
| **RC-01** (`H-01`) | 5-step classification precedence | Implemented | Tested & Passed | Re-raise BaseException, outer BillingWebhookError, Tier 1, guard, DB fallback |
| **RC-02** (`H-02`) | Exact `23505` constraint sets & prefix removal | Implemented | Tested & Passed | `UNIQUE_CONCURRENCY_RACE_PREFIXES = ()`; Buckets 1, 2, 3 disjoint |
| **RC-02** (`H-02`) | Cross-tenant provision constraint moved to Bucket 3 | Implemented | Tested & Passed | `uq_billing_customer_provisions_provider_customer` -> `PERMANENT_INTEGRITY_CONFLICT` |
| **RC-02** (`H-02`) | Missing constraint additions & phantom cleanup | Implemented | Tested & Passed | Replaced phantom with `uq_reconciliation_event_sub`; added missing billing constraints |
| **RC-03** (`M-01`) | PG URI `/` password allowance | Implemented | Tested & Passed | `_PG_URI_CREDENTIAL_RE` matches slash passwords without leaking |
| **RC-03** (`M-01`) | Query secret `&` boundary | Implemented | Tested & Passed | `_URI_QUERY_SECRET_RE` stops cleanly before subsequent query parameters |
| **RC-03** (`M-01`) | Shield tokens for 13-digit epochs & `acct_`/`rfnd_` | Implemented | Tested & Passed | Preserves entity IDs and ms epochs without PAN redaction |
| **RC-03** (`M-01`) | Cycle-safe & depth-bounded diagnostic payload | Implemented | Tested & Passed | Active-path cycle guard + depth decrement across dicts and lists/tuples |
| **F-01** (`M-01`) | Boundary-aware compound secret key sanitization | Implemented | Tested & Passed | `_SECRET_KEY_NAME_PATTERN` supports `razorpay_key_secret`, `my_webhook_secret`, `webhook_signature`, etc.; preserves non-secrets & safe values |
| **RC-04** (`M-02`) | Class 53 capacity exhaustion (`53100`, `53200`, `53300`) | Implemented | Tested & Passed | Mapped to `RETRYABLE_WITH_ALERT` (`db_resource_exhausted`) |
| **RC-04** (`M-02`) | `23001` restrict violation permanent mapping | Implemented | Tested & Passed | Mapped to `PERMANENT` (`db_permanent`) |
| **RC-04** (`M-02`) | `57014` statement timeout context-aware classification | Implemented | Tested & Passed | Statement timeout -> `RETRYABLE_WITH_ALERT`; user request -> `UNKNOWN` |
| **RC-04** (`M-02`) | `53400` configuration limit explicit unknown | Implemented | Tested & Passed | Stays `UNKNOWN` (`requires_alert=True`) |
| **RC-05** (`M-03`) | Innermost traceback frames (`limit=-effective_limit`) | Implemented | Tested & Passed | Captures crash site on deep chains; handles `<= 0` and excessive bounds |
| **RC-05** (`M-03`) | Host-path stripping from traceback locations | Implemented | Tested & Passed | Normalizes and strips leading paths before repo/package anchors |
| **L-01a** | Mid-marker truncation safety | Implemented | Tested & Passed | Steps back before incomplete `[` token boundary |
| **L-01b** | Empty/whitespace `failure_category` normalization | Implemented | Tested & Passed | Normalizes whitespace to class default |
| **L-01c** | Single-node `sanitized_message` scope | Maintained As-Is | Tested & Passed | Chained diagnostics extracted via `extract_db_error_details()` |
| **L-02** | `billing/__init__.py` eager import split | Deferred | Not Tested (Out of Scope) | Explicitly deferred beyond Stage 1 to protect scope |
| **L-03** | Test gap additions | Implemented | Tested & Passed | Acyclic chain truncation, client/customer error checks, uppercase regex |

---

## 7. Status of Downstream D-04A Stages

**Stages 2 through 5 remain strictly unimplemented and unauthorized in this worktree:**
- **Stage 2** (Razorpay exception hierarchy unification & export cleanup): **Not started.**
- **Stage 3** (Atomic `PaymentReconciliation` `ON CONFLICT DO UPDATE`, committed-FK verification, `assert_valid_worker_lease()` savepoint fix): **Not started.**
- **Stage 4** (Phase 3 single atomic transaction, `ED-02` bounded race retry, `CancelledError` propagation & loop hardening): **Not started.**
- **Stage 5** (Live PostgreSQL 16 webhook worker integration suite & CI JUnit gating): **Not started.**
