# -*- coding: utf-8 -*-
"""Custom plan-to-hint generator for QwenPaw.

Differences from AgentScope's DefaultPlanToHint:

1. **No blocking confirmation** — after ``create_plan`` / ``revise_current_plan``,
   the agent may briefly present the plan but **must not** wait for an explicit
   user approval before calling ``update_subtask_state`` and executing tools.
2. Scoped ``no_plan`` hint — only injected when the runner has set the
   plan tool gate (explicit ``/plan`` entry), so normal chat is unaffected.
   That text states **only** ``create_plan`` until a plan exists (blocks
   ``doc_reviewer`` / writer / layout / retrieval shortcuts).
3. Compact plan text — completed subtask outcomes are dropped from the hint
   so per-iteration context cost stays constant.
4. Properly handles abandoned subtasks in all hint branches.
5. Optional **gov-document /plan pipeline** — when the runner sets
   ``_plan_gov_doc_pipeline`` on the notebook, ``create_plan`` is guided by
   intent-first hints (review / layout / write / retrieve / full draft).
   When ``_plan_skip_doc_retrieval`` is set, plans must not call
   ``doc_retrieval`` for material the user already supplied.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentscope.plan import Plan

logger = logging.getLogger(__name__)

try:
    from agentscope.plan._plan_notebook import DefaultPlanToHint

    _HAS_DEFAULT_HINT = True
except ImportError:
    _HAS_DEFAULT_HINT = False

_DESC_LIMIT = 80
_PLAN_DESC_LIMIT = 200


def set_plan_gate(  # pylint: disable=protected-access
    plan_notebook,
    enabled: bool = True,
) -> None:
    """Activate or deactivate the plan tool gate on *plan_notebook*."""
    if plan_notebook is not None:
        plan_notebook._plan_tool_gate = enabled


def check_plan_tool_gate(  # pylint: disable=protected-access
    plan_notebook,
    tool_name: str,
):
    """Return an error string if *tool_name* must be blocked, else ``None``.

    When a ``/plan`` request is pending (gate set by the runner), only
    ``create_plan`` may run.  The gate is cleared once a plan exists.
    """
    if plan_notebook is None:
        return None
    if plan_notebook.current_plan is not None:
        if getattr(plan_notebook, "_plan_tool_gate", False):
            plan_notebook._plan_tool_gate = False
        return None
    if not getattr(plan_notebook, "_plan_tool_gate", False):
        return None
    if tool_name == "create_plan":
        return None
    return (
        f"Tool '{tool_name}' is not available right now. "
        "You MUST call 'create_plan' first (the only allowed tool until a plan "
        "exists). Do not call doc_reviewer, gov_document_writer, "
        "gov_document_layout, doc_retrieval, or other tools before that. "
        "Decompose the user's request into a logical pipeline: each subtask "
        "needs a clear name, description, and measurable expected_outcome. "
        "Write plan text in the same language as the user's request."
    )


# ``Msg.metadata`` keys: hide phantom plan-gated tool rows when loading chat
# history (memory → ``agentscope_msg_to_message``), matching live SSE omission.
PLAN_GATE_UI_HIDE_TOOL_IDS_KEY = "qp_hide_plan_gate_tool_ids"
PLAN_GATE_UI_HIDE_DENIED_TOOL_RESULT_KEY = "qp_hide_plan_gate_denial_tool_msg"


def filter_plan_gate_blocked_tool_calls_for_stream(
    plan_notebook,
    content: list,
) -> list | None:
    """Remove ``tool_use`` blocks that :func:`check_plan_tool_gate` would block.

    When the runner has enabled the ``/plan`` gate and no plan exists yet, the
    model may still emit disallowed ``tool_use`` chunks; execution is skipped
    in the agent ``_acting`` path with an in-context tool result instead.
    Stripping those blocks before streaming ``print`` hides them from SSE and
    console consumers.

    Returns:
        ``None`` when *content* is unchanged (not a list, or gate inactive).
        A new ``list`` when at least one block was removed — possibly empty.
    """
    # pylint: disable=protected-access
    if plan_notebook is None or not isinstance(content, list):
        return None
    out: list = []
    changed = False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            if (
                check_plan_tool_gate(
                    plan_notebook,
                    str(block.get("name", "")),
                )
                is not None
            ):
                changed = True
                continue
        out.append(block)
    if not changed:
        return None
    return out


def should_skip_auto_continue(  # pylint: disable=protected-access
    plan_notebook,
) -> bool:
    """True when auto-continue must be suppressed for the current turn.

    After ``create_plan`` or ``revise_current_plan`` the notebook sets
    ``_plan_just_mutated`` so the agent can present the plan and wait for
    confirmation without auto-continue injecting an extra reasoning pass.
    """
    if plan_notebook is None:
        return False

    val = bool(getattr(plan_notebook, "_plan_just_mutated", False))
    if val:
        plan_notebook._plan_just_mutated = False
        return True

    if (
        bool(getattr(plan_notebook, "_plan_recently_finished", False))
        and not bool(getattr(plan_notebook, "_plan_tool_gate", False))
        and getattr(plan_notebook, "current_plan", None) is None
    ):
        return True

    return False


def _compact_plan_text(plan: "Plan") -> str:
    """将 *plan* 压缩为紧凑文本表示。

    已完成/已放弃的子任务仅显示状态与名称（不含结果）。
    进行中的子任务显示完整详情。待办子任务显示截断后的描述。
    """
    desc = plan.description
    if len(desc) > _PLAN_DESC_LIMIT:
        desc = desc[: _PLAN_DESC_LIMIT - 3] + "..."

    lines = [
        f"# {plan.name}",
        f"Description: {desc}",
        f"State: {plan.state}",
        "## Subtasks",
    ]
    for i, st in enumerate(plan.subtasks):
        if st.state in ("done", "abandoned"):
            lines.append(f"  {i}. [{st.state}] {st.name}")
        elif st.state == "in_progress":
            lines.append(f"  {i}. [in_progress] {st.name}")
            lines.append(f"     Desc: {st.description}")
            lines.append(f"     Expected: {st.expected_outcome}")
        else:
            d = st.description
            if len(d) > _DESC_LIMIT:
                d = d[: _DESC_LIMIT - 3] + "..."
            lines.append(f"  {i}. [todo] {st.name}")
            lines.append(f"     Desc: {d}")
    return "\n".join(lines)


def _count_states(plan: "Plan"):
    """Return (n_todo, n_ip, n_done, n_abn, ip_idx)."""
    n_todo = n_ip = n_done = n_abn = 0
    ip_idx = None
    for idx, st in enumerate(plan.subtasks):
        if st.state == "todo":
            n_todo += 1
        elif st.state == "in_progress":
            n_ip += 1
            ip_idx = idx
        elif st.state == "done":
            n_done += 1
        elif st.state == "abandoned":
            n_abn += 1
    return n_todo, n_ip, n_done, n_abn, ip_idx


_LANG_BLOCK = (
    "语言一致性：计划中展示给用户的文案（子任务名称、说明、结果、摘要等）"
    "须与用户近期消息所用语言一致。\n\n"
)

_GOV_PIPELINE_TOOL_NAMES = (
    "doc_retrieval",
    "gov_document_writer",
    "doc_reviewer",
    "gov_document_layout",
)

_GOV_TOOL_FORBIDDEN_WHEN_NARROW: dict[str, frozenset[str]] = {
    "doc_reviewer": frozenset(
        {"doc_retrieval", "gov_document_writer", "gov_document_layout"},
    ),
    "gov_document_layout": frozenset(
        {"doc_retrieval", "gov_document_writer", "doc_reviewer"},
    ),
    "gov_document_writer": frozenset(
        {"doc_retrieval", "gov_document_layout", "doc_reviewer"},
    ),
    "doc_retrieval": frozenset(
        {"gov_document_writer", "doc_reviewer", "gov_document_layout"},
    ),
}


def _infer_gov_tool_from_text(text: str) -> str | None:
    """Match a gov skill name from subtask title/description (order matters)."""
    if not text:
        return None
    t = text.lower()
    if "doc_reviewer" in t or "审核" in text or "校对" in text or "润色" in text:
        return "doc_reviewer"
    if (
        "gov_document_layout" in t
        or "版式" in text
        or "排版" in text
        or "模板" in text
        or "layout" in t
    ):
        return "gov_document_layout"
    if (
        "gov_document_writer" in t
        or "gov-document-writer" in t
        or "写作" in text
        or "起草" in text
        or "生成公文" in text
        or "拟一份" in text
    ):
        return "gov_document_writer"
    if "doc_retrieval" in t or "检索" in text or "查找资料" in text or "找资料" in text:
        return "doc_retrieval"
    return None


def _subtask_gov_tool_hint(subtask: Any) -> str | None:
    """Infer intended gov tool from one plan subtask's text fields."""
    parts = [
        str(getattr(subtask, "name", "") or ""),
        str(getattr(subtask, "description", "") or ""),
        str(getattr(subtask, "expected_outcome", "") or ""),
    ]
    return _infer_gov_tool_from_text(" ".join(parts))


