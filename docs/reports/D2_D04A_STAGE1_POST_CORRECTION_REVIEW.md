# D-04A Stage 1 — Post-Correction Independent Review

| Field | Value |
|---|---|
| **Document ID** | D2\_D04A\_STAGE1\_POST\_CORRECTION\_REVIEW |
| **Revision** | 1.0 |
| **Date** | 2026-11-14 |
| **Reviewer** | Independent review agent (separate context, no implementation history) |
| **Review scope** | D-04A Stage 1 corrections per Correction Plan Rev 1.2 |
| **Verdict** | **ACCEPT WITH REQUIRED CORRECTIONS** |

---

## Executive Summary

The Stage 1 exception foundation implementation is substantially correct and passes all 158 unit tests. Five of the six correction requirements (RC-01, RC-02, RC-04, RC-05, and L-01..L-03) are fully implemented and independently verified. One requirement is **partially unmet**: RC-03's correction plan requirement to sanitize `razorpay_key_secret=<value>` as a **key name** in `sanitize_billing_error_message()` is not implemented — the `_KV_HEADER_SECRET_RE` word-boundary anchor (`\b`) prevents matching compound names where the secret token is preceded by a word-character prefix (e.g., `razorpay_key_secret`). The test suite does not expose this gap because it only tests the case where `key_secret` is the **key** and `my_razorpay_key_secret` is the **value**. The `sanitize_conflict_diagnostic_payload()` path correctly strips the same key name from structured dicts.

This gap is a **Medium severity** security finding. No other findings of High or Critical severity were identified.

---

## 1. Repository Verification

| Check | Result |
|---|---|
| Worktree | `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16` ✓ |
| Branch | `implementation/d04a-stage1-exception-foundation` ✓ |
| HEAD | `497852e19360d85787107dbae6921519074f0773` ✓ (matches expected baseline) |
| `git diff --stat` | Empty ✓ |
| `git diff --cached --stat` | Empty ✓ |
| Main worktree (`D:\Projects\V1\Aptlyst-AI`) | `main` @ `027ab7a` — **CRLF warnings only, no staged changes** ✓ |
| PG16 validation worktree (`D:\Projects\V1\Aptlyst-AI-pg16-validation`) | `validation/postgres16-ci-hardening` @ `c932cc4` — clean ✓ |

**Why `git diff` is empty:** All implementation files (`webhook_errors.py`, `test_billing_webhook_errors.py`, and all `docs/reports/` entries) are **untracked** (`??` in `git status --short`). They were created as new files in the worktree and were never `git add`ed, as required by the correction plan's staging policy ("Git Staging / Commit / Push State: None staged, none committed, none pushed"). This is intentional and correct.

**Untracked files observed in `git status --short`:**
```
?? STEP_5_2D_3_3B_1E_Z1_D2_STAGE1_EXCEPTION_DIAGNOSTICS_FIX_REPORT.md
?? STEP_5_2D_3_3B_1E_Z2_D2_STAGE1_CUSTOMER_PG16_TEST_REPORT.md
?? STEP_5_2D_3_3B_1E_Z3_D2_STAGE1_CI_INTEGRATION_REPORT.md
?? STEP_5_2D_3_3B_1E_Z4_D2_STAGE1_COMPLETION_READINESS_REPORT.md
?? STEP_5_2D_3_3B_1E_Z5_D2_STAGE1_INDEPENDENT_IMPLEMENTATION_AUDIT.md
?? STEP_5_2D_3_3B_1E_Z6_D2_STAGE1_IMPLEMENTATION_COMMIT_REPORT.md
?? STEP_5_2D_3_3B_1E_Z7_D2_STAGE1_PUSH_REPORT.md
?? STEP_5_2D_3_3B_1E_Z8_D2_STAGE1_MAPPER_REGISTRATION_FIX_REPORT.md
?? apps/api/app/services/billing/webhook_errors.py
?? apps/api/tests/test_billing_webhook_errors.py
?? docs/reports/
```

All pre-existing untracked reports are preserved. No tracked files are modified.

---

## 2. File Identity

