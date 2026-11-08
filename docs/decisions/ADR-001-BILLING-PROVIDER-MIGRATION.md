# ADR-001: Billing Provider Migration and Provider-Neutral Foundation

## Status
**Accepted** (Architectural Design Approved; Foundation Implemented; Settlement Handlers Pending Step 5.2D.3.3B)

## Context
Aptlyst AI's initial billing implementation was tightly coupled to Stripe (`stripe_customer_id`, `stripe_subscription_id`, `stripe_invoices` table). To support the Indian domestic market—including Indian Rupee (INR) transactions, Reserve Bank of India (RBI) recurring e-mandates, and domestic payment options (UPI, Netbanking, RuPay)—Razorpay Subscriptions was selected as the primary billing provider.

Directly replacing Stripe code with Razorpay-specific code would have repeated the original design flaw of tight provider coupling. If another payment gateway or dual-gateway strategy were required in the future, the entire billing engine would again need to be rewritten. Furthermore, existing historical Stripe customer data and subscription records had to remain intact without data loss or downtime.

---

## Accepted Architectural Decisions
1. **Provider-Neutral Billing Abstraction**: Establish a provider-neutral database foundation with generic billing entities:
   - `BillingCustomer`: Maps tenant organizations to provider customer IDs (`provider`, `provider_customer_id`).
   - `BillingCheckoutAttempt`: Manages the checkout flow lifecycle independently of gateway-specific tokens.
   - `BillingInvoice`: Represents financial invoices regardless of whether Stripe or Razorpay issued them.
   - `BillingPayment`: Represents individual payment transactions.
   - `BillingWebhookEvent`: Generic raw webhook ingestion and lease-based worker queue.
2. **Provider Driver Isolation**: Isolate gateway-specific logic into dedicated provider modules (`app/services/billing/razorpay/` and `app/services/billing/stripe_*.py`), adhering to shared dispatch interfaces.
3. **Phased Provider Transition**:
   - Historical Stripe data is preserved as read-only audit records.
   - All new subscription checkouts are routed through Razorpay Subscriptions.
   - Provider-neutral tables serve as the single source of truth for application entitlements.

---

## Implementation State Breakdown

### 1. Implemented Functionality (Verified in Code)
- Provider-neutral models in `apps/api/app/models/billing.py` and `apps/api/app/models/payment.py` (`BillingCustomer`, `BillingCheckoutAttempt`, `BillingInvoice`, `BillingPayment`, `BillingWebhookEvent`).
- Database migrations `a1b2c3d4e5f7`, `b1c2d3e4f5a6`, `c1d2e3f4a5b7`, and `d1e2f3a4b5c6`.
- Razorpay webhook ingestion endpoint (`/api/v1/billing/razorpay/webhook`) with constant-time HMAC signature verification.
- Background webhook queue worker (`apps/api/app/workers/razorpay_webhook_worker.py`) with lease ownership, exponential backoff, and CLI runner.
- Historical Stripe customer and checkout handlers preserved for backward compatibility.

### 2. Proposed Implementation (Pending Step 5.2D.3.3B)
- Full Razorpay payment settlement handlers (`subscription.authenticated`, `subscription.charged`, `payment.captured`).
- Background payment binding sweeper for late-settling captures.

### 3. Outstanding Validation
- Live gateway integration testing against the Razorpay Sandbox API.
- End-to-end checkout modal verification on the frontend.

### 4. Rejected Approaches
- **Direct Code Replacement**: Ripping out Stripe models and substituting Razorpay-specific columns was rejected to prevent recurring technical debt and lock-in.
- **Rewriting Historical Stripe Records**: Retroactively mutating Stripe records into Razorpay structures was rejected to preserve financial auditability.

---

## Consequences

### Positive
- The application business logic is completely decoupled from payment gateway idiosyncrasies.
- Multi-provider support (e.g. running Stripe for international customers and Razorpay for Indian domestic customers) is architecturally enabled.
- Webhook ingestion, retry policies, and worker infrastructure are unified across providers.

### Negative / Trade-offs
- Increased schema complexity requiring mapping tables and composite identity keys.
- Requires careful historical data backfilling during Alembic migrations to populate neutral tables from legacy Stripe tables without introducing conflicts.

---

## Relevant Implementation References
- `apps/api/app/models/billing.py` (`BillingCustomer`, `BillingCheckoutAttempt`, `BillingInvoice`, `BillingPayment`, `BillingWebhookEvent`)
- `apps/api/alembic/versions/20261106_1200_a1b2c3d4e5f7_add_provider_neutral_billing_foundation.py`
- `apps/api/alembic/versions/20261107_1900_b1c2d3e4f5a6_add_billing_customer_provisions.py`
- `apps/api/app/services/billing/razorpay/`
- `apps/api/tests/test_provider_neutral_billing_models.py`

## Supporting Historical Reports
- [`STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md`](../archive/phase_5_2d_foundation/STEP_5_2D_0_2_FINAL_WEBHOOK_DESIGN_AUDIT.md)
- [`STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md`](../archive/phase_5_2d_foundation/STEP_5_2D_0_3_CRITICAL_INVARIANT_RECONCILIATION.md)
- [`STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md`](../archive/phase_5_2d_foundation/STEP_5_2D_1_WEBHOOK_INGESTION_IMPLEMENTATION_REPORT.md)
