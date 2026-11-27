# Aptlyst AI — Platform Development Roadmap

## 1. Milestone Progression Overview

Aptlyst AI follows a staged, gate-controlled engineering roadmap. Each milestone requires comprehensive test coverage and formal architectural review before subsequent implementation phases are authorized.

```text
Phase 1: Project Foundation & Next.js Shell             [COMPLETED]
Phase 2: Multi-Tenant Database & Supabase Auth          [COMPLETED]
Phase 3: Chatbot CRUD API & Configurator Foundation     [CORE COMPLETE - RAG Pending]
Phase 4: Widget Preview & Visitor Conversation API      [FOUNDATION COMPLETE - Live UI Pending]
Phase 5.1: Initial Stripe Billing Integration           [COMPLETED - Legacy]
Phase 5.2 / Stages 1–2K: Billing Infrastructure & CI    [MERGED (6872f47) & FROZEN (Stage 2L)]
Chatbot Product Development (RAG, Live LLM/SSE, UI)     [ACTIVE HANDOFF FOCUS]
Final Razorpay Integration & Cutover                    [DEFERRED - Final Release Gate]
```

---

## 2. Detailed Breakdown: Billing Infrastructure Through Stage 2K (`main` @ `6872f47`)

### Phases 5.2A–5.2D / D-04A — Provider Foundation, Agreement Handlers & Provenance
- **Status**: Completed & CI-Verified (`PR #2` `6d11ec5` and `PR #3` `6872f47`).
- **Deliverables**: Provider-neutral models (`a1b2c3d4e5f7`–`e2f3a4b5c6d1`), HMAC webhook ingress, leased webhook worker, Razorpay agreement handlers, composite FKs, and PostgreSQL immutability triggers.

### Stages 2J – 2K — Checkout Recovery, Lock Unification, Ops Hardening & PG16 CI
- **Status**: Completed in code & PostgreSQL 16 CI (`PR #3` / `6872f47`, Run `46048131091`: `1,322` passed, `0` skipped, `0` failed).
- **Deliverables**: `razorpay_checkout_recovery_worker.py` (`f3a4b5c6d7e2`), `advisory_lock.py` unification, `billing_worker_heartbeats` (`a4b5c6d7e8f3`), `billing_health.py`, `billing_reconciliation.py`, `RENDER_DEPLOYMENT_GUIDE.md`, and separated PostgreSQL 16 CI / offline test isolation.

---

## 3. Active Product Focus & Deferred Final Razorpay Phase

### Active Focus (Stage 2L Billing Handoff & Freeze)
- **Canonical Spec**: [`docs/operations/BILLING_HANDOFF_AND_FREEZE.md`](../operations/BILLING_HANDOFF_AND_FREEZE.md) (`RAZORPAY_ENABLED=false`, `STRIPE_ENABLED=false`).
- **Chatbot Product Scope**: Build RAG knowledge ingestion (`/knowledge`), live LLM providers + streaming widget responses (`/embed/[id]`), and placeholder UIs for conversations, leads, analytics, integrations, and settings (`/conversations`, `/leads`, `/analytics`, `/integrations`, `/settings`).

### Final Razorpay Integration & Cutover Phase (Deferred Behind Release Gate)
1. **Settlement Handlers & Pro Quota Rollover**: Implement `subscription.charged`, `payment.captured`, `payment.failed` with conservative Pro `OrganizationBillingPeriod` provisioning (`DEFAULT_PRO_MONTHLY_TOKEN_QUOTA > 0`).
2. **Sweeper & Reconciliation Clearance (`BLK-04` / `BLK-06`)**: Implement timed-out `pending_attempt_binding` sweeper and pre-activation cross-provider conflict guard.
3. **Checkout Session UI Migration**: Migrate `app/(app)/billing/page.tsx` and `lib/api.ts` from legacy Stripe routes to Razorpay checkout and verification UX.
4. **Live Staging & Render Validation**: Deploy all 3 Render services, run `check_staging_migration_gate.py`, verify real Razorpay test-mode webhooks end-to-end, wire `billing_health --notify`, and satisfy all 7 release gates prior to live production enablement.