def _tool_specific_reminder_lines(tool_name: str) -> str:
    if tool_name == "doc_reviewer":
        return (
            "**必须**将待审正文写入 ``content`` / ``data``（含 "
            "``normalizedResult.document`` 或上一步 writer 的完整 JSON）；"
            "**禁止**用主题臆造 ``file_path``。\n"
        )
    if tool_name == "gov_document_layout":
        return (
            "**仅**传 ``template_title``（公文标题）取版式推荐；"
            "**不要**传大段 ``content`` / ``file_path``。\n"
        )
    if tool_name == "gov_document_writer":
        return (
            "响应仅为 JSON（``normalizedResult.document``）；"
            "**勿**含 ``savePath`` 或调用 write_file 落盘。\n"
        )
    if tool_name == "doc_retrieval":
        return "检索完成后 **勿**自动起草/审核/排版；若用户仅要检索，``finish_plan`` 即可。\n"
    return ""


def _gov_pipeline_tool_reminder(
    subtask_idx: int,
    plan: "Plan",
    plan_notebook,
) -> str:
    """Remind the model which gov tool matches the **current subtask**, not its index."""
    skip = (
        plan_notebook is not None
        and bool(getattr(plan_notebook, "_plan_skip_doc_retrieval", False))
    )
    if subtask_idx < 0 or subtask_idx >= len(plan.subtasks):
        return ""
    st = plan.subtasks[subtask_idx]
    tool = _subtask_gov_tool_hint(st)
    if tool == "doc_retrieval" and skip:
        return (
            "\n**公文 /plan**：用户已提供参考材料，**不要**调用 `doc_retrieval`。"
            "若本子任务仅为消化材料，调用 `finish_subtask` 简述即可；"
            "若需起草/审核/版式，改用对应工具。\n"
        )
    if tool:
        forbidden = _GOV_TOOL_FORBIDDEN_WHEN_NARROW.get(tool, frozenset())
        forbid_line = ""
        if forbidden:
            forbid_line = (
                "**禁止**调用计划外工具："
                + "、".join(f"`{x}`" for x in sorted(forbidden))
                + "。\n"
            )
        return (
            f"\n**公文 /plan · 本子任务**：应调用 `{tool}`（名称须完全一致）。\n"
            + _tool_specific_reminder_lines(tool)
            + forbid_line
        )
    return (
        "\n**公文 /plan**：仅调用与本子任务名称/说明一致的工具；"
        "**禁止**添加计划外步骤（如无检索诉求却 `doc_retrieval`）。\n"
    )