| File | SHA-256 | Lines |
|---|---|---|
| `apps/api/app/services/billing/webhook_errors.py` | `C7C228A3F3C40E2A81C391F54C337ED6FF6DAA105ACAF9BFA61E0FB119B10911` | 1533 |
| `apps/api/tests/test_billing_webhook_errors.py` | `50E26B3697F65DB45FAE01B30FEC7E8F9B7873E87D216ABA37637BDDADEE6D2E` | 1563 |
| `docs/reports/D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md` | `FC0AB87C120225D956D8C5D60ECCFD3FF85964FE489175A1BA21EBEC33E78EEF` | — |

---

## 3. RC-01 — Conservative Programming-Error Classification

**Verdict: PASS**

### 3.1 `UNEQUIVOCAL_PROGRAMMING_ERROR_TYPES` (Tier 1)

Verified contents:
```python
UNEQUIVOCAL_PROGRAMMING_ERROR_TYPES = (TypeError, AttributeError, NameError, UnboundLocalError)
```

- `NotImplementedError` is **absent** (correctly Tier 2 per Rev 1.2 decision). ✓
- `AssertionError` is **absent** (correctly Tier 2). ✓
- Tuple is precisely the 4 types approved in Rev 1.2. ✓

### 3.2 `classify_webhook_exception()` — 5-Step Precedence

**Step 1 — BaseException passthrough:**  
`if not isinstance(exc, Exception): raise exc` (line 1304).  
`KeyboardInterrupt`, `SystemExit`, and `asyncio.CancelledError` are re-raised without classification. Independently confirmed: calling `classify_webhook_exception(KeyboardInterrupt())` re-raises it immediately — the function never returns a classification result for non-`Exception` BaseExceptions. ✓

**Step 2 — BillingWebhookError hierarchy evaluated before Tier 1:**  
`isinstance(exc, BillingWebhookError)` check (line 1311) precedes the Tier 1 check at line 1394. This correctly allows `BillingWebhookRetryableError(TypeError(...))` to be classified as retryable rather than quarantined. ✓

**Step 3 — Tier 1 check on `exc` only:**  
`is_unequivocal_programming_error(exc)` evaluates the top-level exception exclusively — it does not walk `.orig`, `__cause__`, or `__context__`. ✓

**Step 4 — Non-override guard using `_has_causal_or_db_identity()`:**  
Prevents an incidental `__context__` DB error from reclassifying an ordinary Python exception. If `exc` has no causal/DB identity, returns `UNKNOWN_EXCEPTION` without DB reclassification. ✓

**Step 5 — Causal-first DB classification fallback:**  
`classify_db_exception(exc)` applied only after Steps 1–4 pass. ✓

### 3.3 `iter_db_exception_chain()` — Two-Phase BFS with `__suppress_context__`

Independently verified:
- **Phase 1** traverses `.orig` (SQLAlchemy) and `__cause__` (explicit chaining) only.
- **Phase 2** defers `__context__` nodes and respects `__suppress_context__` — when the flag is `True` (set by `raise ... from None`), the `__context__` chain is excluded entirely.
- Cycle deduplication via `seen_ids` using `id()` prevents infinite loops.

Confirmed: `raise RuntimeError() from None` sets `__suppress_context__ = True`; `iter_db_exception_chain` on such an exception does not traverse the suppressed context. ✓

### 3.4 `WebhookExceptionCategory` and `WebhookExceptionClassification`

```
Categories: ['stale_lease', 'permanent_error', 'retryable_error', 'programming_error', 'unknown_exception']
```
All five categories present. `WebhookExceptionClassification` is a dataclass with 15 fields including `is_programming_error`, `should_quarantine_immediately`, `requires_alert`, `db_classification`. ✓

---

## 4. RC-02 — Schema-Exact `23505` Buckets and Precedence

**Verdict: PASS**

### 4.1 Bucket Contents

