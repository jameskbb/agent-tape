from __future__ import annotations

import random

import pytest

from agenttape.adapters.builtin.demo import DemoAdapter
from agenttape.application.events import cap_payload
from agenttape.domain.enums import EventType, FailureCategory, Severity
from agenttape.domain.retry import RetryPolicy
from agenttape.domain.usage import merge_usage


class TestRetry:
    def test_backoff_doubles_and_caps(self) -> None:
        p = RetryPolicy(max_attempts=10, backoff_base_seconds=2, backoff_max_seconds=10)
        assert [p.backoff_seconds(n) for n in range(1, 6)] == [2, 4, 8, 10, 10]

    def test_jitter_stays_in_bounds(self) -> None:
        p = RetryPolicy(
            max_attempts=5, backoff_base_seconds=10, backoff_max_seconds=100, jitter_ratio=0.2
        )
        rng = random.Random(1)
        for _ in range(100):
            assert 8.0 <= p.backoff_seconds(1, rng) <= 12.0

    def test_should_retry(self) -> None:
        p = RetryPolicy(max_attempts=3)
        assert p.should_retry(FailureCategory.ADAPTER_ERROR, 1)
        assert p.should_retry("interrupted", 2)
        assert not p.should_retry(FailureCategory.ADAPTER_ERROR, 3)
        assert not p.should_retry(FailureCategory.TIMEOUT, 1)
        assert not p.should_retry(FailureCategory.VALIDATION_FAILED, 1)
        assert not p.should_retry(None, 1)

    def test_from_config(self) -> None:
        p = RetryPolicy.from_config(
            {"retry_on": ["timeout"], "backoff_base_seconds": 1},
            default_max_attempts=2,
            default_base=5,
            default_max=60,
        )
        assert p.retry_on == frozenset({FailureCategory.TIMEOUT})
        assert p.backoff_base_seconds == 1 and p.max_attempts == 2


class TestUsage:
    def test_merge_sums_and_keeps_simulated(self) -> None:
        total = merge_usage(None, {"input_tokens": 10, "simulated": True})
        total = merge_usage(total, {"input_tokens": 5, "output_tokens": 2})
        assert total == {"input_tokens": 15, "output_tokens": 2, "simulated": True}

    def test_no_delta_keeps_unknown(self) -> None:
        assert merge_usage(None, None) is None


class TestPayloadCaps:
    def test_long_text_is_truncated_with_marker(self) -> None:
        out = cap_payload({"text": "x" * 100}, max_bytes=10_000, max_text=10)
        assert (
            out["text"] == "x" * 10 and out["text_truncated"] and out["text_original_bytes"] == 100
        )

    def test_oversized_payload_becomes_preview(self) -> None:
        out = cap_payload({"blob": ["y" * 50] * 100}, max_bytes=200, max_text=50)
        assert out["truncated"] is True and len(out["preview"]) <= 50


class TestDemoNormalization:
    adapter = DemoAdapter()

    def test_assistant(self) -> None:
        [d] = self.adapter.normalize({"type": "assistant", "text": "hello"})
        assert (
            d.type == EventType.AGENT_MESSAGE
            and d.payload["text"] == "hello"
            and d.source == "demo"
        )

    def test_reasoning_summary_is_explicit(self) -> None:
        [d] = self.adapter.normalize({"type": "reasoning_summary", "text": "plan"})
        assert d.type == EventType.REASONING_SUMMARY and d.payload["provided_by"] == "adapter"

    def test_tool_call_and_result(self) -> None:
        [call] = self.adapter.normalize(
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}}
        )
        assert call.type == EventType.TOOL_CALL and call.summary == "read_file(a.py)"
        [res] = self.adapter.normalize(
            {"type": "tool_result", "tool_use_id": "t1", "output": "boom", "is_error": True}
        )
        assert res.type == EventType.TOOL_RESULT and res.severity == Severity.ERROR
        assert res.payload["tool_use_id"] == "t1"

    def test_usage_is_always_marked_simulated(self) -> None:
        [d] = self.adapter.normalize({"type": "usage", "input_tokens": 3, "output_tokens": 4})
        assert d.type == EventType.USAGE
        assert d.usage == {"input_tokens": 3, "output_tokens": 4, "simulated": True}
        assert d.payload["simulated"] is True

    def test_file_events(self) -> None:
        [r] = self.adapter.normalize({"type": "file_read", "path": "x", "bytes": 1})
        [w] = self.adapter.normalize({"type": "file_write", "path": "x", "bytes": 2})
        assert (r.type, w.type) == (EventType.FILE_READ, EventType.FILE_WRITE)

    def test_unknown_record_is_a_warning(self) -> None:
        [d] = self.adapter.normalize({"type": "mystery"})
        assert d.type == EventType.WARNING and d.severity == Severity.WARNING

    @pytest.mark.parametrize(
        "scenario", ["success", "fail_then_retry", "needs_approval", "timeout", "fail"]
    )
    def test_scenarios_validate(self, scenario: str) -> None:
        cfg = {"scenario": scenario, "timeout_seconds": 5}
        assert [i for i in self.adapter.validate_profile(cfg) if i.severity == "error"] == []

    def test_bad_config(self) -> None:
        issues = self.adapter.validate_profile({"scenario": "nope", "step_delay_ms": -1})
        assert {i.field for i in issues} == {"scenario", "step_delay_ms"}
