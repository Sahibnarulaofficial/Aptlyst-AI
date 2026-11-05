"""Domain service for organization-scoped Stripe customer management.

Guarantees:
1. Idempotency & Reuse: Reuses existing customer record from database whenever available.
2. Safe Deterministic Keying: When creating on Stripe, supplies an idempotency key
   scoped to the organization (`create-customer-{org_id}`). Note that Stripe idempotency
   keys expire after 24 hours and do not provide permanent duplicate prevention.
3. Safe Metadata: Supplies only safe organization identification in metadata
   (`organization_id`). Never passes database secrets or internal billing keys.
4. Concurrency & Race Conflict Recovery: If concurrent requests trigger a database
   uniqueness conflict on insertion, catches IntegrityError, rolls back, and returns
   the winning committed customer ID.
5. Transaction Boundary Safety: Does not hold open database transactions across
   external Stripe network calls.
6. Local Authority & Non-Misrepresentation: Persists customer ID in `subscriptions`
   table with `status=incomplete` and `plan_tier=free` without falsely granting
   paid subscription entitlement.

Architectural Constraints & Lifecycle Invariants:
The current database schema does not enforce one Stripe customer association per
organization. The service relies on organization-scoped lookup, Stripe idempotency,
and uniqueness-conflict recovery. Future webhook and subscription lifecycle handlers
must update existing customer-associated rows rather than blindly inserting new rows.
"""

import logging
import uuid
from typing import Any

import stripe
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.subscription import Subscription, SubscriptionStatus
from app.services.billing.stripe_client import (
    StripeBillingDisabledError,
    StripeConfigurationError,
    get_stripe_client,
    is_stripe_enabled,
)

logger = logging.getLogger(__name__)


class StripeCustomerError(Exception):
    """Raised when customer management operations fail."""


async def get_stripe_customer_id(
    db: AsyncSession,
    organization_id: uuid.UUID,
) -> str | None:
    """Retrieve the Stripe customer ID associated with an organization.

    Args:
        db: Database async session.
        organization_id: UUID of the organization.

    Returns:
        The stripe_customer_id string if exists, otherwise None.
    """
    stmt = (
        select(Subscription.stripe_customer_id)
        .where(Subscription.organization_id == organization_id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_or_create_stripe_customer(
    db: AsyncSession,
    organization: Organization,
    user_email: str | None = None,
) -> str:
    """Obtain or create a Stripe customer for an organization.

    If an association already exists, returns the existing customer ID immediately
    without making any external Stripe API calls.

    If no association exists:
    1. Validates that Stripe billing is enabled and configured.
    2. Ends any open read transaction before initiating the external API call.
    3. Calls Stripe to create a customer with an organization-scoped idempotency key
       and minimal safe metadata.
    4. Persists the returned customer ID locally with status='incomplete' and plan_tier='free'.
    5. Recovers from concurrent insertion conflicts by reloading the existing customer record.

    Args:
        db: Database async session.
        organization: Organization entity.
        user_email: Optional email of the initiating authenticated user.

    Returns:
        stripe_customer_id string.

    Raises:
        StripeBillingDisabledError: If Stripe billing is disabled in settings.
        StripeConfigurationError: If Stripe configuration is incomplete.
        StripeCustomerError: If the Stripe API call fails or returns an invalid customer.
    """
    # 1. Fast path: check for existing association in database
    existing_id = await get_stripe_customer_id(db, organization.id)
    if existing_id:
        return existing_id

    # 2. Check billing enabled status before initiating external calls
    if not is_stripe_enabled():
        raise StripeBillingDisabledError(
            "Stripe billing is currently disabled. Set STRIPE_ENABLED=true to enable."
        )

    client = get_stripe_client()

    # 3. Release any open read transaction before external network call
    # so we don't hold database connections or locks across the network.
    try:
        await db.commit()
    except Exception:
        await db.rollback()

    # 4. Prepare parameters with legitimate org info and safe metadata
    org_name = organization.name.strip() if organization.name else str(organization.id)
    params: dict[str, Any] = {
        "name": org_name,
        "metadata": {
            "organization_id": str(organization.id),
        },
    }
    if user_email and user_email.strip():
        params["email"] = user_email.strip()

    options = {
        "idempotency_key": f"create-customer-{organization.id}",
    }

    # 5. Call Stripe customer API using v1 namespace (compatible with v16.1.0)
    try:
        v1 = getattr(client, "v1", client)
        customers_service = getattr(v1, "customers", getattr(client, "customers", None))
        if customers_service is None or not hasattr(customers_service, "create"):
            raise StripeConfigurationError("Stripe client does not provide a customers service")
        customer = customers_service.create(params=params, options=options)
    except stripe.StripeError as exc:
        logger.error(
            "Stripe API error creating customer for organization %s: %s",
            organization.id,
            getattr(exc, "user_message", None) or str(exc),
        )
        raise StripeCustomerError(f"Stripe API error: {exc}") from exc
    except (StripeBillingDisabledError, StripeConfigurationError):
        raise
    except Exception as exc:
        logger.error(
            "Unexpected error communicating with Stripe for organization %s: %s",
            organization.id,
            str(exc),
        )
        raise StripeCustomerError(f"Unexpected error communicating with Stripe: {exc}") from exc

    # Extract customer ID safely from object or dict
    customer_id = getattr(customer, "id", None)
    if not customer_id and isinstance(customer, dict):
        customer_id = customer.get("id")
    if not customer_id or not isinstance(customer_id, str):
        raise StripeCustomerError("Stripe customer creation returned an invalid or missing customer ID")

    # 6. Persist local record
    subscription = Subscription(
        id=uuid.uuid4(),
        organization_id=organization.id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
        stripe_price_id=None,
        status=SubscriptionStatus.incomplete,
        plan_tier="free",
        cancel_at_period_end=False,
    )
    db.add(subscription)
    try:
        await db.commit()
        await db.refresh(subscription)
        return customer_id
    except IntegrityError:
        # 7. Concurrency conflict recovery:
        # A concurrent worker may have inserted the record first.
        await db.rollback()
        reloaded_id = await get_stripe_customer_id(db, organization.id)
        if reloaded_id:
            return reloaded_id
        # If it was an integrity error for something else, re-raise
        raise
