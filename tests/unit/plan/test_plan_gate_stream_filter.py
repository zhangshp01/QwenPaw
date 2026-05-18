# -*- coding: utf-8 -*-
"""Tests for SSE suppression of plan-gated tool_use blocks."""

from __future__ import annotations

from types import SimpleNamespace

from qwenpaw.plan.hints import filter_plan_gate_blocked_tool_calls_for_stream


def test_filter_returns_none_when_no_notebook_or_not_list():
    blocks = [{"type": "tool_use", "name": "doc_retrieval"}]
    assert filter_plan_gate_blocked_tool_calls_for_stream(None, blocks) is None
    assert filter_plan_gate_blocked_tool_calls_for_stream(
        SimpleNamespace(current_plan=None, _plan_tool_gate=True),
        "not-list",
    ) is None


def test_filter_keeps_when_gate_off_or_plan_exists():
    nb_off = SimpleNamespace(current_plan=None, _plan_tool_gate=False)
    content = [{"type": "tool_use", "name": "doc_retrieval"}]
    assert filter_plan_gate_blocked_tool_calls_for_stream(nb_off, content) is None

    plan_obj = object()
    nb_busy = SimpleNamespace(current_plan=plan_obj, _plan_tool_gate=False)
    assert filter_plan_gate_blocked_tool_calls_for_stream(nb_busy, content) is None


def test_filter_strips_only_blocked_tools_when_gate_on():
    nb = SimpleNamespace(current_plan=None, _plan_tool_gate=True)
    inp = [{"type": "tool_use", "name": "doc_retrieval"}]
    out = filter_plan_gate_blocked_tool_calls_for_stream(nb, inp)
    assert out == []
    assert inp[0]["name"] == "doc_retrieval"


def test_filter_keeps_create_plan_when_gate_on():
    nb = SimpleNamespace(current_plan=None, _plan_tool_gate=True)
    inp = [{"type": "tool_use", "name": "create_plan"}]
    assert filter_plan_gate_blocked_tool_calls_for_stream(nb, inp) is None