| Bucket | Constant | Contents |
|---|---|---|
| Bucket 1 — Idempotent Convergence | `UNIQUE_CONVERGENCE_CONSTRAINTS` | `uq_billing_invoices_provider_invoice`, `uq_billing_payments_provider_payment`, `uq_billing_webhook_provider_event`, `uq_payment_reconciliations_case_key`, `uq_stripe_invoices_invoice_id` |
| Bucket 2 — Concurrency Race | `UNIQUE_CONCURRENCY_RACE_EXACT` | `uq_billing_customer_provisions_org_provider`, `uq_subscriptions_provider_subscription`, `uq_org_scoped_active_billing_checkout_attempt`, `uq_checkout_attempts_provider_sub`, `uq_checkout_attempts_provider_payment`, `uq_org_active_checkout_attempt` |
| Bucket 3 — Permanent Integrity | `UNIQUE_PERMANENT_CONSTRAINTS` | `uq_org_active_subscription`, `uq_subscriptions_provider_customer`, `uq_billing_payments_checkout_attempt`, `uq_billing_checkout_org_idempotency`, `uq_bca_composite_identity`, `uq_reconciliation_event_sub`, `uq_billing_customer_provisions_provider_customer`, `uq_org_active_billing_period` |
| Speculative prefix | `UNIQUE_CONCURRENCY_RACE_PREFIXES` | `()` (empty tuple) |

### 4.2 Key Constraint Verifications

| Constraint | Expected Bucket | Actual | Pass? |
|---|---|---|---|
| `uq_billing_webhook_provider_event` | Bucket 1 | Bucket 1 | ✓ |
| `uq_payment_reconciliations_case_key` | Bucket 1 | Bucket 1 | ✓ |
| `uq_stripe_invoices_invoice_id` | Bucket 1 | Bucket 1 | ✓ |
| `uq_org_active_checkout_attempt` | Bucket 2 | Bucket 2 | ✓ |
| `uq_billing_customer_provisions_provider_customer` | Bucket 3 (not Bucket 2) | Bucket 3 | ✓ |
| `uq_reconciliation_event_sub` | Bucket 3 (replaces phantom) | Bucket 3 | ✓ |
| `uq_org_active_billing_period` | Bucket 3 (added) | Bucket 3 | ✓ |
| `uq_billing_reconciliations_event_incoming` (phantom) | Absent from all | Absent | ✓ |

### 4.3 Pairwise Disjointness

All three named sets (`UNIQUE_CONVERGENCE_CONSTRAINTS`, `UNIQUE_CONCURRENCY_RACE_EXACT`, `UNIQUE_PERMANENT_CONSTRAINTS`) are pairwise disjoint — verified programmatically. ✓

### 4.4 Bucket 3 (PERMANENT) Lookup Precedence

`classify_unique_violation_constraint()` checks `UNIQUE_PERMANENT_CONSTRAINTS` **first** (before Bucket 1 and 2) based on code inspection at line 895. This ensures the most restrictive classification wins in the event of future accidental overlap during schema evolution.

> [!NOTE]
> The correction plan named these Bucket 1/2/3; the implementation names them `CONVERGENCE`/`RACE_EXACT`/`PERMANENT`. The semantic mapping is correct and the enum values (`IDEMPOTENT_CONVERGENCE`, `CONCURRENCY_RACE`, `PERMANENT_INTEGRITY_CONFLICT`) are unambiguous.

---

## 5. RC-03 — Sanitization and Diagnostic Payload Hardening

**Verdict: PARTIAL PASS — Medium severity gap**

### 5.1 PostgreSQL URI with `/` in Password

Input: `postgresql://user:p/w@db.internal/app`  
Output: `postgresql://[REDACTED]@db.internal/app`  
Credential and password with `/` correctly redacted; hostname preserved. ✓

### 5.2 URI Query Parameter at `&` Boundary

Input: `https://api.example.com/v1?client_secret=sec123&other=ok`  
Output: `[REDACTED]`-ed `sec123`; `other=ok` preserved. ✓

### 5.3 13-Digit Millisecond Epoch Preservation

Input: `ts=1700000000123 acct_12345 rfnd_67890 card=2221001234567890`  
Result: `1700000000123` preserved; `acct_12345` and `rfnd_67890` shield tokens preserved; 16-digit PAN `2221001234567890` redacted. ✓

### 5.4 Cycle Detection in `sanitize_conflict_diagnostic_payload()`

Both cyclic dicts (`cyc['self'] = cyc`) and cyclic lists (`cycl.append(cycl)`) produce `[CYCLE_OMITTED]` or equivalent marker rather than infinite recursion. ✓

