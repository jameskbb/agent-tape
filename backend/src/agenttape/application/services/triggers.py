"""TriggerService: webhook trigger management and the inbound delivery pipeline.

A delivery goes through, in order: trigger lookup, token check (hash at rest,
constant-time compare, failed attempts rate-limited per client), per-trigger rate limit,
payload size cap, JSON content type, JSON parse, idempotency key, input mapping, and run
creation through :meth:`RunService.create_run` (``source=webhook``). Every delivery that
passed authentication is recorded in ``trigger_deliveries`` with its result, so the
delivery log shows accepted, duplicate, and rejected attempts.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.schemas.schedules import (
    DeliveryOut,
    DeliveryResult,
    MappingTestResult,
    TriggerCreate,
    TriggerOut,
    TriggerUpdate,
    TriggerWithToken,
)
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, paginate
from agenttape.application.services.runs import RunService
from agenttape.application.services.schedules import workflow_run_fields, workflow_runs_available
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.domain.enums import EventType, RunSource, VariableType
from agenttape.domain.errors import AgentTapeError, ConflictError, ValidationError
from agenttape.domain.prompts import VariableDefinition
from agenttape.domain.workflows import NodeType, template_references
from agenttape.infrastructure.db.base import new_id
from agenttape.infrastructure.db.models import (
    AgentProfile,
    PromptTemplate,
    PromptVersion,
    Trigger,
    TriggerDelivery,
    Workflow,
    WorkflowVersion,
)
from agenttape.security.ratelimit import RateLimiter
from agenttape.security.redaction import DEFAULT_REDACTOR
from agenttape.security.tokens import generate_token, hash_token, token_display_prefix, verify_token
from agenttape.triggers.base import InboundRequest, TriggerSource, get_trigger_source
from agenttape.triggers.jsonpath import MISSING, JsonPathError, parse_path

log = logging.getLogger("agenttape.triggers")

WEBHOOK_TOKEN_PREFIX = "atw_"  # noqa: S105 - public prefix of generated webhook tokens
#: Hard cap applied before any trigger lookup (per-trigger caps are lower or equal).
MAX_WEBHOOK_BODY_BYTES = 4 * 1024 * 1024
PAYLOAD_PREVIEW_BYTES = 2048
_LIMITERS_KEY = "trigger_rate_limiters"
_AUTH_FAILURES_KEY = "webhook_auth_failures"


def webhook_path(trigger_id: str) -> str:
    return f"/api/v1/webhooks/{trigger_id}"


def is_json_content_type(value: str) -> bool:
    media = value.split(";", 1)[0].strip().lower()
    return media == "application/json" or (
        media.startswith("application/") and media.endswith("+json")
    )


@dataclass
class DeliveryOutcome:
    http_status: int
    result: DeliveryResult


class TriggerService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.events = EventRecorder(ctx.settings, ctx.clock)

    # ================================================================== management

    def _validate(self, session: Session, t: Trigger) -> None:
        if get_trigger_source(t.kind) is None:
            raise ValidationError(f"Unknown trigger kind '{t.kind}'")
        mapping = dict(t.input_mapping or {})
        for variable, path in mapping.items():
            if not variable.isidentifier():
                raise ValidationError(f"'{variable}' is not a valid variable name")
            try:
                parse_path(path)
            except JsonPathError as exc:
                raise ValidationError(
                    f"Mapping for '{variable}': {exc}", details={"variable": variable, "path": path}
                ) from exc
        if t.idempotency_key_path:
            try:
                parse_path(t.idempotency_key_path)
            except JsonPathError as exc:
                raise ValidationError(f"Idempotency key path: {exc}") from exc
        if t.target_type == "prompt":
            if not t.prompt_template_id or not t.agent_profile_id:
                raise ValidationError("A prompt trigger needs a saved prompt and an agent profile")
            template = get_or_404(session, PromptTemplate, t.prompt_template_id, "prompt")
            if template.archived:
                raise ConflictError("That prompt is archived")
            profile = get_or_404(session, AgentProfile, t.agent_profile_id, "agent profile")
            if profile.archived:
                raise ConflictError("That agent profile is archived")
            version = self._prompt_version(session, template, t.prompt_version_id)
            names = {str(v.get("name")): v for v in (version.variables if version else [])}
            for variable in [*mapping, *dict(t.config or {}).get("static_inputs", {})]:
                if variable not in names:
                    raise ValidationError(
                        f"The prompt has no variable named '{variable}'",
                        details={"variables": sorted(names)},
                    )
            for variable in mapping:
                if names[variable].get("type") == VariableType.SECRET.value:
                    raise ValidationError(
                        f"'{variable}' is a secret reference; webhook payloads cannot choose which secret is used"
                    )
            t.workflow_id = None
        elif t.target_type == "workflow":
            if not t.workflow_id:
                raise ValidationError("Choose a workflow for this trigger")
            workflow = get_or_404(session, Workflow, t.workflow_id, "workflow")
            if workflow.archived:
                raise ConflictError("That workflow is archived")
            if not workflow_runs_available():
                raise ValidationError("Workflow runs are not available in this build yet")
            latest = session.scalars(
                select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == workflow.id,
                    WorkflowVersion.version == workflow.latest_version,
                )
            ).first()
            if latest is not None:
                self._check_workflow_secrets(session, dict(latest.definition or {}), mapping)
            t.prompt_template_id = t.prompt_version_id = t.agent_profile_id = None
        else:
            raise ValidationError(f"Unknown target type '{t.target_type}'")

    def _check_workflow_secrets(
        self, session: Session, definition: dict[str, Any], mapped: Iterable[str]
    ) -> None:
        """A webhook payload may never choose which secret a workflow run resolves.

        Secret variables hold a secret reference *name*, and a name the payload picks
        would resolve any secret the worker can see. So on the workflow path, as on the
        prompt path, a secret-typed workflow input cannot be mapped from the payload, and
        an ``agent_run`` node cannot fill a secret-typed prompt variable from a template
        (``{{ inputs.x }}`` or ``{{ nodes.y.outputs.z }}`` can carry payload data).
        """
        mapped = set(mapped)
        for raw in definition.get("inputs") or []:
            if (
                isinstance(raw, dict)
                and raw.get("type") == VariableType.SECRET.value
                and raw.get("name") in mapped
            ):
                raise ValidationError(
                    f"Workflow input '{raw.get('name')}' is a secret reference; webhook "
                    "payloads cannot choose which secret is used"
                )
        for node in definition.get("nodes") or []:
            if not isinstance(node, dict) or node.get("type") != NodeType.AGENT_RUN.value:
                continue
            config = dict(node.get("config") or {})
            template_id = config.get("prompt_template_id")
            template = session.get(PromptTemplate, str(template_id)) if template_id else None
            if template is None:
                continue
            try:
                version = self._prompt_version(session, template, config.get("prompt_version_id"))
            except AgentTapeError:
                continue
            node_inputs = dict(node.get("inputs") or {})
            for var in version.variables if version else []:
                name = str(var.get("name"))
                if var.get("type") == VariableType.SECRET.value and template_references(
                    node_inputs.get(name)
                ):
                    raise ValidationError(
                        f"Node '{node.get('key')}' fills the secret variable '{name}' from a "
                        "template; a webhook-started run cannot choose which secret is used. "
                        "Give the node a fixed secret reference name instead",
                        details={"node": node.get("key"), "variable": name},
                    )

    @staticmethod
    def _prompt_version(
        session: Session, template: PromptTemplate, version_id: str | None
    ) -> PromptVersion | None:
        if version_id:
            v = get_or_404(session, PromptVersion, version_id, "prompt version")
            if v.template_id != template.id:
                raise ValidationError("That prompt version belongs to a different prompt")
            return v
        return session.scalars(
            select(PromptVersion).where(
                PromptVersion.template_id == template.id,
                PromptVersion.version == template.latest_version,
            )
        ).first()

    def create(self, data: TriggerCreate, *, actor: str = "local user") -> TriggerWithToken:
        token = data.token or generate_token(WEBHOOK_TOKEN_PREFIX)
        now = self.ctx.clock.now()
        with self.ctx.session() as session:
            ws_id = default_workspace_id(session, self.ctx.clock)
            if session.scalars(
                select(Trigger.id).where(Trigger.workspace_id == ws_id, Trigger.name == data.name)
            ).first():
                raise ConflictError(f"A trigger named '{data.name}' already exists")
            t = Trigger(
                workspace_id=ws_id,
                project_id=data.project_id,
                name=data.name,
                description=data.description,
                kind=data.kind,
                target_type=data.target_type,
                prompt_template_id=data.prompt_template_id,
                prompt_version_id=data.prompt_version_id,
                agent_profile_id=data.agent_profile_id,
                workflow_id=data.workflow_id,
                token_hash=hash_token(token),
                token_prefix=token_display_prefix(token),
                input_mapping=dict(data.input_mapping),
                idempotency_key_path=data.idempotency_key_path or None,
                enabled=data.enabled,
                rate_limit_per_minute=data.rate_limit_per_minute,
                max_payload_bytes=data.max_payload_bytes,
                config={
                    "static_inputs": dict(data.static_inputs),
                    "idempotency": data.idempotency,
                    "allow_query_token": data.allow_query_token,
                },
                created_at=now,
                updated_at=now,
            )
            self._validate(session, t)
            session.add(t)
            session.flush()
            record_audit(
                session,
                self.ctx.clock,
                action="trigger.create",
                resource_type="trigger",
                resource_id=t.id,
                actor=actor,
                workspace_id=ws_id,
                details={
                    "name": t.name,
                    "target_type": t.target_type,
                    "token_supplied": bool(data.token),
                    "allow_query_token": data.allow_query_token,
                },
            )
            return TriggerWithToken(trigger=self._out(session, t), token=token)

    def update(
        self, trigger_id: str, data: TriggerUpdate, *, actor: str = "local user"
    ) -> TriggerOut:
        with self.ctx.session() as session:
            t = get_or_404(session, Trigger, trigger_id, "trigger")
            changes = data.model_dump(exclude_unset=True)
            config = dict(t.config or {})
            if "static_inputs" in changes:
                config["static_inputs"] = dict(changes.pop("static_inputs") or {})
            if "idempotency" in changes:
                config["idempotency"] = changes.pop("idempotency")
            if "allow_query_token" in changes:
                config["allow_query_token"] = bool(changes.pop("allow_query_token"))
            t.config = config
            new_name = changes.get("name")
            clash = (
                new_name is not None
                and new_name != t.name
                and session.scalars(
                    select(Trigger.id).where(
                        Trigger.workspace_id == t.workspace_id,
                        Trigger.name == new_name,
                        Trigger.id != t.id,
                    )
                ).first()
            )
            if clash:
                raise ConflictError(f"A trigger named '{new_name}' already exists")
            for key, value in changes.items():
                setattr(t, key, value)
            self._validate(session, t)
            t.updated_at = self.ctx.clock.now()
            record_audit(
                session,
                self.ctx.clock,
                action="trigger.update",
                resource_type="trigger",
                resource_id=t.id,
                actor=actor,
                workspace_id=t.workspace_id,
                details={
                    "fields": sorted(
                        [
                            *changes,
                            *(
                                ["config"]
                                if data.static_inputs is not None
                                or data.allow_query_token is not None
                                else []
                            ),
                        ]
                    )
                },
            )
            session.flush()
            return self._out(session, t)

    def rotate_token(self, trigger_id: str, *, actor: str = "local user") -> TriggerWithToken:
        token = generate_token(WEBHOOK_TOKEN_PREFIX)
        with self.ctx.session() as session:
            t = get_or_404(session, Trigger, trigger_id, "trigger")
            t.token_hash = hash_token(token)
            t.token_prefix = token_display_prefix(token)
            t.updated_at = self.ctx.clock.now()
            record_audit(
                session,
                self.ctx.clock,
                action="trigger.rotate_token",
                resource_type="trigger",
                resource_id=t.id,
                actor=actor,
                workspace_id=t.workspace_id,
                details={"name": t.name},
            )
            return TriggerWithToken(trigger=self._out(session, t), token=token)

    def delete(self, trigger_id: str, *, actor: str = "local user") -> None:
        with self.ctx.session() as session:
            t = get_or_404(session, Trigger, trigger_id, "trigger")
            record_audit(
                session,
                self.ctx.clock,
                action="trigger.delete",
                resource_type="trigger",
                resource_id=t.id,
                actor=actor,
                workspace_id=t.workspace_id,
                details={"name": t.name},
            )
            session.delete(t)

    def get(self, trigger_id: str) -> TriggerOut:
        with self.ctx.session() as session:
            return self._out(session, get_or_404(session, Trigger, trigger_id, "trigger"))

    def list_triggers(self, *, limit: int = 50, offset: int = 0) -> Page[TriggerOut]:
        with self.ctx.session() as session:
            stmt = select(Trigger).order_by(Trigger.name, Trigger.id)
            return paginate(
                session, stmt, limit=limit, offset=offset, mapper=lambda t: self._out(session, t)
            )

    def deliveries(self, trigger_id: str, *, limit: int = 50, offset: int = 0) -> Page[DeliveryOut]:
        with self.ctx.session() as session:
            get_or_404(session, Trigger, trigger_id, "trigger")
            stmt = (
                select(TriggerDelivery)
                .where(TriggerDelivery.trigger_id == trigger_id)
                .order_by(TriggerDelivery.received_at.desc(), TriggerDelivery.id)
            )
            return paginate(
                session, stmt, limit=limit, offset=offset, mapper=DeliveryOut.model_validate
            )

    def _out(self, session: Session, t: Trigger) -> TriggerOut:
        out = TriggerOut.model_validate(t)
        out.webhook_path = webhook_path(t.id)
        if t.prompt_template_id:
            out.target_name = session.scalar(
                select(PromptTemplate.name).where(PromptTemplate.id == t.prompt_template_id)
            )
        elif t.workflow_id:
            out.target_name = session.scalar(
                select(Workflow.name).where(Workflow.id == t.workflow_id)
            )
        if t.agent_profile_id:
            out.agent_profile_name = session.scalar(
                select(AgentProfile.name).where(AgentProfile.id == t.agent_profile_id)
            )
        out.delivery_count = int(
            session.scalar(
                select(func.count())
                .select_from(TriggerDelivery)
                .where(TriggerDelivery.trigger_id == t.id)
            )
            or 0
        )
        return out

    def test_mapping(self, trigger_id: str, payload: Any) -> MappingTestResult:
        with self.ctx.session() as session:
            t = get_or_404(session, Trigger, trigger_id, "trigger")
            inputs, missing, errors = self._map_inputs(session, t, payload)
        return MappingTestResult(inputs=inputs, missing=missing, errors=errors)

    # ================================================================== delivery

    def _auth_failures(self) -> RateLimiter:
        limiter = self.ctx.extensions.get(_AUTH_FAILURES_KEY)
        if limiter is None:
            limiter = RateLimiter(rate=20, per_seconds=60)
            self.ctx.extensions[_AUTH_FAILURES_KEY] = limiter
        return limiter

    def handle_delivery(self, trigger_id: str, request: InboundRequest) -> DeliveryOutcome:
        with self.ctx.session() as session:
            t = session.get(Trigger, trigger_id)
            if t is None:
                return _reject(404, "not_found", "Unknown trigger")
            source = get_trigger_source(t.kind)
            if source is None:
                return _reject(404, "not_found", "Unknown trigger")
            presented = source.presented_token(request)
            if (
                presented is not None
                and source.token_in_query(request)
                and not dict(t.config or {}).get("allow_query_token")
            ):
                # A bearer credential in a URL leaks into logs, so query
                # tokens are refused unless the trigger opted in. Checked before the
                # token itself, so the response says nothing about its validity.
                self._auth_failures().allow(request.client)
                log.warning("webhook %s: rejected a query-string token (not enabled)", trigger_id)
                return _reject(
                    401,
                    "query_token_disabled",
                    "Send the token in the Authorization or X-AgentTape-Token header; "
                    "query-string tokens are disabled for this trigger",
                )
            if not verify_token(presented, t.token_hash):
                if not self._auth_failures().allow(request.client):
                    return _reject(429, "rate_limited", "Too many failed authentication attempts")
                log.warning(
                    "webhook %s: rejected delivery with a missing or wrong token", trigger_id
                )
                return _reject(401, "unauthorized", "A valid trigger token is required")
            snapshot = _TriggerSnapshot.of(t)
        return self._authenticated(snapshot, source, request)

    def _authenticated(
        self, t: _TriggerSnapshot, source: TriggerSource, request: InboundRequest
    ) -> DeliveryOutcome:
        meta: dict[str, Any] = {**source.metadata(request), "client": request.client}
        size = len(request.body)
        if not t.enabled:
            return self._record_rejection(
                t, 403, "disabled", "This trigger is disabled", meta, size
            )
        if not self._limiter_for(t).allow(t.id):
            return self._record_rejection(
                t,
                429,
                "rate_limited",
                f"Rate limit of {t.rate_limit_per_minute} deliveries per minute exceeded",
                meta,
                size,
            )
        if size > t.max_payload_bytes:
            return self._record_rejection(
                t,
                413,
                "payload_too_large",
                f"Payload exceeds {t.max_payload_bytes} bytes",
                meta,
                size,
            )
        if not is_json_content_type(request.content_type):
            return self._record_rejection(
                t,
                415,
                "unsupported_media_type",
                "Content-Type must be application/json",
                meta,
                size,
            )
        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._record_rejection(
                t, 400, "invalid_json", f"Body is not valid JSON: {exc}", meta, size
            )
        if payload is None:
            return self._record_rejection(
                t, 400, "invalid_json", "An empty body is not a JSON payload", meta, size
            )
        meta["payload_preview"] = _preview(payload)

        key_source, raw_key = self._idempotency(t, source, request, payload)
        if raw_key is None:
            return self._record_rejection(
                t,
                400,
                "idempotency_key_required",
                "This trigger requires an Idempotency-Key header or a delivery id",
                meta,
                size,
            )
        meta["idempotency_source"] = key_source
        key_hash = hashlib.sha256(f"{t.id}:{raw_key}".encode()).hexdigest()

        with self.ctx.session() as session:
            trig = get_or_404(session, Trigger, t.id, "trigger")
            existing = session.scalars(
                select(TriggerDelivery).where(
                    TriggerDelivery.trigger_id == t.id, TriggerDelivery.idempotency_key == key_hash
                )
            ).first()
            if existing is not None and existing.run_id:
                return self._record_duplicate(t, existing.run_id, key_hash, key_source, meta, size)
            inputs, missing, errors = self._map_inputs(session, trig, payload)
        if errors or missing:
            detail = "; ".join(
                errors + [f"missing value for required variable '{m}'" for m in missing]
            )
            return self._record_rejection(
                t, 422, "mapping_failed", f"Input mapping failed: {detail}", meta, size
            )

        run_key = f"trigger:{t.id}:{key_hash}"
        try:
            with self.ctx.session() as session:
                req = self._run_request(session, t, inputs, run_key, key_hash)
            result = RunService(self.ctx).create_run(req, actor=f"webhook:{t.name}")
        except AgentTapeError as exc:
            return self._record_rejection(
                t,
                exc.http_status if exc.http_status >= 400 else 422,
                exc.code,
                exc.message,
                meta,
                size,
                status="error",
            )
        if not result.created:
            return self._record_duplicate(t, result.run.id, key_hash, key_source, meta, size)

        delivery_id = new_id()
        now = self.ctx.clock.now()
        try:
            with self.ctx.session() as session:
                session.add(
                    TriggerDelivery(
                        id=delivery_id,
                        trigger_id=t.id,
                        received_at=now,
                        idempotency_key=key_hash,
                        status="accepted",
                        http_status=202,
                        run_id=result.run.id,
                        payload_bytes=size,
                        metadata_=DEFAULT_REDACTOR.redact_value(meta),
                    )
                )
                row = session.get(Trigger, t.id)
                if row is not None:
                    row.last_delivery_at = now
                self.events.record(
                    session,
                    result.run.id,
                    EventType.TRIGGER,
                    f"Triggered by webhook '{t.name}'",
                    {
                        "trigger_id": t.id,
                        "delivery_id": delivery_id,
                        "idempotency_source": key_source,
                        "event": (meta.get("headers") or {}).get("x-github-event"),
                        "payload_bytes": size,
                    },
                )
        except IntegrityError:
            # A concurrent identical delivery recorded first; the run is the same one.
            return self._record_duplicate(t, result.run.id, key_hash, key_source, meta, size)
        return DeliveryOutcome(
            202,
            DeliveryResult(
                status="accepted",
                delivery_id=delivery_id,
                run_id=result.run.id,
                idempotency_source=key_source,
                message="Run queued",
            ),
        )

    def _limiter_for(self, t: _TriggerSnapshot) -> RateLimiter:
        limiters: dict[tuple[str, int], RateLimiter] = self.ctx.extensions.setdefault(
            _LIMITERS_KEY, {}
        )
        key = (t.id, t.rate_limit_per_minute)
        if key not in limiters:
            limiters[key] = RateLimiter(rate=t.rate_limit_per_minute, per_seconds=60)
        return limiters[key]

    @staticmethod
    def _idempotency(
        t: _TriggerSnapshot, source: TriggerSource, request: InboundRequest, payload: Any
    ) -> tuple[str, str | None]:
        header = request.headers.get("idempotency-key", "").strip()
        if header:
            return "header:idempotency-key", f"key:{header[:255]}"
        if t.idempotency_key_path:
            value = parse_path(t.idempotency_key_path).get(payload)
            if value is not MISSING and value is not None and not isinstance(value, dict | list):
                return f"payload:{t.idempotency_key_path}", f"path:{value}"
        delivery = source.delivery_id(request)
        if delivery is not None:
            return f"header:{delivery[0]}", f"{delivery[0]}:{delivery[1]}"
        if t.idempotency_mode == "require":
            return "none", None
        return "body-sha256", "body:" + hashlib.sha256(request.body).hexdigest()

    def _map_inputs(
        self, session: Session, t: Trigger, payload: Any
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        inputs: dict[str, Any] = dict(dict(t.config or {}).get("static_inputs") or {})
        errors: list[str] = []
        for variable, path in dict(t.input_mapping or {}).items():
            try:
                value = parse_path(str(path)).get(payload)
            except JsonPathError as exc:
                errors.append(f"{variable}: {exc}")
                continue
            if value is MISSING or value is None:
                continue
            if isinstance(value, dict | list):
                value = json.dumps(value, sort_keys=True)[:10_000]
            inputs[variable] = value
        missing: list[str] = []
        if t.target_type == "prompt" and t.prompt_template_id:
            template = session.get(PromptTemplate, t.prompt_template_id)
            version = (
                self._prompt_version(session, template, t.prompt_version_id) if template else None
            )
            for raw in version.variables if version else []:
                d = VariableDefinition.model_validate(raw)
                if d.required and d.default is None and d.name not in inputs:
                    missing.append(d.name)
        return inputs, missing, errors

    def _run_request(
        self,
        session: Session,
        t: _TriggerSnapshot,
        inputs: dict[str, Any],
        run_key: str,
        key_hash: str,
    ) -> RunCreate:
        common: dict[str, Any] = {
            "title": f"{t.name} (webhook)",
            "project_id": t.project_id,
            "inputs": inputs,
            "idempotency_key": run_key,
            "source": RunSource.WEBHOOK,
            "source_ref": f"trigger:{t.id}:{key_hash[:16]}",
            "trigger_id": t.id,
            "tags": ["webhook"],
        }
        if t.target_type == "workflow":
            if not t.workflow_id:
                raise ValidationError("The trigger's workflow was deleted")
            fields = workflow_run_fields(session, t.workflow_id, None)
            definition = dict(
                (fields["extra_snapshot"].get("workflow") or {}).get("definition") or {}
            )
            row = session.get(Trigger, t.id)
            mapping = dict(row.input_mapping or {}) if row is not None else {}
            self._check_workflow_secrets(session, definition, mapping)
            return RunCreate(**fields, **common)
        if not t.prompt_template_id or not t.agent_profile_id:
            raise ValidationError("The trigger's prompt or agent profile was deleted")
        return RunCreate(
            kind="agent",
            agent_profile_id=t.agent_profile_id,
            prompt_template_id=t.prompt_template_id,
            prompt_version_id=t.prompt_version_id,
            **common,
        )

    def _record_rejection(
        self,
        t: _TriggerSnapshot,
        http_status: int,
        code: str,
        message: str,
        meta: dict[str, Any],
        size: int,
        *,
        status: str = "rejected",
    ) -> DeliveryOutcome:
        delivery_id = new_id()
        with self.ctx.session() as session:
            session.add(
                TriggerDelivery(
                    id=delivery_id,
                    trigger_id=t.id,
                    received_at=self.ctx.clock.now(),
                    idempotency_key=f"{status}:{delivery_id}",
                    status=status,
                    http_status=http_status,
                    payload_bytes=size,
                    metadata_=DEFAULT_REDACTOR.redact_value({**meta, "code": code}),
                    error=DEFAULT_REDACTOR.redact_text(message)[:2000],
                )
            )
        return DeliveryOutcome(
            http_status,
            DeliveryResult(
                status=status, code=code, delivery_id=delivery_id, run_id=None, message=message
            ),
        )

    def _record_duplicate(
        self,
        t: _TriggerSnapshot,
        run_id: str,
        key_hash: str,
        key_source: str,
        meta: dict[str, Any],
        size: int,
    ) -> DeliveryOutcome:
        delivery_id = new_id()
        with self.ctx.session() as session:
            session.add(
                TriggerDelivery(
                    id=delivery_id,
                    trigger_id=t.id,
                    received_at=self.ctx.clock.now(),
                    idempotency_key=f"duplicate:{delivery_id}",
                    status="duplicate",
                    http_status=200,
                    run_id=run_id,
                    payload_bytes=size,
                    metadata_=DEFAULT_REDACTOR.redact_value({**meta, "duplicate_of_key": key_hash}),
                )
            )
        return DeliveryOutcome(
            200,
            DeliveryResult(
                status="duplicate",
                delivery_id=delivery_id,
                run_id=run_id,
                duplicate=True,
                idempotency_source=key_source,
                message="Already delivered; returning the existing run",
            ),
        )


@dataclass(frozen=True)
class _TriggerSnapshot:
    """Detached copy of the trigger fields the pipeline needs."""

    id: str
    name: str
    enabled: bool
    target_type: str
    project_id: str | None
    prompt_template_id: str | None
    prompt_version_id: str | None
    agent_profile_id: str | None
    workflow_id: str | None
    rate_limit_per_minute: int
    max_payload_bytes: int
    idempotency_key_path: str | None
    idempotency_mode: str

    @classmethod
    def of(cls, t: Trigger) -> _TriggerSnapshot:
        return cls(
            id=t.id,
            name=t.name,
            enabled=t.enabled,
            target_type=t.target_type,
            project_id=t.project_id,
            prompt_template_id=t.prompt_template_id,
            prompt_version_id=t.prompt_version_id,
            agent_profile_id=t.agent_profile_id,
            workflow_id=t.workflow_id,
            rate_limit_per_minute=t.rate_limit_per_minute,
            max_payload_bytes=t.max_payload_bytes,
            idempotency_key_path=t.idempotency_key_path,
            idempotency_mode=str(dict(t.config or {}).get("idempotency") or "derive"),
        )


def _reject(status: int, code: str, message: str) -> DeliveryOutcome:
    return DeliveryOutcome(
        status,
        DeliveryResult(
            status="rejected", code=code, delivery_id=None, run_id=None, message=message
        ),
    )


def _preview(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, default=str)
    redacted = DEFAULT_REDACTOR.redact_text(text)
    if len(redacted.encode("utf-8")) > PAYLOAD_PREVIEW_BYTES:
        return (
            redacted.encode("utf-8")[:PAYLOAD_PREVIEW_BYTES].decode("utf-8", errors="ignore") + "…"
        )
    return redacted
