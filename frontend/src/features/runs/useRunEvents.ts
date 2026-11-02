import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { fetchEventPage, queryKeys } from "@/api/hooks";
import { streamEvents } from "@/api/sse";
import { TERMINAL_STATUSES, type RunEvent, type RunStatus } from "@/api/types";

import { mergeEvents } from "./timeline/model";

const MAX_INITIAL_PAGES = 20;

export interface RunEventsState {
  events: RunEvent[];
  loading: boolean;
  streaming: boolean;
  error: unknown;
}

/**
 * Loads a run's events page by page, then follows the live SSE stream while the run is
 * active. Resumes from the last sequence it has (no gaps, no duplicates) and restarts the
 * stream if a finished run is retried. Mount it per run (the page keys it by run id).
 */
export function useRunEvents(runId: string, status: RunStatus | undefined): RunEventsState {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const lastSeq = useRef(0);
  const client = useQueryClient();
  const active = status !== undefined && !TERMINAL_STATUSES.has(status);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        for (let page = 0; page < MAX_INITIAL_PAGES; page += 1) {
          const result = await fetchEventPage(runId, lastSeq.current);
          if (cancelled) return;
          if (result.items.length > 0) {
            lastSeq.current = result.items.at(-1)?.sequence ?? lastSeq.current;
            setEvents((prev) => mergeEvents(prev, result.items));
          }
          if (result.next_after_sequence === null) break;
        }
      } catch (err) {
        if (!cancelled) setError(err);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // Re-sync when the run's status changes (e.g. it finished or was retried).
  }, [runId, status]);

  useEffect(() => {
    if (!active || loading) return;
    const stop = streamEvents({
      path: `/runs/${encodeURIComponent(runId)}/events/stream`,
      lastEventId: lastSeq.current,
      onOpen: () => setStreaming(true),
      onError: () => setStreaming(false),
      onEnd: () => {
        setStreaming(false);
        void client.invalidateQueries({ queryKey: queryKeys.run(runId) });
      },
      onMessage: (message) => {
        if (message.event !== "run_event") return;
        try {
          const event = JSON.parse(message.data) as RunEvent;
          if (event.sequence <= lastSeq.current) return;
          lastSeq.current = event.sequence;
          setEvents((prev) => mergeEvents(prev, [event]));
          if (
            event.type === "lifecycle" ||
            event.type.startsWith("approval_") ||
            event.type === "evaluation" ||
            event.type === "artifact"
          ) {
            void client.invalidateQueries({ queryKey: queryKeys.run(runId) });
          }
        } catch {
          /* ignore malformed message */
        }
      },
    });
    return () => {
      stop();
      setStreaming(false);
    };
  }, [runId, active, loading, client]);

  return { events, loading, streaming, error };
}
