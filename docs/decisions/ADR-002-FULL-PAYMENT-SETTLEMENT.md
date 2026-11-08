# ADR-002: Full Payment Settlement Policy and Immutability Invariants

## Status
**Accepted** (Architectural Policy Approved; State Machine Invariants Enforced; Settlement Handlers Pending Step 5.2D.3.3B)

## Context
During the design of Razorpay payment settlement handlers (Step 5.2D.3.2), several edge cases emerged concerning delayed webhooks, out-of-order network delivery, checkout timeouts, and resubscriptions:
1. **Subscription Resurrection**: If a subscription reached a terminal state (`canceled` or `completed`), a delayed webhook event (such as a late `subscription.charged` or replayed `subscription.authenticated`) could potentially flip the status back to `active`.
2. **Immediate Period Reset / Quota Truncation**: An early design proposal suggested that when a customer resubscribes or upgrades, the current billing period should be immediately reset (`period_end = now()`) and a brand new period initialized. This would have caused paying customers to lose remaining days and unconsumed token quotas already paid for in their prior transaction.
3. **Historical Timeout Rewriting**: If a user's checkout attempt timed out or was abandoned (`status = 'expired'`), but the remote payment gateway eventually captured the payment minutes later, an early proposal suggested mutating the historical checkout attempt status from `'expired'` to `'complete'`.

---

## Accepted Architectural Decisions
1. **Refusal of Resurrection**:
   - Terminal subscription states (`canceled`, `completed`) are strictly immutable.
   - Any incoming webhook or settlement event for a subscription already in a terminal state is forbidden from transitioning the agreement back to `active`.
   - Late payments arriving for terminal subscriptions are recorded in the payment ledger (`BillingPayment`) and mapped to an invoice (`BillingInvoice`) for bookkeeping and accounting compliance, but do not resurrect the terminated agreement.
2. **Conservative Core Billing Policy (Excising Immediate Resets)**:
   - When a customer purchases a subscription or resubscribes while an active paid billing period exists, the current period is **never truncated or immediately reset**.
   - The active entitlement period runs to its authoritative expiration timestamp (`period_end`).
   - The newly purchased entitlement period (`P2`) is queued to begin precisely at the conclusion of the active period (`period_start = P1.period_end`).
3. **Refusal to Rewrite History**:
   - A checkout attempt that transitioned to `'expired'` represents a durable historical fact (the client session expired).
   - Late payment captures link to the originating expired attempt via bidirectional foreign keys and durable audit payloads (`metadata`), but do **not** mutate `BillingCheckoutAttempt.status` from `'expired'` to `'complete'`.
   - The payment is settled, the entitlement period is provisioned, and the timeout history remains untampered.

---

## Implementation State Breakdown

### 1. Implemented Functionality (Verified in Code)
- Terminal status transition guards in `apps/api/app/services/billing/razorpay/agreement_handlers.py` refusing to mutate subscriptions once `canceled` or `completed`.
- Database model support for staged captured payments in `apps/api/app/models/payment.py` (`BillingPayment` with nullable `checkout_attempt_id` and `pending_attempt_binding` state).
- Partial unique indexes ensuring scoped checkout attempt uniqueness in migration `c1d2e3f4a5b7` and `e2f3a4b5c6d1`.

### 2. Proposed Implementation (Pending Step 5.2D.3.3B)
- Full conservative period rollover logic in `subscription.charged` / `payment.captured` settlement handlers (`P2.period_start = P1.period_end`).
- Delayed payment settlement handler for terminal agreements (invoice creation without agreement revival).
- Payment binding sweeper for expired checkout attempts.

### 3. Outstanding Validation
- Live execution and verification of chained period provisioning during PostgreSQL CI integration testing.

### 4. Rejected Approaches
- **Immediate Period Reset (`period_end = now()`)**: Truncating an active period upon resubscription was rejected because it destroys customer quota and days already paid for.
- **Agreement Resurrection**: Allowing delayed webhooks to revive canceled agreements was rejected to prevent zombie billing contracts.
- **Rewriting Expired Checkout Attempts**: Mutating historical checkout attempt records from `expired` to `complete` was rejected to preserve session timeline truth.

---

## Consequences

### Positive
- Customers never forfeit unconsumed days or token quotas that they paid for.
- State machines are mathematically monotonic: terminal states cannot cycle back to active states, eliminating infinite webhook processing loops.
- Complete auditability: checkout session expiration logs reflect actual timeline reality rather than retroactive reconciliation mutations.

### Negative / Trade-offs
- Entitlement provisioning logic must support chained billing periods (queued future periods) rather than always updating the single "current" period.
- Webhook reconciliation logic must handle late-arriving payments for expired attempts without assuming every settled payment corresponds to an `open` attempt.

---

## Relevant Implementation References
- `apps/api/app/models/billing.py` (`BillingCheckoutAttempt`, `BillingPayment`, `BillingInvoice`)
- `apps/api/app/models/organization.py` (`OrganizationBillingPeriod`)
- `apps/api/app/models/subscription.py` (`Subscription`)
- `apps/api/tests/test_razorpay_agreement_handlers.py`

## Supporting Historical Reports
- [`STEP_5_2D_3A_1_DESIGN_CORRECTION_REPORT.md`](../archive/phase_5_2d_lifecycle_design/STEP_5_2D_3A_1_DESIGN_CORRECTION_REPORT.md)
- [`STEP_5_2D_3_2_6_TERMINAL_AGREEMENT_DELAYED_PAYMENT_DESIGN.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_6_TERMINAL_AGREEMENT_DELAYED_PAYMENT_DESIGN.md)
- [`STEP_5_2D_3_2_9_FINAL_BILLING_POLICY_REVIEW.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_9_FINAL_BILLING_POLICY_REVIEW.md)
- [`STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_10_FINAL_TECHNICAL_CORRECTIONS.md)
- [`STEP_5_2D_3_2_14_PAYMENT_ASSOCIATION_HARDENING.md`](../archive/phase_5_2d_agreement_handlers/STEP_5_2D_3_2_14_PAYMENT_ASSOCIATION_HARDENING.md)
