// Builds the URL for `/ws/projects/{project_id}` (Phase 19).
//
// Every other backend call in this app goes through `/api/proxy/*`
// (`app/api/proxy/[...path]/route.ts`) so `API_BASE_URL` and
// `AHSEA_API_KEY` stay server-only. A WebSocket can't be proxied through
// a standard Next.js Route Handler (no upgrade support without a custom
// server), so the browser connects to the backend directly instead.
//
// That means, unlike `API_BASE_URL`/`AHSEA_API_KEY`, these two are
// intentionally `NEXT_PUBLIC_*` (shipped to the browser bundle and
// visible in the WebSocket handshake URL, same as any client-side API
// call would be). Only set `NEXT_PUBLIC_WS_API_KEY` if that's acceptable
// for your deployment (e.g. local development, or a backend behind a
// network boundary the browser is already trusted inside of) — for a
// public-facing deployment with `AHSEA_REQUIRE_API_KEY=true`, front this
// with a real reverse-proxy WebSocket pass-through instead of widening
// the key's exposure here.

const WS_BASE_URL = process.env.NEXT_PUBLIC_WS_BASE_URL || "ws://localhost:8000";
const WS_API_KEY = process.env.NEXT_PUBLIC_WS_API_KEY || "";

export function projectEventsWsUrl(projectId: string, afterEventId?: string | null): string {
  const url = new URL(`${WS_BASE_URL}/ws/projects/${encodeURIComponent(projectId)}`);
  if (WS_API_KEY) url.searchParams.set("api_key", WS_API_KEY);
  if (afterEventId) url.searchParams.set("after", afterEventId);
  return url.toString();
}
