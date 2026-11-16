# Aptlyst AI — D-04A Stage 1: Independent Code Review

**Document ID:** `D2_D04A_STAGE1_INDEPENDENT_REVIEW.md`  
**Review Mode:** **READ-ONLY INDEPENDENT AUDIT** *(no application code, tests, models, migrations, workflows, Git refs, or sibling worktrees modified)*  
**Target Worktree:** [`D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16)  
**Audited Branch / Commit:** `implementation/d04a-stage1-exception-foundation` (branched from `497852e19360d85787107dbae6921519074f0773`)  
**Audited Artifacts:**
- [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py) (`947` lines)
- [`test_billing_webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py) (`919` lines)
- [`D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md) (`164` lines)

---

## 1. Executive Verdict

**Verdict: `ACCEPT WITH REQUIRED CORRECTIONS`**

The Stage 1 foundation in [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py) establishes a cleanly structured, stdlib-only, circular-import-free exception hierarchy with disjoint lease error semantics, cycle-safe exception-chain extraction, ReDoS-resistant sanitization, and strict Stage 1 scope isolation.

However, the implementation **cannot be promoted directly to Stage 2 or Stage 4 without targeted corrections**. Independent inspection and offline behavioral verification uncovered **2 High** defects and **3 Medium** defects (plus **3 Low** items):

1. **`H-01` (ED-01 Policy Gap):** No non-DB/programming-error classification contract is exposed yet; `TypeError`, `AttributeError`, `NameError`, and similar defects return the same generic `DbRetryDisposition.UNKNOWN` / `"db_unknown"` result as arbitrary unknown exceptions, failing the approved ED-01 immediate-quarantine vs. bounded-retry contract.
2. **`H-02` (`23505` Classification Defects):** Wildcard prefix matching in `classify_unique_violation_constraint()` runs ahead of exact Bucket 3 rules, misclassifies cross-tenant `uq_billing_customer_provisions_provider_customer` as a silent Bucket 2 `RETRYABLE` race instead of a permanent conflict, auto-retries hypothetical future prefixed constraints without alerting, and lists a non-existent `uq_billing_reconciliations_event_incoming` constraint while omitting real schema constraints.
3. **`M-01` (Sanitization & Diagnostic Payload Gaps):** Underscore-prefixed secret keys (`razorpay_key_secret=...`) bypass `_KV_HEADER_SECRET_RE`; PostgreSQL URI passwords containing `/` corrupt redaction; 13-digit millisecond Unix epochs are corrupted as PANs; non-PG URL query secrets strip trailing `&` query parameters; and `sanitize_conflict_diagnostic_payload()` raises `RecursionError` on cyclic lists, bypasses `max_depth` inside nested lists, and misses non-exact secret key names.
4. **`M-02` (`SQLSTATE` Coverage Gaps):** `57014` (`query_canceled` / PostgreSQL `statement_timeout`), `53100` (`disk_full`), `53200` (`out_of_memory`), and `23001` (`restrict_violation`) fall through to `UNKNOWN` (`should_retry=False`).
5. **`M-03` (Traceback Frame Order & Path Exposure):** `extract_safe_traceback_locations()` calls `traceback.extract_tb(tb, limit=max_frames)` with positive `limit`, keeping the oldest outer caller frames and discarding the actual inner crash site on stacks deeper than `max_frames`, while emitting full host filesystem paths.

Stage 1 must receive a corrective patch and re-verification on `implementation/d04a-stage1-exception-foundation` before **D-04A Stage 2** begins.

---

## 2. Repository and Branch Verification

Verified via direct read-only inspection on [`D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16) and sibling worktrees:

| Check | Verified State |
|---|---|
| **Target Worktree** | `D:\Projects\V1\Aptlyst-AI-d2-stage1-pg16` |
| **Active Implementation Branch** | `implementation/d04a-stage1-exception-foundation` @ `497852e19360d85787107dbae6921519074f0773` |
| **Original Stage 1 Branch** | `validation/d2-stage1-customer-pg16` @ `497852e19360d85787107dbae6921519074f0773` *(untouched)* |
| **Tracked Modifications (`git diff --stat`)** | Empty (`0` modified, deleted, or renamed tracked files) |
| **Staged Changes (`git diff --cached --stat`)** | Empty (`0` staged changes; nothing committed or pushed) |
| **Preserved Root Reports** | `STEP_5_2D_3_3B_1E_Z1` through `Z8` `*_REPORT.md` preserved intact |
| **Preserved `docs/reports/` Baseline** | `D2_STAGE1_FORMAL_CLOSURE_REPORT.md`, `D2_STAGE2_READINESS_AUDIT.md`, `D2_D04_RAZORPAY_WEBHOOK_SAFETY_INVESTIGATION.md`, `D2_D04A_EXCEPTION_ARCHITECTURE_DESIGN.md`, `D2_D04A1_IMPLEMENTATION_BLUEPRINT.md` |
| **Sibling Worktree — Main** | `D:\Projects\V1\Aptlyst-AI` on `main` @ `027ab7ad3ee8a02fb20823a4f743e29b03c0f248` *(untouched; `13` pre-existing dirty tracked files preserved)* |
| **Sibling Worktree — PG16 Hardening** | `D:\Projects\V1\Aptlyst-AI-pg16-validation` on `validation/postgres16-ci-hardening` @ `c932cc480f82b2eea11c7616bf9290e255cffd31` *(untouched; `0` tracked diffs)* |