_GOV_DOC_PIPELINE_NO_PLAN = (
    "当前尚无活动计划。\n"
    + _LANG_BLOCK
    + "你处于 **/plan 公文助手**（自动执行；无需向用户确认）。\n"
    "**先判断用户真实意图（由窄到宽）**；命中靠前类别则 **只** 建该场景所需步数，"
    "**禁止**为多做事追加无关工具或子任务。\n\n"
    "━━ **窄场景（常见为 1 步）** ━━\n"
    "1) **仅审核 / 校对 / 润色**（用户已贴正文，或消息含「如下信息/如下内容/原文」等）：\n"
    "   · `create_plan` → **1 个子任务** → 工具 **仅** `doc_reviewer`\n"
    "   · **禁止** `doc_retrieval`、`gov_document_writer`、`gov_document_layout`\n"
    "2) **仅模板 / 版式 / 排版推荐**：\n"
    "   · **1 步** → **仅** `gov_document_layout`（**只**传 ``template_title``）\n"
    "   · **禁止** 检索、起草、审核\n"
    "3) **仅检索 / 找资料**：\n"
    "   · **1 步** → **仅** `doc_retrieval`；返回 items 后 **即** `finish_plan`\n"
    "   · **禁止** 自动起草、审核、版式或长篇总结\n"
    "4) **仅起草 / 生成公文**（明确要写文但 **未** 要求审核或版式）：\n"
    "   · **1 步** → **仅** `gov_document_writer`\n"
    "   · **禁止** 无必要的检索、审核、版式\n\n"
    "━━ **宽场景：完整正式文稿（建议 4 步，非强制）** ━━\n"
    "仅当用户 **明确** 要「写一篇完整公文/通知/函/报告」且 **不属于** 以上窄场景时，"
    "可建 **4 个子任务**，顺序建议：\n"
    "① `doc_retrieval` → ② `gov_document_writer` → ③ `doc_reviewer` → "
    "④ `gov_document_layout`（仅 ``template_title``）。\n"
    "每步 **只** 调用与本步目标对应的工具；用户诉求已满足时 **不要** 继续后续步。\n\n"
    "**工具要点**：\n"
    "· `gov_document_writer`：JSON 含 ``normalizedResult.document``，勿 ``savePath``/write_file\n"
    "· `doc_reviewer`：正文入 ``content``/``data``，勿臆造 ``file_path``\n"
    "· `gov_document_layout`：仅 ``template_title``，勿传大段正文\n\n"
    "`create_plan` 成功后 **不要** 请用户确认。"
    "**立即** `update_subtask_state`，subtask_idx=0、state='in_progress'，"
    "开始执行（最好同一回合内完成）。\n"
)

