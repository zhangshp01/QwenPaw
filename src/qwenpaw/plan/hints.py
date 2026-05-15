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
   ``_plan_gov_doc_pipeline`` on the notebook, ``create_plan`` is guided to
   a fixed four-tool sequence. When ``_plan_skip_doc_retrieval`` is set,
   subtask 0 must not use ``doc_retrieval``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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


def _gov_pipeline_tool_reminder(subtask_idx: int, plan_notebook) -> str:
    skip = (
        plan_notebook is not None
        and bool(getattr(plan_notebook, "_plan_skip_doc_retrieval", False))
    )
    if subtask_idx == 0 and skip:
        return (
            "\n**公文流水线**：本回合已含用户提供的参考资料（附件与/或消息内明示来源）。"
            "**不要**调用 `doc_retrieval`。起草时仅将用户消息与附件视作事实输入；"
            "调用 `finish_subtask` 给出简要结果后继续。\n"
        )
    if 0 <= subtask_idx < len(_GOV_PIPELINE_TOOL_NAMES):
        name = _GOV_PIPELINE_TOOL_NAMES[subtask_idx]
        return (
            f"\n**Gov-document pipeline**: this subtask MUST invoke tool "
            f"`{name}` (exact name).\n"
        )
    return ""


_GOV_DOC_PIPELINE_NO_PLAN = (
    "当前尚无活动计划。\n"
    + _LANG_BLOCK
    + "你处于 **/plan 流水线**（自动执行；无需向用户确认）。\n"
    "**请先判断**：用户是否在求「**新写一篇正式公文**」（需从检索/材料到起草、审核、版式"
    "的完整链路）。\n"
    "• **不需要写公文**（如仅审核/校对/点评已贴正文、摘录、一般问答、或其它与「新拟公文」"
    "无关的诉求）：调用 `create_plan` 按实际任务拆分子任务；执行时按依赖**按需**选用工具，"
    "**勿**机械套用下述四工具流水线。\n"
    "• **需要写公文**：调用 `create_plan`，**恰好四个**子任务，**严格**按下述顺序；"
    "每个子任务阶段**只**调用对应工具，完成后再进入下一步：\n"
    "1) **检索**：工具 `doc_retrieval` — 针对用户主题/问句检索；若无结果项或"
    "规范化内容为空，则在无材料情况下继续流水线。\n"
    "2) **起草**：工具 `gov_document_writer` — 综合检索结果（如 normalizedResult）、"
    "用户说明与相关用户记忆文件；响应仅为 JSON（``normalizedResult.document``），"
    "勿含 ``savePath`` 或工作区落盘文件。\n"
    "3) **审核**：工具 `doc_reviewer` — 传入上一步 ``gov_document_writer`` 的正文"
    "（``normalizedResult.document`` 或同结构工具 JSON / 块数组）；"
    "4) **版式**：每轮流水线对 `gov_document_layout` **只调用一次**，"
    "且 **仅**传 ``template_title``（公文标题）。**不要**传 ``content``、"
    "``file_path``，也勿再次调用该工具：该步 **仅** 获取版式推荐，**不** 写入 ``.docx``，"
    "并返回非空的 ``savePath`` 字符串作为 **待定** 输出路径（文件名规则同此前；"
    "文件可能尚未存在）。\n"
    "`create_plan` 成功后 **不要** 请用户确认。"
    "**立即**调用 `update_subtask_state`，subtask_idx=0、state='in_progress'，"
    "开始步骤 1（最好在同一回合内完成）。\n"
)

_GOV_DOC_PIPELINE_NO_PLAN_SKIP_RETRIEVAL = (
    "当前尚无活动计划。\n"
    + _LANG_BLOCK
    + "你处于 **公文 /plan 流水线**（自动执行；无需向用户确认）。"
    "**已检测到用户提供的材料**（附件与/或正文中的明确来源线索）："
    "**不要**再为取材料而执行知识库检索。\n"
    "**请先判断**是否要「**新写一篇正式公文**」。**不需要**时：`create_plan` 按实际拆步，"
    "执行时**按需**选用工具，勿套用下述四工具流水线。**需要**时：`create_plan` **恰好四个**"
    "子任务，**严格**按下述顺序，每步只调用对应工具：\n"
    "1) **用户材料（跳过检索）**：**不要**调用 `doc_retrieval`。"
    "仅以用户消息与附件为参考输入；若仍无可用内容，则在外部材料为空的情况下继续。"
    "调用 `finish_subtask` 给出简短结果。\n"
    "2) **起草**：工具 `gov_document_writer` — 结合用户提供的材料、起草说明，"
    "必要时结合用户记忆文件；响应仅为 JSON（``normalizedResult.document``），"
    "勿含 ``savePath`` 或工作区落盘文件。\n"
    "3) **审核**：工具 `doc_reviewer` — 传入上一步 ``gov_document_writer`` 的正文"
    "（``normalizedResult.document`` 或同结构工具 JSON / 块数组）；"
    "仅当缺失时才用 ``file_path`` / ``path``。\n"
    "4) **版式**：每轮流水线对 `gov_document_layout` **只调用一次**，"
    "且 **仅**传 ``template_title``（公文标题）。**不要**传 ``content``、"
    "``file_path``，也勿再次调用该工具：该步 **仅** 获取版式推荐，**不** 写入 ``.docx``，"
    "并返回非空的 ``savePath`` 字符串作为 **待定** 输出路径（文件名规则同此前；"
    "文件可能尚未存在）。\n"
    "`create_plan` 成功后 **不要** 请用户确认。"
    "**立即**调用 `update_subtask_state`，subtask_idx=0、state='in_progress'，"
    "开始步骤 1，且 **不要** 使用 `doc_retrieval`（最好在同一回合内完成）。\n"
)

_GOV_DOC_PIPELINE_AT_START = (
    "当前计划：\n```\n{plan}\n```\n"
    + _LANG_BLOCK
    + "**公文 /plan 流水线**：执行已预批准，**不要**在开始前提请用户确认、修改或等待。\n"
    "**立即**调用 `update_subtask_state`，subtask_idx=0、state='in_progress'，"
    "随后为该子任务调用工具执行。\n"
    "若用户最新消息取消任务，调用 `finish_plan`，state='abandoned'。\n"
    "若用户要求整套重来，先调用 `finish_plan`（abandoned），再调用 `create_plan`；"
    "在未经确认流程前，勿用 `revise_current_plan` 做全盘重做。\n"
    "**关键**：本回合至少包含一次工具调用，勿仅回复文字。\n"
)

_GOV_DOC_PIPELINE_AT_START_SKIP_RETRIEVAL_SUFFIX = (
    "**跳过检索模式**：子任务 0 **禁止**调用 `doc_retrieval`。"
    "仅使用用户消息与附件；按需要完成子任务 0（计划工具 / `finish_subtask`），"
    "再继续后续步骤。\n"
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

            if n_ip == 0 and n_done == 0 and n_abn == 0:
                if nb is not None and getattr(nb, "_plan_gov_doc_pipeline", False):
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
                if nb is not None and getattr(nb, "_plan_gov_doc_pipeline", False):
                    return body + _gov_pipeline_tool_reminder(ip_idx, nb)
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
