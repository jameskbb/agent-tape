"""DAG validation, dependency scheduling, safe templates (incl. injection), and conditions."""

from __future__ import annotations

from typing import Any

import pytest

from agenttape.domain.workflows import (
    ConditionError,
    TemplateError,
    WorkflowDefinition,
    failure_is_fatal,
    find_cycle,
    parse_condition,
    plan_ready,
    render_string,
    render_value,
    template_references,
    topological_order,
    validate_definition,
)


def node(key: str, type_: str = "transform", **extra: Any) -> dict[str, Any]:
    config = extra.pop("config", {"values": {"k": key}} if type_ == "transform" else {})
    return {"key": key, "type": type_, "config": config, **extra}


def edge(a: str, b: str, **cond: Any) -> dict[str, Any]:
    return {"from": a, "to": b, **({"condition": cond} if cond else {})}


def messages(defn: dict[str, Any]) -> list[str]:
    return [i.message for i in validate_definition(defn)]


# --------------------------------------------------------------------------- validation


def test_valid_definition_has_no_issues() -> None:
    defn = {"nodes": [node("a"), node("b")], "edges": [edge("a", "b")]}
    assert validate_definition(defn) == []


def test_cycle_is_rejected_with_the_cycle_named() -> None:
    defn = {
        "nodes": [node("a"), node("b"), node("c")],
        "edges": [edge("a", "b"), edge("b", "c"), edge("c", "a")],
    }
    msgs = messages(defn)
    assert any("cycle" in m and "a -> b -> c -> a" in m for m in msgs), msgs


def test_structural_errors() -> None:
    defn = {
        "nodes": [node("a"), node("a"), node("9bad")],
        "edges": [edge("a", "missing"), edge("a", "a")],
    }
    msgs = " | ".join(messages(defn))
    assert "Duplicate node key 'a'" in msgs
    assert "must start with a letter" in msgs
    assert "Unknown node 'missing'" in msgs
    assert "cannot depend on itself" in msgs


def test_empty_workflow_and_unknown_type() -> None:
    assert "at least one node" in " ".join(messages({"nodes": [], "edges": []}))
    assert messages({"nodes": [{"key": "a", "type": "teleport"}]})


@pytest.mark.parametrize(
    ("type_", "config", "expected"),
    [
        ("agent_run", {}, "agent profile"),
        ("command", {"argv": []}, "argv"),
        ("command", {"argv": ["{{ inputs.exe }}"]}, "executable"),
        ("transform", {}, "values"),
        ("evaluation_gate", {}, "evaluators"),
        ("approval", {}, "title"),
        ("condition", {"expression": "a =="}, "ended unexpectedly"),
    ],
)
def test_node_config_validation(type_: str, config: dict[str, Any], expected: str) -> None:
    defn = {"nodes": [{"key": "n", "type": type_, "config": config}]}
    assert any(expected in m for m in messages(defn)), messages(defn)


def test_templates_must_reference_upstream_nodes_and_declared_inputs() -> None:
    defn = {
        "inputs": [{"name": "goal"}],
        "nodes": [
            node("a"),
            node("b", config={"values": {"x": "{{ nodes.c.outputs.y }}"}}),
            node("c", config={"values": {"x": "{{ inputs.nope }} {{ secrets.x }}"}}),
        ],
        "edges": [edge("a", "b")],
    }
    msgs = " | ".join(messages(defn))
    assert "not upstream of 'b'" in msgs
    assert "undeclared workflow input" in msgs
    assert "unknown root 'secrets'" in msgs


