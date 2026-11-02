from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from agenttape.domain.enums import VariableType
from agenttape.domain.prompts import (
    RenderMode,
    VariableDefinition,
    VariableValidation,
    render_template,
    template_tokens,
    validate_inputs,
)

V = VariableDefinition


def issues_for(defs: list[VariableDefinition], inputs: dict[str, object]) -> dict[str, str]:
    _values, issues = validate_inputs(defs, inputs)
    return {i.variable: i.message for i in issues}


def test_template_tokens_in_order() -> None:
    assert template_tokens("{{a}} {{ b }} {{a}} {{ secret:TOKEN }}") == (["a", "b"], ["TOKEN"])


def test_required_and_defaults() -> None:
    defs = [V(name="a"), V(name="b", default="x"), V(name="c", required=False)]
    values, issues = validate_inputs(defs, {})
    assert [i.variable for i in issues] == ["a"]
    assert values["b"] == "x" and values["c"] is None


def test_undeclared_input_is_reported() -> None:
    assert issues_for([V(name="a")], {"a": "1", "zzz": 2}) == {"zzz": "is not a declared variable"}


def test_string_rejects_newlines_but_multiline_accepts() -> None:
    assert "single line" in issues_for([V(name="a")], {"a": "x\ny"})["a"]
    assert issues_for([V(name="a", type=VariableType.MULTILINE)], {"a": "x\ny"}) == {}


def test_number_coercion_and_bounds() -> None:
    d = V(name="n", type=VariableType.NUMBER, validation=VariableValidation(minimum=1, maximum=10))
    values, issues = validate_inputs([d], {"n": "5"})
    assert values["n"] == 5 and not issues
    assert "must be >= 1" in issues_for([d], {"n": 0})["n"]
    assert "must be <= 10" in issues_for([d], {"n": 11})["n"]
    assert issues_for([d], {"n": "abc"})["n"] == "must be a number"
    assert issues_for([d], {"n": True})["n"] == "must be a number"


def test_boolean_coercion() -> None:
    d = V(name="b", type=VariableType.BOOLEAN)
    assert validate_inputs([d], {"b": "yes"})[0]["b"] is True
    assert validate_inputs([d], {"b": "0"})[0]["b"] is False
    assert issues_for([d], {"b": "maybe"})["b"] == "must be true or false"


def test_enum_requires_options_and_validates() -> None:
    with pytest.raises(PydanticValidationError):
        V(name="e", type=VariableType.ENUM)
    d = V(name="e", type=VariableType.ENUM, options=["a", "b"])
    assert "one of" in issues_for([d], {"e": "c"})["e"]


def test_pattern_and_length() -> None:
    d = V(name="s", validation=VariableValidation(pattern=r"[a-z]+", min_length=2, max_length=4))
    assert issues_for([d], {"s": "abc"}) == {}
    assert "pattern" in issues_for([d], {"s": "ABC"})["s"]
    assert "at least 2" in issues_for([d], {"s": "a"})["s"]


def test_invalid_pattern_rejected_at_definition() -> None:
    with pytest.raises(PydanticValidationError):
        V(name="s", validation=VariableValidation(pattern="("))


def test_path_rejects_traversal() -> None:
    d = V(name="p", type=VariableType.PATH)
    assert "'..'" in issues_for([d], {"p": "../etc/passwd"})["p"]
    assert issues_for([d], {"p": "src/app.py"}) == {}


def test_secret_variable_takes_a_name_not_a_value() -> None:
    d = V(name="tok", type=VariableType.SECRET)
    assert issues_for([d], {"tok": "GITHUB_TOKEN"}) == {}
    assert "never a value" in issues_for([d], {"tok": "ghp_actual value!"})["tok"]


def test_variable_names_must_be_identifiers() -> None:
    with pytest.raises(PydanticValidationError):
        V(name="not valid")


def test_render_redacts_secrets_and_resolves_only_on_request() -> None:
    defs = [V(name="thing"), V(name="tok", type=VariableType.SECRET)]
    body = "Fix {{ thing }} with {{ tok }} and {{ secret:OTHER }}"
    values, _ = validate_inputs(defs, {"thing": "bugs", "tok": "API_TOKEN"})
    stored = render_template(body, defs, values)
    assert stored.text == "Fix bugs with [secret:API_TOKEN] and [secret:OTHER]"
    assert stored.secret_refs == ["API_TOKEN", "OTHER"]
    resolved = render_template(
        body, defs, values, mode=RenderMode.RESOLVE, resolve_secret=lambda n: f"<{n.lower()}>"
    )
    assert resolved.text == "Fix bugs with <api_token> and <other>"


def test_resolve_mode_requires_a_resolver() -> None:
    with pytest.raises(ValueError):
        render_template("x", [], {}, mode=RenderMode.RESOLVE)


def test_marker_text_in_user_input_is_never_resolved() -> None:
    defs = [V(name="thing")]
    values, _ = validate_inputs(defs, {"thing": "[secret:AWS_SECRET] {{ secret:AWS_SECRET }}"})
    out = render_template(
        "Echo: {{ thing }}",
        defs,
        values,
        mode=RenderMode.RESOLVE,
        resolve_secret=lambda n: "LEAKED",
    )
    assert "LEAKED" not in out.text
    assert out.secret_refs == []


def test_undeclared_tokens_are_left_and_reported() -> None:
    out = render_template("Hi {{ who }}", [], {})
    assert out.text == "Hi {{ who }}" and out.undeclared == ["who"]


def test_booleans_render_lowercase() -> None:
    d = V(name="b", type=VariableType.BOOLEAN)
    assert render_template("{{ b }}", [d], {"b": True}).text == "true"
