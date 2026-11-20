# GEMINI.md --- Aptlyst AI Agent Instructions

These instructions apply when Gemini, Gemini CLI, or an Antigravity
agent works in this repository. Follow `AGENTS.md` as the
repository-wide engineering contract. This file adds execution guidance
for Gemini-based agents; it does not override the safety, scope,
security, or Git rules in `AGENTS.md`.

## 1. Start every task with repository discovery

Before editing:

-   Confirm the current working directory is the intended Aptlyst AI
    repository/worktree.
-   Inspect `git status --short`, `git branch --show-current`, and
    `git rev-parse HEAD`.
-   Read `AGENTS.md` and the relevant README, ADRs, roadmap, and status
    documents.
-   Locate the actual route, service, model, schema, migration, and
    tests involved.
-   Search for all callers and state transitions before changing shared
    logic.
-   Identify uncommitted changes and preserve them exactly.

Never assume an earlier conversation's branch, commit, file list, or
baseline is still current. Verify it in the active workspace.

## 2. Plan before implementation

For multi-file or security-sensitive tasks, first produce a concise plan
containing:

1.  Scope and explicit exclusions.
2.  Files likely to change.
3.  Existing behaviour and relevant invariants.
4.  Risks and dependencies.
5.  Tests to add or run.
6.  Whether a schema migration is required.

Do not broaden scope after implementation begins. If a new dependency is
discovered, stop and report it before making out-of-scope changes.

## 3. Aptlyst AI product and architecture rules

-   Product name must remain **Aptlyst AI**.
-   Current product focus is the chatbot SaaS: create, customize, and
    deploy an embeddable chatbot. Do not invent additional product
    modules or unsupported integrations.
-   Keep the frontend aligned with the existing Next.js App Router,
    TypeScript, and Tailwind implementation.
-   Keep backend work aligned with FastAPI, Pydantic, SQLAlchemy,
    Alembic, and existing service patterns.
-   Supabase Auth and PostgreSQL are core infrastructure. Preserve JWT
    verification, organization context, RLS, and tenant isolation.
-   Razorpay is the selected payment gateway. Do not add new Stripe
    integration. Legacy Stripe compatibility must remain narrowly scoped
    and only be touched when the task requires it.
-   Preserve existing billing/usage accounting design unless a task
    explicitly authorizes an architectural change.

## 4. Implementation discipline

-   Prefer a small, reviewable diff.
-   Do not rewrite a whole file when a targeted edit is sufficient.
-   Do not perform broad renames, formatting sweeps, dependency
    upgrades, or unrelated cleanup.
-   Reuse existing utilities and conventions after verifying their
    behaviour.
-   Avoid duplicate sources of truth for status, environment, provider
    identity, and organization ownership.
-   Keep external API calls outside database transactions and
    advisory/row locks.
-   Make retries and webhook handlers idempotent.
-   Enforce monotonic state transitions and preserve terminal historical
    records.
-   Use deterministic reconciliation for ambiguous external state.
-   Fail closed when environment, merchant account, provider, ownership,
    or provenance is unknown.
-   Never infer that a missing or `legacy` environment means
    production/live.
-   Do not log secrets, tokens, webhook signatures, or unnecessary
    personal data.

## 5. Billing, webhooks, and concurrency

Treat billing code as security- and money-sensitive.

Before modifying billing logic, inspect:

-   Checkout creation and attempt lifecycle.
-   Subscription lifecycle and transition helpers.
-   Webhook ingestion, signature verification, event deduplication, and
    handlers.
-   Payment reconciliation models and case-key conventions.
-   Organization and subscription uniqueness constraints.
-   Advisory-lock helpers and documented lock ordering.
-   Accounting and entitlement derivation.
-   Relevant tests and migrations.

Rules:

-   Production entitlements must be based only on verified live Razorpay
    events and explicitly live subscription records.
-   Test and live environments must remain isolated.
-   Validate organization, provider, merchant account, and environment
    before associating provider events with local records.
-   Do not let duplicate or out-of-order events regress state.
-   Do not resurrect expired or failed checkout attempts.
-   Do not create duplicate provider subscriptions to resolve an
    uncertain result.
-   Acquire advisory locks in the documented order. Organization-level
    locks precede row locks where required.
-   Never make Razorpay HTTP requests while holding database locks.
-   Handle only understood database conflicts; do not blanket-catch
    integrity errors.
-   Keep legacy Stripe data immutable unless a specifically authorized
    migration says otherwise.

## 6. Database and migration discipline

A migration is a separate scope of work unless the user explicitly
authorizes it.

Before proposing or creating one:

-   Inspect SQLAlchemy models and current Alembic revision history.
-   Check constraints, partial indexes, foreign keys, triggers, and RLS
    implications.
