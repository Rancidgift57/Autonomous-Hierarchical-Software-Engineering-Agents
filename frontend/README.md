# AHSEA Control Plane — Dashboard (Phase 18)

Next.js + TypeScript + Tailwind + React Flow frontend for the AHSEA
FastAPI control plane (Phases 16/17). Talks to your existing backend
over its `/api/projects/*` routes — no backend changes required.

## Pages

| Route                                 | Shows                                              |
|----------------------------------------|-----------------------------------------------------|
| `/projects`                            | All projects, status, quick create                 |
| `/projects/new`                        | Create-project form (name, idea prompt, repo)       |
| `/projects/[id]`                       | Overview: status, run controls, task breakdown      |
| `/projects/[id]/agents`                | CTO → Manager → Worker graph (React Flow) + roster  |
| `/projects/[id]/tasks`                 | Task DAG (React Flow) + filterable table            |
| `/projects/[id]/artifacts`             | Artifact metadata (path, type, producing task)      |
| `/projects/[id]/logs`                  | Agent + project event stream, filterable            |
| `/projects/[id]/qa`                    | QA reports: lint/type-check/coverage/tests          |
| `/projects/[id]/deployment`            | Deployment pipeline stage + human approval gate     |

## Setup

```bash
npm install
cp .env.local.example .env.local
# edit .env.local: point API_BASE_URL at your running FastAPI backend
npm run dev
```

Open http://localhost:3000 — it redirects to `/projects`.

## How it talks to the backend

Every browser request goes to this app's own `/api/proxy/[...path]`
route, which forwards to `API_BASE_URL` server-side and attaches
`X-API-Key` (if you set `AHSEA_API_KEY`) before forwarding. Neither
value is prefixed with `NEXT_PUBLIC_`, so neither ever ships to the
client bundle — the backend's location and credential stay server-only.

If your backend has `AHSEA_REQUIRE_API_KEY=true`, set `AHSEA_API_KEY`
in `.env.local` to a key from your backend's `AHSEA_API_KEYS` list.
If auth is off (the backend's default), leave `AHSEA_API_KEY` empty.

## What's intentionally not shown

- No raw agent system prompts, LLM request/response bodies, or
  credentials are fetched or rendered anywhere — the backend's own API
  doesn't expose the `llm_request_repository` table, and this frontend
  doesn't add a route to reach it.
- `idea_prompt` (the user's own project brief) is shown on the overview
  page since it's not a secret, but the create form warns against
  pasting credentials into it.

## Known gap vs. the Phase 18 spec

The spec's "Repairs" bullet doesn't have a backing API route today —
the backend has a `repair_attempt_repository` but no
`/api/projects/{id}/repairs` endpoint. Repair-relevant signal is
surfaced today via `task.retries`/`max_retries` on the Tasks page and
via error/warning events on the Logs page. Add a real repairs endpoint
on the backend and this frontend can add a dedicated Repairs view or
section against it.

## Stack

- Next.js 14 (App Router), TypeScript, Tailwind CSS
- React Flow (`reactflow`) for the agent hierarchy and task DAG graphs
- SWR for data fetching, polling, and cache invalidation after actions
- Every data page has loading, error (with retry), and empty states
