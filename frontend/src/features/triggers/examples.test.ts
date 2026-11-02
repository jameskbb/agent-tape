import { describe, expect, it } from "vitest";

import {
  curlExample,
  githubActionsExample,
  githubDispatchExample,
  samplePayload,
} from "./examples";

describe("webhook examples", () => {
  const mapping = {
    branch: "$.client_payload.branch",
    reason: "$.client_payload.reason",
    n: "$.commits[0].id",
  };

  it("builds a sample payload from dotted mapping paths only", () => {
    expect(samplePayload(mapping)).toEqual({
      client_payload: { branch: "<branch>", reason: "<reason>" },
    });
  });

  it("puts a placeholder instead of a token unless one is given", () => {
    const url = "http://127.0.0.1:8787/api/v1/webhooks/abc";
    expect(curlExample(url, mapping)).toContain('"Authorization: Bearer $AGENTTAPE_WEBHOOK_TOKEN"');
    const withToken = curlExample(url, mapping, "atw_secret");
    expect(withToken).toContain("'Authorization: Bearer atw_secret'");
    expect(withToken).toContain("-H 'Content-Type: application/json'");
    expect(withToken).toContain(
      `-d '{"client_payload":{"branch":"<branch>","reason":"<reason>"}}'`,
    );
  });

  it("builds GitHub examples", () => {
    const step = githubActionsExample("https://example.test/api/v1/webhooks/abc", mapping);
    expect(step).toContain("secrets.AGENTTAPE_WEBHOOK_TOKEN");
    expect(step).toContain('"branch":"${{ github.ref_name }}"');
    expect(githubDispatchExample(mapping)).toContain("-f 'client_payload[branch]=<branch>'");
  });
});
