"""Workflow definitions: the DAG model, validation, safe templates, and conditions.

Pure domain code (no I/O). See docs/ORCHESTRATION.md for the user-facing description.

Templates
---------
``{{ path }}`` or ``{{ path | filter }}`` inside any string of a node's ``inputs`` or
``config``. A path is a dotted lookup on a fixed context::

    inputs.<name>                      workflow run inputs
    nodes.<key>.outputs.<field>...     outputs of a completed upstream node
    nodes.<key>.status                 status of an upstream node
    node.inputs.<name>                 the current node's resolved inputs
    workflow.run_id | workflow.name | workflow.version

Segments are identifiers or list indices. Lookups only index dicts and lists; there is no
attribute access, no function call, and no evaluation. Resolution is a single pass over
the *original* text, so values that themselves contain ``{{ ... }}`` are inserted
literally and never expanded (no template injection). When a string is exactly one
template, the value keeps its JSON type (a list stays a list); otherwise values are
converted to text (objects and lists as JSON). Filters: ``json``, ``length``, ``join``.

Conditions
----------
A tiny expression language parsed by a recursive-descent parser (never ``eval``)::

    expr   := or
    or     := and ("or" and)*
    and    := not ("and" not)*
    not    := "not" not | cmp
    cmp    := value (("=="|"!="|"<"|"<="|">"|">="|"in"|"contains") value)?
    value  := path | "string" | 'string' | number | true | false | null | "(" expr ")"

Paths use the template context above; a missing path evaluates to ``null``.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agenttape.domain.prompts import VariableDefinition

# --------------------------------------------------------------------------- model


class NodeType(StrEnum):
    AGENT_RUN = "agent_run"
    COMMAND = "command"
    TRANSFORM = "transform"
    EVALUATION_GATE = "evaluation_gate"
    APPROVAL = "approval"
    CONDITION = "condition"
    COLLECT = "collect"
    FINDING_REVIEW = "finding_review"


NODE_TYPE_LABELS: dict[NodeType, str] = {
    NodeType.AGENT_RUN: "Agent run",
    NodeType.COMMAND: "Deterministic command",
    NodeType.TRANSFORM: "Prompt render / data transform",
    NodeType.EVALUATION_GATE: "Evaluation gate",
    NodeType.APPROVAL: "Human approval",
    NodeType.CONDITION: "Conditional branch",
    NodeType.COLLECT: "Artifact / output collection",
    NodeType.FINDING_REVIEW: "Finding review (challenge findings)",
}

KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class EdgeOn(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    ALWAYS = "always"


class EdgeCondition(BaseModel):
    """When an edge is *active* (lets its target run)."""

    model_config = {"extra": "forbid"}

    #: Which upstream outcome activates the edge.
    on: EdgeOn = EdgeOn.SUCCESS
    #: Only for edges leaving a ``condition`` node: follow the true or false branch.
    branch: bool | None = None
    #: Optional expression that must also be true.
    when: str | None = Field(default=None, max_length=2000)


class EdgeDef(BaseModel):
    model_config = {"extra": "forbid", "populate_by_name": True}

    source: str = Field(alias="from", min_length=1, max_length=100)
    target: str = Field(alias="to", min_length=1, max_length=100)
    condition: EdgeCondition | None = None

    def dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {"from": self.source, "to": self.target}
        if self.condition is not None:
            out["condition"] = self.condition.model_dump(exclude_none=True)
        return out


class NodeDef(BaseModel):
    model_config = {"extra": "forbid"}

    key: str = Field(min_length=1, max_length=100)
    type: NodeType
    name: str = Field(default="", max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    #: Type-specific configuration (see ``validate_node_config``).
    config: dict[str, Any] = Field(default_factory=dict)
    #: Named inputs resolved from templates before the node runs.
    inputs: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=7 * 24 * 3600)
    max_attempts: int = Field(default=1, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=5.0, ge=0, le=3600)
    continue_on_failure: bool = False
    required: bool = True
    position: dict[str, float] | None = None

    @property
    def label(self) -> str:
        return self.name or self.key


class WorkflowSettings(BaseModel):
    model_config = {"extra": "allow"}

    #: Upper bound on nodes executing at once in one workflow run.
    max_parallel: int = Field(default=4, ge=1, le=64)
    #: Project whose repository commands and child runs use (optional).
    project_id: str | None = None


class WorkflowDefinition(BaseModel):
    model_config = {"extra": "forbid"}

    nodes: list[NodeDef] = Field(default_factory=list)
    edges: list[EdgeDef] = Field(default_factory=list)
    inputs: list[VariableDefinition] = Field(default_factory=list)
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)

    @field_validator("nodes")
    @classmethod
    def _limit_nodes(cls, v: list[NodeDef]) -> list[NodeDef]:
        if len(v) > 200:
            raise ValueError("a workflow may have at most 200 nodes")
        return v

    def node(self, key: str) -> NodeDef:
        for n in self.nodes:
            if n.key == key:
                return n
        raise KeyError(key)

    def node_map(self) -> dict[str, NodeDef]:
        return {n.key: n for n in self.nodes}

    def upstream(self, key: str) -> list[EdgeDef]:
        return [e for e in self.edges if e.target == key]

    def downstream(self, key: str) -> list[EdgeDef]:
        return [e for e in self.edges if e.source == key]

    def dump(self) -> dict[str, Any]:
        return {
            "nodes": [n.model_dump(mode="json", exclude_none=True) for n in self.nodes],
            "edges": [e.dump() for e in self.edges],
            "inputs": [v.model_dump(mode="json", exclude_none=True) for v in self.inputs],
            "settings": self.settings.model_dump(mode="json", exclude_none=True),
        }


# --------------------------------------------------------------------------- validation


@dataclass
class Issue:
    path: str
    message: str
    severity: str = "error"

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message, "severity": self.severity}


def topological_order(keys: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[str] | None:
    """Kahn's algorithm. Returns an order, or ``None`` when the graph has a cycle.

    Ties are broken by first appearance in ``keys`` so the order is deterministic.
    """
    ordered_keys = list(keys)
    index = {k: i for i, k in enumerate(ordered_keys)}
    indegree = dict.fromkeys(ordered_keys, 0)
    children: dict[str, list[str]] = {k: [] for k in ordered_keys}
    for a, b in edges:
        if a in indegree and b in indegree:
            indegree[b] += 1
            children[a].append(b)
    ready = deque(sorted((k for k, d in indegree.items() if d == 0), key=index.__getitem__))
    out: list[str] = []
    while ready:
        k = ready.popleft()
        out.append(k)
        for child in sorted(children[k], key=index.__getitem__):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return out if len(out) == len(ordered_keys) else None


def find_cycle(keys: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[str]:
    """Return one cycle as a list of keys (first == last), or ``[]``."""
    adj: dict[str, list[str]] = {k: [] for k in keys}
    for a, b in edges:
        if a in adj and b in adj:
            adj[a].append(b)
    color: dict[str, int] = dict.fromkeys(adj, 0)
    stack: list[str] = []

    def visit(n: str) -> list[str]:
        color[n] = 1
        stack.append(n)
        for m in adj[n]:
            if color[m] == 1:
                return [*stack[stack.index(m) :], m]
            if color[m] == 0:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        color[n] = 2
        return []

    for k in adj:
        if color[k] == 0:
            cycle = visit(k)
            if cycle:
                return cycle
    return []


def ancestors(definition: WorkflowDefinition, key: str) -> set[str]:
    seen: set[str] = set()
    todo = [e.source for e in definition.upstream(key)]
    while todo:
        k = todo.pop()
        if k in seen:
            continue
        seen.add(k)
        todo.extend(e.source for e in definition.upstream(k))
    return seen


def validate_definition(raw: Mapping[str, Any] | WorkflowDefinition) -> list[Issue]:
    """Every problem with a definition; empty means it can be saved and run."""
    if isinstance(raw, WorkflowDefinition):
        definition = raw
    else:
        try:
            definition = WorkflowDefinition.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError
            errors = getattr(exc, "errors", None)
            if callable(errors):
                return [
                    Issue(".".join(str(p) for p in e.get("loc", ())), str(e.get("msg")))
                    for e in errors()
                ]
            return [Issue("", str(exc))]
    issues: list[Issue] = []
    if not definition.nodes:
        issues.append(Issue("nodes", "A workflow needs at least one node"))
    keys: list[str] = []
    for i, node in enumerate(definition.nodes):
        where = f"nodes[{i}]"
        if not KEY_RE.match(node.key):
            issues.append(
                Issue(
                    f"{where}.key",
                    f"Node key '{node.key}' must start with a letter and use only letters, "
                    "digits, and underscores",
                )
            )
        if node.key in keys:
            issues.append(Issue(f"{where}.key", f"Duplicate node key '{node.key}'"))
        keys.append(node.key)
    key_set = set(keys)
    pairs: set[tuple[str, str]] = set()
    for i, edge in enumerate(definition.edges):
        where = f"edges[{i}]"
        for end, value in (("from", edge.source), ("to", edge.target)):
            if value not in key_set:
                issues.append(Issue(f"{where}.{end}", f"Unknown node '{value}'"))
        if edge.source == edge.target:
            issues.append(Issue(where, f"Node '{edge.source}' cannot depend on itself"))
        if (edge.source, edge.target) in pairs:
            issues.append(Issue(where, f"Duplicate edge {edge.source} -> {edge.target}"))
        pairs.add((edge.source, edge.target))
        cond = edge.condition
        if cond is not None:
            if cond.branch is not None and edge.source in key_set:
                src = definition.node(edge.source)
                if src.type != NodeType.CONDITION:
                    issues.append(
                        Issue(
                            f"{where}.condition.branch",
                            "Only edges leaving a conditional-branch node can choose a branch",
                        )
                    )
            if cond.when:
                try:
                    parse_condition(cond.when)
                except ConditionError as exc:
                    issues.append(Issue(f"{where}.condition.when", str(exc)))
    edge_pairs = [(e.source, e.target) for e in definition.edges]
    if key_set and topological_order(keys, edge_pairs) is None:
        cycle = find_cycle(keys, edge_pairs)
        issues.append(
            Issue("edges", "The graph has a cycle: " + " -> ".join(cycle) if cycle else "cycle")
        )
        return issues  # ancestry checks below assume a DAG
    input_names = {v.name for v in definition.inputs}
    for i, node in enumerate(definition.nodes):
        where = f"nodes[{i}]"
        for problem in validate_node_config(node):
            issues.append(Issue(f"{where}.config", problem))
        allowed = ancestors(definition, node.key) if node.key in key_set else set()
        if node.type == NodeType.FINDING_REVIEW:
            target = node.config.get("target")
            if isinstance(target, str) and target:
                if target not in key_set:
                    issues.append(
                        Issue(f"{where}.config", f"Finding review target '{target}' is not a node")
                    )
                elif target not in allowed:
                    issues.append(
                        Issue(
                            f"{where}.config",
                            f"Finding review target '{target}' is not upstream of "
                            f"'{node.key}' (add an edge so its findings exist first)",
                        )
                    )
                elif definition.node(target).type != NodeType.AGENT_RUN:
                    issues.append(
                        Issue(
                            f"{where}.config",
                            f"Finding review target '{target}' must be an agent run node",
                        )
                    )
        for ref in template_references({"inputs": node.inputs, "config": node.config}):
            root = ref.split(".", 2)
            if root[0] == "nodes":
                if len(root) < 2:
                    issues.append(Issue(where, f"Template '{ref}' must name a node"))
                elif root[1] not in key_set:
                    issues.append(Issue(where, f"Template '{ref}' references unknown node"))
                elif root[1] not in allowed:
                    issues.append(
                        Issue(
                            where,
                            f"Template '{ref}' references '{root[1]}', which is not upstream "
                            f"of '{node.key}' (add an edge so its outputs exist first)",
                        )
                    )
            elif root[0] == "inputs":
                if len(root) > 1 and root[1] not in input_names:
                    issues.append(
                        Issue(where, f"Template '{ref}' references an undeclared workflow input")
                    )
            elif root[0] not in ("node", "workflow"):
                issues.append(Issue(where, f"Template '{ref}' has an unknown root '{root[0]}'"))
    return issues


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def validate_node_config(node: NodeDef) -> list[str]:
    c = node.config
    problems: list[str] = []
    if node.type == NodeType.AGENT_RUN:
        if not c.get("agent_profile_id"):
            problems.append("Agent run nodes need an agent profile")
        if not c.get("prompt_template_id") and not str(c.get("prompt") or "").strip():
            problems.append("Agent run nodes need a prompt template or an inline prompt")
    elif node.type == NodeType.COMMAND:
        argv = c.get("argv")
        if not _str_list(argv) or not argv:
            problems.append("Command nodes need 'argv', a non-empty list of strings")
        elif "{{" in argv[0]:
            problems.append("The executable (argv[0]) must be fixed text, not a template")
        codes = c.get("expected_exit_codes", [0])
        if not isinstance(codes, list) or not all(isinstance(x, int) for x in codes):
            problems.append("'expected_exit_codes' must be a list of integers")
    elif node.type == NodeType.TRANSFORM:
        if "values" not in c and "template" not in c:
            problems.append("Transform nodes need 'values' (an object) or 'template' (text)")
        if "template" in c and not isinstance(c["template"], str):
            problems.append("'template' must be text")
    elif node.type == NodeType.EVALUATION_GATE:
        evaluators = c.get("evaluators") or []
        if not isinstance(evaluators, list):
            problems.append("'evaluators' must be a list")
        elif not evaluators and not c.get("definition_ids") and not c.get("suite_id"):
            problems.append("Evaluation gates need evaluators, definitions, or a suite")
        else:
            for j, spec in enumerate(evaluators):
                if not isinstance(spec, dict) or not spec.get("type"):
                    problems.append(f"evaluators[{j}] needs a 'type'")
    elif node.type == NodeType.APPROVAL:
        if not str(c.get("title") or node.name or "").strip():
            problems.append("Approval nodes need a title")
        exp = c.get("expires_in_seconds")
        if exp is not None and (not isinstance(exp, int | float) or exp <= 0):
            problems.append("'expires_in_seconds' must be a positive number")
    elif node.type == NodeType.CONDITION:
        expr = c.get("expression")
        if not isinstance(expr, str) or not expr.strip():
            problems.append("Conditional nodes need an 'expression'")
        else:
            try:
                parse_condition(expr)
            except ConditionError as exc:
                problems.append(str(exc))
    elif node.type == NodeType.COLLECT:
        include = c.get("include")
        if include is not None and not _str_list(include):
            problems.append("'include' must be a list of node keys")
    elif node.type == NodeType.FINDING_REVIEW:
        if not str(c.get("target") or "").strip():
            problems.append(
                "Finding review nodes need a 'target': the node whose findings to challenge"
            )
        if not c.get("agent_profile_id"):
            problems.append("Finding review nodes need an agent profile")
        version_id = c.get("agent_profile_version_id")
        if version_id is not None and not isinstance(version_id, str):
            problems.append("'agent_profile_version_id' must be text")
    return problems


# --------------------------------------------------------------------------- templates


class TemplateError(ValueError):
    pass


_TEMPLATE_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
_SEGMENT_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_\-]*|\d+)$")
FILTERS = ("json", "length", "join")
_MISSING = object()


def _split_expr(expr: str) -> tuple[str, list[str]]:
    parts = [p.strip() for p in expr.split("|")]
    path, filters = parts[0], parts[1:]
    if not path:
        raise TemplateError("Empty template expression")
    segments = path.split(".")
    if not all(_SEGMENT_RE.match(s) for s in segments):
        raise TemplateError(
            f"'{path}' is not a valid path (use dotted names and list indices only)"
        )
    for f in filters:
        if f not in FILTERS:
            raise TemplateError(f"Unknown filter '{f}' (supported: {', '.join(FILTERS)})")
    return path, filters


def lookup(context: Mapping[str, Any], path: str) -> Any:
    """Follow ``path`` through dicts and lists only. Returns ``_MISSING`` when absent."""
    current: Any = context
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            idx = int(segment)
            if idx >= len(current):
                return _MISSING
            current = current[idx]
        else:
            return _MISSING
    return current


def _apply_filters(value: Any, filters: list[str]) -> Any:
    for f in filters:
        if f == "json":
            value = json.dumps(value, sort_keys=True, default=str)
        elif f == "length":
            value = len(value) if isinstance(value, list | dict | str) else 0
        elif f == "join":
            value = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return value


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def render_string(text: str, context: Mapping[str, Any]) -> Any:
    """Resolve templates in one string (single pass; values are never re-expanded)."""
    whole = _TEMPLATE_RE.fullmatch(text)
    if whole is not None and text.count("{{") == 1:
        path, filters = _split_expr(whole.group(1))
        value = lookup(context, path)
        if value is _MISSING:
            raise TemplateError(f"'{path}' is not available")
        return _apply_filters(value, filters)

    def replace(match: re.Match[str]) -> str:
        path, filters = _split_expr(match.group(1))
        value = lookup(context, path)
        if value is _MISSING:
            raise TemplateError(f"'{path}' is not available")
        return _to_text(_apply_filters(value, filters))

    return _TEMPLATE_RE.sub(replace, text)


def render_value(value: Any, context: Mapping[str, Any]) -> Any:
    """Recursively resolve templates in strings inside dicts and lists."""
    if isinstance(value, str):
        return render_string(value, context) if "{{" in value else value
    if isinstance(value, list):
        return [render_value(v, context) for v in value]
    if isinstance(value, dict):
        return {k: render_value(v, context) for k, v in value.items()}
    return value


def template_references(value: Any) -> list[str]:
    """Every template path referenced anywhere in ``value`` (invalid ones included)."""
    refs: list[str] = []
    if isinstance(value, str):
        for m in _TEMPLATE_RE.finditer(value):
            refs.append(m.group(1).split("|")[0].strip())
    elif isinstance(value, list):
        for v in value:
            refs.extend(template_references(v))
    elif isinstance(value, dict):
        for v in value.values():
            refs.extend(template_references(v))
    return refs


# --------------------------------------------------------------------------- conditions


class ConditionError(ValueError):
    pass


_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<num>-?\d+(?:\.\d+)?)
      | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
      | (?P<op>==|!=|<=|>=|<|>|\(|\))
      | (?P<word>[A-Za-z_][A-Za-z0-9_\-]*(?:\.(?:[A-Za-z_][A-Za-z0-9_\-]*|\d+))*)
    )""",
    re.VERBOSE,
)
_COMPARATORS = {"==", "!=", "<", "<=", ">", ">=", "in", "contains"}


