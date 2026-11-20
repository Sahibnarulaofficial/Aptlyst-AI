# AGENTS.md --- Aptlyst AI Engineering Rules

This file defines the repository-wide working agreement for AI coding
agents and human contributors. Follow it together with the task-specific
instructions in the current issue or prompt. More specific, explicitly
authorized task instructions take precedence where they do not violate
the safety rules below.

## 1. Product context

-   Product: **Aptlyst AI**.
-   Current product focus: an embeddable AI chatbot SaaS. Do not expand
    the product scope or promise integrations/features that are not
    implemented.
-   Payment gateway decision: **Razorpay**. Do not introduce new Stripe
    functionality or treat Stripe as the chosen payment provider.
    Preserve legacy Stripe records and code where needed for
    compatibility; change legacy paths only when explicitly required and
    narrowly scoped.
-   Architecture: multi-tenant SaaS. Organization isolation, billing
    correctness, webhook integrity, and data security are critical
    requirements.

## 2. Technology stack

Respect the existing repository architecture and versions. The known
stack includes:

-   Frontend: Next.js App Router, React, TypeScript, Tailwind CSS.
-   Backend: Python, FastAPI, Pydantic, SQLAlchemy, Alembic.
-   Database and identity: Supabase Auth and PostgreSQL.
-   AI/application integrations: existing LangChain/Groq-related
    components where present.

Do not replace libraries, frameworks, providers, or architectural
patterns without explicit authorization. Inspect the actual repository
before assuming a path, version, module, or implementation detail.

## 3. Core engineering principles

1.  **Inspect before editing.** Read the relevant implementation,
    callers, models, schemas, migrations, tests, and project
    documentation before changing code.
2.  **Make the smallest safe change.** Prefer focused changes over broad
    refactors.
3.  **Preserve existing work.** Never discard, overwrite, reset, stash,
    or reorganize unrelated or uncommitted changes.
4.  **Tenant isolation first.** Every organization-scoped operation must
    verify organization ownership and authorization. Never trust a
    client-supplied organization ID without server-side
    membership/context validation.
5.  **Fail closed.** Unknown, missing, or ambiguous security-sensitive
    provenance must not be treated as trusted.
6.  **Keep state transitions explicit.** Model allowed transitions;
    preserve terminal and historical states. Do not resurrect expired,
    failed, cancelled, or otherwise terminal records unless domain rules
    explicitly permit it.
7.  **Idempotency matters.** Webhooks, retries, background jobs, and
    external callbacks must safely tolerate duplicate delivery.
8.  **No network calls inside database locks/transactions** unless an
    existing, documented design explicitly requires it. Keep external
    API calls outside critical database transactions.
9.  **Use deterministic reconciliation.** Ambiguous external state
    should be recorded for investigation with stable identifiers and
    idempotent case keys, not silently guessed.
10. **Do not hide errors.** Catch only expected exceptions. Unexpected
    database, provider, and programming errors must remain visible
    through the established error handling.
11. **No secrets in source control or logs.** Never print, commit, or
    expose API keys, tokens, passwords, signing secrets, private
    customer data, or full sensitive payloads.
12. **Avoid speculative abstractions.** Do not add frameworks, services,
    dependencies, or generalized systems without a demonstrated need.

## 4. Multi-tenant, authentication, and data security

-   Enforce organization membership and tenant boundaries in backend
    services, not only in the UI.
-   Use the existing Supabase JWT verification and organization-context
    mechanisms.
-   Apply least privilege to database access and service credentials.
-   Preserve PostgreSQL row-level security policies and tenant-aware
    constraints.
-   Never weaken authorization, RLS, validation, or isolation tests to
    make a change pass.
-   Treat webhook payloads, provider metadata, and user input as
    untrusted until verified.
-   Validate provider, merchant account, environment, organization, and
    ownership when binding external records.
-   Do not assume historical `legacy`, null, or missing environment
    values mean `live`.

## 5. Billing and Razorpay rules

-   Razorpay is the selected payment gateway.
-   Keep billing and usage accounting architecture consistent; avoid
    unrelated redesigns.
-   Separate test and live payment environments. Production entitlements
    must derive only from verified live-provider events and explicitly
    live records.
-   Never allow test-mode subscriptions or test webhooks to grant
    production entitlements.
-   Verify webhook signatures and event provenance using existing
    security mechanisms.
-   Make webhook processing idempotent and protect against out-of-order
    events.
-   Preserve immutable historical checkout attempts and terminal states.
-   Do not bind an event to an attempt solely because an ID exists;
    validate organization, provider, merchant account, and environment.
-   Handle uncertain provider outcomes through explicit
    recovery/reconciliation states. Do not create duplicate remote
    subscriptions to resolve ambiguity.
-   Respect the documented advisory-lock hierarchy and acquire locks in
    the prescribed order. Organization-level coordination must precede
    row locks where the architecture requires it.
-   Do not perform external Razorpay HTTP calls while holding database
    locks.
-   Do not access production databases or make live Razorpay API calls
    during development or tests.
