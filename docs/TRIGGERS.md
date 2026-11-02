# Webhook triggers

A trigger lets another system start a saved prompt (or a workflow, once the workflow
executor is installed) by POSTing JSON to a private URL. Every accepted delivery creates a
normal run through `RunService` with `source=webhook`.

Create triggers in **Settings > Webhook triggers** or with `POST /api/v1/triggers`. The
response contains the token **once**; AgentTape stores only its SHA-256 hash. You can also
supply your own token (24 characters or more). `POST /triggers/{id}/rotate-token` replaces
it. `agenttape triggers list` shows triggers from the CLI.

## Delivering

```
POST /api/v1/webhooks/{trigger_id}
Content-Type: application/json
Authorization: Bearer <token>
```

The token can also be sent as `X-AgentTape-Token: <token>`.

A `?token=` query parameter is **refused by default** (401, `query_token_disabled`,
checked before the token itself so the response reveals nothing about it). URLs are
written to sender, proxy, and server logs, so a token in a URL is a leaked credential
waiting to happen. A trigger can opt in with `allow_query_token: true` (API) or **Allow
URL tokens** on its detail page, which asks for confirmation. Opt in only for a sender
that cannot set headers, keep the endpoint behind TLS, and rotate the token if a log
leaks. Accepted query-token deliveries are recorded with `auth: query`.

