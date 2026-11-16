"""Billing and Stripe customer API router."""

import logging
import urllib.parse
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.organizations import _require_org_role
from app.auth.dependencies import AuthenticatedUser
from app.config import settings
from app.db.session import AsyncSessionFactory, get_db, get_session_factory
from app.models.billing import OrganizationBillingPeriod
from app.models.organization import MemberRole, Organization
from app.models.subscription import Subscription, SubscriptionStatus
from app.schemas.billing import (
    BillingPeriodOut,
    BillingSubscriptionOut,
    RazorpayCheckoutVerificationIn,
    RazorpayCheckoutVerificationOut,
    RazorpaySubscriptionCheckoutOut,
    StripeBillingPortalOut,
    StripeCheckoutSessionOut,
    StripeCustomerOut,
)
from app.services.billing.razorpay.checkout import (
    ActiveSubscriptionExistsError as RazorpayActiveSubscriptionExistsError,
    CheckoutInProgressError as RazorpayCheckoutInProgressError,
    CheckoutRecoveryRequiredError as RazorpayCheckoutRecoveryRequiredError,
    RazorpayCheckoutAmbiguousError,
    RazorpayCheckoutConflictError,
    RazorpayCheckoutError,
    RazorpayCheckoutNotFoundError,
    RazorpayCheckoutPaymentError,
    RazorpayCheckoutSignatureError,
    create_razorpay_subscription_checkout,
    verify_razorpay_checkout_callback,
)
from app.services.billing.razorpay.client import (
    RazorpayBillingDisabledError,
    RazorpayConfigurationError,
)
from app.services.billing.razorpay.webhook_ingress import (
    RazorpayWebhookMerchantMismatchError,
    RazorpayWebhookPayloadMalformedError,
    RazorpayWebhookPayloadTooLargeError,
    RazorpayWebhookPersistenceError,
    RazorpayWebhookSignatureVerificationError,
    extract_razorpay_event_id,
    get_webhook_scope,
    ingest_razorpay_webhook_event,
    parse_and_validate_razorpay_envelope,
    verify_razorpay_webhook_signature,
)
from app.services.billing.stripe_checkout import (
    ActiveSubscriptionExistsError,
    CheckoutInProgressError,
    CheckoutRecoveryRequiredError,
    StripeCheckoutError,
    create_stripe_checkout_session,
)
from app.services.billing.stripe_client import (
    StripeBillingDisabledError,
    StripeConfigurationError,
    get_stripe_client,
    is_stripe_enabled,
)
from app.services.billing.stripe_customer import (
    StripeCustomerError,
    get_or_create_stripe_customer,
    get_stripe_customer_id,
)
from app.services.billing.stripe_webhook import (
    WebhookPayloadMalformedError,
    WebhookPayloadTooLargeError,
    WebhookPersistenceError,
    WebhookSignatureVerificationError,
    ingest_stripe_webhook_event,
    read_bounded_request_body,
    verify_stripe_webhook_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/organizations/{org_id}/billing", tags=["billing"])
webhook_router = APIRouter(prefix="/billing", tags=["billing-webhook"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
BillingSessionFactory = Annotated[AsyncSessionFactory, Depends(get_session_factory)]


def validate_stripe_redirect_url(url: str) -> None:
    """Validate that the checkout redirect URL is strictly a genuine Stripe Checkout HTTPS URL.

    Security criteria:
    - Must be a non-empty string.
    - Require strictly HTTPS scheme.
    - Require exact hostname 'checkout.stripe.com' (no broad suffix matching).
    - Reject unexpected ports (only default HTTPS port 443 or None allowed).
    - Reject user credentials (username/password in URL netloc).
    - Reject malformed URLs or invalid hostnames.
    """
    if not url or not isinstance(url, str) or not url.strip():
        raise ValueError("Checkout URL must be a non-empty string")

    parsed = urllib.parse.urlsplit(url.strip())

    if parsed.scheme != "https":
        raise ValueError(f"Invalid checkout URL scheme: '{parsed.scheme}'. HTTPS is required.")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Invalid checkout URL: credentials in URL are forbidden.")

    if parsed.hostname != "checkout.stripe.com":
        raise ValueError(
            f"Invalid checkout URL hostname: '{parsed.hostname}'. Expected 'checkout.stripe.com'."
        )

    # Reject unexpected ports
    if parsed.port is not None and parsed.port != 443:
        raise ValueError(f"Invalid checkout URL port: {parsed.port}.")

    # Netloc should strictly be 'checkout.stripe.com' (or 'checkout.stripe.com:443')
    if parsed.netloc not in ("checkout.stripe.com", "checkout.stripe.com:443"):
        raise ValueError(f"Invalid checkout URL netloc: '{parsed.netloc}'.")


def validate_stripe_portal_url(url: str) -> None:
    """Validate that the portal redirect URL is strictly a genuine Stripe Customer Portal HTTPS URL.

    Security criteria:
    - Must be a non-empty string.
    - Require strictly HTTPS scheme.
    - Require exact hostname 'billing.stripe.com' (no broad suffix matching).
    - Reject unexpected ports (only default HTTPS port 443 or None allowed).
    - Reject user credentials (username/password in URL netloc).
    - Reject malformed URLs or invalid hostnames.
    """
    if not url or not isinstance(url, str) or not url.strip():
        raise ValueError("Portal URL must be a non-empty string")

    parsed = urllib.parse.urlsplit(url.strip())

    if parsed.scheme != "https":
        raise ValueError(f"Invalid portal URL scheme: '{parsed.scheme}'. HTTPS is required.")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Invalid portal URL: credentials in URL are forbidden.")

    if parsed.hostname != "billing.stripe.com":
        raise ValueError(
            f"Invalid portal URL hostname: '{parsed.hostname}'. Expected 'billing.stripe.com'."
        )

    # Reject unexpected ports
    if parsed.port is not None and parsed.port != 443:
        raise ValueError(f"Invalid portal URL port: {parsed.port}.")

    # Netloc should strictly be 'billing.stripe.com' (or 'billing.stripe.com:443')
    if parsed.netloc not in ("billing.stripe.com", "billing.stripe.com:443"):
        raise ValueError(f"Invalid portal URL netloc: '{parsed.netloc}'.")


def _call_stripe_create_portal_session(
    customer_id: str,
    return_url: str,
) -> Any:
    """Invoke the external Stripe Billing Portal Sessions creation API."""
    client = get_stripe_client()
    v1 = getattr(client, "v1", None)
    if v1 is not None and hasattr(v1, "billing_portal"):
        portal = v1.billing_portal
    else:
        portal = getattr(client, "billing_portal", None)
    if portal is None:
        raise StripeConfigurationError("Stripe client does not provide a billing_portal service")
    sessions = getattr(portal, "sessions", getattr(portal, "session", None))
    if sessions is None or not hasattr(sessions, "create"):
        raise StripeConfigurationError("Stripe client does not provide billing portal sessions service")

    return sessions.create(
        params={
            "customer": customer_id,
            "return_url": return_url,
        }
    )


@router.get("/customer", response_model=StripeCustomerOut)
async def get_billing_customer(
    org_id: uuid.UUID,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> StripeCustomerOut:
    """Retrieve the current organization's billing customer association.

    Guarantees:
    - Organization scoped: customer is tied strictly to org_id.
    - Safe metadata: returns only safe customer identification without internal secrets.
    - Tenant isolation: verifies caller membership and role (minimum role: admin).
    - Local authority: retrieves locally persisted customer association without requiring Stripe connectivity.
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.admin)

    customer_id = await get_stripe_customer_id(db, org_id)
    if not customer_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No billing customer found for this organization",
        )

    return StripeCustomerOut(
        organization_id=org_id,
        stripe_customer_id=customer_id,
    )


@router.post("/customer", response_model=StripeCustomerOut, status_code=status.HTTP_200_OK)
async def create_or_get_billing_customer(
    org_id: uuid.UUID,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> StripeCustomerOut:
    """Obtain or create a Stripe customer association for the organization.

    Guarantees:
    - Organization scoped: customer is tied strictly to org_id.
    - Idempotent: creates customer only once; reuses existing association without contacting Stripe.
    - Safe metadata: passes legitimate org details without exposing sensitive internals.
    - Concurrency-safe: handles uniqueness race conditions gracefully.
    - Tenant isolation: verifies caller membership and role (minimum role: admin).
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.admin)

    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    try:
        customer_id = await get_or_create_stripe_customer(
            db=db,
            organization=org,
            user_email=current_user.email,
        )
    except StripeBillingDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    except StripeCustomerError as exc:
        logger.error("Failed to create billing customer for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error while creating customer",
        )

    return StripeCustomerOut(
        organization_id=org_id,
        stripe_customer_id=customer_id,
    )


@router.post(
    "/checkout-session",
    response_model=StripeCheckoutSessionOut,
    status_code=status.HTTP_200_OK,
)
async def create_checkout_session(
    org_id: uuid.UUID,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> StripeCheckoutSessionOut:
    """Initiate or reuse a Stripe Pro subscription checkout session for the organization.

    Security & Domain Guarantees:
    - Authentication required (401 for unauthenticated).
    - Tenant isolation: verified membership and role (minimum role: admin).
      Non-members receive 404 Not Found to prevent tenant enumeration.
      Members with role 'member' receive 403 Forbidden.
    - Zero client-controlled billing parameters:
      Customer ID, Price ID, URLs, metadata, and idempotency keys are server-controlled.
    - Verified checkout redirect URL:
      Strictly requires HTTPS and exact hostname 'checkout.stripe.com'.
    - Safe error mapping:
      Never leaks Stripe secrets, raw payloads, or internal exception messages.
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.admin)

    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    try:
        result = await create_stripe_checkout_session(
            db=db,
            organization=org,
            user_email=current_user.email,
        )
    except (StripeBillingDisabledError, StripeConfigurationError) as exc:
        logger.warning("Stripe billing disabled/misconfigured for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe billing is currently disabled or unavailable",
        )
    except ActiveSubscriptionExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization already has an active subscription",
        )
    except CheckoutInProgressError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A checkout session or payment is already in progress for this organization",
        )
    except CheckoutRecoveryRequiredError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A previous checkout attempt requires reconciliation before a new session can be initiated",
        )
    except StripeCheckoutError as exc:
        logger.error("Stripe checkout error for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error while creating checkout session",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error during checkout session creation for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )

    try:
        validate_stripe_redirect_url(result.checkout_url)
    except ValueError as exc:
        logger.error("Invalid checkout redirect URL for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider returned an invalid checkout URL",
        )

    return StripeCheckoutSessionOut(
        organization_id=result.organization_id,
        session_id=result.stripe_session_id,
        checkout_url=result.checkout_url,
    )


@router.post(
    "/razorpay/checkout-subscription",
    response_model=RazorpaySubscriptionCheckoutOut,
    status_code=status.HTTP_200_OK,
)
async def create_razorpay_checkout_subscription_endpoint(
    org_id: uuid.UUID,
    current_user: AuthenticatedUser,
    db: DbSession,
    session_factory: BillingSessionFactory,
) -> RazorpaySubscriptionCheckoutOut:
    """Initiate or reuse a Razorpay Pro subscription checkout session for the organization.

    Security & Domain Guarantees:
    - Authentication required (401 for unauthenticated).
    - Tenant isolation: verified membership and role (minimum role: admin).
      Non-members receive 404 Not Found to prevent tenant enumeration.
      Members with role 'member' receive 403 Forbidden.
    - Resolves customer identity through the decoupled customer provisioning service.
    - Zero client-controlled billing parameters: plan ID, currency, and scope are server-controlled.
    - Server-controlled checkout attempt tracking: enforces single active checkout attempt.
    - Never grants Pro entitlement or provisions billing quota.
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.admin)

    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    try:
        result = await create_razorpay_subscription_checkout(
            db=db,
            organization=org,
            user_email=current_user.email,
            customer_session_factory=session_factory,
        )
    except (RazorpayBillingDisabledError, RazorpayConfigurationError) as exc:
        logger.warning("Razorpay billing disabled/misconfigured for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Razorpay billing is currently disabled or unavailable",
        )
    except RazorpayActiveSubscriptionExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization already has an active subscription",
        )
    except RazorpayCheckoutInProgressError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A checkout session or payment is already in progress for this organization",
        )
    except RazorpayCheckoutRecoveryRequiredError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A previous checkout attempt requires reconciliation before a new session can be initiated",
        )
    except RazorpayCheckoutAmbiguousError as exc:
        logger.error("Ambiguous Razorpay checkout outcome for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider outcome is ambiguous; recovery required",
        )
    except RazorpayCheckoutError as exc:
        logger.error("Razorpay checkout error for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error while creating checkout session",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error during Razorpay checkout session creation for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )

    return RazorpaySubscriptionCheckoutOut(
        organization_id=result.organization_id,
        attempt_id=result.attempt_id,
        provider=result.provider,
        subscription_id=result.subscription_id,
        key_id=result.key_id,
        plan_id=result.plan_id,
        checkout_url=result.checkout_url,
        status=result.status.value,
    )


