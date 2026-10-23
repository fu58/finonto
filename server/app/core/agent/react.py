"""ReAct 推理引擎：Thought → Action → Observation 循环。

职责：
- 组装单步决策的 prompt（技能系统提示 + 可用工具 + 推理轨迹 + 长期记忆）；
- 解析并修复模型输出（JSON 围栏、散文、工具名拼写、参数字符串化）；
- 执行工具、把结果压缩成 observation，控制步预算与重复动作；
- 持久化 AgentRun/AgentStep，并在收尾时把经验沉淀进长期记忆。

引擎只通过工具面影响本体，而工具面的写操作只产生 ChangeProposal。
"""

from __future__ import annotations

import difflib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import AgentRun, AgentStep, ChangeProposal
from ...models.domain import utcnow
from ...providers.base import ChatMessage
from ...providers.registry import registry
from ..ontology.packs import DomainPack, pack_for_space
from ..text import truncate
from .mcp_client import mcp_manager
from .memory import MemoryService
from .skills import SKILLS, Skill, get_skill
from .tools import ToolContext, register_mcp_tools, registry_tools

FINISH = "finish"


def _json_object(raw: str) -> dict[str, Any] | None:
    """取出第一个花括号平衡的 JSON 对象，容忍代码围栏与前后散文。"""
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escaped = False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_str:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_str = False
                continue
            if char == '"':
                in_str = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    snippet = raw[start : index + 1]
                    try:
                        parsed = json.loads(snippet)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = raw.find("{", start + 1)
    return None


def _normalize_action(name: str) -> str:
    cleaned = name.strip().strip("`'\"() ")
    cleaned = cleaned.replace(" ", ".").replace("_", ".") if "." not in cleaned else cleaned
    return cleaned.lower()


def _closest(action: str, available: set[str]) -> str | None:
    """把模型写的工具名对齐到真实工具名（后缀匹配 + 最近邻）。"""
    norm = _normalize_action(action)
    if norm in available:
        return norm
    exact = [name for name in available if name.lower().split(".")[-1] == norm.split(".")[-1]]
    if len(exact) == 1:
        return exact[0]
    matches = difflib.get_close_matches(norm, sorted(available), n=1, cutoff=0.6)
    return matches[0] if matches else None


def parse_step(raw: str, available: set[str]) -> dict[str, Any]:
    """解析一步决策；无法解析 JSON 时把正文当作收敛答案，无法识别工具时报错给下一轮。"""
    data = _json_object(raw)
    if data is None:
        text = (raw or "").strip()
        return {
            "thought": truncate(text, 200),
            "action": FINISH,
            "action_input": {"answer": truncate(text, 800) or "（模型未给出可用输出）"},
            "repaired": "unparseable_to_finish",
        }
    action = str(data.get("action") or data.get("tool") or "").strip()
    args = data.get("action_input") or data.get("input") or data.get("arguments") or data.get("params") or {}
    if isinstance(args, str):
        args = _json_object(args) or {"query": args}
    if not isinstance(args, dict):
        args = {}
    repaired = ""
    if not action:
        action, repaired, args = FINISH, "missing_action_to_finish", {"answer": truncate(str(data.get("thought", "")), 600)}
    elif action.lower() != FINISH:
        if action not in available:
            match = _closest(action, available)
            if match:
                repaired = f"{action}→{match}"
                action = match
            else:
                repaired = "unknown_tool"
    return {
        "thought": str(data.get("thought") or data.get("reasoning") or ""),
        "action": action if action.lower() != FINISH else FINISH,
        "action_input": args,
        "repaired": repaired,
    }


def _observation(result: dict[str, Any], budget: int) -> str:
    """把工具返回值压缩成适合放进推理轨迹的观察。"""
    if not result.get("ok", False):
        return f"失败：{result.get('error', '未知错误')}"
    parts: list[str] = []
    if result.get("summary"):
        parts.append(str(result["summary"]))
    for key in ("intent", "status", "proposal_id", "label", "count", "total", "empty"):
        if key in result and not isinstance(result[key], (list, dict)):
            parts.append(f"{key}={result[key]}")
    for row in (result.get("results") or result.get("types") or result.get("chunks") or [])[:6]:
        if isinstance(row, dict):
            text = row.get("text") or row.get("definition") or row.get("label") or row.get("name") or ""
            label = row.get("doc") or row.get("label") or row.get("locator") or row.get("ref") or ""
            parts.append(f"{label}: {truncate(str(text), 320)}".strip(": "))
    for row in (result.get("duplicates") or [])[:4]:
        parts.append(f"已有相似类型 {json.dumps(row, ensure_ascii=False)[:160]}")
    if result.get("detail"):
        parts.append(json.dumps(result["detail"], ensure_ascii=False)[:600])
    if result.get("report"):
        parts.append(json.dumps(result["report"], ensure_ascii=False)[:500])
    if result.get("stdout"):
        parts.append(str(result["stdout"])[:800])
    if result.get("reasoning_hint"):
        parts.append(str(result["reasoning_hint"]))
    if not parts:
        parts.append(json.dumps({k: v for k, v in result.items() if k != "ok"}, ensure_ascii=False)[:600])
    return truncate(" | ".join(p for p in parts if p), budget)


