# Aptlyst AI — Architecture

## Repository

```
Aptlyst-AI/
```

---

## Applications

```
apps/
├── backend/
├── desktop/
└── website/
```

---

## Application Architecture

App

↓

RouterProvider

↓

RootLayout

↓

AppShell

├── Sidebar

├── Header

├── Outlet

└── Status Bar

## Desktop

Uses the official Electron-Vite architecture.

```
desktop/

src/
├── main/
├── preload/
└── renderer/
    ├── index.html
    └── src/
```

Renderer structure:

```
renderer/src/

app/
├── errors/
├── layouts/
├── providers/
├── router/
└── store/

assets/

components/
├── common/
├── layout/
└── ui/

features/
├── assistant/
├── command-palette/
├── dashboard/
├── history/
├── keyboard/
├── meeting/
├── settings/
└── transcripts/

hooks/

lib/

pages/

services/

stores/

styles/

types/

utils/
```

---

## Technology Stack

Electron

Electron-Vite

React

TypeScript

Tailwind CSS v4

Radix UI

Lucide React

pnpm Workspace

React Router

Zustand

---

## Design Principles

- Modular
- Reusable
- Feature-based
- Secure IPC
- Production-ready
- Clean Architecture

---

## UI Infrastructure

Reusable application infrastructure:

- Command Registry
- Keyboard Shortcut Infrastructure
- Global Command Palette
- Error Boundary
- Zustand State Management
- Shared UI Components

---

## Backend

Separate application.

Responsible for:

- Authentication
- Billing
- Database
- AI orchestration
- Cloud APIs

---

## Communication

Renderer

↓

Preload

↓

Main Process

↓

Backend API

---

## Future

Shared packages:

```
packages/

ui/
shared/
config/
types/
```

AI modules:

```
ai/

local/
cloud/
prompts/
```
