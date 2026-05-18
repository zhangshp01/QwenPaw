# -*- coding: utf-8 -*-
"""Chat history hides plan-gated phantom tools (metadata contract)."""

from __future__ import annotations

from agentscope.message import Msg, ToolResultBlock

from qwenpaw.app.runner.utils import agentscope_msg_to_message
from qwenpaw.plan.hints import (
    PLAN_GATE_UI_HIDE_DENIED_TOOL_RESULT_KEY,
    PLAN_GATE_UI_HIDE_TOOL_IDS_KEY,
)


def test_history_skips_denied_gate_tool_use_and_matching_result():
    call_id = "call_gate_abc"
    asst = Msg(
        "assistant",
        [
            {
                "type": "tool_use",
                "id": call_id,
                "name": "doc_retrieval",
                "input": {"query": "x"},
            },
        ],
        "assistant",
        metadata={
            PLAN_GATE_UI_HIDE_TOOL_IDS_KEY: [call_id],
        },
    )
    sysm = Msg(
        "system",
        [
            ToolResultBlock(
                type="tool_result",
                id=call_id,
                name="doc_retrieval",
                output=[{"type": "text", "text": "gate"}],
            ),
        ],
        "system",
        metadata={PLAN_GATE_UI_HIDE_DENIED_TOOL_RESULT_KEY: True},
    )
    converted = agentscope_msg_to_message([asst, sysm])
    assert not converted


def test_history_keeps_unmarked_tool_calls():
    call_id = "call_ok"
    asst = Msg(
        "assistant",
        [
            {
                "type": "tool_use",
                "id": call_id,
                "name": "create_plan",
                "input": {"name": "p"},
            },
        ],
        "assistant",
        metadata={},
    )
    out = agentscope_msg_to_message(asst)
    assert len(out) == 1
    m0 = out[0]
    mtype = getattr(m0, "type", None)
    if hasattr(mtype, "value"):
        mtype = mtype.value
    assert mtype == "plugin_call"