def _finding_review_workflow(
    *,
    target: str | None = "agent",
    agent_profile_id: str | None = "p",
    extra_nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    review_config: dict[str, Any] = {}
    if target is not None:
        review_config["target"] = target
    if agent_profile_id is not None:
        review_config["agent_profile_id"] = agent_profile_id
    nodes = [
        node("agent", "agent_run", config={"agent_profile_id": "p", "prompt": "x"}),
        node("review", "finding_review", config=review_config),
        *(extra_nodes or []),
    ]
    return {"nodes": nodes, "edges": edges if edges is not None else [edge("agent", "review")]}


def test_finding_review_target_must_be_a_real_upstream_agent_run_node() -> None:
    assert validate_definition(_finding_review_workflow()) == []

    msgs = messages(_finding_review_workflow(target=None))
    assert any("need a 'target'" in m for m in msgs), msgs

    msgs = messages(_finding_review_workflow(agent_profile_id=None))
    assert any("need an agent profile" in m for m in msgs), msgs

    msgs = messages(_finding_review_workflow(target="ghost"))
    assert any("Finding review target 'ghost' is not a node" in m for m in msgs), msgs

    # Target exists but has no edge path to the review node: not upstream.
    msgs = messages(_finding_review_workflow(target="other", extra_nodes=[node("other")]))
    assert any("not upstream" in m for m in msgs), msgs

    # Target exists but is downstream of the review node: not upstream.
    msgs = messages(
        _finding_review_workflow(
            target="down",
            extra_nodes=[node("down")],
            edges=[edge("agent", "review"), edge("review", "down")],
        )
    )
    assert any("not upstream" in m for m in msgs), msgs

    # Target names an upstream node that is not an agent run node.
    msgs = messages(
        {
            "nodes": [
                node("prep"),
                node("agent", "agent_run", config={"agent_profile_id": "p", "prompt": "x"}),
                node(
                    "review", "finding_review", config={"target": "prep", "agent_profile_id": "p"}
                ),
            ],
            "edges": [edge("prep", "agent"), edge("agent", "review")],
        }
    )
    assert any("must be an agent run node" in m for m in msgs), msgs

    # Target naming the review node itself is rejected: it is not upstream of itself.
    msgs = messages(_finding_review_workflow(target="review"))
    assert any("not upstream" in m for m in msgs), msgs


def test_branch_edges_only_from_condition_nodes() -> None:
    defn = {"nodes": [node("a"), node("b")], "edges": [edge("a", "b", branch=True)]}
    assert any("conditional-branch" in m for m in messages(defn))


def test_topological_order_is_deterministic_and_detects_cycles() -> None:
    assert topological_order(["a", "b", "c", "d"], [("a", "c"), ("b", "c"), ("c", "d")]) == [
        "a",
        "b",
        "c",
        "d",
    ]
    assert topological_order(["a", "b"], [("a", "b"), ("b", "a")]) is None
    assert find_cycle(["a", "b"], [("a", "b"), ("b", "a")]) in (["a", "b", "a"], ["b", "a", "b"])


# --------------------------------------------------------------------------- scheduling


def diamond() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "nodes": [node("a"), node("b"), node("c"), node("d")],
            "edges": [edge("a", "b"), edge("a", "c"), edge("b", "d"), edge("c", "d")],
        }
    )


def test_parallel_branches_become_ready_together() -> None:
    d = diamond()
    statuses = {"a": "succeeded", "b": "queued", "c": "queued", "d": "queued"}
    ready = plan_ready(d, statuses, {}, {})
    assert ready.run == ["b", "c"]
    assert ready.skip == {}


def test_join_waits_for_all_upstream_nodes() -> None:
    d = diamond()
    statuses = {"a": "succeeded", "b": "succeeded", "c": "running", "d": "queued"}
    assert plan_ready(d, statuses, {}, {}).run == []
    statuses["c"] = "succeeded"
    assert plan_ready(d, statuses, {}, {}).run == ["d"]


def test_roots_start_immediately_and_skips_cascade() -> None:
    d = diamond()
    assert plan_ready(d, dict.fromkeys("abcd", "queued"), {}, {}).run == ["a"]
    statuses = {"a": "failed", "b": "queued", "c": "queued", "d": "queued"}
    ready = plan_ready(d, statuses, {}, {})
    assert set(ready.skip) == {"b", "c"}
    statuses.update({"b": "skipped", "c": "skipped"})
    assert set(plan_ready(d, statuses, {}, {}).skip) == {"d"}


def test_condition_branches_and_rejoin() -> None:
    d = WorkflowDefinition.model_validate(
        {
            "nodes": [
                node("check", "condition", config={"expression": "true"}),
                node("yes"),
                node("no"),
                node("join"),
            ],
            "edges": [
                edge("check", "yes", branch=True),
                edge("check", "no", branch=False),
                edge("yes", "join"),
                edge("no", "join"),
            ],
        }
    )
    statuses = {"check": "succeeded", "yes": "queued", "no": "queued", "join": "queued"}
    ready = plan_ready(d, statuses, {"check": {"result": True}}, {})
    assert ready.run == ["yes"] and set(ready.skip) == {"no"}
    statuses.update({"yes": "succeeded", "no": "skipped"})
    assert plan_ready(d, statuses, {}, {}).run == ["join"]