_GOV_DOC_PIPELINE_NO_PLAN_SKIP_RETRIEVAL = (
    "当前尚无活动计划。\n"
    + _LANG_BLOCK
    + "你处于 **/plan 公文助手**（自动执行；无需向用户确认）。"
    "**用户已提供参考材料**（附件与/或正文线索），**不要**为取材料而调用 "
    "`doc_retrieval`。\n"
    "**先判断真实意图（由窄到宽）**；窄场景规则同常规 /plan（仅审核→1 步 "
    "`doc_reviewer`；仅版式→1 步 layout；仅起草→1 步 writer）。\n"
    "仅当用户 **明确** 要完整正式文稿时，可建多步计划（**跳过检索**）：\n"
    "① 消化用户材料（**不**调用 `doc_retrieval`，`finish_subtask` 简述）→ "
    "② `gov_document_writer` → ③ `doc_reviewer` → ④ `gov_document_layout`。\n"
    "步数按实际需要，**勿**机械凑满四步；诉求已满足即 `finish_plan`。\n\n"
    "**工具要点**同常规 /plan（writer 勿落盘；reviewer 勿臆造 path；layout 仅 "
    "``template_title``）。\n"
    "`create_plan` 成功后 **立即** `update_subtask_state`，subtask_idx=0、"
    "state='in_progress'，且 **不要** 使用 `doc_retrieval`。\n"
)

_GOV_DOC_PIPELINE_AT_START = (
    "当前计划：\n```\n{plan}\n```\n"
    + _LANG_BLOCK
    + "**公文 /plan**：按 **已创建计划** 的步数与说明执行；**禁止**添加计划外工具或子任务。\n"
    "执行已预批准，**不要**在开始前提请用户确认。\n"
    "**立即** `update_subtask_state`，subtask_idx=0、state='in_progress'，"
    "随后调用与本步目标一致的工具。\n"
    "若用户最新消息取消任务 → `finish_plan`，state='abandoned'。\n"
    "若用户要求整套重来 → 先 `finish_plan`（abandoned），再 `create_plan`。\n"
    "用户诉求已在当前步完成时 → `finish_subtask` 后 **直接** `finish_plan`，"
    "**勿**自动进入无关后续步。\n"
    "**关键**：本回合至少包含一次工具调用。\n"
)

