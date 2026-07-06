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
assets/
components/
│
├── common/
├── layout/
└── ui/

features/
hooks/
layouts/
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

---

## Design Principles

- Modular
- Reusable
- Feature-based
- Secure IPC
- Production-ready
- Clean Architecture

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