Webhook paths are public in the sense that they skip the local API token (they are listed
in the router's `PUBLIC_PATHS`); each trigger authenticates with its own token.

Checks, in order:

| Check | Result when it fails | Logged in deliveries |
|---|---|---|
| Body over 4 MB (hard cap, before lookup) | 413 | no |
| Unknown trigger | 404 | no |
| Missing or wrong token | 401; 429 after 20 failures per minute from one client | no |
| Trigger disabled | 403 | yes |
| Per-trigger rate limit (`rate_limit_per_minute`, token bucket) | 429 with `Retry-After` | yes |
| Per-trigger size cap (`max_payload_bytes`, default 256 KB) | 413 | yes |
| `Content-Type` is not `application/json` or `application/*+json` | 415 | yes |
| Body is not JSON | 400 | yes |
| No idempotency key when the trigger requires one | 400 | yes |
| Input mapping fails or a required input is missing | 422 | yes |
| Run creation fails (for example an archived prompt) | 4xx | yes (`error`) |

An accepted delivery returns `202` with `{"status": "accepted", "run_id", "delivery_id",
"idempotency_source"}`. A repeated delivery returns `200` with `"duplicate": true` and the
same `run_id`. Errors use the standard envelope `{"error": {"code", "message", "details"}}`.

## Idempotency

Each delivery gets a key from the first of:

1. the `Idempotency-Key` header;
2. the trigger's `idempotency_key_path` (a JSON path into the body, for example `$.delivery.id`);
3. a delivery id header: `X-GitHub-Delivery`, `X-Gitlab-Event-UUID`, `X-Delivery-Id`, or
   `X-Request-Id`;
4. a SHA-256 of the raw body (fallback: identical bodies count as the same delivery).

Set `"idempotency": "require"` on a trigger to reject deliveries that have none of 1-3.
The key is hashed with the trigger id and used both for the delivery log and as the run's
idempotency key (`trigger:<id>:<hash>`), so concurrent identical deliveries create one run.

## Input mapping

`input_mapping` maps prompt variables to JSON paths in the body:

```json
{"branch": "$.client_payload.branch", "commit": "$.commits[0].id"}
```

The path syntax is deliberately small: `$`, then `.name`, `[0]`, or `["quoted key"]`
segments. No wildcards, filters, slices, or recursive descent, so a path selects at most
one value. Missing values fall back to the prompt's defaults; objects and arrays become
JSON strings. Secret-reference variables cannot be mapped, because the sender would choose
which secret gets resolved. For a workflow target the same holds for secret-typed workflow
inputs and for any `agent_run` node that fills a secret prompt variable from a template;
such a trigger is refused when saved and again at delivery. `static_inputs` sets fixed values. `POST
/triggers/{id}/test-mapping` applies the mapping to a sample body without creating a run.

## What is recorded

Each logged delivery (`GET /triggers/{id}/deliveries`) stores its status (`accepted`,
`duplicate`, `rejected`, `error`), HTTP status, run id, payload size, the idempotency
source, a few non-secret headers (`User-Agent`, `X-GitHub-Event`, `X-GitHub-Hook-ID`,
`X-Gitlab-Event`, `Content-Type`), and a redacted 2 KB payload preview. The run gets a
`trigger` event naming the trigger and delivery. Creating, updating, rotating, and deleting
triggers write audit entries.

## Examples

### Generic webhook (curl)

```bash
curl -X POST "http://127.0.0.1:8787/api/v1/webhooks/$TRIGGER_ID" \
  -H "Authorization: Bearer $AGENTTAPE_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: deploy-2026-09-23-1" \
  -d '{"client_payload": {"branch": "main"}}'
```

### GitHub `repository_dispatch`

**Recommended: a GitHub Actions step** (Option 2 below) that sends the token in the
`Authorization` header from a repository secret.

Option 1, a repository webhook. GitHub repository webhooks cannot set custom headers, and
their `X-Hub-Signature-256` HMAC cannot be verified because AgentTape stores only a hash
of the trigger token, never the token itself. So a repository webhook works only with a
URL token: first enable **Allow URL tokens** on the trigger (or PATCH
`{"allow_query_token": true}`), then in the GitHub repository settings add a webhook:

* Payload URL: `https://<your agenttape host>/api/v1/webhooks/<trigger id>?token=<token>`
* Content type: `application/json`
* Events: "Let me select individual events", then "Repository dispatches"

Map inputs from the event body, for example `{"branch": "$.client_payload.branch"}`. The
`X-GitHub-Delivery` header makes redeliveries idempotent. Then send an event:

```bash
gh api repos/OWNER/REPO/dispatches \
  -f event_type=agenttape \
  -f 'client_payload[branch]=main'
```

GitHub must be able to reach AgentTape. AgentTape binds to loopback by default; binding
beyond it requires an API token (see the README) and should sit behind TLS.

Option 2, a GitHub Actions step that posts a `repository_dispatch`-shaped body:

```yaml
- name: Start an AgentTape run
  run: |
    curl -fsS -X POST "$AGENTTAPE_URL/api/v1/webhooks/<trigger id>" \
      -H "Authorization: Bearer ${{ secrets.AGENTTAPE_WEBHOOK_TOKEN }}" \
      -H "Content-Type: application/json" \
      -H "Idempotency-Key: ${{ github.run_id }}-${{ github.run_attempt }}" \
      -d '{"event_type":"agenttape","client_payload":{"branch":"${{ github.ref_name }}"}}'
```

The trigger detail page generates these examples with the real URL and mapping.

## Adding a trigger source (Jira, GitHub App)

V1 ships one source, the generic webhook (`agenttape/triggers/webhook.py`, kind `webhook`).
Sources plug into `agenttape/triggers/base.py`:

```python
from agenttape.triggers.base import InboundRequest, TriggerSource, register_trigger_source

@register_trigger_source
class JiraSource(TriggerSource):
    kind = "jira"
    display_name = "Jira automation"

    def presented_token(self, request: InboundRequest) -> str | None: ...
    def delivery_id(self, request: InboundRequest) -> tuple[str, str] | None: ...
    def metadata(self, request: InboundRequest) -> dict[str, object]: ...
```

A trigger with `kind = "jira"` then flows through the same pipeline in
`TriggerService.handle_delivery`: size cap, content type, rate limit, idempotency, input
mapping, run creation, and the delivery log. Notes for the two planned integrations:

* **Jira automation.** Jira's "Send web request" action can send a custom header, so the
  bearer-token scheme works as is. A Jira source would read the delivery id from the
  `X-Atlassian-Webhook-Identifier` header and could offer mapping presets such as
  `$.issue.key` and `$.issue.fields.summary`. Jira Cloud *app* webhooks are signed with a
  JWT from the app's shared secret; verifying those needs the secret at verification time
  (a `SecretReference` resolved when a delivery arrives), not only a hash.
* **GitHub App.** App webhooks are signed with HMAC-SHA256 of the body using the app's
  webhook secret (`X-Hub-Signature-256`). Like Jira JWTs this needs the secret itself, so a
  GitHub App source should resolve it through the secret resolver (environment variable
  named by a `SecretReference`) and compare signatures in constant time. App installation
  flows are out of scope for V1.

Both would register a module in `agenttape/triggers/`; nothing central changes.