### 5.5 Idempotency

`sanitize_billing_error_message(sanitize_billing_error_message(msg)) == sanitize_billing_error_message(msg)` for representative inputs. ✓

### 5.6 `_KV_HEADER_SECRET_RE` — Underscore-Prefixed Key Names (FAIL)

> [!CAUTION]
> **RC-03 Gap — Medium Severity: Underscore-prefixed secret key names not sanitized in string messages**

The correction plan (Rev 1.2, §RC-03, test-case table, row "Underscore-prefixed KV secrets") explicitly requires:

```
razorpay_key_secret=mySuperSecret999  →  razorpay_key_secret=[REDACTED]
my_webhook_secret=mySuperSecret999    →  my_webhook_secret=[REDACTED]
```

The `_KV_HEADER_SECRET_RE` pattern uses `\b` (word boundary) before each key token:
```python
r"\b(x-razorpay-signature|...|key_secret|...|secret)\b([^\S\r\n]*[:=][^\S\r\n]*)"
```

Because `_` is a word character in Python's `re` module, `\bkey_secret\b` does **not** match in the string `razorpay_key_secret=val` — there is no word boundary between `razorpay_` and `key_secret`. The pattern correctly matches `key_secret=val` (bare key) and `razorpay-key-secret=val` (hyphen-separated), but NOT `razorpay_key_secret=val` (underscore-prefixed compound key name).

**Confirmed behaviour:**
```
sanitize_billing_error_message('razorpay_key_secret=mySuperSecret999 signature_verified=False')
# returns: 'razorpay_key_secret=mySuperSecret999 signature_verified=False'  ← NO REDACTION
```

**Test coverage gap:** The existing test `test_razorpay_stripe_webhook_secrets_and_signatures_redacted` (line 966–996) tests `key_secret=my_razorpay_key_secret` where `key_secret` is the **key** and `my_razorpay_key_secret` is the **value**. It does NOT test `razorpay_key_secret=value` where `razorpay_key_secret` is the key — which is the case described in the correction plan.

**Mitigation in `sanitize_conflict_diagnostic_payload()`:** `_is_sensitive_diagnostic_key('razorpay_key_secret')` returns `True` (the function tokenises on `_`, matching `secret`). Structured dict payloads are correctly sanitized. The gap only affects free-form string messages.

**Risk:** Webhook error messages containing `razorpay_key_secret=<live_key>` in string form would not have their values redacted before being stored in error logs or observability systems.

**Required correction:**  
Replace `\b` prefix with a suffix-sensitive pattern or use a negative lookbehind to allow word-char prefixes, e.g.:
```python
# Option A: match the token at the END of a word (preceded by non-word OR start-of-string)
r"(?<![A-Za-z0-9])(key_secret|webhook_secret|...)"
# Option B: add explicit compound prefixes to the alternation
r"\b(razorpay_key_secret|my_webhook_secret|key_secret|...)\b"
```
Additionally, add a regression test: `sanitize_billing_error_message('razorpay_key_secret=val') → 'razorpay_key_secret=[REDACTED]'`.

### 5.7 Diagnostic Payload Key Detection (Structured Dicts)

`sanitize_conflict_diagnostic_payload({'razorpay_key_secret': 'raw', 'custom_token': 'tok_1', 'customer_email': {'raw': 'x'}, 'idempotency_key': 'k1'})`:
- `razorpay_key_secret`: detected sensitive → stripped ✓
- `customer_email`: detected sensitive → stripped ✓
- `custom_token`: detected sensitive → stripped ✓
- `idempotency_key`: not sensitive → preserved ✓
- `lease_token_prefix`: not sensitive → preserved ✓

Structured dict path is correctly implemented for the same examples where string sanitization fails. ✓

---

## 6. RC-04 — SQLSTATE Classification

**Verdict: PASS**

### 6.1 Class 53 Resource Exhaustion → `RETRYABLE_WITH_ALERT`

