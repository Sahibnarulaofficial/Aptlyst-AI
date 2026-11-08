# ADR-003: Subscription and Entitlement Separation (Option A Decoupling)

## Status
**Accepted** (Architectural Model Approved; Schema Decoupling Implemented; Settlement Handlers Pending Step 5.2D.3.3B)

## Context
In legacy SaaS billing implementations, entitlement limits (e.g., token usage caps, team seats, chatbot limits) are frequently attached directly to the payment gateway's subscription object. If the subscription is canceled or lapses into a payment failure state, the organization's access is immediately downgraded.

During Phase 5.2D.3.2, two primary modeling approaches were evaluated:
- **Coupled Model (Option B)**: The subscription object directly dictates organization tier, quota, and validity.
- **Decoupled Model (Option A)**: Subscriptions represent recurring payment contracts; billing periods (`OrganizationBillingPeriod`) represent authoritative entitlement windows.

Under Option B, if a user cancels their auto-renewal halfway through a 30-day billing cycle, their subscription transitions to `canceled` in the payment gateway. In a coupled model, this either cuts off access prematurely or requires messy synthetic "pending_cancel" states in the agreement table. Furthermore, if a customer pays for an annual plan via a one-time settlement or finite agreement, the coupling breaks down completely.

---

## Accepted Architectural Decisions
Adopt **Option A: Strictly Decouple Subscriptions from Entitlements**:

1. **`Subscription` Represents the Payment Agreement**:
   - Represents the gateway contract (`razorpay_subscription_id`, `plan_id`, billing cycle schedule).
   - Tracks whether the gateway will automatically attempt recurring charges.
   - States: `created`, `authenticated`, `active`, `pending`, `halted`, `canceled`, `completed`, `expired`.
2. **`OrganizationBillingPeriod` Represents Tenant Entitlement**:
   - The authoritative entity used by API rate limiters and feature gates to enforce usage limits.
   - Stores `tier`, `tokens_limit`, `tokens_used`, `period_start`, and `period_end`.
   - Linked to the originating subscription via `subscription_id` (provenance), but independent in lifecycle.
3. **Lifecycle Independence**:
   - A successful subscription payment provisions or extends an `OrganizationBillingPeriod`.
   - Cancellation of a subscription sets `Subscription.status = 'canceled'`, ensuring no further automatic renewals occur.
   - However, the `OrganizationBillingPeriod` remains active until `period_end`, allowing the customer full access to the quota they have already purchased.

---

## Implementation State Breakdown

### 1. Implemented Functionality (Verified in Code)
- Independent data models in `apps/api/app/models/organization.py` (`OrganizationBillingPeriod`) and `apps/api/app/models/subscription.py` (`Subscription`).
- Added `subscription_id` foreign key column and index to `organization_billing_periods` in migration `e2f3a4b5c6d1`.
- Agreement handler separation in `apps/api/app/services/billing/razorpay/agreement_handlers.py` updating agreement state without mutating active billing periods.

### 2. Proposed Implementation (Pending Step 5.2D.3.3B)
- Settlement handler entitlement extension logic (`subscription.charged` / `payment.captured`) provisioning decoupled billing periods.

### 3. Outstanding Validation
- Live execution and verification of migration `e2f3a4b5c6d1` against real PostgreSQL in CI.

### 4. Rejected Approaches
- **Option B (Tightly Coupled Model)**: Direct coupling was rejected because it causes premature quota revocation upon cancellation, complicates resubscription, and makes non-subscription payments (one-time purchases, manual invoices) impossible to model cleanly.

---

## Consequences

### Positive
- Prevents premature quota revocation when auto-renewal is canceled.
- Cleanly supports alternative payment methods (e.g., manual invoicing, one-time payments, credits) without forcing dummy subscription agreements into the payment gateway table.
- Simplifies resubscription: a new subscription agreement can be created without conflicting with the ongoing entitlement period.

### Negative / Trade-offs
- Entitlement checking requires querying `organization_billing_periods` rather than reading a tier column on `subscriptions`.
- Provenance must be maintained bidirectionally so that audits can establish exactly which subscription and invoice funded each billing period.

---

## Relevant Implementation References
- `apps/api/app/models/organization.py` (`OrganizationBillingPeriod`, `Organization`)
- `apps/api/app/models/subscription.py` (`Subscription`)
- `apps/api/app/models/billing.py` (`BillingInvoice`, `BillingPayment`)
- `apps/api/alembic/versions/20261108_1600_e2f3a4b5c6d1_add_payment_association_and_entitlement_provenance.py`

## Supporting Historical Reports
- [`STEP_5_2D_3_2_3_OPTION_A_DESIGN_VALIDATION.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_3_OPTION_A_DESIGN_VALIDATION.md)
- [`STEP_5_2D_3_2_4_OPTION_A_FINAL_CONCURRENCY_DESIGN.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_4_OPTION_A_FINAL_CONCURRENCY_DESIGN.md)
- [`STEP_5_2D_3_2_7_ENTITLEMENT_ASSOCIATION_RESUBSCRIPTION_DESIGN.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_7_ENTITLEMENT_ASSOCIATION_RESUBSCRIPTION_DESIGN.md)
