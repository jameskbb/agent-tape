/**
 * Server-Sent Events over `fetch` (so the auth header can be sent, which EventSource
 * cannot do). Reconnects with `Last-Event-ID` so no event is lost or duplicated.
 */
import { authHeaders, buildUrl } from "./client";

export interface SseMessage {
  id: string | null;
  event: string;
  data: string;
}

/** Incremental parser for the text/event-stream format. */
export class SseParser {
  private buffer = "";
  private id: string | null = null;
  private event = "message";
  private data: string[] = [];

  feed(chunk: string): SseMessage[] {
    this.buffer += chunk;
    const out: SseMessage[] = [];
    let index: number;
    while ((index = this.buffer.search(/\r\n|\r|\n/)) >= 0) {
      const line = this.buffer.slice(0, index);
      const sep = this.buffer.startsWith("\r\n", index) ? 2 : 1;
      this.buffer = this.buffer.slice(index + sep);
      if (line === "") {
        if (this.data.length > 0)
          out.push({ id: this.id, event: this.event, data: this.data.join("\n") });
        this.event = "message";
        this.data = [];
        continue;
      }
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon >= 0 ? line.slice(0, colon) : line;
      let value = colon >= 0 ? line.slice(colon + 1) : "";
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "data") this.data.push(value);
      else if (field === "event") this.event = value;
      else if (field === "id") this.id = value;
    }
    return out;
  }
}

export interface StreamOptions {
  path: string;
  lastEventId?: number;
  onMessage: (message: SseMessage) => void;
  onOpen?: () => void;
  onError?: (error: unknown) => void;
  /** Called when the server says the stream is complete (`event: end`). */
  onEnd?: (data: string) => void;
}

/** Start streaming; returns a function that stops it. Reconnects with backoff. */
export function streamEvents(options: StreamOptions): () => void {
  const controller = new AbortController();
  let lastId = options.lastEventId ?? 0;
  let attempt = 0;
  let stopped = false;

  const run = async (): Promise<void> => {
    while (!stopped) {
      try {
        const response = await fetch(buildUrl(options.path, { after_sequence: lastId }), {
          headers: {
            Accept: "text/event-stream",
            ...authHeaders(),
            ...(lastId ? { "Last-Event-ID": String(lastId) } : {}),
          },
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error(`stream failed (${response.status})`);
        attempt = 0;
        options.onOpen?.();
        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        const parser = new SseParser();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          for (const message of parser.feed(value)) {
            if (message.event === "end") {
              stopped = true;
              options.onEnd?.(message.data);
              controller.abort();
              return;
            }
            if (message.id && /^\d+$/.test(message.id)) lastId = Number(message.id);
            options.onMessage(message);
          }
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        options.onError?.(error);
      }
      if (stopped) return;
      attempt += 1;
      const delay = Math.min(10_000, 500 * 2 ** Math.min(attempt, 5));
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  };
  void run();
  return () => {
    stopped = true;
    controller.abort();
  };
}