-   Determine upgrade and downgrade behaviour.
-   Identify whether the migration is safe for existing production data.
-   Add migration tests or validation appropriate to the project.

Never: - Edit an already-applied historical migration. - Reset a
database to make a migration pass. - Delete or rewrite user data. -
Apply a migration to production. - Sneak schema changes into a task
described as "no migration".

If the task cannot be completed safely without a migration, stop and
request authorization.

## 7. Testing workflow and watchdog

Use focused test commands. Do not start with unrestricted `pytest`.

Recommended sequence:

1.  New regression tests.
2.  Directly affected existing tests.
3.  Adjacent service/handler tests.
4.  Relevant security and tenant-isolation tests.
5.  Relevant billing, webhook, and advisory-lock tests.
6.  Build, lint, type-check, or migration validation when applicable.

For every long-running test/build/lint/migration command:

-   Monitor meaningful output or process activity.
-   Ten consecutive minutes without meaningful progress triggers
    diagnostics: capture output, PID, current test, and process details.
-   Terminate only the stalled process and its owned children.
-   Do not kill unrelated Antigravity, Gemini, database, or development
    processes.
-   Diagnose before retrying.
-   Make a minimal fix and add a regression test when appropriate.
-   If the same test stalls a second time, stop and report it.
-   Never skip, weaken, or silently deselect tests.
-   Report timed-out/terminated tests as such, never as passed.
-   Do not reset databases or delete test data automatically.

When using mocks for concurrency, state clearly that mock-level
serialization is not proof of PostgreSQL concurrency correctness.
Reserve real PostgreSQL concurrency claims for actual PostgreSQL
integration tests.

## 8. Windows and process handling

The development environment may be Windows-based.

-   Use commands compatible with the actual shell and environment.
-   Be alert to Windows asyncio event-loop and subprocess behaviour.
-   Diagnose hangs using process IDs, test output, and available
    stack/process diagnostics.
-   Do not kill processes by broad name matching.
-   Do not assume WSL is installed.
-   Do not modify global Python, Node, npm, pnpm, or system
    configuration unless explicitly authorized.
-   Avoid changing lockfiles or dependency versions unless required by
    the task.

## 9. Git and worktree protocol

Before any task, capture:

``` bash
git status --short
git branch --show-current
git rev-parse HEAD
```

During work:

-   Stay in the authorized worktree and branch.
-   Preserve all pre-existing modifications.
-   Do not switch branches or move changes between worktrees.
-   Do not commit, push, merge, rebase, reset, stash, cherry-pick, or
    create a PR unless the user explicitly authorizes that action.
-   Do not run destructive Git commands.
-   Do not use `git clean`, `git checkout --`, or `git reset --hard` as
    cleanup shortcuts.

At the end, repeat the baseline commands and inspect the full diff.
Report the exact changed files and whether the HEAD changed.

## 10. Antigravity task execution format

When a user authorizes implementation:

-   Treat the user's latest explicit task scope as the authorization
    boundary.
-   If the user says "begin" after a proposed stage, implement only that
    stage.
-   Do not assume approval for later stages, migrations, commits,
    pushes, merges, or production operations.
-   Keep stages isolated and independently reviewable.
-   If the user requests a prompt rather than implementation, provide a
    complete, copy-ready prompt with explicit scope, baseline checks,
    tests, watchdog, exclusions, and reporting requirements.
-   Do not claim work was performed unless it was actually performed in
    the repository.

## 11. Final report format

Every implementation task must end with a concise but sufficiently
detailed report:

### Baseline

-   Repository/worktree path.
-   Branch and starting HEAD.
-   Initial working-tree state.

### Implementation

-   Exact files changed.
-   Behaviour added or corrected.
-   Important design and security decisions.

### Validation

-   Exact commands.
-   Passed, failed, skipped, terminated, and not-run counts.
-   Build/lint/type-check/migration results where applicable.
-   Any watchdog incidents and their diagnosis.

### Safety and scope

-   Confirm whether migrations, production access, live provider calls,
    or destructive operations occurred.
-   Confirm unrelated worktrees were untouched when relevant.
-   Confirm no unauthorized Git operation occurred.

### Remaining work

-   Known limitations.
-   Deferred stages and dependencies.
-   Any item requiring user approval.

Do not use vague statements such as "everything works" or "fully
production-ready" without evidence.

## 12. Stop conditions

Stop without making further changes if:

-   The current branch, HEAD, worktree, or baseline is unexpected.
-   Existing uncommitted work may be overwritten.
-   The requested change requires a migration that was not authorized.
-   The implementation would weaken tenant isolation, payment safety,
    webhook verification, or access controls.
-   A safe fix requires work outside the authorized stage.
-   Tests repeatedly hang or the environment is unstable.
-   The repository contains conflicting instructions that cannot be
    resolved safely.

Explain the blocker and ask for the smallest necessary clarification or
authorization.