### Integrity Verification of Stage 1 Files

| File | Verified Lines | Verified SHA-256 |
|---|---:|---|
| [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py) | `947` | `3CAF5313E5E56F85B6BD3485FA5A8782BD8F313EAEE93A9E857B35B2E07A493B` |
| [`test_billing_webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py) | `919` | `487C4D84E06E4C429B40ACFD4850078A768C0009558D19DECE7BDD220D839B14` |
| [`D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md) | `164` | Present in `docs/reports/` prior to this review |

---

## 3. Findings Grouped by Severity

### High Severity

#### `H-01` — ED-01 Policy Alignment Gap: No Classification Signal Distinguishing Programming Errors from Unknown Exceptions
- **Location:** [`webhook_errors.py#L417-L423`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L417-L423), [`#L861-L913`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L861-L913)
- **Root Cause:** [`D2_D04A1_IMPLEMENTATION_BLUEPRINT.md#L20, #L61-L78`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A1_IMPLEMENTATION_BLUEPRINT.md#L20) recommended ED-01 Option A (bounded retry for all unmapped Python exceptions with `_UNEXPECTED_FAIL_FAST_TYPES = ()`), while noting unresolved uncertainty around Option B. Under the approved **ED-01 policy**, clearly identified programming errors (`TypeError`, `AttributeError`, `NameError`, and similar defects) **must be immediately quarantined**, while unknown exceptions retain bounded retries, and **Stage 1 must expose a stable, unambiguous classification signal that Stage 4 can consume**.
- **Empirically Observed Behavior:** [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py) exposes only DB/SQLSTATE classification (`classify_db_exception`, `classify_pg_sqlstate`, `DbRetryDisposition`). Calling `classify_db_exception()` on `TypeError("bad type")`, `AttributeError("missing attr")`, `NameError("undef")`, and `KeyError("k")` produces **identical** output (`disposition=DbRetryDisposition.UNKNOWN`, `failure_category="db_unknown"`, `sqlstate=None`, `should_retry=False`, `is_permanent=False`, `requires_alert=True`, `is_known=False`).
- **Impact:** Stage 4 has no Stage 1 classification contract separating deterministic Python code defects from genuinely unknown runtime failures and would either retry programmer bugs 5 times or duplicate ad-hoc `isinstance` checks in the worker.

#### `H-02` — `23505` Prefix Precedence Bug, Cross-Tenant False-Negative (`uq_billing_customer_provisions_provider_customer`), and Phantom Constraint Name
- **Location:** [`webhook_errors.py#L440-L457`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L440-L457), [`#L699-L718`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L699-L718); [`test_billing_webhook_errors.py#L412-L451`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py#L412-L451)
- **Three Compounding Schema / Logic Defects Verified Against [`payment.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py) and [`subscription.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/subscription.py):**
  1. **Dangerous Cross-Tenant Misclassification:** `UNIQUE_CONCURRENCY_RACE_PREFIXES` (`"uq_billing_customer_provisions_"`, `#L446`) matches **`uq_billing_customer_provisions_provider_customer`** ([`payment.py#L848-L856`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L848-L856)) and silently classifies it as Bucket 2 `CONCURRENCY_RACE` (`RETRYABLE`, `requires_alert=False`). That partial unique index enforces 1:1 global uniqueness of `(provider, merchant_account_id, environment, provider_customer_id)` so two organizations cannot claim the same gateway customer ID (confirmed by [`customer.py#L434-L447`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/customer.py#L434-L447) and mirrored by `uq_subscriptions_provider_customer` at [`subscription.py#L209-L216`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/subscription.py#L209-L216), which Stage 1 places in Bucket 3 Permanent). Only **`uq_billing_customer_provisions_org_provider`** ([`payment.py#L841-L847`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L841-L847)) is a same-organization concurrency race.
  2. **Prefix Wildcard Evaluated Before Exact Permanent Rules & Speculative Auto-Allowlisting:** In [`classify_unique_violation_constraint()` #L707-L718](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L707-L718), `UNIQUE_CONCURRENCY_RACE_PREFIXES` (`"uq_billing_customer_provisions_"`, `"uq_checkout_attempts_provider_"`) is checked **before** `UNIQUE_PERMANENT_CONSTRAINTS`, allowing broad prefixes to shadow explicit permanent names and silently route invented future index names (tested verbatim in [`test_billing_webhook_errors.py#L417, #L422`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py#L417)) to Bucket 2 without `requires_alert=True` instead of failing safe into Bucket 4 `UNMAPPED_UNIQUE_VIOLATION`.
  3. **Phantom & Omitted Constraints:** `UNIQUE_PERMANENT_CONSTRAINTS` (`#L456`) lists `"uq_billing_reconciliations_event_incoming"`, which does **not** exist in any model or Alembic migration. The real legacy reconciliation constraint is **`uq_reconciliation_event_sub`** ([`subscription.py#L480-L484`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/subscription.py#L480-L484), [`20261030_1600_d4e5f6a1b2c3#L113`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/alembic/versions/20261030_1600_d4e5f6a1b2c3_add_stripe_checkout_and_reconciliation.py#L113)). Meanwhile, `uq_billing_webhook_provider_event` ([`payment.py#L128-L134`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L128-L134)) and `uq_payment_reconciliations_case_key` ([`payment.py#L750-L756`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L750-L756)) are omitted from Buckets 1–3 and drop into Bucket 4. *(Both defects originated in [`D2_D04A1_IMPLEMENTATION_BLUEPRINT.md#L131-L132`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A1_IMPLEMENTATION_BLUEPRINT.md#L131-L132) and were copied into Stage 1 code.)*

---

### Medium Severity

#### `M-01` — Sanitization Bypass, Corruption, and Crash Defects
Five empirically verified edge cases affect [`sanitize_billing_error_message()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L265-L329) and [`sanitize_conflict_diagnostic_payload()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L332-L394):

1. **Underscore-Prefixed KV Secret Bypass (`#L152-L163`):** `_KV_HEADER_SECRET_RE` anchors with leading `\b`. Because `_` is a word character (`\w`), keys like `razorpay_key_secret=mySuperSecret999` or `my_webhook_secret=mySuperSecret999` do **not** have a word boundary before `key_secret`/`webhook_secret`, so raw plaintext secrets leak completely unredacted.
2. **PostgreSQL URI `/` Password Leak & Comma-Separated DSN Corruption (`#L131-L134`):** Excluding `/` from the userinfo character class (`[^/?#\s'\"]+`) causes `postgresql://u:p/w@db.internal/app` to escape userinfo matching, redact `@db.internal` via `@` + email matching or leave `u:p` exposed (`postgresql://u:p/[REDACTED_EMAIL]/app`), and conversely an unslashed DSN mention such as `postgresql://dbhost:5432,email=bob@example.com` matches greedily across spaces/commas up to `@example.com` -> `postgresql://[REDACTED]@example.com`.
3. **13-Digit Millisecond Epoch Mangling & Entity Prefix Gap (`#L177-L188`):** `_SHIELD_PATTERNS` (`#L187`) only shields 10-digit epoch seconds (`1500000000`–`2499999999`), causing 13-digit millisecond timestamps (e.g. `ts=1700000000123`) to be redacted into `ts=[REDACTED_PAN]`. Separately, `#L184` shields `acc_` rather than Razorpay Connect `acct_` / `rfnd_` prefixes.
4. **Non-PG URL Query Parameter Over-Redaction (`#L135-L138`, `#L157-L162`):** Query secret names present in `_KV_HEADER_SECRET_RE` but absent from `_URI_QUERY_SECRET_RE` (e.g., `https://api.example.com/v1?client_secret=sec123&other=ok`) bypass `_URI_QUERY_SECRET_RE` and match `_KV_HEADER_SECRET_RE`'s `[^\s,;}\]]+`, swallowing `&other=ok` into `[REDACTED]`.
5. **`sanitize_conflict_diagnostic_payload()` Recursion Crash, Depth Bypass & Weak Key Blocklist (`#L342-L394`):**
   - List/tuple elements call `_clean_value()` recursively without depth decrement or cycle tracking (`visited: set[int]`). Passing a self-referential list (`a = []; a.append(a)`) raises uncaught **`RecursionError`**, and nested lists (`{"x": [[[{"secret": "sk_123"}]]]}`) bypass `max_depth=1`.
   - `disallowed_keys` (`#L342-L367`) uses exact string equality only; compound key names like `razorpay_key_secret`, `custom_token`, or `{"customer_email": {"raw": "x"}}` survive unstripped (`custom_token="tok_not_prefixed_12345"` leaks plaintext).

#### `M-02` — PostgreSQL `SQLSTATE` Coverage Gaps (`57014`, `53100`/`53200`, `23001`)
- **Location:** [`webhook_errors.py#L459-L479`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L459-L479)
- **Details:**
  - `RETRYABLE_PG_SQLSTATE_PREFIXES` includes `"57P"` (operator shutdown) and misses PostgreSQL **`57014`** (`query_canceled`, emitted on `statement_timeout` or query cancel). Empirically, `classify_pg_sqlstate("57014")` returns `DbRetryDisposition.UNKNOWN` (`should_retry=False`), whereas statement/lock timeouts are transient retryable conditions under bounded retry.
  - Class `53` (`insufficient_resources`) maps only `"53300"` (`too_many_connections`), omitting `"53100"` (`disk_full`), `"53200"` (`out_of_memory`), and `"53400"` (`configuration_limit_exceeded`).
  - `PERMANENT_PG_SQLSTATES` maps `23502`, `23503`, `23514`, and `23505`, while omitting **`23001`** (`restrict_violation`), leaving `ON DELETE RESTRICT` integrity triggers classified as `UNKNOWN` (`should_retry=False` only without `is_permanent=True`). *(Note: [`23503`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L682-L707) covers FK checks on child insert/delete; explicit `23001` covers PostgreSQL `RESTRICT` triggers cleanly.)*

#### `M-03` — `extract_safe_traceback_locations()` Retains Outer Frames Instead of Crash Site & Leaks Host Paths
- **Location:** [`webhook_errors.py#L397-L409`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L397-L409)
- **Details:** Passing positive `limit=max_frames` to `traceback.extract_tb(exc.__traceback__, limit=max_frames)` returns the **first (outermost) `max_frames` frames** from the entrypoint (`uvicorn`/`pytest`/worker loop), discarding the innermost frames where the exception actually occurred. Empirically verified on a 14-frame stack (`outer_0 -> ... -> crash_13`): `extract_safe_traceback_locations(exc, max_frames=8)` returned frames `0..7` and omitted `crash_13` completely. Calling `traceback.extract_tb(exc.__traceback__, limit=-max_frames)` (or slicing `[-max_frames:]` when `max_frames > 0`, returning `[]` if `max_frames <= 0`) preserves the crash site. Additionally, emitting raw `frame.filename` leaks absolute host filesystem paths (`D:\Projects\...`) into logs instead of repo-relative paths or `Path(frame.filename).name`.

---

### Low Severity

#### `L-01` — Truncation Mid-Marker, Empty `failure_category` Fallback, and Single-Node `sanitized_message`
- **Location:** [`webhook_errors.py#L62`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L62), [`#L66-L69`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L66-L69), [`#L329`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L329)
- **Details:** `msg[:max_chars].strip()` can bisect a redaction token cleanly without leaking PII (verified empirically: `[REDACTED_EMAIL]` truncated to `[REDACTED_EMAI`), though ellipsis or marker-boundary trim is cleaner telemetry. Second, `self.failure_category = failure_category or self.default_failure_category` (`#L62`) ignores an explicit `""` or whitespace string instead of checking `failure_category if failure_category is not None and failure_category.strip() else ...`. Third, `BillingWebhookError.sanitized_message` formats only `self`, not chained `__cause__` context (acceptable for exception message string semantics, provided callers use `extract_db_error_details()`).

#### `L-02` — Dotted Package Import Executes `billing/__init__.py` Side Effects
- **Location:** [`billing/__init__.py#L1-L100`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/__init__.py#L1-L100)
- **Details:** While `webhook_errors.py` itself imports only stdlib modules (`collections`, `dataclasses`, `enum`, `re`, `traceback`, `typing`), Python evaluates `app/services/billing/__init__.py` whenever any caller executes `import app.services.billing.webhook_errors`, pulling in `stripe` and SQLAlchemy models. This is a pre-existing package layout property (correctly disclosed in [`D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md#L137`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md#L137)) and does not affect normal app/worker execution or cause circular imports when Stage 2 imports `webhook_errors` inside [`webhook_engine.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/webhook_engine.py).

#### `L-03` — Test Suite Edge-Case Coverage Gaps
- **Location:** [`test_billing_webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py)
- **Details:**
  - `iter_db_exception_chain()` is never tested against an **acyclic** chain deeper than `max_nodes` (`>32` nodes) to verify `chain_depth == max_nodes` truncation.
  - `test_existing_provider_hierarchies_unmodified_in_stage1` (`#L250-L254`) omits `RazorpayClientError` ([`client.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/client.py)) and `RazorpayCustomerError` ([`customer.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/customer.py)).
  - Missing negative/edge tests for empty `BillingWebhookError()`, deep `>8`-frame tracebacks, cyclic/nested lists in `sanitize_conflict_diagnostic_payload()`, 13-digit ms epochs, underscore-prefixed KV keys, `&` query-param delimiter preservation, and case-insensitive `UNIQUE CONSTRAINT` fallback extraction.

---

## 4. Exception Hierarchy Assessment

Inspected at [`webhook_errors.py#L47-L115`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L47-L115):

```text
Exception
 └── BillingWebhookError                     (default_failure_category="webhook_error", is_retryable=False)
      ├── BillingWebhookRetryableError       (default_failure_category="retryable_failure", is_retryable=True)
      ├── BillingWebhookPermanentError       (default_failure_category="permanent_failure", is_retryable=False)
      │    ├── BillingWebhookValidationError    ("validation")
      │    ├── BillingWebhookScopeMismatchError ("scope_mismatch")
      │    ├── BillingWebhookConflictError      ("reconciliation_conflict")
      │    └── BillingWebhookUnknownEventError  ("unknown_event")
      └── BillingWebhookLeaseError           (default_failure_category="stale_lease", is_retryable=False)
```

| Assessment Criterion | Result | Evidence |
|---|---|---|
| **Hierarchy & Coherence** | **Strong** | Minimal `8`-class tree matching design topology without over-proliferation; clear `is_retryable` and `default_failure_category` defaults. |
| **Disjoint Lease Semantics** | **Pass** | [`BillingWebhookLeaseError`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L110-L115) inherits directly from `BillingWebhookError`, completely disjoint from both `BillingWebhookRetryableError` and `BillingWebhookPermanentError`. |
| **Structured Diagnostics** | **Pass** *(minor `L-01`)* | Captures `failure_category`, optional `error_code`, and optional `constraint_name`; exposes safe [`sanitized_message`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L66-L69). |
| **Chaining Compatibility** | **Pass** | Standard `Exception` subclassing preserves `__cause__` / `__context__` seamlessly across `raise ... from exc` wrappers. |
| **Zero Circular Imports** | **Pass** | AST verification confirms strictly Python stdlib imports (`collections`, `collections.abc`, `dataclasses`, `enum`, `re`, `traceback`, `typing`). Safe for import by `razorpay`, `stripe`, and workers inside an initialized package environment (`L-02` noted for bare standalone imports). |

---

## 5. SQLSTATE and Constraint-Classification Assessment

### 5.1 Exception-Chain Traversal & Diagnostic Safety
- [`iter_db_exception_chain()` #L568-L601](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L568-L601) traverses `(orig first, then exc, nested .orig, __cause__, __context__)` via BFS deque with `visited: set[int]` deduplication and `max_nodes` cap (`default 32`, clamped `>= 1`).
- [`_safe_getattr()` #L521-L528](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L521-L528) and [`_safe_str()` #L531-L536](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L531-L536) guard broken descriptor proxies and unprintable exception objects cleanly.
- [`_extract_fallback_constraint_from_text()` #L557-L565](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L557-L565) scrubs `[parameters: ...]` via `_redact_sqlalchemy_parameters()` **before** running `_UNIQUE_CONSTRAINT_FALLBACK_RE` (`unique constraint "..."`) so spoofed attacker payloads in SQL parameters cannot impersonate Bucket 1/2 constraints.
- [`DbRetryDisposition` #L417-L423](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L417-L423) (`retryable`, `retryable_with_alert`, `permanent`, `unknown`) stays strictly decoupled from worker queue statuses (`pending`, `processed`, `failed`, `quarantined`).

### 5.2 Complete Schema Audit of Unique Constraints vs. Implemented `23505` Buckets

Every unique constraint and unique index across [`payment.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py), [`subscription.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/subscription.py), and all `14` Alembic migrations was cross-checked against [`webhook_errors.py#L435-L457`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L435-L457):

| Schema Constraint / Index | Verified Schema Location | Stage 1 Result | Independent Assessment & Required Fix (`H-02`) |
|---|---|---|---|
| `uq_billing_invoices_provider_invoice` | [`payment.py#L432-L438`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L432-L438) | Bucket 1 (`IDEMPOTENT_CONVERGENCE`) | **Correct** |
| `uq_billing_payments_provider_payment` | [`payment.py#L587-L593`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L587-L593) | Bucket 1 (`IDEMPOTENT_CONVERGENCE`) | **Correct** |
| `uq_billing_webhook_provider_event` | [`payment.py#L128-L134`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L128-L134) | Bucket 4 (`UNMAPPED`, alerts) | **Gaps in Bucket 1:** Should be explicit in Bucket 1 (even though normally caught at HTTP ingress) |
| `uq_billing_customer_provisions_org_provider` | [`payment.py#L841-L847`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L841-L847) | Bucket 2 (via prefix `uq_billing_customer_provisions_`) | **Should be exact in Bucket 2** *(drop wildcard prefix)* |
| **`uq_billing_customer_provisions_provider_customer`** | [`payment.py#L848-L856`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L848-L856) | **Bucket 2 `RETRYABLE` (`requires_alert=False`)** | **BUG (`H-02`): Move to Bucket 3 `PERMANENT_INTEGRITY_CONFLICT`** *(cross-org customer ID collision; mirrors `uq_subscriptions_provider_customer`)* |
| `uq_subscriptions_provider_subscription` | [`subscription.py#L217-L225`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/subscription.py#L217-L225) | Bucket 2 (`CONCURRENCY_RACE`) | **Correct** |
| `uq_org_scoped_active_billing_checkout_attempt` | [`payment.py#L262-L272`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L262-L272) | Bucket 2 (`CONCURRENCY_RACE`) | **Correct** |
| `uq_checkout_attempts_provider_sub` | [`payment.py#L274-L282`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L274-L282) | Bucket 2 (via prefix `uq_checkout_attempts_provider_`) | **Make exact in Bucket 2** *(drop prefix)* |
| `uq_checkout_attempts_provider_payment` | [`payment.py#L283-L291`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L283-L291) | Bucket 2 (via prefix `uq_checkout_attempts_provider_`) | **Make exact in Bucket 2** *(drop prefix)* |
| `uq_org_active_subscription` | [`subscription.py#L203-L208`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/subscription.py#L203-L208) | Bucket 3 (`PERMANENT`) | **Correct** |
| `uq_subscriptions_provider_customer` | [`subscription.py#L209-L216`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/subscription.py#L209-L216) | Bucket 3 (`PERMANENT`) | **Correct** |
| `uq_billing_payments_checkout_attempt` | [`payment.py#L632-L637`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L632-L637) | Bucket 3 (`PERMANENT`) | **Correct** |
| `uq_billing_checkout_org_idempotency` | `payment.py` (`BillingCheckoutAttempt`) | Bucket 3 (`PERMANENT`) | **Correct** |
| `uq_bca_composite_identity` | [`payment.py#L253-L261`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L253-L261) | Bucket 3 (`PERMANENT`) | **Correct** |
| `uq_billing_reconciliations_event_incoming` *(Stage 1 literal)* | **Does NOT exist in codebase** | Bucket 3 (`PERMANENT`) | **BUG (`H-02`): Remove phantom name**; replace with real **`uq_reconciliation_event_sub`** ([`subscription.py#L483`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/subscription.py#L483)) and add **`uq_payment_reconciliations_case_key`** ([`payment.py#L755`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/models/payment.py#L755)) *(or explicitly classify un-upserted `case_key` collisions into Bucket 2 `CONCURRENCY_RACE` if outside `ON CONFLICT`, never letting it conflict with Bucket 4)* |

---

## 6. Sanitization and Security Assessment

Evaluated against [`webhook_errors.py#L121-L409`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L121-L409):

| Security / Redaction Dimension | Assessment | Verified Details |
|---|---|---|
| **Pipeline Architecture & Order** | **Strong** | Strips `[parameters: ...]` cleanly first (bracket- and quote-aware), redacts PG URIs / query secrets / `libpq` params / auth headers / gateway keys / KV secrets -> emails -> explicit `+`/labeled phones -> shields UUIDs / ISO timestamps / prefixed entity IDs / DB constraints / 10-digit epochs -> formatted local phones & PANs -> restores tokens. |
| **Non-Destructive Preservation** | **Strong** | Verified preservation of UUIDs (`550e8400-...`, digit-only UUIDs), `SQLSTATE` codes, `uq_*`/`fk_*`/`ix_*`/`pk_*`, entity IDs (`sub_*`, `pay_*`, `inv_*`, `cust_*`, `order_*`, `evt_*`), correlation IDs (`corr_*`, `req_*`, `trace-*`), ISO timestamps, 10-digit epochs, and `signature_verified=False`. |
| **Idempotency** | **Pass** | Verified `f(x) == f(f(x)) == f(f(f(x)))` across composite messages containing credentials, keys, signatures, emails, phones, PANs, and bound parameters. |
| **ReDoS Resistance** | **Pass** | Empirically benchmarked on `50k`–`150k`-char adversarial strings: `stripe_sig_long` (`1.27ms`), `pan_long` (`42.89ms`), `params_nested` (`4.80ms`), `uri_at_many` (`0.12ms`), `huge_ws` (`119.16ms`). Linear scaling with zero catastrophic backtracking. |
| **Identified Sanitization Defects** | **Must Fix (`M-01`, `M-03`)** | Underscore-prefixed key bypass (`razorpay_key_secret=...`), `/` in PG URI passwords + unslashed comma DSNs, 13-digit ms epoch PAN mangling, non-PG query `&` over-redaction, cyclic list `RecursionError` + nested list depth bypass + exact-key misses in `sanitize_conflict_diagnostic_payload()`, and outer-frame truncation / host path exposure in `extract_safe_traceback_locations()`. |

---

## 7. ED-01 Policy Alignment

### Approved ED-01 Policy vs. Current Stage 1 State

The approved **ED-01** engineering policy requires:
1. Clearly identified programming errors (`TypeError`, `AttributeError`, `NameError`, and similar defects) **must be immediately quarantined** by the future worker.
2. Unknown exceptions retain bounded retries.
3. Programming errors must **not** be silently classified as ordinary transient failures.
4. **Stage 1 must expose a stable, unambiguous classification signal that Stage 4 can consume**, without pulling worker-queue transitions into Stage 1.

In the current Stage 1 implementation:
- [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py) reflects the earlier Blueprint Option A posture ([`D2_D04A1_IMPLEMENTATION_BLUEPRINT.md#L68, #L78`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A1_IMPLEMENTATION_BLUEPRINT.md#L68)), which treated all unmapped Python exceptions uniformly.
- `classify_db_exception()` returns `DbErrorClassification(disposition=DbRetryDisposition.UNKNOWN, failure_category="db_unknown", should_retry=False, is_permanent=False, requires_alert=True, is_known=False)` identically for `TypeError`, `AttributeError`, `NameError`, and unmapped runtime exceptions like `KeyError` or custom library errors.

### Required Pure Stage 1 Declarative Contract

Stage 1 must remain pure data/classification (zero worker `status`, `attempts`, or backoff logic), while exposing an explicit **non-DB / runtime exception classification API** alongside the DB classifier (or unifying via a clear helper) so Stage 4 needs zero ad-hoc `isinstance` branching:

- Define a canonical tuple/frozenset of unambiguous Python programming-error types:
  - **`PROGRAMMING_ERROR_TYPES`**: `(TypeError, AttributeError, NameError, UnboundLocalError, NotImplementedError, AssertionError, ZeroDivisionError, RecursionError, SyntaxError, IndentationError)` *(preserving `KeyError`/`ValueError` outside immediate-quarantine unless explicitly wrapped as `BillingWebhookValidationError`, avoiding false quarantines when external-payload lookups raise unmapped errors)* — adjust carefully if policy expects strict inclusion rules and document explicitly.
- Expose a dedicated enum / classifier function in [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py) (e.g., `UnexpectedExceptionDisposition` with `IMMEDIATE_QUARANTINE` vs. `BOUNDED_RETRY`, or `classify_unexpected_exception(exc)` / `is_programming_error(exc)` returning a structured frozen dataclass containing `is_programming_error`, `should_quarantine_immediately`, `should_retry`, `requires_alert`, `failure_category`, e.g., `"programming_error"` vs. `"unexpected_internal_error"`, and `error_type` + sanitized summary).
- Ensure DB/SQLSTATE chains take precedence when an exception wraps a database or `BillingWebhookError` failure so `TypeError`/`AttributeError` raised inside broken driver proxies still inspects DB context cleanly before falling back to runtime classification.

---

## 8. Test-Quality Assessment and Validation Evidence

### 8.1 Audit of `test_billing_webhook_errors.py` (`88` Tests)
- **Strengths:** Fast, deterministic, strictly offline (`0` PG/network calls), inspects `webhook_errors.py` imports via `ast`, verifies exception chaining (`raise ... from exc`), broken descriptors/unprintable `__str__`, cyclic `orig`/`__cause__`/`__context__`, spoofed parameter strings, composite redaction idempotency, and decoupling of `DbRetryDisposition` values from worker queue statuses.
- **Test Quality Defects Found:**
  1. [`test_bucket_2_representative_concurrency_race_constraints` #L416-L422](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py#L416-L422) actively locks in the `H-02` bugs (`uq_billing_customer_provisions_provider_customer`, `uq_billing_customer_provisions_custom_future_idx`, `uq_checkout_attempts_provider_order`).
  2. [`test_bucket_3_every_named_permanent_integrity_conflict` #L449](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py#L449) asserts the phantom constraint `uq_billing_reconciliations_event_incoming`.
  3. Coverage gaps documented in `L-03` (`max_nodes` truncation on deep acyclic chains, `RazorpayClientError`/`RazorpayCustomerError`, cyclic/nested diagnostic lists, 13-digit ms epochs, underscore KV keys, `&` query-param delimiter preservation, deep `>8`-frame tracebacks, and ED-01 programming-error vs. unknown-exception assertions).

### 8.2 Provenance of Validation Evidence

> [!IMPORTANT]
> **Zero PostgreSQL 16 integration coverage exists in Stage 1.** All passing results below come strictly from offline unit and mock-session tests. Real `asyncpg`/PostgreSQL 16 error wire formats, trigger `P0001` behavior, `23505`/`23503` constraint names, and worker transaction semantics are **not** exercised in Stage 1.

| Evidence Category | Specific Artifacts / Commands / Observations | Status |
|---|---|---|
| **(a) Independently Verified from Saved Execution Logs & Read-Only Checks** | - Saved log `.system_generated/steps/28163/output.txt`: `test_billing_webhook_errors.py` -> **`88 passed, 0 skipped` in `3.44s`**.<br>- Saved log `.system_generated/steps/28165/output.txt`: `7` unmodified Razorpay/Stripe files -> **`124 passed, 0 skipped` in `8.65s`**.<br>- Saved log `.system_generated/tasks/task-28170.log`: `7` CI Step 13 offline regression files -> **`375 passed, 0 skipped` in `55.79s`**.<br>- Saved secret grep `.system_generated/steps/28172`: `0` matches across the first `2` test logs (`task-28170.log` verified to contain pytest progress dots + summary only).<br>- Read-only `ast` check across all `1 + 7 + 7 = 15` test files confirmed `0` cross-imported `test_*` functions and `0` `conftest.py` files in `apps/api/`.<br>- Read-only offline Python checks reproduced ReDoS linear timings, `H-01`, `H-02`, `M-01`, `M-02`, `M-03`, and `L-01` behaviors directly. | Verified *(logs from Stage 1 session; full pytest suites were not re-executed during this review)* |
| **(b) Reported by Implementation (Not Independently Re-Run)** | Bare system-Python `importlib.util.spec_from_file_location` standalone import test reported in [`D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md#L130`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md#L130). *(AST stdlib-only isolation was verified directly.)* | Reported; full pytest not re-run |
| **(c) Not Covered / Out of Stage 1 Scope** | Live PostgreSQL 16 integration tests (`PG-*`), SQLAlchemy + `asyncpg` real exception chains from PG server errors, Stage 2 Razorpay hierarchy wiring, Stage 3–4 worker transactions. | **Not covered** |

---

## 9. Compatibility and Scope Assessment

- **Stage 1 Scope Discipline:** **PASS.** Only [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py), [`test_billing_webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py), [`D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md), and now this independent review report exist on top of `497852e`.
- **Untouched Subsystems Confirmed:**
  - Razorpay services (`webhook_engine.py`, `handler_base.py`, `webhook_ingress.py`, `client.py`, `customer.py`, `agreement_handlers.py`, `razorpay/__init__.py`) and `razorpay_webhook_worker.py`: **bit-identical** to `497852e`.
  - Stripe services (`stripe_webhook_engine.py`, handlers, worker): **bit-identical** to `497852e`.
  - Database models, Alembic migrations, and [`.github/workflows/ci.yml`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/.github/workflows/ci.yml): **bit-identical** to `497852e`.
  - Zero live Supabase, PostgreSQL, Razorpay, Stripe, or secret access performed.
- **Forward Compatibility Note:** None of the required corrections (`RC-01`–`RC-05`) require worker orchestration or provider coupling in Stage 1; fixing them inside `webhook_errors.py` and `test_billing_webhook_errors.py` keeps the Stage 2–4 integration contract cleaner and safer.

---

## 10. Required Corrections

### Mandatory Before D-04A Stage 2

1. **`RC-01` (Fixes `H-01` — ED-01 Policy Signal):** Add pure declarative runtime/unexpected exception classification in [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py) exposing an unambiguous `PROGRAMMING_ERROR_TYPES` constant and classifier helper (distinguishing immediate-quarantine programming errors such as `TypeError`, `AttributeError`, `NameError`, `UnboundLocalError`, `NotImplementedError`, `AssertionError`, `ZeroDivisionError`, `RecursionError`, `SyntaxError`, etc. with `failure_category="programming_error"` from unknown runtime exceptions eligible for bounded retry with `failure_category="unexpected_internal_error"`), plus comprehensive unit tests.
2. **`RC-02` (Fixes `H-02` — Schema-Exact `23505` Classification):**
   - Move `uq_billing_customer_provisions_provider_customer` **out of** Bucket 2 and explicitly **into** `UNIQUE_PERMANENT_CONSTRAINTS` (Bucket 3).
   - Remove `UNIQUE_CONCURRENCY_RACE_PREFIXES` (`"uq_billing_customer_provisions_"`, `"uq_checkout_attempts_provider_"`) — or always evaluate exact Bucket 1 / Bucket 2 / Bucket 3 sets **before** any narrow prefix rule and replace speculative wildcards with exact real index names (`uq_billing_customer_provisions_org_provider`, `uq_checkout_attempts_provider_sub`, `uq_checkout_attempts_provider_payment`) so unmapped constraints fail safe to Bucket 4 (`UNMAPPED_UNIQUE_VIOLATION` with `requires_alert=True`).
   - Remove phantom `"uq_billing_reconciliations_event_incoming"`; audit and explicitly place `"uq_reconciliation_event_sub"`, `"uq_billing_webhook_provider_event"`, and `"uq_payment_reconciliations_case_key"` accurately across Buckets 1–3 based on real model names; fix `test_billing_webhook_errors.py#L412-L451` accordingly.
3. **`RC-03` (Fixes `M-01` — Sanitizer & Diagnostic Payload Hardening):**
   - Replace leading `\b` in `_KV_HEADER_SECRET_RE` with an identifier/delimiter boundary (e.g., `(?<![A-Za-z0-9-])` or allow `[a-z0-9_-]*_?(?:secret|signature|api[_-]?key|..._token)`) while still preserving harmless diagnostic booleans (`signature_verified=False`).
   - Fix `_PG_URI_CREDENTIAL_RE` to redact `scheme://userinfo@host` reliably even when passwords contain `/` (stopping before `/path` outside userinfo vs. permitting `/` only prior to the authority `@` on the same URI/line, while refusing to cross whitespace or commas outside the URI authority).
   - Ensure `_URI_QUERY_SECRET_RE` covers all secret/token query parameters (e.g., `client_secret`, `access_token`, `refresh_token`, `api_secret`, `signature`, `webhook_secret`) and stops at `&` so non-secret query params remain intact.
   - Shield 13-digit millisecond Unix epochs (e.g., `1[5-9]\d{11}|2[0-4]\d{11}`) cleanly before `_PAN_RE`, and shield `acct_` / `rfnd_` alongside `cust_` / `sub_` / `pay_`.
   - Update [`sanitize_conflict_diagnostic_payload()`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py#L332-L394) to enforce `max_depth` and `visited: set[int]` cycle detection across **both mappings and lists/tuples**, and expand disallowed-key detection beyond exact literal matches (case-insensitive substring/suffix tokens like `_secret`, `_token`, `signature`, `password`, `email`, `phone`, `raw_payload`, `payload`, `headers`) without dropping safe IDs like `token_prefix` only if explicitly allowlisted or safe.
4. **`RC-04` (Fixes `M-02` — Missing `SQLSTATE` Mappings):** Add `57014` (`query_canceled` / `statement_timeout`), `53100` (`disk_full`), and `53200` (`out_of_memory`) to `RETRYABLE_PG_SQLSTATES`, and `23001` (`restrict_violation`) to `PERMANENT_PG_SQLSTATES`, with unit tests.
5. **`RC-05` (Fixes `M-03` — Crash-Site Tracebacks & Path Safety):** Change `extract_safe_traceback_locations()` to take the **innermost** `max_frames` frames (`limit=-max_frames` / slice `[-max_frames:]`, returning `[]` for `max_frames <= 0`) and sanitize filenames to basename or repo-relative path rather than exposing host filesystem directories.

### Recommended Cleanups (`L-01` / `L-03`)

6. **`RC-06`:** Respect explicit empty string vs. `None` cleanly on `BillingWebhookError(..., failure_category=...)` (`failure_category if failure_category is not None and failure_category.strip() else ...`).
7. **`RC-07`:** Avoid bisecting `[REDACTED_*]` markers right at `max_chars` when truncating long error strings.
8. **`RC-08`:** Expand [`test_billing_webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py) to cover acyclic `>32`-node `max_nodes` bounds, `RazorpayClientError` / `RazorpayCustomerError` non-mutation, deep `>8`-frame tracebacks, cyclic/nested list payloads, 13-digit ms epochs, underscore-prefixed secrets, `&` query-param delimiters, and case-insensitive `UNIQUE CONSTRAINT` fallback extraction.

---

## 11. Explicit Stage 2 Readiness Recommendation

**Recommendation: `DO NOT PROCEED TO D-04A STAGE 2` until `RC-01` through `RC-05` are implemented, tested, and re-verified on `implementation/d04a-stage1-exception-foundation`.**

**Why Hold Stage 2 Now:**
- Stage 2 reparents all Razorpay exceptions onto `BillingWebhookError` and wires [`webhook_engine.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/razorpay/webhook_engine.py) directly to Stage 1's classifier, `DbErrorClassification`, `sanitize_billing_error_message()`, and ED-01 exception disposition contract.
- Fixing `H-01` (ED-01), `H-02` (`23505` constraint buckets), `M-01` (sanitizer bypasses/cycle crash), `M-02` (`57014`/`53100`/`53200`/`23001`), and `M-03` (traceback direction) **inside Stage 1 first** prevents building Stage 2–4 tests and worker logic atop faulty constraint buckets or missing ED-01 semantics.

**Gate for Authorizing Stage 2:**
1. Apply `RC-01`–`RC-05` (and ideally `RC-06`–`RC-08`) strictly within [`webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/app/services/billing/webhook_errors.py) and [`test_billing_webhook_errors.py`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/apps/api/tests/test_billing_webhook_errors.py).
2. Re-run the Stage 1 unit suite + Razorpay/Stripe offline regression suites with zero skips/failures and confirm zero tracked changes outside Stage 1 files.
3. Update [`D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md`](file:///D:/Projects/V1/Aptlyst-AI-d2-stage1-pg16/docs/reports/D2_D04A_STAGE1_IMPLEMENTATION_REPORT.md) (or add an addendum) reflecting corrected bucket mappings, ED-01 contract, and updated file hashes, then perform a brief Stage 1 re-check before starting Stage 2.
