"""ReAct 循环：Thought → Action → Observation，直到收敛或触达预算。

三重保险防止「转圈烧钱」
----------------------
1. **步数预算**（``FINONTO_AGENT_MAX_STEPS``）；
2. **重复检测**：同一工具 + 同一参数的第二次调用直接判为无进展；
3. **无动作退出**：模型没给出可解析的 Action 也不再重试。

Agent 写本体的唯一通道是 ``propose_type`` → ``ChangeProposal``，
所以哪怕模型胡说，也需要人点一次「确认」才会落库。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from ...db import session_scope
from ...models import AgentRun, AgentStep, OntologySpace, utcnow
from ...providers.base import Message
from ...providers.registry import get_components
from ..ontology import packs
from . import memory as memory_service
from . import skills
from .tools import ToolContext, parse_arguments, registry

THOUGHT_RE = re.compile(r"Thought:\s*(.*?)(?=\n(?:Action:|Final Answer:)|$)", re.S)
ACTION_RE = re.compile(r"Action:\s*([A-Za-z0-9_.\-]+)")
INPUT_RE = re.compile(r"Action Input:\s*(.+?)(?=\n(?:Observation:|Thought:)|\Z)", re.S)
FINAL_RE = re.compile(r"Final Answer:\s*(.*)$", re.S)


@dataclass(slots=True)
class Step:
    ordinal: int
    thought: str
    action: str
    action_input: dict
    observation: dict
    elapsed_ms: int = 0
    tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "ordinal": self.ordinal,
            "thought": self.thought,
            "action": self.action,
            "action_input": self.action_input,
            "observation": self.observation,
            "elapsed_ms": self.elapsed_ms,
            "tokens": self.tokens,
        }


@dataclass(slots=True)
class RunResult:
    run_id: int
    status: str
    conclusion: str
    steps: list[dict] = field(default_factory=list)
    proposal_ids: list[int] = field(default_factory=list)
    tokens: int = 0
    skill: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "conclusion": self.conclusion,
            "steps": self.steps,
            "proposal_ids": self.proposal_ids,
            "tokens": self.tokens,
            "skill": self.skill,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
def build_prompt(
    space: OntologySpace,
    goal: str,
    skill,
    tools_catalog: list[dict],
    steps: list[Step],
) -> list[Message]:
    domain_context = packs.prompt_context_for(space.domain)
    tool_lines = [
        f"{tool['name']} — {tool['description']}（风险：{tool['risk']}）" for tool in tools_catalog
    ]
    done_lines: list[str] = []
    for step in steps:
        done_lines.append(
            f"{step.ordinal}. Thought: {step.thought}\n"
            f"   Action: {step.action}\n"
            f"   Action Input: {step.action_input}\n"
            f"   Observation: {_short(step.observation)}"
        )
    recent = _short(steps[-1].observation) if steps else "（无）"

    user = "\n\n".join(
        [
            "# 任务：react",
            f"【目标】\n{goal}",
            f"【领域】\n{domain_context}",
            f"【可用工具】\n" + "\n".join(tool_lines),
            f"【已完成步骤】\n" + ("\n".join(done_lines) if done_lines else "（无）"),
            f"【最近观察】\n{recent}",
            f"【输出契约】\n{skill.output_contract}",
            "下一步只输出一个动作，格式严格如下（三行）：\n"
            "Thought: <你的推理>\n"
            "Action: <工具名>\n"
            "Action Input: <JSON 对象>\n"
            "如果已可收尾，输出：\nThought: <总结>\nFinal Answer: <结论>",
        ]
    )
    return [
        Message(role="system", content=skill.system_prompt),
        Message(role="user", content=user),
    ]


def _short(observation: dict, limit: int = 420) -> str:
    import json

    text = json.dumps(observation, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def parse_response(text: str) -> dict[str, Any]:
    final = FINAL_RE.search(text or "")
    if final:
        return {"kind": "final", "conclusion": final.group(1).strip()}
    action = ACTION_RE.search(text or "")
    if not action:
        return {"kind": "invalid", "reason": "未找到 Action 或 Final Answer"}
    thought_match = THOUGHT_RE.search(text or "")
    input_match = INPUT_RE.search(text or "")
    return {
        "kind": "action",
        "thought": (thought_match.group(1).strip() if thought_match else ""),
        "action": action.group(1).strip(),
        "arguments": parse_arguments(input_match.group(1) if input_match else ""),
    }


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def run(
    session,
    space_id: int,
    goal: str,
    *,
    skill_key: str = "doc-extraction",
    components=None,
    max_steps: int | None = None,
    token_budget: int | None = None,
) -> RunResult:
    components = components or get_components()
    skill = skills.get_skill(skill_key)
    space = session.get(OntologySpace, space_id)
    if space is None:
        raise ValueError(f"空间不存在：{space_id}")

    tool_registry = registry()
    allowed = skill.allowed_tools
    tools_catalog = tool_registry.catalog(allowed)

    agent_run = AgentRun(space_id=space_id, skill=skill.key, goal=goal, status="running")
    session.add(agent_run)
    session.commit()
    run_id = agent_run.id
    context = ToolContext(session=session, space_id=space_id, components=components, run_id=run_id)

    limit = int(max_steps or skill.max_steps)
    budget = int(token_budget or 1800)
    steps: list[Step] = []
    proposal_ids: list[int] = []
    total_tokens = 0
    seen_calls: set[str] = set()
    status = "running"
    conclusion = ""
    error = ""

    for ordinal in range(1, limit + 1):
        messages = build_prompt(space, goal, skill, tools_catalog, steps)
        started = time.perf_counter()
        raw = components.llm.chat(messages, max_tokens=512)
        elapsed = int((time.perf_counter() - started) * 1000)
        tokens = len(raw) // 2 + 1
        total_tokens += tokens

        parsed = parse_response(raw)
        if parsed["kind"] == "final":
            conclusion = parsed["conclusion"]
            steps.append(
                Step(ordinal=ordinal, thought=conclusion, action="finish", action_input={},
                     observation={"ok": True, "conclusion": conclusion}, elapsed_ms=elapsed, tokens=tokens)
            )
            status = "succeeded"
            break

        if parsed["kind"] == "invalid":
            error = parsed.get("reason", "无法解析模型输出")
            status = "failed"
            steps.append(
                Step(ordinal=ordinal, thought=raw[:200], action="", action_input={},
                     observation={"ok": False, "error": error}, elapsed_ms=elapsed, tokens=tokens)
            )
            break

        action = parsed["action"]
        arguments = parsed["arguments"]
        thought = parsed.get("thought", "")

        if action not in allowed:
            observation = {"ok": False, "error": f"工具 {action} 不在技能「{skill.display}」的白名单内"}
            steps.append(Step(ordinal=ordinal, thought=thought, action=action,
                              action_input=arguments, observation=observation,
                              elapsed_ms=elapsed, tokens=tokens))
            continue

        signature = f"{action}:{sorted(arguments.items())}"
        if signature in seen_calls:
            error = f"重复调用 {action} 且参数相同，判定为无进展，提前终止"
            status = "failed"
            steps.append(Step(ordinal=ordinal, thought=thought, action=action,
                              action_input=arguments, observation={"ok": False, "error": error},
                              elapsed_ms=elapsed, tokens=tokens))
            break

        observation = tool_registry.invoke(action, context, arguments)
        seen_calls.add(signature)
        if observation.get("proposal_id"):
            proposal_ids.append(int(observation["proposal_id"]))
        steps.append(Step(ordinal=ordinal, thought=thought, action=action,
                          action_input=arguments, observation=observation,
                          elapsed_ms=elapsed, tokens=tokens))

        if total_tokens >= budget:
            error = f"token 预算 {budget} 已用尽"
            status = "budget_exceeded"
            break

    if status == "running":
        status = "budget_exceeded" if total_tokens >= budget else "succeeded"
        conclusion = conclusion or "达到最大步数，未给出显式结论"

    for step in steps:
        session.add(
            AgentStep(
                run_id=run_id,
                ordinal=step.ordinal,
                thought=step.thought,
                action=step.action,
                action_input=step.action_input,
                observation=step.observation,
                token_count=step.tokens,
                elapsed_ms=step.elapsed_ms,
            )
        )
    agent_run.status = status
    agent_run.step_count = len(steps)
    agent_run.token_count = total_tokens
    agent_run.conclusion = conclusion
    agent_run.proposal_ids = proposal_ids
    agent_run.error = error
    agent_run.finished_at = utcnow()
    session.commit()

    if status == "succeeded":
        memory_service.consolidate(
            session,
            space_id,
            f"{skill.display}：{conclusion}",
            keywords=",".join(item["label"] for item in _proposal_labels(session, proposal_ids)),
        )
        session.commit()

    return RunResult(
        run_id=run_id,
        status=status,
        conclusion=conclusion,
        steps=[step.as_dict() for step in steps],
        proposal_ids=proposal_ids,
        tokens=total_tokens,
        skill=skill.key,
        error=error,
    )


def _proposal_labels(session, proposal_ids: list[int]) -> list[dict]:
    from ...models import ChangeProposal

    if not proposal_ids:
        return []
    rows = session.query(ChangeProposal).filter(ChangeProposal.id.in_(proposal_ids)).all()
    return [{"label": (row.payload or {}).get("label", "")} for row in rows]


# --------------------------------------------------------------------------- #
# 流式
# --------------------------------------------------------------------------- #
def run_stream(
    space_id: int,
    goal: str,
    *,
    skill_key: str = "doc-extraction",
    max_steps: int | None = None,
) -> Iterator[dict]:
    """逐事件推送，供 SSE 使用（自己开 session，避免请求结束后 session 关闭）。"""
    components = get_components()
    skill = skills.get_skill(skill_key)
    tool_registry = registry()
    allowed = skill.allowed_tools
    tools_catalog = tool_registry.catalog(allowed)

    with session_scope() as session:
        space = session.get(OntologySpace, space_id)
        if space is None:
            yield {"type": "error", "message": f"空间不存在：{space_id}"}
            return
        agent_run = AgentRun(space_id=space_id, skill=skill.key, goal=goal, status="running")
        session.add(agent_run)
        session.flush()
        run_id = agent_run.id

    yield {"type": "start", "data": {"run_id": run_id, "skill": skill.key, "goal": goal,
                                     "max_steps": int(max_steps or skill.max_steps),
                                     "tools": [tool["name"] for tool in tools_catalog]}}

    steps: list[Step] = []
    proposal_ids: list[int] = []
    total_tokens = 0
    seen_calls: set[str] = set()
    limit = int(max_steps or skill.max_steps)
    status = "running"
    conclusion = ""
    error = ""

    with session_scope() as session:
        space = session.get(OntologySpace, space_id)
        context = ToolContext(session=session, space_id=space_id, components=components, run_id=run_id)
        for ordinal in range(1, limit + 1):
            messages = build_prompt(space, goal, skill, tools_catalog, steps)
            started = time.perf_counter()
            raw = components.llm.chat(messages, max_tokens=512)
            elapsed = int((time.perf_counter() - started) * 1000)
            tokens = len(raw) // 2 + 1
            total_tokens += tokens
            parsed = parse_response(raw)

            if parsed["kind"] == "final":
                conclusion = parsed["conclusion"]
                status = "succeeded"
                step = Step(ordinal=ordinal, thought=conclusion, action="finish",
                            action_input={}, observation={"ok": True}, elapsed_ms=elapsed, tokens=tokens)
                steps.append(step)
                yield {"type": "step", "data": step.as_dict()}
                break

            if parsed["kind"] == "invalid":
                error = parsed.get("reason", "无法解析模型输出")
                status = "failed"
                yield {"type": "error", "message": error}
                break

            action, arguments, thought = parsed["action"], parsed["arguments"], parsed.get("thought", "")
            if action not in allowed:
                observation = {"ok": False, "error": f"工具 {action} 不在白名单内"}
            else:
                signature = f"{action}:{sorted(arguments.items())}"
                if signature in seen_calls:
                    error = f"重复调用 {action}，判定无进展"
                    status = "failed"
                    yield {"type": "error", "message": error}
                    break
                observation = tool_registry.invoke(action, context, arguments)
                seen_calls.add(signature)
                if observation.get("proposal_id"):
                    proposal_ids.append(int(observation["proposal_id"]))

            step = Step(ordinal=ordinal, thought=thought, action=action, action_input=arguments,
                        observation=observation, elapsed_ms=elapsed, tokens=tokens)
            steps.append(step)
            yield {"type": "step", "data": step.as_dict()}

            if total_tokens >= 1800:
                status = "budget_exceeded"
                error = "token 预算已用尽"
                break

        if status == "running":
            status = "succeeded"
            conclusion = conclusion or "达到最大步数，未给出显式结论"

        for step in steps:
            session.add(
                AgentStep(
                    run_id=run_id, ordinal=step.ordinal, thought=step.thought, action=step.action,
                    action_input=step.action_input, observation=step.observation,
                    token_count=step.tokens, elapsed_ms=step.elapsed_ms,
                )
            )
        agent_run = session.get(AgentRun, run_id)
        if agent_run is not None:
            agent_run.status = status
            agent_run.step_count = len(steps)
            agent_run.token_count = total_tokens
            agent_run.conclusion = conclusion
            agent_run.proposal_ids = proposal_ids
            agent_run.error = error
            agent_run.finished_at = utcnow()
        session.commit()

    yield {
        "type": "done",
        "data": {
            "run_id": run_id,
            "status": status,
            "conclusion": conclusion,
            "proposal_ids": proposal_ids,
            "tokens": total_tokens,
            "error": error,
            "skill": skill.key,
        },
    }