-   Do not add Stripe features. Legacy Stripe compatibility work must be
    minimal, read-only where possible, and explicitly authorized.

## 6. Database and migration rules

-   Inspect current SQLAlchemy models, constraints, indexes, RLS
    policies, and Alembic history before changing schema.
-   A schema change requires explicit task authorization.
-   Migrations must be deliberate, reversible where practical, and
    tested against the supported PostgreSQL version.
-   Do not edit historical migrations to retrofit current behaviour.
-   Do not create a migration merely to make a test fixture convenient.
-   Preserve global uniqueness and tenant integrity invariants.
-   Do not run destructive database commands, reset databases, or delete
    test data without explicit approval.
-   Report migration creation, application, and validation separately.

## 7. Frontend and UX rules

-   Follow the established design system and existing component
    conventions.
-   Keep the product UI clean, responsive, accessible, and consistent.
-   Use semantic HTML, keyboard-accessible controls, clear
    loading/error/empty states, and appropriate form validation.
-   Do not invent product capabilities, integrations, analytics, or
    security claims in UI copy.
-   Keep public marketing claims aligned with implemented functionality.
-   Avoid unnecessary dependencies and heavy client-side code.

## 8. API and code quality

-   Preserve established API versioning, response formats, error
    semantics, and validation patterns.
-   Use Pydantic schemas for request/response boundaries as appropriate.
-   Keep business logic in the established service/domain layers rather
    than duplicating it in route handlers.
-   Add type annotations where consistent with the surrounding code.
-   Keep functions focused and avoid unrelated formatting churn.
-   Update documentation when behaviour, architecture, security
    assumptions, or operational procedures materially change.

## 9. Testing and validation

-   Add regression tests for every bug fix and meaningful behaviour
    change.
-   Test both success and failure paths, including authorization, tenant
    mismatch, duplicate events, stale state, invalid provenance, and
    concurrency where relevant.
-   Prefer focused test commands over unrestricted repository-wide test
    runs.
-   Never skip tests, weaken assertions, or change expected results
    merely to get a green run.
-   Distinguish passed, failed, skipped, terminated, and not-run tests
    in reports.
-   Do not claim a test passed if it timed out, was interrupted, or was
    not executed.
-   For database concurrency or locking claims, distinguish
    mocks/simulations from real PostgreSQL integration evidence.
-   Run relevant lint, type-check, build, and migration checks when
    applicable and authorized.

### Test watchdog

For long-running validation commands:

1.  Monitor meaningful progress, not only the displayed percentage.
2.  If there is no meaningful progress for 10 consecutive minutes,
    capture command output, process ID, current test, and available
    diagnostics.
3.  Terminate only the stalled test process and its owned child
    processes. Never kill unrelated tools, databases, or services.
4.  Diagnose the root cause before retrying.
5.  Apply a minimal fix and add a regression test where appropriate.
6.  If the same test times out a second time, stop and report the
    blocker; do not retry indefinitely.
7.  Never reset a database or delete test data automatically.

## 10. Git and workspace safety

-   Before work, record the repository path, branch, HEAD, and
    working-tree status.
-   Respect the user's designated worktree and branch.
-   Preserve uncommitted changes, including changes made by other
    agents.
-   Do not switch branches, create worktrees, cherry-pick, rebase,
    reset, stash, commit, push, merge, or open a PR unless explicitly
    authorized.
-   Do not edit files outside the authorized repository.
-   At completion, report changed files, diff summary, branch, HEAD, and
    working-tree status.
-   Verify that unrelated worktrees remain untouched when multiple
    worktrees are in use.

## 11. Documentation and decision records

-   Read relevant ADRs, roadmaps, status documents, and runbooks before
    changing architecture.
-   Follow accepted decisions; do not silently replace them.
-   Clearly distinguish proposed, approved, implemented, validated, and
    deployed decisions.
-   Update the relevant documentation only when authorized and within
    scope.
-   Never claim a feature is production-ready solely because unit tests
    pass.

## 12. Working protocol for AI agents

For every task:

1.  Restate the requested outcome internally and identify explicit scope
    boundaries.
2.  Inspect repository state and relevant implementation.
3.  Identify dependencies, security implications, and migration
    requirements.
4.  Propose or execute only the authorized work.
5.  Implement the smallest coherent change.
6.  Add focused regression tests.
7.  Run authorized validation with the watchdog.
8.  Review the final diff for scope creep, secrets, and unintended
    changes.
9.  Report exact files, behaviour, test results, limitations, and
    repository state.
10. Stop at the authorized boundary and wait for further approval.

If requirements conflict, a baseline is unexpected, or a safe
implementation requires work outside scope, stop and ask for
authorization rather than improvising.

## 13. Definition of done

A task is complete only when:

-   The requested behaviour is implemented.
-   Security and tenant boundaries are preserved.
-   Relevant regression tests pass.
-   No tests are misreported or silently skipped.
-   Documentation is updated when required and authorized.
-   The final diff contains only intended changes.
-   The repository/worktree state is accurately reported.
-   Deferred work and known limitations are clearly stated.