def test_failure_edges_and_when_expressions() -> None:
    d = WorkflowDefinition.model_validate(
        {
            "nodes": [node("a"), node("recover"), node("big")],
            "edges": [
                edge("a", "recover", on="failure"),
                edge("a", "big", when="nodes.a.outputs.size > 10"),
            ],
        }
    )
    ctx = {"nodes": {"a": {"status": "failed", "outputs": {"size": 3}}}}
    ready = plan_ready(d, {"a": "failed", "recover": "queued", "big": "queued"}, {}, ctx)
    assert ready.run == ["recover"] and "big" in ready.skip
    ctx = {"nodes": {"a": {"status": "succeeded", "outputs": {"size": 30}}}}
    ready = plan_ready(d, {"a": "succeeded", "recover": "queued", "big": "queued"}, {}, ctx)
    assert ready.run == ["big"] and "recover" in ready.skip
    # A node with a failure path is not fatal when it fails.
    assert not failure_is_fatal(d, d.node("a"))
    assert failure_is_fatal(d, d.node("recover"))


def test_optional_and_continue_on_failure_are_not_fatal() -> None:
    d = WorkflowDefinition.model_validate(
        {"nodes": [node("a", required=False), node("b", continue_on_failure=True), node("c")]}
    )
    assert not failure_is_fatal(d, d.node("a"))
    assert not failure_is_fatal(d, d.node("b"))
    assert failure_is_fatal(d, d.node("c"))


# --------------------------------------------------------------------------- templates

CTX: dict[str, Any] = {
    "inputs": {"goal": "tidy", "count": 3},
    "nodes": {
        "prep": {"status": "succeeded", "outputs": {"files": ["a.py", "b.py"], "meta": {"n": 1}}},
    },
}


def test_whole_template_keeps_its_type() -> None:
    assert render_string("{{ nodes.prep.outputs.files }}", CTX) == ["a.py", "b.py"]
    assert render_string("{{inputs.count}}", CTX) == 3


def test_mixed_template_renders_text_and_filters() -> None:
    assert render_string("Fix {{ inputs.goal }} in {{ nodes.prep.outputs.files | join }}", CTX) == (
        "Fix tidy in a.py, b.py"
    )
    assert render_string("n={{ nodes.prep.outputs.files | length }}", CTX) == "n=2"
    assert render_string("{{ nodes.prep.outputs.meta | json }}", CTX) == '{"n": 1}'
    assert render_string("first={{ nodes.prep.outputs.files.0 }}", CTX) == "first=a.py"


def test_render_value_recurses() -> None:
    value = {"a": ["{{ inputs.goal }}", 1], "b": {"c": "{{ inputs.count }}"}}
    assert render_value(value, CTX) == {"a": ["tidy", 1], "b": {"c": 3}}


@pytest.mark.parametrize(
    "template",
    [
        "{{ inputs.missing }}",
        "{{ __class__.__mro__ }}",
        "{{ inputs.goal.upper() }}",
        "{{ inputs['goal'] }}",
        "{{ inputs.goal | eval }}",
        "{{ }}",
    ],
)
def test_unsafe_or_missing_templates_are_errors(template: str) -> None:
    with pytest.raises(TemplateError):
        render_string(template, CTX)


def test_values_are_never_re_expanded() -> None:
    """Template injection: an output that looks like a template is inserted literally."""
    ctx = {"inputs": {"evil": "{{ inputs.secret }}", "secret": "s3cret"}}
    assert render_string("x {{ inputs.evil }}", ctx) == "x {{ inputs.secret }}"
    assert render_string("{{ inputs.evil }}", ctx) == "{{ inputs.secret }}"


def test_lookup_only_indexes_dicts_and_lists() -> None:
    class Obj:
        attr = "leak"

    with pytest.raises(TemplateError):
        render_string("{{ inputs.obj.attr }}", {"inputs": {"obj": Obj()}})


def test_template_references() -> None:
    refs = template_references({"a": "{{ nodes.x.outputs }} and {{ inputs.y | json }}", "b": [1]})
    assert refs == ["nodes.x.outputs", "inputs.y"]


# --------------------------------------------------------------------------- conditions


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("nodes.prep.status == 'succeeded'", True),
        ('inputs.count > 2 and inputs.goal == "tidy"', True),
        ("inputs.count >= 5 or not (inputs.goal != 'tidy')", True),
        ("'a.py' in nodes.prep.outputs.files", True),
        ("nodes.prep.outputs.files contains 'c.py'", False),
        ("inputs.missing == null", True),
        ("inputs.missing > 1", False),
        ("inputs.count < 'x'", False),
        ("true and false", False),
        ("-1 < 0", True),
    ],
)
def test_conditions(expr: str, expected: bool) -> None:
    assert parse_condition(expr).evaluate(CTX) is expected


@pytest.mark.parametrize(
    "expr",
    ["", "a ==", "(a == 1", "a == 1 b", "__import__('os').system('x')", "a; b", "a == 1)"],
)
def test_invalid_conditions_are_rejected(expr: str) -> None:
    with pytest.raises(ConditionError):
        parse_condition(expr)