@dataclass
class ReActConfig:
    max_steps: int | None = None
    skill: str | None = None
    observation_budget: int = field(default_factory=lambda: settings.agent_step_token_budget)
    use_memory: bool = True
    include_mcp: bool = True


class ReActEngine:
    def __init__(self, session: Session, space_id: int, config: ReActConfig | None = None) -> None:
        self.session = session
        self.space_id = space_id
        self.config = config or ReActConfig()
        self.skill: Skill | None = get_skill(self.config.skill)
        self.memory = MemoryService(session)
        self.pack: DomainPack = pack_for_space(session, space_id)

    # ------------------------------------------------------------- 可用工具
    def available_tools(self) -> list[str]:
        if self.config.include_mcp:
            register_mcp_tools()
        allowed: list[str] | None = None
        if self.skill:
            allowed = list(self.skill.allowed_tools)
        if self.config.include_mcp:
            mcp_names = [tool.qualified for tool in mcp_manager.tools()]
            allowed = (allowed or [t.name for t in registry_tools.tools()]) + mcp_names
        return [tool.name for tool in registry_tools.tools(allowed)]

    def build_messages(self, goal: str, scratchpad: list[dict[str, Any]], available: set[str], memory_block: str, hint: str = "") -> list[ChatMessage]:
        system = self.skill.system_prompt if self.skill else (
            f"你是「{self.pack.label}」领域的本体建模 Agent。每一步先思考，再选择一个工具并给出 JSON 参数；"
            "拿到观察结果后继续推理，信息足够时调用 finish 收敛。新增对象类型必须走 "
            "ontology.propose_type 生成待人工确认的提案，不要试图直接写入本体。"
        )
        tools_desc = "\n".join(
            tool.render() for tool in registry_tools.tools(available) if tool.name in available
        )
        trajectory = "\n".join(
            f"Thought: {step.get('thought') or '（无）'}\n"
            f"Action: {step['action']}\n"
            f"ActionInput: {json.dumps(step.get('action_input', {}), ensure_ascii=False)}\n"
            f"Observation: {step.get('observation', '')}"
            for step in scratchpad[-8:]
        )
        blocks = [
            "[[TASK:react_step]]",
            "## 目标",
            goal,
            "",
            "## 领域上下文",
            self.pack.prompt_context(),
            "",
            "## 输出要求",
            '只输出一个 JSON 对象：{"thought": "...", "action": "<工具名>", "action_input": {...}}；'
            f'收敛时输出 {{"thought":"...","action":"finish","action_input":{{"answer":"结论"}}}}。',
        ]
        if memory_block:
            blocks += ["", "## 长期记忆", memory_block]
        blocks += ["", "## 可用工具", tools_desc or "- （无可用工具，请直接 finish）"]
        if trajectory:
            blocks += ["", "## 已执行的推理轨迹", trajectory]
        else:
            blocks += ["", "## 已执行的推理轨迹", "（尚未执行任何步骤）"]
        if hint:
            blocks += ["", "## 提示", hint]
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content="\n".join(blocks))]

    # ---------------------------------------------------------------- 主循环
    async def run_stream(self, goal: str, *, run_id: int | None = None, session_id: int | None = None) -> AsyncIterator[dict[str, Any]]:
        goal = (goal or "").strip()
        if not goal:
            yield {"type": "error", "message": "目标不能为空"}
            return

        names = self.available_tools()
        available = set(names)
        llm = registry.llm()
        budget = min(
            self.config.max_steps or settings.agent_max_steps,
            self.skill.max_steps if self.skill else settings.agent_max_steps,
        )

        run = self.session.get(AgentRun, run_id) if run_id else None
        if run is None:
            run = AgentRun(
                session_id=session_id,
                space_id=self.space_id,
                goal=goal,
                mode="react",
                status="running",
                summary=f"技能：{self.skill.display if self.skill else '自由推理'}｜工具：{len(names)}",
            )
            self.session.add(run)
            self.session.flush()
        else:
            run.status = "running"
            run.error = ""
        self.session.commit()
        run_id = run.id

        ctx = ToolContext(session=self.session, space_id=self.space_id, run_id=run_id, skill=self.config.skill)
        memory_block = self.memory.context_block(goal) if self.config.use_memory else ""
        scratchpad: list[dict[str, Any]] = []
        proposal_ids: list[int] = []
        steps_used = 0
        tokens = 0
        seen_actions: dict[str, int] = {}
        nudged = False
        final_answer = ""
        status = "running"

        yield {
            "type": "run_start",
            "run_id": run_id,
            "goal": goal,
            "skill": self.skill.name if self.skill else None,
            "max_steps": budget,
            "tools": sorted(names),
            "memory_hits": [line.strip("- ") for line in memory_block.splitlines() if line.strip()],
        }

        started = time.perf_counter()
        try:
            for ordinal in range(1, budget + 1):
                messages = self.build_messages(goal, scratchpad, available, memory_block)
                raw, usage = await llm.complete(messages, task="react_step", json_mode=True, temperature=0.0)
                tokens += usage.total
                step = parse_step(raw, available)

                if step["action"] == FINISH and not scratchpad and not nudged:
                    # 零证据收敛：给一次提示，避免 Agent 空转即宣称完成
                    nudged = True
                    yield {
                        "type": "hint",
                        "run_id": run_id,
                        "message": "尚无观察结果，Agent 被要求先获取证据再收敛。",
                    }
                    messages = self.build_messages(
                        goal,
                        scratchpad,
                        available,
                        memory_block,
                        hint="还没有任何观察结果。请先调用检索/列表类工具获取证据，再考虑 finish。",
                    )
                    raw, usage = await llm.complete(messages, task="react_step", json_mode=True, temperature=0.0)
                    tokens += usage.total
                    step = parse_step(raw, available)

                action_input = step["action_input"]
                event = {
                    "type": "step",
                    "run_id": run_id,
                    "ordinal": ordinal,
                    "thought": step["thought"],
                    "action": step["action"],
                    "action_input": action_input,
                    "repaired": step.get("repaired", ""),
                    "remaining": budget - ordinal,
                }
                yield event

                if step["action"] == FINISH:
                    final_answer = str(action_input.get("answer") or action_input.get("output") or step["thought"] or "")
                    status = "done"
                    steps_used += 1
                    self._persist_step(run_id, ordinal, step["thought"], FINISH, action_input, final_answer, "done", started)
                    break

                if step.get("repaired") == "unknown_tool":
                    observation = f"工具「{step['action']}」不在可用列表中。可选：{', '.join(sorted(available))}"
                    scratchpad.append({"thought": step["thought"], "action": step["action"], "action_input": action_input, "observation": observation})
                    steps_used += 1
                    self._persist_step(run_id, ordinal, step["thought"], step["action"], action_input, observation, "invalid", started)
                    yield {"type": "observation", "run_id": run_id, "ordinal": ordinal, "ok": False, "observation": observation}
                    continue

                key = f"{step['action']}:{json.dumps(action_input, ensure_ascii=False, sort_keys=True)[:200]}"
                seen_actions[key] = seen_actions.get(key, 0) + 1
                if seen_actions[key] > 2:
                    status = "done"
                    final_answer = "重复调用同一工具仍未获得新信息，停止推理。" + (
                        f" 已生成 {len(proposal_ids)} 个待确认提案。" if proposal_ids else ""
                    )
                    steps_used += 1
                    self._persist_step(run_id, ordinal, step["thought"], step["action"], action_input, "重复动作，触发循环保护", "loop_guard", started)
                    break

                step_started = time.perf_counter()
                result = await registry_tools.execute(ctx, step["action"], action_input)
                observation = _observation(result, self.config.observation_budget)
                ok = bool(result.get("ok", False))
                scratchpad.append(
                    {"thought": step["thought"], "action": step["action"], "action_input": action_input, "observation": observation}
                )
                ctx.scratchpad = scratchpad
                steps_used += 1
                self._persist_step(
                    run_id, ordinal, step["thought"], step["action"], action_input, observation,
                    "ok" if ok else "tool_error", step_started,
                )
                yield {
                    "type": "observation",
                    "run_id": run_id,
                    "ordinal": ordinal,
                    "action": step["action"],
                    "ok": ok,
                    "observation": observation,
                    "detail": {k: v for k, v in result.items() if k not in {"ok", "results", "detail", "report"}} if ok else result,
                }
                if ok and result.get("proposal_id"):
                    proposal_ids.append(int(result["proposal_id"]))
                    yield {
                        "type": "proposal",
                        "run_id": run_id,
                        "ordinal": ordinal,
                        "proposal_id": int(result["proposal_id"]),
                        "title": result.get("title", ""),
                        "summary": result.get("summary", f"提案 #{result['proposal_id']} 待人工确认"),
                        "evidence": result.get("evidence", [])[:5],
                    }
            else:
                status = "max_steps"
                final_answer = (
                    f"达到步数上限（{budget} 步）后停止。"
                    f"本轮生成 {len(proposal_ids)} 个待确认提案："
                    + ("".join(f"#{pid} " for pid in proposal_ids) if proposal_ids else "无")
                )
        except Exception as exc:  # noqa: BLE001  运行异常要落库并可被前端观察到
            status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            final_answer = f"推理中断：{run.error}"
            yield {"type": "error", "run_id": run_id, "message": run.error}

        run.status = status
        run.final_answer = final_answer
        run.steps_used = steps_used
        run.tokens_used = tokens
        run.finished_at = utcnow()
        self.session.add(run)
        self.session.commit()

        proposals = []
        if proposal_ids:
            proposals = [
                {"id": p.id, "title": p.title, "kind": p.kind, "status": p.status, "confidence": p.confidence}
                for p in self.session.scalars(select(ChangeProposal).where(ChangeProposal.id.in_(proposal_ids))).all()
            ]

        memory_id = None
        if status in {"done", "max_steps"}:
            try:
                item = await self.memory.consolidate_run(run_id)
                memory_id = item.id if item else None
                self.session.commit()
            except Exception:  # noqa: BLE001  记忆沉淀失败不应影响本轮结论
                self.session.rollback()
                memory_id = None

        yield {
            "type": "final",
            "run_id": run_id,
            "status": status,
            "answer": final_answer,
            "steps": len(scratchpad),
            "tokens": tokens,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "proposals": proposals,
            "memory_item_id": memory_id,
        }

    def _persist_step(
        self,
        run_id: int,
        ordinal: int,
        thought: str,
        action: str,
        action_input: dict[str, Any],
        observation: str,
        status: str,
        started: float,
    ) -> None:
        self.session.add(
            AgentStep(
                run_id=run_id,
                ordinal=ordinal,
                thought=thought or "",
                action=action,
                action_input=action_input or {},
                observation=observation or "",
                status=status,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        self.session.commit()

    async def run(self, goal: str, **kwargs: Any) -> dict[str, Any]:
        events = [event async for event in self.run_stream(goal, **kwargs)]
        final = next((e for e in reversed(events) if e["type"] == "final"), None)
        if final is None:
            error = next((e for e in events if e["type"] == "error"), {})
            return {"type": "final", "status": "failed", "answer": error.get("message", "运行失败"), "events": events}
        return {**final, "events": events}


# ------------------------------------------------------------------ 查询辅助
def load_run(session: Session, run_id: int) -> dict[str, Any] | None:
    run = session.get(AgentRun, run_id)
    if run is None:
        return None
    steps = list(
        session.scalars(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.ordinal)).all()
    )
    return {
        "id": run.id,
        "space_id": run.space_id,
        "session_id": run.session_id,
        "goal": run.goal,
        "mode": run.mode,
        "status": run.status,
        "final_answer": run.final_answer,
        "steps_used": run.steps_used,
        "tokens_used": run.tokens_used,
        "error": run.error,
        "summary": run.summary,
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "finished_at": run.finished_at.isoformat() if run.finished_at else "",
        "steps": [
            {
                "ordinal": s.ordinal,
                "thought": s.thought,
                "action": s.action,
                "action_input": s.action_input,
                "observation": s.observation,
                "status": s.status,
                "latency_ms": s.latency_ms,
            }
            for s in steps
        ],
    }


def list_runs(session: Session, space_id: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
    stmt = select(AgentRun).order_by(AgentRun.id.desc()).limit(limit)
    if space_id:
        stmt = stmt.where(AgentRun.space_id == space_id)
    return [
        {
            "id": run.id,
            "goal": truncate(run.goal, 120),
            "status": run.status,
            "steps_used": run.steps_used,
            "created_at": run.created_at.isoformat() if run.created_at else "",
            "summary": run.summary,
        }
        for run in session.scalars(stmt).all()
    ]


def skill_catalog() -> list[dict[str, Any]]:
    """技能目录 + 每步实际可见的工具，供前端选择技能时预览。"""
    rows = []
    for skill in SKILLS:
        try:
            names = [tool.name for tool in registry_tools.tools(skill.allowed_tools)]
        except Exception:  # noqa: BLE001  目录预览不应因为单个技能失败
            names = list(skill.allowed_tools)
        rows.append({**skill.as_dict(), "tools": names})
    return rows
