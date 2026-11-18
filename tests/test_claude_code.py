"""Adaptador do Claude Code (precisa de pydantic por causa do config: roda no PC)."""

from __future__ import annotations

import json

from cortes.adapters.assistant.claude_code import build_args, parse_result
from cortes.config import AssistantSettings


def test_args_never_contain_the_prompt_and_limit_tools() -> None:
    args = build_args("claude", AssistantSettings())
    assert args[:2] == ["claude", "-p"]
    assert "--output-format" in args and "json" in args
    assert "Bash(uv run cortes rerender:*)" in args and "Bash(ffmpeg:*)" in args
    assert "WebFetch" in args and "WebSearch" in args
    no_ffmpeg = build_args("claude", AssistantSettings(allow_ffmpeg=False))
    assert "Bash(ffmpeg:*)" not in no_ffmpeg


def test_parse_success() -> None:
    out = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Corrigi a legenda.",
            "session_id": "0f6c1b2a-1111-2222-3333-444455556666",
            "total_cost_usd": 0.12,
            "num_turns": 7,
        }
    )
    result = parse_result(out, 0, "")
    assert result.ok and result.reply == "Corrigi a legenda."
    assert result.session_id == "0f6c1b2a-1111-2222-3333-444455556666"
    assert result.turns == 7 and result.cost_usd == 0.12


def test_parse_error_and_garbage() -> None:
    out = json.dumps({"subtype": "error_max_turns", "is_error": True, "session_id": "abcdef12"})
    result = parse_result(out, 1, "")
    assert not result.ok and result.error and result.session_id == "abcdef12"
    garbage = parse_result("não é json", 1, "Invalid API key")
    assert not garbage.ok and "Invalid API key" in (garbage.error or "")
    evil = parse_result(json.dumps({"result": "x", "session_id": "a & del *"}), 0, "")
    assert evil.session_id is None  # nunca vai para um comando de terminal