| SQLSTATE | Expected | Actual Disposition | Category | requires_alert |
|---|---|---|---|---|
| `53100` (disk_full) | `RETRYABLE_WITH_ALERT` | `RETRYABLE_WITH_ALERT` ✓ | `db_resource_exhausted` ✓ | `True` ✓ |
| `53200` (out_of_memory) | `RETRYABLE_WITH_ALERT` | `RETRYABLE_WITH_ALERT` ✓ | `db_resource_exhausted` ✓ | `True` ✓ |
| `53300` (too_many_connections) | `RETRYABLE_WITH_ALERT` | `RETRYABLE_WITH_ALERT` ✓ | `db_resource_exhausted` ✓ | `True` ✓ |

`53300` is **absent** from `RETRYABLE_PG_SQLSTATES` (plain retryable set). ✓

### 6.2 `23001` (restrict_violation) → `PERMANENT`

`classify_pg_sqlstate('23001')`:  
`disposition = PERMANENT`, `requires_alert = False`. ✓

### 6.3 `53400` (configuration_limit_exceeded) → `UNKNOWN`

`classify_pg_sqlstate('53400')`:  
`disposition = UNKNOWN`. Correct — this is a known but unclassified edge case per the correction plan. ✓

### 6.4 Context-Aware `57014` (query_canceled)

| Scenario | Expected | Actual |
|---|---|---|
| `classify_pg_sqlstate('57014')` bare | `UNKNOWN` | `UNKNOWN` ✓ |
| Exception with message `canceling statement due to statement timeout` | `RETRYABLE_WITH_ALERT`, `db_statement_timeout` | `RETRYABLE_WITH_ALERT`, `db_statement_timeout` ✓ |
| Exception with message `canceling statement due to user request` | `UNKNOWN` | `UNKNOWN` ✓ |

### 6.5 Set Disjointness

`RETRYABLE_PG_SQLSTATES` = `{'40001', '40P01', '55P03'}`  
`ALERT_RETRYABLE_PG_SQLSTATES` = `{'53100', '53200', '53300'}`  
All three sets (retryable, alert-retryable, permanent) are pairwise disjoint. ✓

---

## 7. RC-05 — Traceback Frame Ordering and Path Sanitization

**Verdict: PASS**

### 7.1 `MAX_TRACEBACK_FRAMES`

```python
MAX_TRACEBACK_FRAMES = 64
```
✓

### 7.2 `max_frames <= 0` Guard

`extract_safe_traceback_locations(exc, max_frames=0)` returns `[]`.  
`extract_safe_traceback_locations(exc, max_frames=-5)` returns `[]`. ✓

### 7.3 Innermost Frames

For a 14-frame deep call stack (`deep(13)` → … → `deep(0)` → `RuntimeError`):  
`extract_safe_traceback_locations(exc, max_frames=5)` returns exactly 5 frames.  
The last frame correctly references `deep` and `:34:deep` (the innermost call site). ✓

### 7.4 Path Sanitization in `_sanitize_traceback_filename()`

| Input | Expected | Actual |
|---|---|---|
| `D:\Projects\V1\Aptlyst-AI\apps\api\app\services\billing\webhook_errors.py` | `apps/api/...` prefix, no `D:` | `apps/api/app/services/billing/webhook_errors.py` ✓ |
| `/home/alice/secret/run.py` (unrecognized) | `run.py` (basename) | `run.py` ✓ |
| `<string>` (pseudo-filename) | `<string>` | `<string>` ✓ |

---

## 8. L-01 through L-03 — Latent Defect Dispositions

**Verdict: PASS**

### 8.1 L-01b — Whitespace `failure_category` Normalization

`BillingWebhookConflictError('msg', failure_category='')` → `failure_category = 'reconciliation_conflict'` (default). ✓  
`BillingWebhookValidationError('msg', failure_category='   ')` → `failure_category = 'validation'` (default). ✓

### 8.2 L-01a — Mid-Marker Truncation Safety

`sanitize_billing_error_message('A' * 1010 + ' [REDACTED_EMAIL]', max_chars=1024)`:  
Result length ≤ 1024. The `[REDACTED_EMAIL]` marker is entirely excluded (truncated before the `[` starts) rather than left dangling mid-marker. No unmatched `[` bracket in output. ✓

### 8.3 L-02 — `billing/__init__.py` Untouched

`app/services/billing/__init__.py`: 7505 bytes; content is the standard billing domain services import aggregator. The file was **not modified** by Stage 1 corrections — it is a tracked file and `git diff` shows no changes. ✓