@router.post(
    "/razorpay/verify-checkout",
    response_model=RazorpayCheckoutVerificationOut,
    status_code=status.HTTP_200_OK,
)
async def verify_razorpay_checkout_endpoint(
    org_id: uuid.UUID,
    payload: RazorpayCheckoutVerificationIn,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> RazorpayCheckoutVerificationOut:
    """Verify a Razorpay standard checkout callback, validate payment, and record verification.

    Security & Domain Guarantees:
    - Authentication required (401 for unauthenticated).
    - Tenant isolation: verified membership and role (minimum role: admin).
    - Never trusts browser-supplied subscription ID; verifies against server-owned checkout attempt.
    - Cryptographic HMAC-SHA256 constant-time verification.
    - Validates gateway payment status outside database transactions.
    - Distinguishes captured payments from pending authorizations.
    - Idempotent: repeated callbacks with matching payment ID return verified state.
    - Entitlement Protection: never provisions Pro tier or grants token quota.
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.admin)

    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    try:
        result = await verify_razorpay_checkout_callback(
            db=db,
            organization_id=org_id,
            attempt_id=payload.attempt_id,
            razorpay_payment_id=payload.razorpay_payment_id,
            razorpay_subscription_id=payload.razorpay_subscription_id,
            razorpay_signature=payload.razorpay_signature,
        )
    except RazorpayCheckoutNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Checkout attempt not found",
        )
    except RazorpayCheckoutSignatureError as exc:
        logger.warning("Razorpay checkout signature verification failed for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except RazorpayCheckoutPaymentError as exc:
        logger.warning("Razorpay payment verification failed for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except RazorpayCheckoutConflictError as exc:
        logger.warning("Razorpay checkout conflict for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except (RazorpayBillingDisabledError, RazorpayConfigurationError) as exc:
        logger.warning("Razorpay billing disabled/misconfigured for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Razorpay billing is currently disabled or unavailable",
        )
    except RazorpayCheckoutError as exc:
        logger.error("Razorpay checkout verification error for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error while verifying checkout",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected error verifying Razorpay checkout for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )

    return RazorpayCheckoutVerificationOut(
        organization_id=result.organization_id,
        attempt_id=result.attempt_id,
        provider=result.provider,
        subscription_id=result.subscription_id,
        payment_id=result.payment_id,
        status=result.status.value,
        payment_status=result.payment_status,
    )


@router.get(
    "/subscription",
    response_model=BillingSubscriptionOut,
    status_code=status.HTTP_200_OK,
)
async def get_billing_subscription(
    org_id: uuid.UUID,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> BillingSubscriptionOut:
    """Retrieve the organization's subscription status and active billing period usage.

    Security & Domain Guarantees:
    - Authentication required (401 for unauthenticated).
    - Tenant isolation: verified membership (minimum role: member).
      Non-members receive 404 Not Found to prevent tenant enumeration.
    - Zero data leakage: never exposes Stripe secrets, webhook payloads, or payment details.
    - Local authority: reads from local database; works even if Stripe is disabled.
    - Non-mutating: strictly read-only; never fabricates or rolls over billing periods.
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.member)

    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # 1. Query active OrganizationBillingPeriod (guaranteed at most one by partial unique index)
    stmt_bp = (
        select(OrganizationBillingPeriod)
        .where(
            OrganizationBillingPeriod.organization_id == org_id,
            OrganizationBillingPeriod.is_active == True,
        )
    )
    res_bp = await db.execute(stmt_bp)
    active_bp = res_bp.scalar_one_or_none()

    billing_period_out: BillingPeriodOut | None = None
    if active_bp is not None:
        tokens_remaining = max(
            0,
            active_bp.monthly_token_quota - active_bp.tokens_used - active_bp.tokens_reserved,
        )
        billing_period_out = BillingPeriodOut(
            id=active_bp.id,
            period_start=active_bp.period_start,
            period_end=active_bp.period_end,
            plan_tier=active_bp.plan_tier,
            monthly_token_quota=active_bp.monthly_token_quota,
            tokens_used=active_bp.tokens_used,
            tokens_reserved=active_bp.tokens_reserved,
            tokens_remaining=tokens_remaining,
            is_blocked=active_bp.is_blocked,
            blocked_reason=active_bp.blocked_reason,
        )

    # 2. Query subscriptions for organization, ordered by status priority
    #    Active/trialing > past_due > paused/unpaid > incomplete > canceled/expired
    status_priority = case(
        (Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.trialing]), 1),
        (Subscription.status == SubscriptionStatus.past_due, 2),
        (Subscription.status.in_([SubscriptionStatus.paused, SubscriptionStatus.unpaid]), 3),
        (Subscription.status == SubscriptionStatus.incomplete, 4),
        else_=5,
    )
    stmt_sub = (
        select(Subscription)
        .where(Subscription.organization_id == org_id)
        .order_by(status_priority, Subscription.created_at.desc())
        .limit(1)
    )
    res_sub = await db.execute(stmt_sub)
    sub = res_sub.scalar_one_or_none()

    # 3. Check customer existence
    customer_id = (
        sub.stripe_customer_id
        if sub and sub.stripe_customer_id
        else await get_stripe_customer_id(db, org_id)
    )
    has_stripe_customer = bool(customer_id and customer_id.strip())

    stripe_enabled = is_stripe_enabled()

    # 4. Resolve subscription status and plan tier
    if sub is not None:
        return BillingSubscriptionOut(
            organization_id=org_id,
            plan_tier=sub.plan_tier,
            status=sub.status.value,
            current_period_start=sub.current_period_start,
            current_period_end=sub.current_period_end,
            cancel_at_period_end=bool(sub.cancel_at_period_end),
            canceled_at=sub.canceled_at,
            past_due_since=sub.past_due_since,
            has_stripe_customer=has_stripe_customer,
            stripe_enabled=stripe_enabled,
            billing_period=billing_period_out,
        )

    # Safe Free-tier display defaults when no Stripe subscription exists
    effective_plan = active_bp.plan_tier if active_bp is not None else "free"
    return BillingSubscriptionOut(
        organization_id=org_id,
        plan_tier=effective_plan,
        status="active",
        current_period_start=None,
        current_period_end=None,
        cancel_at_period_end=False,
        canceled_at=None,
        past_due_since=None,
        has_stripe_customer=has_stripe_customer,
        stripe_enabled=stripe_enabled,
        billing_period=billing_period_out,
    )


