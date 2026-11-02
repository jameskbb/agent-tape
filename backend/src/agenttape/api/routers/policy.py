"""The V1 safety policy: what needs approval, what is never allowed, and a command check."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agenttape.domain.policy import (
    APPROVAL_REQUIRED_BY_DEFAULT,
    DEFAULT_COMMAND_ALLOWLIST,
    DEFAULT_RISK,
    HARD_DISALLOWED,
    check_command,
)

router = APIRouter(tags=["policy"])


class PolicyOut(BaseModel):
    approval_required: list[dict[str, str]]
    hard_disallowed: list[dict[str, str]]
    default_command_allowlist: list[str]


class CommandCheck(BaseModel):
    argv: list[str] = Field(min_length=1, max_length=200)
    elevated: bool = False
    allowlist: list[str] | None = None


@router.get("/policy", response_model=PolicyOut, summary="The V1 safety policy")
def get_policy() -> PolicyOut:
    return PolicyOut(
        approval_required=[
            {"type": t.value, "risk_level": DEFAULT_RISK[t].value}
            for t in sorted(APPROVAL_REQUIRED_BY_DEFAULT, key=lambda t: t.value)
        ],
        hard_disallowed=[{"action": a.value, "reason": r} for a, r in HARD_DISALLOWED.items()],
        default_command_allowlist=list(DEFAULT_COMMAND_ALLOWLIST),
    )


@router.post(
    "/policy/check-command",
    response_model=dict[str, Any],
    summary="Would this argument array be allowed, need approval, or be refused?",
)
def check(body: CommandCheck) -> dict[str, Any]:
    return check_command(body.argv, allowlist=body.allowlist, elevated=body.elevated).as_dict()
