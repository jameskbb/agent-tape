import { describe, expect, it } from "vitest";

import { SseParser } from "./sse";

describe("SseParser", () => {
  it("parses messages split across chunks", () => {
    const p = new SseParser();
    expect(p.feed("id: 1\nevent: run_event\nda")).toEqual([]);
    expect(p.feed('ta: {"a":1}\n\n: ping\n\nid: 2\ndata: x\ndata: y\n\n')).toEqual([
      { id: "1", event: "run_event", data: '{"a":1}' },
      { id: "2", event: "message", data: "x\ny" },
    ]);
  });

  it("handles CRLF line endings", () => {
    expect(new SseParser().feed("event: end\r\ndata: {}\r\n\r\n")).toEqual([
      { id: null, event: "end", data: "{}" },
    ]);
  });
});