### 8.4 L-03 — Bare `BillingWebhookError` Defaults

`BillingWebhookError()` → `failure_category = 'webhook_error'`. ✓  
`BillingWebhookError('some message')` → `failure_category = 'webhook_error'`. ✓

---

## 9. Test Evidence

### 9.1 Stage 1 Unit Suite

```
Command: python -B -m pytest -p no:cacheprovider tests/test_billing_webhook_errors.py
Result: 158 passed in 1.80s
Exit code: 0
```

**Test classes observed:**

| Class | Purpose |
|---|---|
| `TestBillingWebhookExceptionHierarchy` | Exception hierarchy, chaining, L-01b, L-03 |
| `TestDbErrorExtraction` | DB error extraction, chain traversal, L-03 edge cases |
| `TestUniqueViolationClassification` | Bucket 1/2/3/4 parametrized constraint coverage |
| `TestOtherSqlstateClassification` | SQLSTATE classification (non-23505) |
| `TestAlertRetryableSqlstates` | RC-04: 53100/53200/53300 alert-retryable |
| `TestBillingErrorSanitization` | RC-03, L-01a, RC-05 sanitization |
| `TestWebhookExceptionClassification` | RC-01: full 5-step classifier scenarios |

**Total test functions (AST-verified):** 92 test methods across 11 classes (3 helper classes, 8 test classes).

> [!WARNING]
> The test count of 158 exceeds the method count of 92 because parametrized tests expand at collection time. Each parametrized variant is counted as a separate test by pytest. This is expected and not a discrepancy.

### 9.2 Limitations

- **No PostgreSQL 16 coverage:** All tests are offline. No live database was contacted. SQLSTATE classifications are tested through mock/stub exceptions with `.sqlstate` / `.pgcode` attributes.
- **No Razorpay/Stripe live API calls** were made during this review.

---

## 10. Scope Verification

### 10.1 Files Modified (Expected)

All three authorized files are present as new untracked files only:

| File | Status | Authorized |
|---|---|---|
| `apps/api/app/services/billing/webhook_errors.py` | `??` (untracked, new) | ✓ |
| `apps/api/tests/test_billing_webhook_errors.py` | `??` (untracked, new) | ✓ |
| `docs/reports/D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md` | `??` (untracked, new, in `docs/reports/`) | ✓ |

### 10.2 Files That Must Remain Untouched

| File | Expected State | Verified |
|---|---|---|
| `app/services/billing/__init__.py` | Tracked, unmodified | `git diff` empty ✓ |
| `app/services/billing/stripe/` | Tracked, unmodified | `git diff` empty ✓ |
| Webhook workers and handlers | Tracked, unmodified | `git diff` empty ✓ |
| CI workflows | Tracked, unmodified | `git diff` empty ✓ |
| Database models and migrations | Tracked, unmodified | `git diff` empty ✓ |

### 10.3 Sibling Worktrees

| Worktree | Branch | HEAD | Modified |
|---|---|---|---|
| `D:\Projects\V1\Aptlyst-AI` | `main` | `027ab7a` | CRLF warnings only (no staged changes) ✓ |
| `D:\Projects\V1\Aptlyst-AI-pg16-validation` | `validation/postgres16-ci-hardening` | `c932cc4` | Clean ✓ |

---

## 11. Findings Summary

| ID | Severity | Requirement | Description | Status |
|---|---|---|---|---|
| F-01 | **Medium** | RC-03 | `_KV_HEADER_SECRET_RE` word-boundary anchor (`\b`) prevents sanitization of `razorpay_key_secret=val` and other underscore-prefixed compound secret key names in free-form string messages. String `sanitize_billing_error_message('razorpay_key_secret=live_key')` returns input unchanged. Test suite does not cover this case. | **OPEN** |
| F-02 | Low | RC-02 | Plan's "Bucket 3" label maps to `UNIQUE_PERMANENT_CONSTRAINTS` / `PERMANENT_INTEGRITY_CONFLICT` enum in implementation. No semantic error — naming is different, semantics correct. | Closed (by convention) |
| F-03 | Low | RC-05 | Classify_webhook_exception result for `KeyboardInterrupt` is a re-raise (by design) — initial review script misidentified this as a failure. Confirmed correct per Step 1 spec. | Closed (confirmed correct) |

