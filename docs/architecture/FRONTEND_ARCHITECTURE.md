# Aptlyst AI — Frontend Architecture

## 1. Overview

The Aptlyst AI frontend is a modern web application built on **Next.js 16 (App Router)**, **React 19**, and **Tailwind CSS v4**. It is hosted directly from the repository root.

The frontend has two primary responsibilities:
1. **Customer Dashboard**: An administrative portal where authenticated users manage organizations, configure AI chatbots, ingest knowledge bases, view analytics, and inspect conversations/leads.
2. **Embeddable Chat Widget**: A lightweight, standalone embeddable chat client (`/embed`) that customers can embed onto their own external websites via a script tag or iframe.

---

## 2. Directory Layout & App Router Structure

```text
Aptlyst-AI/
├── app/
│   ├── (auth)/                    # Authentication routes (login, signup, callback)
│   ├── (app)/                     # Authenticated customer dashboard (requires session)
│   │   ├── layout.tsx             # Main dashboard shell & sidebar navigation
│   │   ├── dashboard/             # Overview dashboard (usage metrics, recent activity)
│   │   ├── chatbots/              # Chatbot management & prompt configurator
│   │   ├── knowledge/             # Document upload & knowledge base indexing
│   │   ├── conversations/         # Live chat transcripts & user sessions
│   │   ├── leads/                 # Captured lead contact lists & export
│   │   ├── analytics/             # Usage graphs & conversational performance
│   │   ├── billing/               # Plan tiers, quota meters, checkout trigger
│   │   ├── integrations/          # External integrations (Zapier, Webhooks, Slack)
│   │   └── settings/              # Organization settings, team members, API keys
│   ├── embed/                     # Embeddable chat widget route
│   ├── layout.tsx                 # Root HTML layout & fonts
│   ├── page.tsx                   # Public landing page
│   └── globals.css                # Tailwind CSS v4 styling directives
├── components/
│   ├── chatbots/                  # Chatbot configuration & testing widgets
│   ├── dashboard/                 # Metrics cards, graphs, and usage widgets
│   ├── layout/                    # Sidebar, header, navigation breadcrumbs
│   ├── organization/              # Org switcher, invitation modals, member tables
│   └── ui/                        # Reusable UI primitives (buttons, inputs, dialogs)
├── lib/
│   ├── supabase/                  # Supabase SSR client, server client, and cookies
│   ├── api.ts                     # Typed backend API client & error handlers
│   ├── organization-context.tsx   # React Context for active organization state
│   ├── navigation.ts              # Route configuration & breadcrumb helpers
│   ├── redirect.ts                # Auth redirect & session routing logic
│   └── types.ts                   # Shared TypeScript interfaces & API types
└── tests/                         # Node.js built-in test runner suites
```

---

## 3. Authentication & Session Management

Authentication is powered by **Supabase Auth** with Server-Side Rendering (SSR) support:
- **`lib/supabase/client.ts`**: Browser client for client-side authentication interactions.
- **`lib/supabase/server.ts`**: Server component client using Next.js cookie stores.
- **Middleware / Route Guards**: Unauthenticated requests to `/(app)` are automatically redirected to `/(auth)/login` while preserving return paths.
- **JWT Propagation**: When communicating with the FastAPI backend, `lib/api.ts` extracts the active Supabase JWT session token and transmits it in the `Authorization: Bearer <token>` header.

---

## 4. Multi-Tenant Organization Context

State management for the active organization is handled via React Context:
- **`OrganizationProvider` (`lib/organization-context.tsx`)**:
  - Fetches the user's accessible organizations on initial load.
  - Persists the active `organization_id` in local storage and cookies.
  - Automatically scopes API client requests to the active organization.
  - Provides instant organization switching via the top navigation bar.

---

## 5. Embeddable Widget Architecture

The embeddable widget is served from `app/embed/`:
- **Isolation**: Optimized for zero CSS contamination with the host website by running in an isolated container or iframe.
- **Streaming UI**: Uses Server-Sent Events (SSE) or chunked streams from the backend API to render real-time assistant responses token-by-token.
- **Customization**: Renders custom themes, brand colors, bot avatars, and placeholder queries configured by the bot owner.
- **Lead Capture Forms**: Inline form components triggered dynamically during conversation to collect visitor email addresses and names.

---

## 6. Frontend Testing

Frontend tests use Node.js built-in test runner with TypeScript stripping:
```bash
npm test
```
The test suite covers:
- Route redirection & authentication guards (`tests/redirect.test.ts`)
- Backend API client error handling (`tests/api.test.ts`)
- Organization context & switching logic (`tests/organization.test.ts`)
- Chatbot configuration schemas (`tests/chatbots.test.ts`)