@router.post(
    "/portal",
    response_model=StripeBillingPortalOut,
    status_code=status.HTTP_200_OK,
)
async def create_billing_portal_session(
    org_id: uuid.UUID,
    current_user: AuthenticatedUser,
    db: DbSession,
) -> StripeBillingPortalOut:
    """Create a Stripe Billing Portal session for managing subscription and payment methods.

    Security & Domain Guarantees:
    - Authentication required (401 for unauthenticated).
    - Tenant isolation: verified membership and role (minimum role: admin).
      Non-members receive 404 Not Found to prevent tenant enumeration.
      Members with role 'member' receive 403 Forbidden.
    - Zero client-controlled billing parameters:
      Customer ID, return URL, and metadata are server-controlled.
    - Customer verification:
      Customer ID is resolved from local database association.
      If missing, returns 404 Not Found (no silent customer creation).
    - Verified portal redirect URL:
      Strictly requires HTTPS and exact hostname 'billing.stripe.com'.
    - Safe error mapping:
      Never leaks Stripe secrets, raw payloads, or internal exception messages.
    """
    await _require_org_role(org_id, current_user.id, db, minimum_role=MemberRole.admin)

    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    customer_id = await get_stripe_customer_id(db, org_id)
    if not customer_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No billing customer found for this organization",
        )

    if not is_stripe_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe billing is currently disabled",
        )

    return_url = settings.stripe_billing_return_url.strip() if settings.stripe_billing_return_url else ""
    if not return_url:
        logger.error("Stripe billing return URL is not configured for portal in org %s", org_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe billing configuration unavailable",
        )

    try:
        session = _call_stripe_create_portal_session(
            customer_id=customer_id,
            return_url=return_url,
        )
    except (StripeBillingDisabledError, StripeConfigurationError) as exc:
        logger.warning("Stripe billing disabled/misconfigured for portal in org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe billing is currently disabled or unavailable",
        )
    except Exception as exc:
        logger.error("Stripe portal session error for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error while creating billing portal session",
        )

    portal_url = getattr(session, "url", None) or (session.get("url") if isinstance(session, dict) else None)
    if not portal_url or not isinstance(portal_url, str):
        logger.error("Stripe billing portal session missing or invalid URL for org %s", org_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider returned an invalid billing portal URL",
        )

    try:
        validate_stripe_portal_url(portal_url)
    except ValueError as exc:
        logger.error("Invalid portal redirect URL for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider returned an invalid billing portal URL",
        )

    return StripeBillingPortalOut(
        organization_id=org_id,
        portal_url=portal_url,
    )


