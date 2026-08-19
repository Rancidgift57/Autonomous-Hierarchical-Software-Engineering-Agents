"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { projectEventsWsUrl } from "./ws";
import type { RealtimeEvent } from "./types";

export type ConnectionState = "connecting" | "open" | "reconnecting" | "closed";

//: How many recent events this hook keeps in memory for consumers that
//: want a live activity strip -- older ones are still on the server
//: (`GET /api/projects/{id}/events`) and don't need to live in the DOM.
const MAX_BUFFERED_EVENTS = 200;

//: Reconnect backoff: starts fast (flaky wifi should recover in ~1s) and
//: caps at 15s so a genuinely-down backend doesn't get hammered.
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 15000;

export interface UseProjectEventsResult {
  /** Connection lifecycle state, for a small "live"/"reconnecting" indicator. */
  connectionState: ConnectionState;
  /** Most recent events first, capped at `MAX_BUFFERED_EVENTS`. */
  events: RealtimeEvent[];
  /** Every event ever received this session, keyed by task_id -> latest event for that task. */
  latestByTaskId: Map<string, RealtimeEvent>;
  /** Same, keyed by agent_id. */
  latestByAgentId: Map<string, RealtimeEvent>;
}

/**
 * Subscribes to `/ws/projects/{project_id}` and keeps a small rolling
 * window of events plus "latest event per task/agent" maps live-updated
 * in React state. Reconnects with exponential backoff on drop, and
 * replays anything missed via `?after=<last_event_id>` so a flaky
 * connection never silently loses events.
 *
 * `onEvent`, if given, fires for every event as it arrives -- the
 * natural place for a consumer to call an SWR `mutate()` and pick up
 * the authoritative REST state rather than trying to reconstruct it
 * from the event payload alone.
 */
export function useProjectEvents(
  projectId: string | null | undefined,
  onEvent?: (event: RealtimeEvent) => void,
): UseProjectEventsResult {
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<RealtimeEvent[]>([]);
  const [latestByTaskId, setLatestByTaskId] = useState<Map<string, RealtimeEvent>>(new Map());
  const [latestByAgentId, setLatestByAgentId] = useState<Map<string, RealtimeEvent>>(new Map());

  // Refs so the reconnect loop always sees the latest callback/value
  // without re-subscribing the whole effect on every render.
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const lastEventIdRef = useRef<string | null>(null);
  const attemptRef = useRef(0);
  const closedByUsRef = useRef(false);

  const handleEvent = useCallback((event: RealtimeEvent) => {
    lastEventIdRef.current = event.event_id;

    setEvents((prev) => [event, ...prev].slice(0, MAX_BUFFERED_EVENTS));
    if (event.task_id) {
      setLatestByTaskId((prev) => new Map(prev).set(event.task_id as string, event));
    }
    if (event.agent_id) {
      setLatestByAgentId((prev) => new Map(prev).set(event.agent_id as string, event));
    }
    onEventRef.current?.(event);
  }, []);

  useEffect(() => {
    if (!projectId) return;

    closedByUsRef.current = false;
    // A fresh subscription (new projectId) has no continuity to preserve.
    lastEventIdRef.current = null;
    attemptRef.current = 0;
    setEvents([]);
    setLatestByTaskId(new Map());
    setLatestByAgentId(new Map());

    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      setConnectionState(attemptRef.current === 0 ? "connecting" : "reconnecting");

      const url = projectEventsWsUrl(projectId, lastEventIdRef.current);
      socket = new WebSocket(url);

      socket.onopen = () => {
        attemptRef.current = 0;
        setConnectionState("open");
      };

      socket.onmessage = (message) => {
        let parsed: unknown;
        try {
          parsed = JSON.parse(message.data);
        } catch {
          return; // not JSON -- ignore rather than crash the feed
        }
        if (
          parsed &&
          typeof parsed === "object" &&
          "event_type" in parsed &&
          "event_id" in parsed
        ) {
          handleEvent(parsed as RealtimeEvent);
        }
        // Anything else (e.g. `{"type": "ping"}` heartbeats) is intentionally ignored.
      };

      socket.onclose = () => {
        if (closedByUsRef.current) return;
        setConnectionState("reconnecting");
        const delay = Math.min(
          RECONNECT_BASE_DELAY_MS * 2 ** attemptRef.current,
          RECONNECT_MAX_DELAY_MS,
        );
        attemptRef.current += 1;
        reconnectTimer = setTimeout(connect, delay);
      };

      socket.onerror = () => {
        // `onclose` always follows `onerror` for a WebSocket -- the
        // reconnect is scheduled there, nothing to do here beyond letting
        // the browser's own error propagate to `onclose`.
      };
    };

    connect();

    return () => {
      closedByUsRef.current = true;
      setConnectionState("closed");
      if (reconnectTimer) clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [projectId, handleEvent]);

  return { connectionState, events, latestByTaskId, latestByAgentId };
}