@dataclass
class _Tok:
    kind: str
    value: Any


def _tokenize(text: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None or m.end() == pos:
            raise ConditionError(f"Unexpected text at position {pos}: '{text[pos : pos + 12]}'")
        pos = m.end()
        if m.group("num") is not None:
            raw = m.group("num")
            tokens.append(_Tok("lit", float(raw) if "." in raw else int(raw)))
        elif m.group("str") is not None:
            raw = m.group("str")
            tokens.append(_Tok("lit", re.sub(r"\\(.)", r"\1", raw[1:-1])))
        elif m.group("op") is not None:
            tokens.append(_Tok("op", m.group("op")))
        else:
            word = m.group("word")
            if word in ("true", "false"):
                tokens.append(_Tok("lit", word == "true"))
            elif word == "null":
                tokens.append(_Tok("lit", None))
            elif word in ("and", "or", "not", "in", "contains"):
                tokens.append(_Tok("op", word))
            else:
                tokens.append(_Tok("path", word))
        if len(tokens) > 200:
            raise ConditionError("Expression is too long")
    return tokens


@dataclass
class Condition:
    """A parsed expression tree: ``(op, *args)`` tuples, ``("path", p)``, ``("lit", v)``."""

    tree: tuple[Any, ...]
    paths: list[str] = field(default_factory=list)

    def evaluate(self, context: Mapping[str, Any]) -> bool:
        return bool(_eval(self.tree, context))


def parse_condition(text: str) -> Condition:
    if len(text) > 2000:
        raise ConditionError("Expression is too long")
    tokens = _tokenize(text)
    if not tokens:
        raise ConditionError("Expression is empty")
    pos = 0
    paths: list[str] = []

    def peek() -> _Tok | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> _Tok:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def parse_or() -> tuple[Any, ...]:
        left = parse_and()
        while (t := peek()) is not None and t.kind == "op" and t.value == "or":
            take()
            left = ("or", left, parse_and())
        return left

    def parse_and() -> tuple[Any, ...]:
        left = parse_not()
        while (t := peek()) is not None and t.kind == "op" and t.value == "and":
            take()
            left = ("and", left, parse_not())
        return left

    def parse_not() -> tuple[Any, ...]:
        t = peek()
        if t is not None and t.kind == "op" and t.value == "not":
            take()
            return ("not", parse_not())
        return parse_cmp()

    def parse_cmp() -> tuple[Any, ...]:
        left = parse_value()
        t = peek()
        if t is not None and t.kind == "op" and t.value in _COMPARATORS:
            take()
            return (t.value, left, parse_value())
        return left

    def parse_value() -> tuple[Any, ...]:
        if pos >= len(tokens):
            raise ConditionError("Expression ended unexpectedly")
        t = take()
        if t.kind == "lit":
            return ("lit", t.value)
        if t.kind == "path":
            paths.append(t.value)
            return ("path", t.value)
        if t.kind == "op" and t.value == "(":
            inner = parse_or()
            close = take() if pos < len(tokens) else None
            if close is None or close.value != ")":
                raise ConditionError("Missing closing parenthesis")
            return inner
        raise ConditionError(f"Unexpected '{t.value}'")

    tree = parse_or()
    if pos != len(tokens):
        raise ConditionError(f"Unexpected '{tokens[pos].value}' after the expression")
    return Condition(tree=tree, paths=paths)


def _eval(node: tuple[Any, ...], ctx: Mapping[str, Any]) -> Any:
    op = node[0]
    if op == "lit":
        return node[1]
    if op == "path":
        value = lookup(ctx, node[1])
        return None if value is _MISSING else value
    if op == "not":
        return not _eval(node[1], ctx)
    if op == "and":
        return bool(_eval(node[1], ctx)) and bool(_eval(node[2], ctx))
    if op == "or":
        return bool(_eval(node[1], ctx)) or bool(_eval(node[2], ctx))
    left, right = _eval(node[1], ctx), _eval(node[2], ctx)
    try:
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "in":
            return right is not None and left in right
        if op == "contains":
            return left is not None and right in left
        if left is None or right is None:
            return False
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
    except TypeError:
        return False
    raise ConditionError(f"Unknown operator {op}")


# --------------------------------------------------------------------------- scheduling


SUCCEEDED = "succeeded"
FAILED_STATES = frozenset({"failed", "timed_out"})
TERMINAL_STEP_STATES = frozenset({"succeeded", "failed", "timed_out", "cancelled", "skipped"})


def edge_active(
    edge: EdgeDef,
    source_status: str,
    source_outputs: Mapping[str, Any] | None,
    ctx: Mapping[str, Any],
) -> bool:
    """Whether a settled upstream node lets ``edge.target`` run through this edge."""
    cond = edge.condition or EdgeCondition()
    if cond.on == EdgeOn.SUCCESS and source_status != SUCCEEDED:
        return False
    if cond.on == EdgeOn.FAILURE and source_status not in FAILED_STATES:
        return False
    if cond.on == EdgeOn.ALWAYS and source_status == "skipped":
        return False
    if cond.branch is not None:
        result = bool((source_outputs or {}).get("result"))
        if result is not cond.branch:
            return False
    if cond.when:
        return parse_condition(cond.when).evaluate(ctx)
    return True


def failure_is_fatal(definition: WorkflowDefinition, node: NodeDef) -> bool:
    """A failed node stops the workflow unless it is optional, allowed to fail, or has an
    explicit failure path."""
    if not node.required or node.continue_on_failure:
        return False
    return not any(
        e.condition is not None and e.condition.on in (EdgeOn.FAILURE, EdgeOn.ALWAYS)
        for e in definition.downstream(node.key)
    )


@dataclass
class Readiness:
    """Result of evaluating which queued nodes may start."""

    run: list[str] = field(default_factory=list)
    skip: dict[str, str] = field(default_factory=dict)


def plan_ready(
    definition: WorkflowDefinition,
    statuses: Mapping[str, str],
    outputs: Mapping[str, Mapping[str, Any] | None],
    ctx: Mapping[str, Any],
) -> Readiness:
    """Queued nodes whose upstream nodes are all settled: run them or skip them.

    A node with no incoming edges runs. Otherwise it runs when at least one incoming edge
    is active (see :func:`edge_active`) and is skipped when none are, which lets branches
    re-join after a conditional node.
    """
    result = Readiness()
    order = topological_order(
        [n.key for n in definition.nodes], [(e.source, e.target) for e in definition.edges]
    ) or [n.key for n in definition.nodes]
    for key in order:
        if statuses.get(key) != "queued":
            continue
        incoming = definition.upstream(key)
        if any(statuses.get(e.source) not in TERMINAL_STEP_STATES for e in incoming):
            continue
        if not incoming:
            result.run.append(key)
            continue
        try:
            active = [
                e
                for e in incoming
                if edge_active(e, statuses.get(e.source, ""), outputs.get(e.source), ctx)
            ]
        except ConditionError as exc:
            result.skip[key] = f"Edge condition could not be evaluated: {exc}"
            continue
        if active:
            result.run.append(key)
        else:
            result.skip[key] = (
                "No incoming path was taken ("
                + ", ".join(f"{e.source}: {statuses.get(e.source)}" for e in incoming)
                + ")"
            )
    return result