@webhook_router.post("/webhook", status_code=status.HTTP_200_OK)
async def handle_stripe_webhook(
    request: Request,
    db: DbSession,
) -> dict[str, Any]:
    """Receive, verify, and durably ingest incoming Stripe webhook events.

    Security & Reliability Guarantees:
    - Public Unauthenticated: Called directly by Stripe without user authentication.
    - Strict Request Body Limit: Maximum 64 KB streaming payload limit enforced.
    - Cryptographic Verification: HMAC-SHA256 signature verified against original raw bytes.
    - Zero Secret Leakage: Webhook secret, signature, and raw payload are never logged.
    - Durable Receipt: Transaction committed to database before HTTP 200 response is sent.
    - Safe Idempotency: Duplicate deliveries return HTTP 200 without altering state.
    - Fail-Safe Configuration: Returns 503 if Stripe is disabled or secret is missing.
    """
    try:
        raw_body = await read_bounded_request_body(request)
    except WebhookPayloadTooLargeError:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Webhook payload exceeds maximum allowed size (64 KB)",
        )

    sig_header = request.headers.get("stripe-signature")

    try:
        event_payload = verify_stripe_webhook_signature(
            raw_payload=raw_body,
            sig_header=sig_header,
        )
    except StripeBillingDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe billing is currently disabled",
        )
    except StripeConfigurationError as exc:
        logger.error("Stripe webhook configuration error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe webhook configuration unavailable",
        )
    except (WebhookSignatureVerificationError, WebhookPayloadMalformedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    event_id = event_payload["id"]
    event_type = event_payload["type"]

    try:
        event_record, is_new = await ingest_stripe_webhook_event(
            db=db,
            event_id=event_id,
            event_type=event_type,
            payload=event_payload,
        )
    except WebhookPersistenceError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to durably persist webhook event",
        )
    except Exception as exc:
        logger.error("Unexpected error persisting Stripe webhook event %s: %s", event_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )

    return {
        "received": True,
        "event_id": event_id,
        "status": "pending" if is_new else "duplicate",
    }