---

## 12. Required Correction for F-01

> [!IMPORTANT]
> The following correction is required before Stage 1 can be fully closed.

**File:** `apps/api/app/services/billing/webhook_errors.py`  
**Location:** `_KV_HEADER_SECRET_RE` (lines 159–170)

**Problem:** `\b` before the key name alternation prevents matching compound names where the secret token (`key_secret`, `secret`, etc.) is preceded by underscore-character prefixes.

**Minimum fix:** Add explicit compound names to the alternation, **before** their plain versions, so the full prefix is part of the matched group and the word boundary fires at the true start:

```python
_KV_HEADER_SECRET_RE = re.compile(
    r"\b("
    r"x-razorpay-signature|x_razorpay_signature|razorpay-signature|razorpay_signature|"
    r"stripe-signature|stripe_signature|webhook_signature|x-razorpay-secret|"
    # RC-03 fix: add compound underscore variants BEFORE bare tokens:
    r"razorpay_key_secret|my_webhook_secret|"
    r"webhook_secret|webhook-secret|key_secret|api_secret|api_key|api-key|"
    r"client_secret|access_token|refresh_token|id_token|auth_token|bearer_token|"
    r"session_token|authorization|x-api-key|signature|secret"
    r")\b([^\S\r\n]*[:=][^\S\r\n]*)"
    r"(?!(?:\s|True\b|False\b|true\b|false\b|None\b|null\b|\[REDACTED|(?:Basic|Bearer)[^\S\r\n]+\[REDACTED))"
    r"('(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"|(?:t=\d+,[^\S\r\n]*)?(?:v\d+=[0-9A-Za-z+/=]+(?:,[^\S\r\n]*)?)+|[^\s,;&}\]]+)",
    re.IGNORECASE,
)
```

Alternatively, replace the prefix `\b` with a negative lookbehind for word characters:
```python
r"(?<![A-Za-z0-9_])(key_secret|secret|...)"
```
This would match anywhere the token appears at a "true" word-start position, including after `razorpay_`.

**Required regression test:**
```python
def test_rc03_underscore_prefixed_key_names_redacted():
    msg = 'razorpay_key_secret=mySuperSecret999 my_webhook_secret=abc123 signature_verified=False'
    cleaned = sanitize_billing_error_message(msg)
    assert 'mySuperSecret999' not in cleaned
    assert 'abc123' not in cleaned
    assert 'signature_verified=False' in cleaned  # boolean diagnostic preserved
    assert sanitize_billing_error_message(cleaned) == cleaned  # idempotent
```

---

## 13. Stage 1 Closure Recommendation

**Verdict: ACCEPT WITH REQUIRED CORRECTIONS**

Stage 1 may be closed after F-01 is corrected:

| Requirement | Status |
|---|---|
| RC-01 — Conservative exception classification | ✅ PASS |
| RC-02 — Schema-exact 23505 buckets | ✅ PASS |
| RC-03 — Sanitization hardening | ⚠️ PARTIAL — string underscore-KV gap (F-01) |
| RC-04 — SQLSTATE classification | ✅ PASS |
| RC-05 — Traceback frame ordering and path sanitization | ✅ PASS |
| L-01a — Mid-marker truncation | ✅ PASS |
| L-01b — Whitespace failure_category normalization | ✅ PASS |
| L-02 — `billing/__init__.py` scope discipline | ✅ PASS |
| L-03 — Bare BillingWebhookError defaults | ✅ PASS |

The implementation is architecturally sound. The exception hierarchy, 5-step classifier, causal-first DB chain traversal, SQLSTATE mappings, constraint bucket taxonomy, cycle-safe diagnostic sanitization, and traceback frame extraction are all correctly implemented and tested. The single open gap (F-01) is bounded in scope and does not affect the structured dict sanitization path used by the diagnostic payload system.

**Authorized next step:** Implement F-01 correction, add regression test, re-verify 158+ tests pass, then proceed to Stage 1 closure.

**Stage 2 is NOT authorized until F-01 is resolved.**
