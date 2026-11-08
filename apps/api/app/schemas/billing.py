"""Schemas for billing and Stripe customer management."""

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class StripeCustomerOut(BaseModel):
    """Safe representation of an organization's Stripe customer association.

    Guarantees:
    - Never exposes Stripe API secrets, payment methods, or internal payloads.
    - Exposes strictly the organization ID and associated Stripe customer ID.
    """

    model_config = ConfigDict(from_attributes=True)

    organization_id: uuid.UUID
    stripe_customer_id: str


class StripeCheckoutSessionOut(BaseModel):
    """Safe representation of an organization's Stripe Checkout Session.

    Guarantees:
    - Never exposes Stripe secret keys, webhook secrets, payment methods, or internal payloads.
    - Exposes strictly organization_id, session_id, and verified checkout_url.
    """

    model_config = ConfigDict(from_attributes=True)

    organization_id: uuid.UUID
    session_id: str
    checkout_url: str


class BillingPeriodOut(BaseModel):
    """Safe representation of an active billing period and usage accounting."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period_start: datetime
    period_end: datetime
    plan_tier: str
    monthly_token_quota: int
    tokens_used: int
    tokens_reserved: int
    tokens_remaining: int
    is_blocked: bool
    blocked_reason: str | None = None


class BillingSubscriptionOut(BaseModel):
    """Safe organization billing summary containing subscription and usage details."""

    model_config = ConfigDict(from_attributes=True)

    organization_id: uuid.UUID
    plan_tier: str
    status: str
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    canceled_at: datetime | None = None
    past_due_since: datetime | None = None
    has_stripe_customer: bool = False
    stripe_enabled: bool = False
    billing_period: BillingPeriodOut | None = None


class StripeBillingPortalOut(BaseModel):
    """Safe representation of an organization's Stripe Customer Portal session."""

    model_config = ConfigDict(from_attributes=True)

    organization_id: uuid.UUID
    portal_url: str


class RazorpaySubscriptionCheckoutOut(BaseModel):
    """Safe representation of an organization's Razorpay Subscription Checkout."""

    model_config = ConfigDict(from_attributes=True)

    organization_id: uuid.UUID
    attempt_id: uuid.UUID
    provider: str
    subscription_id: str
    key_id: str
    plan_id: str
    checkout_url: str | None = None
    status: str


class RazorpayCheckoutVerificationIn(BaseModel):
    """Callback parameters received from Razorpay standard checkout."""

    model_config = ConfigDict(from_attributes=True)

    attempt_id: uuid.UUID
    razorpay_payment_id: str
    razorpay_subscription_id: str
    razorpay_signature: str


class RazorpayCheckoutVerificationOut(BaseModel):
    """Safe outcome of a verified Razorpay subscription checkout callback."""

    model_config = ConfigDict(from_attributes=True)

    organization_id: uuid.UUID
    attempt_id: uuid.UUID
    provider: str
    subscription_id: str
    payment_id: str
    status: str
    payment_status: str