_GOV_DOC_PIPELINE_AT_START_SKIP_RETRIEVAL_SUFFIX = (
    "**用户材料已就绪**：除非计划子任务 **明确** 要求检索，否则 **禁止** "
    "`doc_retrieval`；以用户消息/附件为输入。\n"
)

# Strong ordering under explicit ``/plan`` when gov-doc auto-pipeline is off:
# models often jump to ``doc_reviewer`` / writer tools; gate rejects them after
# the fact — front-load the constraint in the system hint.
_PLAN_TOOL_GATE_GENERIC_NO_PLAN = (
    "当前尚无活动计划。\n"
    + _LANG_BLOCK
    + "**/plan 工具门禁**：在 `create_plan` **成功创建活动计划之前**，"
    "你 **只能** 调用工具 `create_plan`。"
    "**禁止**调用 `doc_reviewer`、`gov_document_writer`、`gov_document_layout`、"
    "`doc_retrieval` 或其它业务工具；即使你认为自己可以直接完成任务，"
    "也必须先建计划，否则只会收到拒绝并重耗轮次。\n\n"
    "请先调用 `create_plan`，将用户诉求拆成带子任务的结构化计划："
    "每个子任务需名称、说明、expected_outcome，并按依赖关系排序。\n"
    "**本回合的首要动作**：发起一次合规且完整的 `create_plan`。\n"
)

