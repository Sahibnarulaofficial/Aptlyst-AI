"""Provider-neutral billing webhook exception hierarchy, SQLSTATE classifier, and log sanitizer.

Guarantees & Invariants:
1. Coherent Minimal Hierarchy:
   - BillingWebhookError
     ├── BillingWebhookRetryableError
     ├── BillingWebhookPermanentError
     │    ├── BillingWebhookValidationError
     │    ├── BillingWebhookScopeMismatchError
     │    ├── BillingWebhookConflictError
     │    └── BillingWebhookUnknownEventError
     └── BillingWebhookLeaseError
   - Lease errors remain strictly disjoint from retryable and permanent classifications.
2. Cycle-Safe SQLSTATE & Constraint Classification:
   - Traverses SQLAlchemy, asyncpg, DBAPI .orig, __cause__, and __context__ chains safely
     with visited-node deduplication and node-count capping.
   - Separates retry/classification recommendations (DbRetryDisposition) from worker
     event-status transitions.
   - Classifies PostgreSQL 23505 unique violations across 4 explicit constraint buckets
     and classifies permanent vs. retryable vs. unknown SQLSTATEs without guessing.
3. Non-Destructive Secret & PII Sanitization:
   - Deterministic, idempotent redaction of credentials, PostgreSQL DSNs/passwords,
     SQLAlchemy [parameters: ...] dumps, signatures, emails, phones, and PAN-like values.
   - Preserves UUIDs, SQLSTATE codes, constraint names, prefixed payment entity IDs,
     correlation IDs, and Unix epoch timestamps.
4. Zero Circular Dependencies:
   - Depends only on Python standard library modules. Does not import provider modules,
     workers, database models, or FastAPI runtime objects at import time.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
import enum
from pathlib import PurePosixPath
import re
import traceback
from typing import Any


# =============================================================================
# Provider-Neutral Webhook Exception Hierarchy
# =============================================================================


class BillingWebhookError(Exception):
    """Base exception for provider-neutral webhook processing diagnostics."""

    default_failure_category: str = "webhook_error"
    is_retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        failure_category: str | None = None,
        error_code: str | None = None,
        constraint_name: str | None = None,
    ) -> None:
        super().__init__(message)
        # L-01b: strip whitespace so category slugs are always non-empty.
        cleaned = failure_category.strip() if isinstance(failure_category, str) else None
        self.failure_category: str = cleaned or self.default_failure_category
        self.error_code: str | None = error_code
        self.constraint_name: str | None = constraint_name

    @property
    def sanitized_message(self) -> str:
        """Return a redacted, bounded summary of this exception."""
        return sanitize_billing_error_message(self)


class BillingWebhookRetryableError(BillingWebhookError):
    """Base exception for transient webhook errors eligible for bounded retry."""

    default_failure_category: str = "retryable_failure"
    is_retryable: bool = True


class BillingWebhookPermanentError(BillingWebhookError):
    """Base exception for deterministic errors that cannot succeed on blind retry."""

    default_failure_category: str = "permanent_failure"
    is_retryable: bool = False


class BillingWebhookValidationError(BillingWebhookPermanentError):
    """Payload schema, timestamp, numeric, or mandatory identifier validation error."""

    default_failure_category: str = "validation"


class BillingWebhookScopeMismatchError(BillingWebhookPermanentError):
    """Provider, merchant account, environment, or verification-scope mismatch."""

    default_failure_category: str = "scope_mismatch"


class BillingWebhookConflictError(BillingWebhookPermanentError):
    """Authoritative state or tenant-binding conflict requiring reconciliation audit."""

    default_failure_category: str = "reconciliation_conflict"


class BillingWebhookUnknownEventError(BillingWebhookPermanentError):
    """Unregistered or unsupported webhook event type."""

    default_failure_category: str = "unknown_event"


class BillingWebhookLeaseError(BillingWebhookError):
    """Ownership or fencing-lease violation (disjoint from retry and permanent failure)."""

    default_failure_category: str = "stale_lease"
    is_retryable: bool = False


# =============================================================================
# Secret & PII Sanitization (Non-Destructive & Idempotent)
# =============================================================================

MAX_SANITIZED_ERROR_CHARS: int = 1024
MAX_CHAIN_NODES_DEFAULT: int = 32

_SAFE_SQLSTATE_RE = re.compile(r"^[0-9A-Z]{5}$")
_SAFE_CONSTRAINT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_$.]{0,127}$")
_UNIQUE_CONSTRAINT_FALLBACK_RE = re.compile(
    r'unique\s+constraint\s+"([^"\s]{1,128})"',
    re.IGNORECASE,
)

# RC-03: Allow `/` inside the password component after `:` while blocking whitespace,
# commas, semicolons, `?`, `#`, quotes and brackets across the userinfo section.
_PG_URI_CREDENTIAL_RE = re.compile(
    r"(postgres(?:ql)?(?:\+[a-z0-9_.-]+)?://)([^\s/?#@:;,='\"()\[\]<>]+(?::[^\s?#;,'\"()\[\]<>]+)?)@(?=[A-Za-z0-9._\-\[])",
    re.IGNORECASE,
)
# RC-03: Allow prefixed/full query secret names; stop at `&` cleanly.
_URI_QUERY_SECRET_RE = re.compile(
    r"([?&](?:[A-Za-z0-9_-]{0,32}(?:password|passfile|secret|token|api[_-]?key|signature))=)(?!\[REDACTED)([^&\s;,)\]\'\"]+)",
    re.IGNORECASE,
)
_LIBPQ_KV_PASSWORD_RE = re.compile(
    r"\b(password|passfile)([^\S\r\n]*=[^\S\r\n]*)(?!(?:\s|\[REDACTED))"
    r"('(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"|[^\s,;)\]]+)",
    re.IGNORECASE,
)
_AUTH_SCHEME_RE = re.compile(
    r"\b(Basic|Bearer)[^\S\r\n]+(?!\[REDACTED)[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_RAZORPAY_SECRET_RE = re.compile(r"\brzp_[A-Za-z0-9_-]{4,}\b")
_WHSEC_SECRET_RE = re.compile(r"\bwhsec_[A-Za-z0-9_-]+\b")
_STRIPE_SECRET_KEY_RE = re.compile(r"\b(?:sk|rk)_[A-Za-z0-9_-]{4,}\b")
_STRIPE_PUB_KEY_RE = re.compile(r"\bpk_(?:test|live)_[A-Za-z0-9_-]+\b")
# F-01: Boundary-aware compound secret-key pattern supporting underscore-separated names.
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
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Explicit international (+ prefix) and contact-labeled phones run before epoch shielding.
_INTL_PHONE_RE = re.compile(r"(?<!\w)\+\d[\d ().\\-]{6,18}\d\b")
_LABELED_PHONE_RE = re.compile(
    r"\b(phone|mobile|contact|tel|telephone)\b([^\S\r\n]*[:=][^\S\r\n]*)"
    r"(?!(?:\s|\[REDACTED))"
    r"('(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"|\+?\d[\d ().\\-]{6,18}\d)",
    re.IGNORECASE,
)
_FORMATTED_PHONE_RE = re.compile(
    r"(?<!\w)(?:\(\d{2,4}\)[^\S\r\n]*\d{3,4}[-. ]\d{4}|\d{3}[-. ]\d{3}[-. ]\d{4}|\d{5}[- ]\d{5})\b"
)
_PAN_RE = re.compile(r"(?<![\w\[])\d(?:[ -]?\d){12,18}(?![\w\]])")

# Identifiers preserved untouched across phone / PAN sanitization.
# RC-03: Added `acct|rfnd` to entity pattern; expanded epoch to 10/13-digit ms.
_SHIELD_PATTERNS = (
    re.compile(r"\[(?:REDACTED(?:_[A-Z]+)?|parameters: REDACTED)\]"),
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"),
    re.compile(r"\b(?:sub|pay|inv|cust|order|evt|plan|acc|ch|cs|si|in|pi|cus|pm|acct|rfnd)_[A-Za-z0-9][A-Za-z0-9_-]*\b"),
    re.compile(r"\b(?:corr|req|trace)[_-][A-Za-z0-9][A-Za-z0-9_-]*\b"),
    re.compile(r"\b(?:uq|fk|ix|ck|pk)_[a-zA-Z0-9_]+\b"),
    re.compile(r"\b(?:1[5-9]\d{8}|2[0-4]\d{8})(?:\.\d+|\d{3})?\b"),
)


def _redact_sqlalchemy_parameters(text: str) -> str:
    """Strip SQLAlchemy `[parameters: ...]` blocks without eating surrounding diagnostics."""
    lower = text.lower()
    marker = "[parameters:"
    pos = 0
    parts: list[str] = []

    while True:
        idx = lower.find(marker, pos)
        if idx == -1:
            parts.append(text[pos:])
            break

        parts.append(text[pos:idx])
        depth = 1
        cursor = idx + len(marker)
        in_quote: str | None = None
        escaped = False
        length = len(text)

        while cursor < length and depth > 0:
            ch = text[cursor]
            if in_quote is not None:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == in_quote:
                    in_quote = None
            else:
                if ch in ("'", '"'):
                    in_quote = ch
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
            cursor += 1

        parts.append("[parameters: REDACTED]")
        pos = cursor

    return "".join(parts)


def _shield_safe_tokens(text: str) -> tuple[str, dict[str, str]]:
    """Temporarily replace non-secret diagnostic identifiers before PAN/phone redaction."""
    token_prefix = "__APTY_SHIELD_"
    while token_prefix in text:
        token_prefix = f"{token_prefix}X_"

    preserved: dict[str, str] = {}
    counter = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal counter
        placeholder = f"{token_prefix}{counter}__"
        counter += 1
        preserved[placeholder] = match.group(0)
        return placeholder

    shielded = text
    for pattern in _SHIELD_PATTERNS:
        shielded = pattern.sub(_replace, shielded)
    return shielded, preserved


def _restore_shielded_tokens(text: str, preserved: dict[str, str]) -> str:
    """Restore shielded diagnostic tokens in reverse insertion order."""
    restored = text
    for placeholder, original in reversed(list(preserved.items())):
        restored = restored.replace(placeholder, original)
    return restored


def sanitize_billing_error_message(
    err: BaseException | str | None,
    max_chars: int = MAX_SANITIZED_ERROR_CHARS,
) -> str:
    """Sanitize and bound exception or log text without destroying safe identifiers.

    Removes:
    - SQLAlchemy `[parameters: ...]` JSON/tuple payload dumps.
    - PostgreSQL connection URI credentials and libpq `password=` / `passfile=` entries.
    - Authorization `Basic` / `Bearer` tokens, Razorpay keys (`rzp_*`), Stripe keys
      (`sk_*`, `rk_*`, `pk_test_*`/`pk_live_*`), webhook secrets (`whsec_*`), and signatures.
    - Emails, phone numbers, and PAN-like card numbers.

    Preserves:
    - Standard UUIDs, SQLSTATE codes, DB constraint names, prefixed payment entity IDs
      (`sub_*`, `pay_*`, `inv_*`, `cust_*`, `order_*`, `evt_*`, `acct_*`, `rfnd_*`),
      correlation IDs, and Unix epoch timestamps.
    """
    if err is None:
        return ""

    if max_chars <= 0:
        return ""

    try:
        raw_text = str(err)
    except Exception:
        raw_text = type(err).__name__

    if not raw_text.strip():
        return ""

    # 1. Strip SQLAlchemy bound parameter dumps before matching payloads/URIs.
    msg = _redact_sqlalchemy_parameters(raw_text)

    # 2. Redact PostgreSQL URI userinfo, query secrets, and libpq password parameters.
    msg = _PG_URI_CREDENTIAL_RE.sub(r"\1[REDACTED]@", msg)
    msg = _URI_QUERY_SECRET_RE.sub(r"\1[REDACTED]", msg)
    msg = _LIBPQ_KV_PASSWORD_RE.sub(r"\1=[REDACTED]", msg)

    # 3. Redact HTTP Auth headers, Razorpay/Stripe/webhook keys, and header signatures.
    msg = _AUTH_SCHEME_RE.sub(r"\1 [REDACTED_TOKEN]", msg)
    msg = _RAZORPAY_SECRET_RE.sub("[REDACTED_KEY]", msg)
    msg = _WHSEC_SECRET_RE.sub("[REDACTED_KEY]", msg)
    msg = _STRIPE_SECRET_KEY_RE.sub("[REDACTED_KEY]", msg)
    msg = _STRIPE_PUB_KEY_RE.sub("[REDACTED_KEY]", msg)
    msg = _KV_HEADER_SECRET_RE.sub(r"\1\2[REDACTED]", msg)

    # 4. Redact email addresses.
    msg = _EMAIL_RE.sub("[REDACTED_EMAIL]", msg)

    # 5. Redact explicit international (+ prefix) and phone-labeled values prior to epoch shielding.
    msg = _LABELED_PHONE_RE.sub(r"\1\2[REDACTED_PHONE]", msg)
    msg = _INTL_PHONE_RE.sub("[REDACTED_PHONE]", msg)

    # 6. Shield UUIDs, entity IDs, constraint names, ISO timestamps, and Unix epochs.
    shielded, preserved = _shield_safe_tokens(msg)

    # 7. Redact formatted local phones and PAN-like card sequences cleanly.
    shielded = _FORMATTED_PHONE_RE.sub("[REDACTED_PHONE]", shielded)
    shielded = _PAN_RE.sub("[REDACTED_PAN]", shielded)

    msg = _restore_shielded_tokens(shielded, preserved)
    msg = re.sub(r"\s+", " ", msg).strip()

    # L-01a: Step back before truncating mid-marker to keep telemetry clean.
    if len(msg) > max_chars:
        truncated = msg[:max_chars]
        last_bracket = truncated.rfind("[")
        if last_bracket >= 0 and "]" not in truncated[last_bracket:]:
            truncated = truncated[:last_bracket]
        return truncated.strip()
    return msg


# =============================================================================
# RC-03: Sensitive diagnostic key detection & value sanitization helpers
# =============================================================================

_SENSITIVE_KEY_TOKENS: frozenset[str] = frozenset({
    "secret", "signature", "password", "passfile", "passphrase",
    "email", "phone", "mobile", "tel", "telephone", "contact",
    "payload", "headers", "authorization", "pan", "cardnumber",
    "cvv", "cvc", "ssn", "apikey", "privatekey", "secretkey",
    "signingkey", "encryptionkey",
})

_SENSITIVE_KEY_SPLIT_RE = re.compile(r"[_.\s\-]+")


def _is_sensitive_diagnostic_key(key_str: str) -> bool:
    """Return True if any normalized token in key_str is a sensitive sentinel.

    Checks token sub-components split on `[_. -]+`, normalized to lowercase alnum.
    Preserves safe compound keys like `signature_verified`, `idempotency_key`,
    and `lease_token_prefix`.
    """
    # Allow explicit safe keys regardless of token content.
    normalized = key_str.strip().lower()
    # `signature_verified` is explicitly safe (boolean diagnostic flag).
    if normalized == "signature_verified":
        return False
    # `idempotency_key` is explicitly safe.
    if normalized == "idempotency_key":
        return False
    # `lease_token_prefix` is explicitly safe (UUID prefix, not secret).
    if normalized.endswith("_prefix"):
        return False

    tokens = _SENSITIVE_KEY_SPLIT_RE.split(normalized)
    for token in tokens:
        # Normalize to alpha-numeric only for comparison.
        alnum = re.sub(r"[^a-z0-9]", "", token)
        if alnum in _SENSITIVE_KEY_TOKENS:
            return True
    return False


_DIAG_NODE_BUDGET: int = 512


def _sanitize_diag_value(
    val: Any,
    *,
    depth_left: int,
    seen_ids: set[int],
    node_budget: list[int],
    max_str_chars: int,
) -> Any:
    """Recursively sanitize a diagnostic value with cycle/depth/budget guards."""
    if node_budget[0] <= 0:
        return "[BUDGET_EXCEEDED]"

    node_budget[0] -= 1

    if val is None or isinstance(val, (bool, int, float)):
        return val

    if isinstance(val, str):
        return sanitize_billing_error_message(val, max_chars=max_str_chars)

    if isinstance(val, Mapping):
        if depth_left <= 0:
            return "[NESTED_OBJECT_OMITTED]"
        obj_id = id(val)
        if obj_id in seen_ids:
            return "[CYCLE_OMITTED]"
        seen_ids.add(obj_id)
        try:
            result: dict[str, Any] = {}
            for raw_k, raw_v in val.items():
                k_str = str(raw_k).strip()
                if not k_str or _is_sensitive_diagnostic_key(k_str):
                    continue
                result[k_str[:128]] = _sanitize_diag_value(
                    raw_v,
                    depth_left=depth_left - 1,
                    seen_ids=seen_ids,
                    node_budget=node_budget,
                    max_str_chars=max_str_chars,
                )
            return result
        finally:
            seen_ids.discard(obj_id)

    if isinstance(val, (list, tuple)):
        if depth_left <= 0:
            return "[NESTED_LIST_OMITTED]"
        obj_id = id(val)
        if obj_id in seen_ids:
            return "[CYCLE_OMITTED]"
        seen_ids.add(obj_id)
        try:
            items = []
            for item in list(val)[:25]:
                items.append(_sanitize_diag_value(
                    item,
                    depth_left=depth_left - 1,
                    seen_ids=seen_ids,
                    node_budget=node_budget,
                    max_str_chars=max_str_chars,
                ))
            return items
        finally:
            seen_ids.discard(obj_id)

    return sanitize_billing_error_message(str(val), max_chars=max_str_chars)


def sanitize_conflict_diagnostic_payload(
    raw_data: Mapping[str, Any] | None,
    *,
    max_str_chars: int = 512,
    max_depth: int = 2,
) -> dict[str, Any]:
    """Produce a bounded, sanitized scalar/list diagnostic dict stripping raw request payloads."""
    if not raw_data or not isinstance(raw_data, Mapping):
        return {}

    node_budget: list[int] = [_DIAG_NODE_BUDGET]
    seen_ids: set[int] = set()

    cleaned: dict[str, Any] = {}
    for raw_key, raw_val in raw_data.items():
        key_str = str(raw_key).strip()
        if not key_str:
            continue
        if _is_sensitive_diagnostic_key(key_str):
            continue
        cleaned[key_str[:128]] = _sanitize_diag_value(
            raw_val,
            depth_left=max_depth,
            seen_ids=seen_ids,
            node_budget=node_budget,
            max_str_chars=max_str_chars,
        )
    return cleaned


# =============================================================================
# RC-05: Innermost traceback frames with host-path sanitization
# =============================================================================

MAX_TRACEBACK_FRAMES: int = 64

_TRACEBACK_ANCHOR_PATTERNS: tuple[str, ...] = (
    "apps/api/",
    "tests/",
    "app/",
    "alembic/",
    "site-packages/",
    "dist-packages/",
)


def _sanitize_traceback_filename(raw_path: str) -> str:
    """Strip host filesystem prefixes from a traceback filename.

    Preserves synthetic Python pseudo-filenames (`<...>`).
    Normalizes `\\` to `/`; strips leading host root before well-known relative
    anchors. Falls back to basename for unrecognized paths.
    """
    if raw_path.startswith("<") and raw_path.endswith(">"):
        return raw_path

    norm = raw_path.replace("\\", "/")

    for anchor in _TRACEBACK_ANCHOR_PATTERNS:
        idx = norm.find(anchor)
        if idx >= 0:
            return norm[idx:]

    # Unrecognized host path: return only the basename.
    return PurePosixPath(norm).name


def extract_safe_traceback_locations(
    exc: BaseException | None,
    *,
    max_frames: int = 8,
) -> list[str]:
    """Extract non-secret `filename:lineno:func` stack locations (zero frame locals).

    Extracts the innermost `max_frames` frames (closest to the crash site).
    """
    if exc is None or exc.__traceback__ is None:
        return []
    if max_frames <= 0:
        return []
    effective_limit = min(max_frames, MAX_TRACEBACK_FRAMES)
    try:
        frames = traceback.extract_tb(exc.__traceback__, limit=-effective_limit)
    except Exception:
        return []
    return [
        f"{_sanitize_traceback_filename(frame.filename)}:{frame.lineno}:{frame.name}"
        for frame in frames
    ]


# =============================================================================
# PostgreSQL SQLSTATE & Constraint-Aware 23505 Classification
# =============================================================================


class DbRetryDisposition(str, enum.Enum):
    """Recommendation yielded by database exception analysis (distinct from worker status)."""

    RETRYABLE = "retryable"
    RETRYABLE_WITH_ALERT = "retryable_with_alert"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


class UniqueConstraintBucket(str, enum.Enum):
    """Explicit classification buckets for PostgreSQL SQLSTATE 23505 unique violations."""

    IDEMPOTENT_CONVERGENCE = "idempotent_convergence"
    CONCURRENCY_RACE = "concurrency_race"
    PERMANENT_INTEGRITY_CONFLICT = "permanent_integrity_conflict"
    UNMAPPED_UNIQUE_VIOLATION = "unmapped_unique_violation"


# RC-02: Updated constraint sets with schema-verified names and corrected bucket assignments.
UNIQUE_CONVERGENCE_CONSTRAINTS: frozenset[str] = frozenset({
    # Bucket 1: idempotent convergence — duplicate/concurrent delivery retries safely.
    "uq_billing_invoices_provider_invoice",
    "uq_billing_payments_provider_payment",
    "uq_billing_webhook_provider_event",        # added
    "uq_payment_reconciliations_case_key",      # added
    "uq_stripe_invoices_invoice_id",            # added
})

UNIQUE_CONCURRENCY_RACE_EXACT: frozenset[str] = frozenset({
    # Bucket 2: concurrency race — racing concurrent inserts, retry resolves.
    "uq_billing_customer_provisions_org_provider",
    "uq_subscriptions_provider_subscription",
    "uq_org_scoped_active_billing_checkout_attempt",
    "uq_checkout_attempts_provider_sub",        # added
    "uq_checkout_attempts_provider_payment",    # added
    "uq_org_active_checkout_attempt",           # added (stripe_checkout_attempts)
})

# RC-02: Set to empty — all exact constraint names are catalogued above.
# Keep symbol for import compatibility; speculative future names fall through to Bucket 4.
UNIQUE_CONCURRENCY_RACE_PREFIXES: tuple[str, ...] = ()

UNIQUE_PERMANENT_CONSTRAINTS: frozenset[str] = frozenset({
    # Bucket 3: permanent integrity conflicts — retrying cannot cure these.
    "uq_org_active_subscription",
    "uq_subscriptions_provider_customer",
    "uq_billing_payments_checkout_attempt",
    "uq_billing_checkout_org_idempotency",
    "uq_bca_composite_identity",
    "uq_reconciliation_event_sub",                              # replaces phantom
    "uq_billing_customer_provisions_provider_customer",         # moved from Bucket 2!
    "uq_org_active_billing_period",                             # added
})

# RC-04: Class 53 capacity-exhaustion SQLSTATEs promoted to RETRYABLE_WITH_ALERT.
ALERT_RETRYABLE_PG_SQLSTATES: frozenset[str] = frozenset({
    "53100",  # disk_full
    "53200",  # out_of_memory
    "53300",  # too_many_connections
})

RETRYABLE_PG_SQLSTATES: frozenset[str] = frozenset({
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
    "55P03",  # lock_not_available (lock_timeout)
    # NOTE: "53300" moved to ALERT_RETRYABLE_PG_SQLSTATES (RC-04)
})
RETRYABLE_PG_SQLSTATE_PREFIXES: tuple[str, ...] = (
    "57P",    # admin_shutdown / crash_shutdown / cannot_connect_now
    "08",     # connection_exception class
)

PERMANENT_PG_SQLSTATES: frozenset[str] = frozenset({
    "P0001",  # raise_exception (PL/pgSQL immutability & domain triggers)
    "23001",  # restrict_violation (RC-04: added)
    "23502",  # not_null_violation
    "23503",  # foreign_key_violation
    "23514",  # check_violation
})
PERMANENT_PG_SQLSTATE_PREFIXES: tuple[str, ...] = (
    "22",     # data_exception class
    "42",     # syntax_error_or_access_rule_violation class (incl. 42501 RLS)
)

_TRANSPORT_CLASS_NAMES: frozenset[str] = frozenset({
    "DisconnectionError",
    "PostgresConnectionError",
    "ConnectionDoesNotExistError",
    "CannotConnectNowError",
    "ConnectionFailureError",
})


@dataclass(frozen=True)
class DbErrorDetails:
    """Sanitized, cycle-safe diagnostic details extracted from an exception chain."""

    error_type: str
    sqlstate: str | None
    constraint_name: str | None
    table_name: str | None
    schema_name: str | None
    sanitized_summary: str
    sanitized_detail: str | None
    chain_depth: int


@dataclass(frozen=True)
class DbErrorClassification:
    """Structured database failure recommendation (does not mutate or decide worker state)."""

    disposition: DbRetryDisposition
    failure_category: str
    sqlstate: str | None
    constraint_name: str | None
    table_name: str | None
    unique_bucket: UniqueConstraintBucket | None
    should_retry: bool
    is_permanent: bool
    requires_alert: bool
    is_known: bool
    details: DbErrorDetails


def _safe_getattr(obj: Any, attr_name: str) -> Any:
    """Safely read an object attribute without raising on broken proxies or descriptors."""
    if obj is None:
        return None
    try:
        return getattr(obj, attr_name, None)
    except Exception:
        return None


def _safe_str(obj: Any) -> str:
    """Safely format an exception object as text."""
    try:
        return str(obj)
    except Exception:
        return type(obj).__name__


def _normalize_sqlstate(raw_value: Any) -> str | None:
    if not isinstance(raw_value, str):
        return None
    cleaned = raw_value.strip().upper()
    if _SAFE_SQLSTATE_RE.match(cleaned):
        return cleaned
    return None


def _normalize_identifier(raw_value: Any) -> str | None:
    if not isinstance(raw_value, str):
        return None
    cleaned = raw_value.strip().lower()
    if _SAFE_CONSTRAINT_RE.match(cleaned):
        return cleaned
    return None


def _extract_fallback_constraint_from_text(raw_text: str | None) -> str | None:
    """Extract unique constraint name only after stripping bound parameters and secrets."""
    if not raw_text:
        return None
    redacted_params = _redact_sqlalchemy_parameters(raw_text)
    match = _UNIQUE_CONSTRAINT_FALLBACK_RE.search(redacted_params)
    if not match:
        return None
    return _normalize_identifier(match.group(1))


def iter_db_exception_chain(
    exc: BaseException,
    *,
    max_nodes: int = MAX_CHAIN_NODES_DEFAULT,
) -> list[Any]:
    """Traverse SQLAlchemy/asyncpg/DBAPI `.orig`, `__cause__`, and `__context__` without cycles.

    RC-01 / RC-04: Two-phase causal-first traversal with `__suppress_context__` respect:
    - Phase 1 (Explicit Causal Subgraph): BFS over `.orig` and `__cause__` edges.
      Defers non-suppressed `__context__` children into a separate queue.
    - Phase 2 (Deferred Implicit `__context__`): Only after Phase 1 is exhausted,
      processes deferred `__context__` nodes within the remaining budget.
    This ensures explicit `__cause__`/`.orig` always takes precedence over incidental
    `__context__` and `raise ... from None` correctly stops context leakage.
    """
    if max_nodes < 1:
        max_nodes = 1

    chain: list[Any] = []
    visited: set[int] = set()
    # Phase 1: explicit causal subgraph (orig + __cause__)
    causal_queue: deque[Any] = deque()
    # Phase 2: deferred implicit context
    context_queue: deque[Any] = deque()

    # Seed Phase 1: check .orig first (SQLAlchemy DBAPI wrapper), then exc itself.
    orig = _safe_getattr(exc, "orig")
    if orig is not None:
        causal_queue.append(orig)
    causal_queue.append(exc)

    def _enqueue_children(node: Any) -> None:
        """Enqueue causal children; defer __context__ to Phase 2."""
        for attr in ("orig", "__cause__"):
            nxt = _safe_getattr(node, attr)
            if nxt is not None and id(nxt) not in visited:
                causal_queue.append(nxt)
        # Defer non-suppressed __context__ for Phase 2.
        ctx = _safe_getattr(node, "__context__")
        if ctx is not None and id(ctx) not in visited:
            suppress = _safe_getattr(node, "__suppress_context__")
            if not suppress:
                context_queue.append(ctx)

    # Phase 1: drain the causal subgraph.
    while causal_queue and len(chain) < max_nodes:
        current = causal_queue.popleft()
        if current is None:
            continue
        node_id = id(current)
        if node_id in visited:
            continue
        visited.add(node_id)
        chain.append(current)
        _enqueue_children(current)

    # Phase 2: process deferred __context__ nodes within remaining budget.
    while context_queue and len(chain) < max_nodes:
        current = context_queue.popleft()
        if current is None:
            continue
        node_id = id(current)
        if node_id in visited:
            continue
        visited.add(node_id)
        chain.append(current)
        _enqueue_children(current)

    return chain


def extract_db_error_details(
    exc: BaseException,
    *,
    max_nodes: int = MAX_CHAIN_NODES_DEFAULT,
) -> DbErrorDetails:
    """Extract SQLSTATE, constraint name, table/schema, and sanitized summary safely."""
    chain = iter_db_exception_chain(exc, max_nodes=max_nodes)

    sqlstate: str | None = None
    constraint_name: str | None = None
    table_name: str | None = None
    schema_name: str | None = None
    raw_detail: str | None = None

    for node in chain:
        diag = _safe_getattr(node, "diag")

        if sqlstate is None:
            for cand in (
                _safe_getattr(node, "sqlstate"),
                _safe_getattr(node, "pgcode"),
                _safe_getattr(diag, "sqlstate"),
                _safe_getattr(diag, "pgcode"),
            ):
                sqlstate = _normalize_sqlstate(cand)
                if sqlstate is not None:
                    break

        if constraint_name is None:
            for cand in (
                _safe_getattr(node, "constraint_name"),
                _safe_getattr(diag, "constraint_name"),
            ):
                constraint_name = _normalize_identifier(cand)
                if constraint_name is not None:
                    break

        if table_name is None:
            for cand in (
                _safe_getattr(node, "table_name"),
                _safe_getattr(diag, "table_name"),
            ):
                table_name = _normalize_identifier(cand)
                if table_name is not None:
                    break

        if schema_name is None:
            for cand in (
                _safe_getattr(node, "schema_name"),
                _safe_getattr(diag, "schema_name"),
            ):
                schema_name = _normalize_identifier(cand)
                if schema_name is not None:
                    break

        if raw_detail is None:
            for cand in (
                _safe_getattr(node, "detail"),
                _safe_getattr(node, "message_detail"),
                _safe_getattr(diag, "message_detail"),
                _safe_getattr(diag, "detail"),
            ):
                if isinstance(cand, str) and cand.strip():
                    raw_detail = cand.strip()
                    break

        if (
            sqlstate is not None
            and constraint_name is not None
            and table_name is not None
            and raw_detail is not None
        ):
            break

    # Safe regex fallback only when driver attrs omitted constraint_name (parameters scrubbed first).
    if constraint_name is None and sqlstate == "23505":
        for node in chain:
            constraint_name = _extract_fallback_constraint_from_text(_safe_str(node))
            if constraint_name is not None:
                break

    return DbErrorDetails(
        error_type=type(exc).__name__,
        sqlstate=sqlstate,
        constraint_name=constraint_name,
        table_name=table_name,
        schema_name=schema_name,
        sanitized_summary=sanitize_billing_error_message(_safe_str(exc)),
        sanitized_detail=(
            sanitize_billing_error_message(raw_detail) if raw_detail is not None else None
        ),
        chain_depth=len(chain),
    )


def classify_unique_violation_constraint(
    constraint_name: str | None,
) -> UniqueConstraintBucket:
    """Classify a PostgreSQL SQLSTATE 23505 constraint into one of 4 explicit buckets.

    RC-02: Check order: Bucket 3 (PERMANENT) first, then Bucket 1, then Bucket 2,
    then prefixes (empty), then Bucket 4 (UNMAPPED). This prevents the former
    wildcard-prefix misclassification of `uq_billing_customer_provisions_provider_customer`
    into Bucket 2.
    """
    normalized = _normalize_identifier(constraint_name)
    if normalized is None:
        return UniqueConstraintBucket.UNMAPPED_UNIQUE_VIOLATION

    # Bucket 3 first — permanent integrity conflicts must be checked before Buckets 1 & 2.
    if normalized in UNIQUE_PERMANENT_CONSTRAINTS:
        return UniqueConstraintBucket.PERMANENT_INTEGRITY_CONFLICT

    if normalized in UNIQUE_CONVERGENCE_CONSTRAINTS:
        return UniqueConstraintBucket.IDEMPOTENT_CONVERGENCE

    if normalized in UNIQUE_CONCURRENCY_RACE_EXACT or any(
        normalized.startswith(prefix) for prefix in UNIQUE_CONCURRENCY_RACE_PREFIXES
    ):
        return UniqueConstraintBucket.CONCURRENCY_RACE

    return UniqueConstraintBucket.UNMAPPED_UNIQUE_VIOLATION


def _is_transport_disconnect_chain(chain: list[Any]) -> bool:
    """Detect unambiguous transport/pool disconnect exceptions without SQLSTATE."""
    for node in chain:
        if isinstance(node, (ConnectionError, BrokenPipeError, EOFError, TimeoutError)):
            return True
        if any(cls.__name__ in _TRANSPORT_CLASS_NAMES for cls in type(node).__mro__):
            return True
    return False


# RC-04: Statement timeout detection after scrubbing [parameters: ...].
_STATEMENT_TIMEOUT_RE = re.compile(r"\bstatement\s+timeout\b", re.IGNORECASE)
_USER_REQUEST_RE = re.compile(r"\buser\s+request\b", re.IGNORECASE)


def _is_statement_timeout_57014(chain: list[Any]) -> bool:
    """Return True iff the exception chain contains an unambiguous PG `statement_timeout` message.

    Scrubs [parameters: ...] blocks first so spoofed payload content cannot match.
    Returns False when the message matches `user request` (operator/driver cancel).
    """
    for node in chain:
        try:
            raw = str(node)
        except Exception:
            continue
        scrubbed = _redact_sqlalchemy_parameters(raw)
        if _USER_REQUEST_RE.search(scrubbed):
            return False
        if _STATEMENT_TIMEOUT_RE.search(scrubbed):
            return True
    return False


def classify_pg_sqlstate(
    sqlstate: str | None,
    *,
    constraint_name: str | None = None,
    table_name: str | None = None,
    details: DbErrorDetails | None = None,
) -> DbErrorClassification:
    """Classify a normalized PostgreSQL SQLSTATE without guessing unknown codes."""
    norm_state = _normalize_sqlstate(sqlstate)
    norm_constraint = _normalize_identifier(constraint_name)
    norm_table = _normalize_identifier(table_name)
    err_details = details or DbErrorDetails(
        error_type="SQLState",
        sqlstate=norm_state,
        constraint_name=norm_constraint,
        table_name=norm_table,
        schema_name=None,
        sanitized_summary=f"SQLSTATE={norm_state or 'UNKNOWN'}",
        sanitized_detail=None,
        chain_depth=1,
    )

    if norm_state == "23505":
        bucket = classify_unique_violation_constraint(norm_constraint)
        if bucket == UniqueConstraintBucket.IDEMPOTENT_CONVERGENCE:
            return DbErrorClassification(
                disposition=DbRetryDisposition.RETRYABLE,
                failure_category="db_idempotency_race",
                sqlstate="23505",
                constraint_name=norm_constraint,
                table_name=norm_table,
                unique_bucket=bucket,
                should_retry=True,
                is_permanent=False,
                requires_alert=False,
                is_known=True,
                details=err_details,
            )
        if bucket == UniqueConstraintBucket.CONCURRENCY_RACE:
            return DbErrorClassification(
                disposition=DbRetryDisposition.RETRYABLE,
                failure_category="db_concurrent_unique_race",
                sqlstate="23505",
                constraint_name=norm_constraint,
                table_name=norm_table,
                unique_bucket=bucket,
                should_retry=True,
                is_permanent=False,
                requires_alert=False,
                is_known=True,
                details=err_details,
            )
        if bucket == UniqueConstraintBucket.PERMANENT_INTEGRITY_CONFLICT:
            return DbErrorClassification(
                disposition=DbRetryDisposition.PERMANENT,
                failure_category="db_permanent",
                sqlstate="23505",
                constraint_name=norm_constraint,
                table_name=norm_table,
                unique_bucket=bucket,
                should_retry=False,
                is_permanent=True,
                requires_alert=False,
                is_known=True,
                details=err_details,
            )
        return DbErrorClassification(
            disposition=DbRetryDisposition.RETRYABLE_WITH_ALERT,
            failure_category="db_unmapped_unique_violation",
            sqlstate="23505",
            constraint_name=norm_constraint,
            table_name=norm_table,
            unique_bucket=bucket,
            should_retry=True,
            is_permanent=False,
            requires_alert=True,
            is_known=True,
            details=err_details,
        )

    if norm_state is not None:
        # RC-04: Class 53 capacity-exhaustion SQLSTATEs → RETRYABLE_WITH_ALERT.
        if norm_state in ALERT_RETRYABLE_PG_SQLSTATES:
            return DbErrorClassification(
                disposition=DbRetryDisposition.RETRYABLE_WITH_ALERT,
                failure_category="db_resource_exhausted",
                sqlstate=norm_state,
                constraint_name=norm_constraint,
                table_name=norm_table,
                unique_bucket=None,
                should_retry=True,
                is_permanent=False,
                requires_alert=True,
                is_known=True,
                details=err_details,
            )

        if norm_state in RETRYABLE_PG_SQLSTATES or any(
            norm_state.startswith(prefix) for prefix in RETRYABLE_PG_SQLSTATE_PREFIXES
        ):
            return DbErrorClassification(
                disposition=DbRetryDisposition.RETRYABLE,
                failure_category="db_retryable",
                sqlstate=norm_state,
                constraint_name=norm_constraint,
                table_name=norm_table,
                unique_bucket=None,
                should_retry=True,
                is_permanent=False,
                requires_alert=False,
                is_known=True,
                details=err_details,
            )

        if norm_state in PERMANENT_PG_SQLSTATES or any(
            norm_state.startswith(prefix) for prefix in PERMANENT_PG_SQLSTATE_PREFIXES
        ):
            return DbErrorClassification(
                disposition=DbRetryDisposition.PERMANENT,
                failure_category="db_permanent",
                sqlstate=norm_state,
                constraint_name=norm_constraint,
                table_name=norm_table,
                unique_bucket=None,
                should_retry=False,
                is_permanent=True,
                requires_alert=False,
                is_known=True,
                details=err_details,
            )

        # Unrecognized non-empty SQLSTATE must stay explicitly UNKNOWN rather than guessed.
        return DbErrorClassification(
            disposition=DbRetryDisposition.UNKNOWN,
            failure_category="db_unknown_sqlstate",
            sqlstate=norm_state,
            constraint_name=norm_constraint,
            table_name=norm_table,
            unique_bucket=None,
            should_retry=False,
            is_permanent=False,
            requires_alert=True,
            is_known=False,
            details=err_details,
        )

    return DbErrorClassification(
        disposition=DbRetryDisposition.UNKNOWN,
        failure_category="db_unknown",
        sqlstate=None,
        constraint_name=norm_constraint,
        table_name=norm_table,
        unique_bucket=None,
        should_retry=False,
        is_permanent=False,
        requires_alert=True,
        is_known=False,
        details=err_details,
    )


def classify_db_exception(
    exc: BaseException,
    *,
    max_nodes: int = MAX_CHAIN_NODES_DEFAULT,
) -> DbErrorClassification:
    """Extract exception details safely and classify DB/driver failures without guessing."""
    chain = iter_db_exception_chain(exc, max_nodes=max_nodes)
    details = extract_db_error_details(exc, max_nodes=max_nodes)

    if details.sqlstate is not None:
        # RC-04: Handle 57014 contextually before generic SQLSTATE classification.
        if details.sqlstate == "57014":
            if _is_statement_timeout_57014(chain):
                return DbErrorClassification(
                    disposition=DbRetryDisposition.RETRYABLE_WITH_ALERT,
                    failure_category="db_statement_timeout",
                    sqlstate="57014",
                    constraint_name=details.constraint_name,
                    table_name=details.table_name,
                    unique_bucket=None,
                    should_retry=True,
                    is_permanent=False,
                    requires_alert=True,
                    is_known=True,
                    details=details,
                )
            # User request / ambiguous cancel → UNKNOWN.
            return DbErrorClassification(
                disposition=DbRetryDisposition.UNKNOWN,
                failure_category="db_unknown_sqlstate",
                sqlstate="57014",
                constraint_name=details.constraint_name,
                table_name=details.table_name,
                unique_bucket=None,
                should_retry=False,
                is_permanent=False,
                requires_alert=True,
                is_known=False,
                details=details,
            )
        return classify_pg_sqlstate(
            details.sqlstate,
            constraint_name=details.constraint_name,
            table_name=details.table_name,
            details=details,
        )

    if _is_transport_disconnect_chain(chain):
        return DbErrorClassification(
            disposition=DbRetryDisposition.RETRYABLE,
            failure_category="db_connection_retryable",
            sqlstate=None,
            constraint_name=details.constraint_name,
            table_name=details.table_name,
            unique_bucket=None,
            should_retry=True,
            is_permanent=False,
            requires_alert=False,
            is_known=True,
            details=details,
        )

    return classify_pg_sqlstate(
        None,
        constraint_name=details.constraint_name,
        table_name=details.table_name,
        details=details,
    )


# =============================================================================
# RC-01: Webhook-Level Exception Classification (ED-01)
# =============================================================================

# Tier 1: Unequivocal programming errors — deterministic Python code defects.
# TypeError, AttributeError, NameError, UnboundLocalError escape only from bugs;
# blind retrying repeats the identical code defect. Immediate quarantine on attempt 1.
#
# NOT included (Tier 2 — context-dependent):
#   NotImplementedError, AssertionError, ZeroDivisionError, RecursionError,
#   SyntaxError, IndentationError — may stem from OS/provider/runtime capability
#   mismatches, transport assertions, or partial deploy artifacts where bounded
#   retry + alert is safer than irreversible attempt-1 quarantine.
UNEQUIVOCAL_PROGRAMMING_ERROR_TYPES: tuple[type[Exception], ...] = (
    TypeError,
    AttributeError,
    NameError,
    UnboundLocalError,
)


class WebhookExceptionCategory(str, enum.Enum):
    """Declarative classification signals for webhook exception handling.

    Strictly disjoint from BillingWebhookEvent.status values
    {'pending', 'processed', 'failed', 'quarantined'}.
    """

    STALE_LEASE = "stale_lease"
    PERMANENT_ERROR = "permanent_error"
    RETRYABLE_ERROR = "retryable_error"
    PROGRAMMING_ERROR = "programming_error"
    UNKNOWN_EXCEPTION = "unknown_exception"


@dataclass(frozen=True)
class WebhookExceptionClassification:
    """Immutable classification result for a webhook exception.

    Provides declarative signals for Stage 4 worker dispatch without encoding
    retry counts, backoff intervals, queue transitions, or worker state.
    """

    category: WebhookExceptionCategory
    failure_category: str
    error_type: str
    should_quarantine_immediately: bool
    should_retry: bool
    is_lease_error: bool
    is_programming_error: bool
    is_permanent_error: bool
    requires_alert: bool
    sanitized_summary: str
    sqlstate: str | None
    constraint_name: str | None
    table_name: str | None
    unique_bucket: UniqueConstraintBucket | None
    db_classification: DbErrorClassification | None


def is_unequivocal_programming_error(exc: BaseException) -> bool:
    """Return True iff `exc` is a top-level unequivocal programming error.

    Checks only the top-level `exc` instance; never walks `.orig`, `__cause__`,
    or `__context__`. An incidental Tier-1 exception inside a chain MUST NOT
    promote an outer non-Tier-1 exception into immediate quarantine.
    """
    return isinstance(exc, UNEQUIVOCAL_PROGRAMMING_ERROR_TYPES)


def _has_causal_or_db_identity(exc: BaseException) -> bool:
    """Return True iff `exc` has an explicit causal child or direct DB/driver identity.

    Used as a non-override guard (Step 4) to prevent an incidental `__context__`
    DB error from reclassifying an ordinary Python exception (KeyError, ValueError,
    NotImplementedError, etc.) as PERMANENT_ERROR or silent RETRYABLE_ERROR.
    """
    # Explicit causal child (SQLAlchemy .orig or __cause__).
    if _safe_getattr(exc, "orig") is not None:
        return True
    if _safe_getattr(exc, "__cause__") is not None:
        return True
    # Direct DB/driver identity: has sqlstate/pgcode/diag directly on exc.
    if _normalize_sqlstate(_safe_getattr(exc, "sqlstate")) is not None:
        return True
    if _normalize_sqlstate(_safe_getattr(exc, "pgcode")) is not None:
        return True
    diag = _safe_getattr(exc, "diag")
    if diag is not None:
        if _normalize_sqlstate(_safe_getattr(diag, "sqlstate")) is not None:
            return True
    # Transport/disconnect identity.
    if isinstance(exc, (ConnectionError, BrokenPipeError, EOFError, TimeoutError)):
        return True
    if any(cls.__name__ in _TRANSPORT_CLASS_NAMES for cls in type(exc).__mro__):
        return True
    return False


def classify_webhook_exception(
    exc: BaseException,
    *,
    max_nodes: int = MAX_CHAIN_NODES_DEFAULT,
) -> WebhookExceptionClassification:
    """Classify a webhook exception using 5-step precedence logic.

    Step 1: Re-raise non-Exception BaseException (CancelledError, KeyboardInterrupt,
            SystemExit) — these must propagate untouched.
    Step 2: Authoritative BillingWebhookError hierarchy — outer type wins.
    Step 3: Top-level Tier-1 unequivocal programming error check.
    Step 4: Non-override guard — prevents ambient __context__ DB error from
            overriding an ordinary Python exception.
    Step 5: Causal-first database / transport classification fallback.
    """
    # Step 1: Non-Exception BaseException must propagate without classification.
    if not isinstance(exc, Exception):
        raise exc

    error_type = type(exc).__name__
    sanitized = sanitize_billing_error_message(_safe_str(exc))

    # Step 2: Authoritative BillingWebhookError hierarchy.
    if isinstance(exc, BillingWebhookError):
        # Read optional DB context as read-only diagnostic metadata.
        db_cls: DbErrorClassification | None = None
        try:
            db_cls = classify_db_exception(exc, max_nodes=max_nodes)
        except Exception:
            pass

        if isinstance(exc, BillingWebhookLeaseError):
            return WebhookExceptionClassification(
                category=WebhookExceptionCategory.STALE_LEASE,
                failure_category=exc.failure_category,
                error_type=error_type,
                should_quarantine_immediately=False,
                should_retry=False,
                is_lease_error=True,
                is_programming_error=False,
                is_permanent_error=False,
                requires_alert=False,
                sanitized_summary=sanitized,
                sqlstate=db_cls.sqlstate if db_cls else None,
                constraint_name=db_cls.constraint_name if db_cls else None,
                table_name=db_cls.table_name if db_cls else None,
                unique_bucket=db_cls.unique_bucket if db_cls else None,
                db_classification=db_cls,
            )
        if isinstance(exc, BillingWebhookPermanentError):
            return WebhookExceptionClassification(
                category=WebhookExceptionCategory.PERMANENT_ERROR,
                failure_category=exc.failure_category,
                error_type=error_type,
                should_quarantine_immediately=True,
                should_retry=False,
                is_lease_error=False,
                is_programming_error=False,
                is_permanent_error=True,
                requires_alert=False,
                sanitized_summary=sanitized,
                sqlstate=db_cls.sqlstate if db_cls else None,
                constraint_name=db_cls.constraint_name if db_cls else None,
                table_name=db_cls.table_name if db_cls else None,
                unique_bucket=db_cls.unique_bucket if db_cls else None,
                db_classification=db_cls,
            )
        if isinstance(exc, BillingWebhookRetryableError):
            return WebhookExceptionClassification(
                category=WebhookExceptionCategory.RETRYABLE_ERROR,
                failure_category=exc.failure_category,
                error_type=error_type,
                should_quarantine_immediately=False,
                should_retry=True,
                is_lease_error=False,
                is_programming_error=False,
                is_permanent_error=False,
                requires_alert=(db_cls.requires_alert if db_cls else False),
                sanitized_summary=sanitized,
                sqlstate=db_cls.sqlstate if db_cls else None,
                constraint_name=db_cls.constraint_name if db_cls else None,
                table_name=db_cls.table_name if db_cls else None,
                unique_bucket=db_cls.unique_bucket if db_cls else None,
                db_classification=db_cls,
            )
        # Bare BillingWebhookError (not specialized) → UNKNOWN_EXCEPTION with alert.
        return WebhookExceptionClassification(
            category=WebhookExceptionCategory.UNKNOWN_EXCEPTION,
            failure_category=exc.failure_category,
            error_type=error_type,
            should_quarantine_immediately=False,
            should_retry=True,
            is_lease_error=False,
            is_programming_error=False,
            is_permanent_error=False,
            requires_alert=True,
            sanitized_summary=sanitized,
            sqlstate=db_cls.sqlstate if db_cls else None,
            constraint_name=db_cls.constraint_name if db_cls else None,
            table_name=db_cls.table_name if db_cls else None,
            unique_bucket=db_cls.unique_bucket if db_cls else None,
            db_classification=db_cls,
        )

    # Step 3: Top-level Tier-1 unequivocal programming error.
    # Evaluates exc ONLY — never walks .orig, __cause__, or __context__.
    if is_unequivocal_programming_error(exc):
        return WebhookExceptionClassification(
            category=WebhookExceptionCategory.PROGRAMMING_ERROR,
            failure_category="programming_error",
            error_type=error_type,
            should_quarantine_immediately=True,
            should_retry=False,
            is_lease_error=False,
            is_programming_error=True,
            is_permanent_error=True,
            requires_alert=True,
            sanitized_summary=sanitized,
            sqlstate=None,
            constraint_name=None,
            table_name=None,
            unique_bucket=None,
            db_classification=None,
        )

    # Step 4: Non-override guard — prevent ambient __context__ DB error from masking exc.
    # If exc has no causal/DB identity, classify as UNKNOWN_EXCEPTION without DB reclassification.
    if not _has_causal_or_db_identity(exc):
        return WebhookExceptionClassification(
            category=WebhookExceptionCategory.UNKNOWN_EXCEPTION,
            failure_category="unexpected_internal_error",
            error_type=error_type,
            should_quarantine_immediately=False,
            should_retry=True,
            is_lease_error=False,
            is_programming_error=False,
            is_permanent_error=False,
            requires_alert=True,
            sanitized_summary=sanitized,
            sqlstate=None,
            constraint_name=None,
            table_name=None,
            unique_bucket=None,
            db_classification=None,
        )

    # Step 5: Causal-first DB / transport classification fallback.
    db_cls = classify_db_exception(exc, max_nodes=max_nodes)

    if db_cls.disposition == DbRetryDisposition.PERMANENT:
        return WebhookExceptionClassification(
            category=WebhookExceptionCategory.PERMANENT_ERROR,
            failure_category=db_cls.failure_category,
            error_type=error_type,
            should_quarantine_immediately=True,
            should_retry=False,
            is_lease_error=False,
            is_programming_error=False,
            is_permanent_error=True,
            requires_alert=False,
            sanitized_summary=sanitized,
            sqlstate=db_cls.sqlstate,
            constraint_name=db_cls.constraint_name,
            table_name=db_cls.table_name,
            unique_bucket=db_cls.unique_bucket,
            db_classification=db_cls,
        )

    if db_cls.disposition in (DbRetryDisposition.RETRYABLE, DbRetryDisposition.RETRYABLE_WITH_ALERT):
        return WebhookExceptionClassification(
            category=WebhookExceptionCategory.RETRYABLE_ERROR,
            failure_category=db_cls.failure_category,
            error_type=error_type,
            should_quarantine_immediately=False,
            should_retry=True,
            is_lease_error=False,
            is_programming_error=False,
            is_permanent_error=False,
            requires_alert=db_cls.requires_alert,
            sanitized_summary=sanitized,
            sqlstate=db_cls.sqlstate,
            constraint_name=db_cls.constraint_name,
            table_name=db_cls.table_name,
            unique_bucket=db_cls.unique_bucket,
            db_classification=db_cls,
        )

    # UNKNOWN disposition → UNKNOWN_EXCEPTION with alert.
    return WebhookExceptionClassification(
        category=WebhookExceptionCategory.UNKNOWN_EXCEPTION,
        failure_category=db_cls.failure_category,
        error_type=error_type,
        should_quarantine_immediately=False,
        should_retry=True,
        is_lease_error=False,
        is_programming_error=False,
        is_permanent_error=False,
        requires_alert=True,
        sanitized_summary=sanitized,
        sqlstate=db_cls.sqlstate,
        constraint_name=db_cls.constraint_name,
        table_name=db_cls.table_name,
        unique_bucket=db_cls.unique_bucket,
        db_classification=db_cls,
    )


__all__ = [
    "ALERT_RETRYABLE_PG_SQLSTATES",
    "BillingWebhookConflictError",
    "BillingWebhookError",
    "BillingWebhookLeaseError",
    "BillingWebhookPermanentError",
    "BillingWebhookRetryableError",
    "BillingWebhookScopeMismatchError",
    "BillingWebhookUnknownEventError",
    "BillingWebhookValidationError",
    "DbErrorClassification",
    "DbErrorDetails",
    "DbRetryDisposition",
    "MAX_CHAIN_NODES_DEFAULT",
    "MAX_SANITIZED_ERROR_CHARS",
    "MAX_TRACEBACK_FRAMES",
    "PERMANENT_PG_SQLSTATES",
    "PERMANENT_PG_SQLSTATE_PREFIXES",
    "RETRYABLE_PG_SQLSTATES",
    "RETRYABLE_PG_SQLSTATE_PREFIXES",
    "UNIQUE_CONCURRENCY_RACE_EXACT",
    "UNIQUE_CONCURRENCY_RACE_PREFIXES",
    "UNIQUE_CONVERGENCE_CONSTRAINTS",
    "UNIQUE_PERMANENT_CONSTRAINTS",
    "UNEQUIVOCAL_PROGRAMMING_ERROR_TYPES",
    "UniqueConstraintBucket",
    "WebhookExceptionCategory",
    "WebhookExceptionClassification",
    "classify_db_exception",
    "classify_pg_sqlstate",
    "classify_unique_violation_constraint",
    "classify_webhook_exception",
    "extract_db_error_details",
    "extract_safe_traceback_locations",
    "is_unequivocal_programming_error",
    "iter_db_exception_chain",
    "sanitize_billing_error_message",
    "sanitize_conflict_diagnostic_payload",
]