@webhook_router.post("/razorpay/webhook", status_code=status.HTTP_200_OK)
async def handle_razorpay_webhook(
    request: Request,
    db: DbSession,
) -> dict[str, Any]:
    """Receive, verify, and durably ingest incoming Razorpay webhook events.

    Security & Reliability Guarantees:
    - Public Unauthenticated: Called directly by Razorpay without user authentication.
    - Strict Request Body Limit: Maximum 64 KB streaming payload limit enforced dynamically.
    - Cryptographic Verification: HMAC-SHA256 signature verified against exact raw bytes.
    - Constant-time comparison: Rejects invalid or malformed signatures before JSON parsing.
    - Zero Secret Leakage: Webhook secret, signature, and raw payload are never logged.
    - Durable Receipt: Transaction committed to database before HTTP 200 response is sent.
    - Safe Idempotency: Duplicate deliveries return HTTP 200 without altering state or creating records.
    - Fail-Safe Configuration: Returns 503 if Razorpay is disabled or secret is missing.
    - Tenant Isolation: Validates that payload account_id matches configured merchant account.
    """
    try:
        raw_body = await read_bounded_request_body(request)
    except (WebhookPayloadTooLargeError, RazorpayWebhookPayloadTooLargeError):
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Webhook payload exceeds maximum allowed size (64 KB)",
        )

    sig_header = request.headers.get("x-razorpay-signature")
    event_id_header = request.headers.get("x-razorpay-event-id")

    try:
        verify_razorpay_webhook_signature(
            raw_payload=raw_body,
            sig_header=sig_header,
        )
    except RazorpayBillingDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Razorpay billing is currently disabled",
        )
    except RazorpayConfigurationError as exc:
        logger.error("Razorpay webhook configuration error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Razorpay webhook configuration unavailable",
        )
    except RazorpayWebhookSignatureVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    try:
        event_payload = parse_and_validate_razorpay_envelope(raw_body)
    except (RazorpayWebhookPayloadMalformedError, RazorpayWebhookMerchantMismatchError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    event_id = extract_razorpay_event_id(
        raw_payload=raw_body,
        event_payload=event_payload,
        event_id_header=event_id_header,
    )
    event_type = event_payload["event"]

    try:
        expected_merchant, env = get_webhook_scope()
        payload_account_id = str(event_payload.get("account_id", "")).strip()
        merchant_account_id = (
            payload_account_id if expected_merchant == "default" else expected_merchant
        )
    except Exception as exc:
        logger.error("Error resolving Razorpay webhook scope: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Razorpay webhook configuration unavailable",
        )

    try:
        event_record, is_new = await ingest_razorpay_webhook_event(
            db=db,
            event_id=event_id,
            event_type=event_type,
            payload=event_payload,
            merchant_account_id=merchant_account_id,
            environment=env,
        )
    except (RazorpayWebhookPersistenceError, IntegrityError):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to durably persist webhook event",
        )
    except Exception as exc:
        logger.error(
            "Unexpected error persisting Razorpay webhook event %s: %s", event_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )

    return {
        "received": True,
        "event_id": event_id,
        "status": "pending" if is_new else "duplicate",
    }