if _HAS_DEFAULT_HINT:

    class SimplePlanToHint(DefaultPlanToHint):
        """Simplified plan hint generator (auto-execute; no user-confirm gate)."""

        at_the_beginning: str = (
            "当前计划：\n```\n{plan}\n```\n"
            + _LANG_BLOCK
            + "请对照用户 **最新** 一条消息：\n"
            "- 若为确认（开始、好的、可以、是、确认、执行、继续等，**任意语言**），"
            "**立即**调用 `update_subtask_state`，subtask_idx=0、state='in_progress'，"
            "然后开始执行。\n"
            "- 若用户以任何方式要求修改或更换计划，先调用 `finish_plan`，"
            "state='abandoned'，再调用 `create_plan` 按用户意见重做完整计划；"
            "此处 **不要** 使用 `revise_current_plan`。\n"
            "- 若用户取消，调用 `finish_plan`，state='abandoned'。\n"
            "\n"
            "**执行策略**：不要以「等待用户确认」为由停顿。若尚无子任务处于 "
            "in_progress，**立即**调用 `update_subtask_state`（subtask_idx=0、"
            "state='in_progress'）并开始执行；可用一两句话概括计划要点，"
            "但本回合须含至少一次工具调用。\n"
        )

        when_a_subtask_in_progress: str = (
            "当前计划：\n```\n{plan}\n```\n"
            + _LANG_BLOCK
            + "子任务 {subtask_idx}（「{subtask_name}」）状态为 in_progress。\n"
            "执行本子任务：\n"
            "1. 每一轮：简短文字 + 至少一次工具调用。\n"
            "2. 目标达成时，调用 `finish_subtask`，结果表述精炼。\n"
            "3. 若多次尝试仍卡住，也调用 `finish_subtask` 给出阶段性结果。\n"
            "**关键**：勿仅回复文字而无工具调用 — 否则 ReAct 循环会终止。\n"
        )

        when_no_subtask_in_progress: str = (
            "当前计划：\n```\n{plan}\n```\n"
            + _LANG_BLOCK
            + "前 {index} 个子任务已完成，且当前无任何子任务处于 in_progress。\n"
            "调用 `update_subtask_state`，将下一个待办子任务标为 in_progress，"
            "并继续用工具执行该子任务。\n"
            "**关键**：须包含工具调用 — 仅文字回复会结束本次运行。\n"
        )

        at_the_end: str = (
            "当前计划：\n```\n{plan}\n```\n"
            + _LANG_BLOCK
            + "所有子任务均已完成。调用 `finish_plan`，state='done'，并附精炼结果摘要。\n"
            "**关键**：必须发起一次 `finish_plan` 工具调用。\n"
        )

        no_plan: str | None = _PLAN_TOOL_GATE_GENERIC_NO_PLAN

        at_the_beginning_after_mutation: str = (
            "当前计划：\n```\n{plan}\n```\n"
            + _LANG_BLOCK
            + "本计划 **刚被创建或修订**。可简要说明计划要点；**不要**再次调用 "
            "`revise_current_plan`（用户尚未针对此次更新提出修改前，勿重复修订）。\n"
            "**立即**调用 `update_subtask_state`，subtask_idx=0、state='in_progress'，"
            "并开始执行第一个子任务（最好本回合内完成）。**不要**将「等待用户确认」"
            "作为执行前置条件；本回合须含至少一次工具调用。\n"
        )

        recently_finished_guard: str | None = (
            "当前没有活动计划。\n"
            + _LANG_BLOCK
            + "上一份计划已结束或已取消。**不要**继续执行旧计划的子任务。\n"
            "请对照用户 **最新** 消息：\n"
            "- 若用户要求修改、更换或重做计划，调用 `create_plan`，"
            "按其意见新建计划。\n"
            "- 否则直接答复用户最新消息，**不要**再创建计划。\n"
        )

        def _hint_no_plan(self, nb) -> str | None:
            """Select hint when there is no active plan."""
            if nb is not None and getattr(nb, "_plan_tool_gate", False):
                if getattr(nb, "_plan_gov_doc_pipeline", False):
                    if getattr(nb, "_plan_skip_doc_retrieval", False):
                        return _GOV_DOC_PIPELINE_NO_PLAN_SKIP_RETRIEVAL
                    return _GOV_DOC_PIPELINE_NO_PLAN
                return self.no_plan
            if nb is not None and getattr(
                nb,
                "_plan_recently_finished",
                False,
            ):
                return self.recently_finished_guard
            return None

        def _hint_with_plan(self, plan: "Plan", nb) -> str | None:
            """Select hint when a plan is active."""
            _, n_ip, n_done, n_abn, ip_idx = _count_states(plan)
            just_mutated = nb is not None and getattr(
                nb,
                "_plan_just_mutated",
                False,
            )
            gov = nb is not None and getattr(nb, "_plan_gov_doc_pipeline", False)

            if n_ip == 0 and n_done == 0 and n_abn == 0:
                if gov:
                    out = _GOV_DOC_PIPELINE_AT_START.format(
                        plan=plan.to_markdown(),
                    )
                    if getattr(nb, "_plan_skip_doc_retrieval", False):
                        out += _GOV_DOC_PIPELINE_AT_START_SKIP_RETRIEVAL_SUFFIX
                    return out
                tmpl = (
                    self.at_the_beginning_after_mutation
                    if just_mutated
                    else self.at_the_beginning
                )
                return tmpl.format(plan=plan.to_markdown())

            if n_ip > 0 and ip_idx is not None:
                body = self.when_a_subtask_in_progress.format(
                    plan=_compact_plan_text(plan),
                    subtask_idx=ip_idx,
                    subtask_name=plan.subtasks[ip_idx].name,
                    subtask=plan.subtasks[ip_idx].to_markdown(detailed=True),
                )
                if gov:
                    return body + _gov_pipeline_tool_reminder(ip_idx, plan, nb)
                return body

            if n_done + n_abn == len(plan.subtasks):
                return self.at_the_end.format(plan=_compact_plan_text(plan))

            if n_ip == 0 and (n_done + n_abn) > 0:
                return self.when_no_subtask_in_progress.format(
                    plan=_compact_plan_text(plan),
                    index=n_done + n_abn,
                )

            return None

        def __call__(self, plan: "Plan | None") -> str | None:
            nb = getattr(self, "_bound_notebook", None)
            if nb is not None:
                nb = nb() if callable(nb) else nb

            hint = (
                self._hint_no_plan(nb)
                if plan is None
                else self._hint_with_plan(plan, nb)
            )

            if hint:
                return f"{self.hint_prefix}{hint}{self.hint_suffix}"
            return hint

        def bind_notebook(self, plan_notebook) -> None:
            """Store a weak reference to the notebook for gate checks."""
            import weakref

            if plan_notebook is None:
                self._bound_notebook = None
            else:
                self._bound_notebook = weakref.ref(plan_notebook)

else:
    SimplePlanToHint = None  # type: ignore[misc,assignment]
