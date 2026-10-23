"""本地零依赖 LLM 实现（MockLLM）。

它不是随机字符串生成器：按 `[[TASK:*]]` 分派到对应的确定性算法，从 prompt 携带的
上下文中做抽取式合成，因此在不接任何外部模型的情况下，RAG 回答、本体候选草稿、
ReAct 步骤、Wiki 页面都能产出真实可用、可引用的结果。接上真实模型后由
OpenAICompatProvider 取代。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator

from ...core.text import (
    CLAUSE_MARKERS,
    candidate_terms,
    extract_definitions,
    overlap,
    split_sentences,
    truncate,
)
from ..base import ChatMessage, LLMUsage

CTX_BLOCK = re.compile(r"^\[(c\d+)\]\s*(.*)$", re.M)
TASK_MARK = re.compile(r"\[\[TASK:([a-z_]+)\]\]")
DOMAIN_CUE_LINE = re.compile(r"子域线索：([^\n]+)")
TAG_CUE_LINE = re.compile(r"标签线索：([^\n]+)")
TERM_SUFFIX_LINE = re.compile(r"术语后缀：([^\n]+)")


def task_of(messages: list[ChatMessage]) -> str:
    for m in messages:
        found = TASK_MARK.search(m.content)
        if found:
            return found.group(1)
    return "generic"


def _cue_table(prompt: str) -> dict[str, tuple[str, ...]]:
    """解析提示词里的「子域线索」表，让领域判定跟随当前领域包。"""
    found = DOMAIN_CUE_LINE.search(prompt or "")
    if not found:
        return {}
    table: dict[str, tuple[str, ...]] = {}
    for entry in found.group(1).split(";"):
        domain, sep, cues = entry.strip().partition("=")
        if sep and domain:
            table[domain] = tuple(c for c in cues.split(",") if c)
    return table


def _tag_table(prompt: str) -> dict[str, str]:
    found = TAG_CUE_LINE.search(prompt or "")
    if not found:
        return {}
    mapping: dict[str, str] = {}
    for entry in found.group(1).split(";"):
        cue, sep, tag = entry.strip().partition("→")
        if sep and cue:
            mapping[cue] = tag
    return mapping


def _term_suffixes(prompt: str) -> tuple[str, ...]:
    found = TERM_SUFFIX_LINE.search(prompt or "")
    if not found:
        return ()
    return tuple(item.strip() for item in found.group(1).split("、") if item.strip())


def prompt_text(messages: list[ChatMessage]) -> str:
    """摊平整段对话：领域说明书在 system，资料块与轨迹在 user，抽取时两者都要看。"""
    return "\n".join(m.content for m in messages if m.content)


def split_context(prompt: str) -> list[tuple[str, str]]:
    """把 prompt 中 `[cN] ...` 标记的资料块解析为 (ref, text)。"""
    lines = prompt.splitlines()
    blocks: list[tuple[str, list[str]]] = []
    for line in lines:
        found = CTX_BLOCK.match(line)
        if found:
            blocks.append((found.group(1), [found.group(2)]))
        elif blocks and line.strip():
            blocks[-1][1].append(line)
    return [(ref, "\n".join(body)) for ref, body in blocks]


def json_block(text: str) -> str:
    """提取 prompt 中最后一个 ```json 围栏或裸 JSON 对象。"""
    fences = re.findall(r"```json\s*(.*?)```", text or "", re.S)
    if fences:
        return fences[-1].strip()
    start = (text or "").find("{")
    end = (text or "").rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return ""


def _question_of(prompt: str) -> str:
    m = re.search(r"## 用户问题\s*\n(.+)", prompt)
    if not m:
        m = re.search(r"Question:\s*(.+)", prompt)
    return (m.group(1).strip() if m else "")[:400]


class MockLLMProvider:
    name = "mock"
    model = "finonto-extractive-v1"

    def __init__(self, speed_hint: bool = True) -> None:
        self.speed_hint = speed_hint

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        task: str = "generic",
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> tuple[str, LLMUsage]:
        text = self._render(messages, task)
        prompt_len = sum(len(m.content) for m in messages)
        return text, LLMUsage(prompt_tokens=prompt_len // 2, completion_tokens=len(text) // 2)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        task: str = "generic",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        text = self._render(messages, task)
        for piece in re.findall(r"\S{1,8}\s*", text):
            yield piece
            if self.speed_hint:
                await asyncio.sleep(0.004)

    # ------------------------------------------------------------ 分派
    def _render(self, messages: list[ChatMessage], task_hint: str) -> str:
        task = task_of(messages) if task_hint in ("", "generic") else task_hint
        prompt = prompt_text(messages)
        handler = {
            "rag_answer": self._rag_answer,
            "ontology_draft": self._ontology_draft,
            "react_step": self._react_step,
            "wiki_page": self._wiki_page,
            "definition_refine": self._definition_refine,
            "memory_summary": self._memory_summary,
            "rerank": self._rerank,
        }.get(task)
        return handler(prompt) if handler else self._generic(prompt)

    # ------------------------------------------------------------ RAG 抽取式回答
    def _rag_answer(self, prompt: str) -> str:
        question = _question_of(prompt)
        contexts = split_context(prompt)
        if not contexts:
            return (
                "当前知识库中没有检索到与该问题相关的资料，无法给出有依据的回答。"
                "建议先上传相关监管制度或业务文档，或放宽检索范围后重试。"
            )
        scored: list[tuple[float, str, str]] = []
        for ref, body in contexts:
            for sent in split_sentences(body):
                if len(sent) < 10:
                    continue
                s = overlap(question, sent) if question else 0.0
                s += 0.15 * min(len(sent) / 120.0, 1.0)
                scored.append((s, ref, sent))
        scored.sort(key=lambda t: -t[0])
        picked: list[tuple[str, str]] = []
        seen: set[str] = set()
        for _s, ref, sent in scored:
            key = sent[:24]
            if key in seen:
                continue
            seen.add(key)
            picked.append((ref, sent))
            if len(picked) >= 5:
                break
        if not picked:
            picked = [(contexts[0][0], truncate(contexts[0][1], 120))]
        refs_used = {r for r, _ in picked}
        lines = ["根据知识库检索到的资料：", ""]
        for idx, (ref, sent) in enumerate(picked, start=1):
            lines.append(f"{idx}. {truncate(sent, 160)} [{ref}]")
        lines.append("")
        lines.append(
            "综上，上述结论均来自 "
            + "、".join(sorted(refs_used))
            + " 对应的原文片段；若需要把其中出现的核心概念沉淀为本体对象类型，"
            "可交给本体构建 Agent 生成草稿并人工确认。"
        )
        return "\n".join(lines)

    # ---------------------------------------------------- 本体候选草稿（JSON）
    def _ontology_draft(self, prompt: str) -> str:
        contexts = split_context(prompt)
        corpus = "\n".join(body for _, body in contexts) or prompt
        defs = extract_definitions(corpus, limit=8)
        terms = dict(candidate_terms(corpus, max_terms=60, extra_suffixes=_term_suffixes(prompt)))
        domain_table = _cue_table(prompt)
        tag_table = _tag_table(prompt)
        aliases_map: dict[str, list[str]] = {}
        from ...core.text import extract_aliases

        for term, aliases in extract_aliases(corpus).items():
            aliases_map[term] = aliases

        existing = set(re.findall(r"- ([\u4e00-\u9fa5A-Za-z]{2,20})\b", prompt.partition("## 已有对象类型")[2].partition("##")[0]))
        items: list[dict] = []
        for d in defs:
            term = d["term"]
            if term in existing:
                continue
            items.append(
                {
                    "label": term,
                    "definition": truncate(d["definition"], 220),
                    "kind": "class",
                    "business_domain": self._guess_domain(term + d["definition"], domain_table),
                    "confidence": round(min(0.9, 0.45 + overlap(term, corpus) * 0.5), 2),
                    "aliases": aliases_map.get(term, []),
                    "tags": self._guess_tags(term + d["definition"], tag_table),
                    "evidence": truncate(d["sentence"], 160),
                }
            )
        if len(items) < 4:
            for term, score in sorted(terms.items(), key=lambda kv: -kv[1])[: 8 - len(items)]:
                if term in existing or len(term) < 2:
                    continue
                items.append(
                    {
                        "label": term,
                        "definition": f"从资料中识别出的候选业务概念「{term}」，需在确认前补充精确定义。",
                        "kind": "class",
                        "business_domain": self._guess_domain(term, domain_table),
                        "confidence": round(min(0.6, 0.25 + score / 25.0), 2),
                        "aliases": aliases_map.get(term, []),
                        "tags": self._guess_tags(term, tag_table),
                        "evidence": "",
                    }
                )
        relations = self._guess_relations(items, corpus)
        return json.dumps(
            {
                "object_types": items,
                "relations": relations,
                "notes": "由抽取式基线模型生成，字段置信度反映原文证据强度，建议人工复核后入库。",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _guess_domain(text: str, table: dict[str, tuple[str, ...]]) -> str:
        """按当前领域包注入的子域线索归类；没有线索时落到「通用」。"""
        for domain, cues in table.items():
            if any(cue in text for cue in cues):
                return domain
        return "通用"

    @staticmethod
    def _guess_tags(text: str, mapping: dict[str, str]) -> list[str]:
        tags = [tag for cue, tag in mapping.items() if cue in text]
        return tags or ["候选"]

    @staticmethod
    def _guess_relations(items: list[dict], corpus: str) -> list[dict]:
        out: list[dict] = []
        labels = [i["label"] for i in items]
        for a in labels:
            for b in labels:
                if a == b or overlap(a, b) > 0.6:
                    continue
                if a in corpus and b in corpus:
                    sa = corpus.find(a)
                    sb = corpus.find(b)
                    window = corpus[max(0, min(sa, sb) - 30) : max(sa, sb) + 40]
                    if any(c in window for c in ("包括", "属于", "关联", "对应", "基于", "用于")):
                        out.append(
                            {
                                "source": a,
                                "target": b,
                                "relation": "关联",
                                "description": truncate(window.replace(a, "《" + a + "》"), 120),
                            }
                        )
        return out[:6]

    # ------------------------------------------------------- ReAct 单步决策
    _EVIDENCE_ACTIONS = ("rag.search", "doc.read", "rag.ask", "search.web", "search.fetch", "datasource.profile")

    @staticmethod
    def _parse_trajectory(scratch: str) -> list[tuple[str, str]]:
        """把 Thought/Action/ActionInput/Observation 轨迹还原成 (动作, 观察) 序列。"""
        pairs: list[tuple[str, str]] = []
        action = ""
        observation: list[str] = []
        capturing = False
        for line in scratch.splitlines():
            if line.startswith("Action:"):
                action = line.split(":", 1)[1].strip()
                capturing = False
            elif line.startswith("ActionInput:"):
                continue
            elif line.startswith("Observation:"):
                observation = [line.split(":", 1)[1].strip()]
                capturing = True
            elif line.startswith("Thought:"):
                if action:
                    pairs.append((action, " ".join(observation)))
                action, observation, capturing = "", [], False
            elif capturing:
                observation.append(line)
        if action:
            pairs.append((action, " ".join(observation)))
        return pairs

    @staticmethod
    def _strip_observation_meta(observation: str) -> str:
        """去掉观察结果里的流程性字段（检索条数、intent、提案号等），只留原文证据。"""
        kept: list[str] = []
        for segment in observation.split("|"):
            text = segment.strip()
            if not text:
                continue
            if re.match(r"^(检索到|共|当前空间|提案|intent=|status=|proposal_id=|count=|total=|empty=|ref=)", text):
                continue
            if text.startswith("可基于") or text.startswith("无结果"):
                continue
            kept.append(re.sub(r"^\S{1,60}\.(md|txt|pdf|docx|csv|xlsx|json)\s*[:：]\s*", "", text))
        return "\n".join(kept)

    _CLAUSE_MARKERS = CLAUSE_MARKERS

    @classmethod
    def _is_clause_fragment(cls, term: str) -> bool:
        """过滤掉从义务句式里切出来的动词片段，它们不适合做对象类型名。"""
        return any(marker in term for marker in cls._CLAUSE_MARKERS) or term.endswith("的")

    def _react_step(self, prompt: str) -> str:
        goal = ""
        m = re.search(r"## 目标\s*\n(.+)", prompt)
        if m:
            goal = m.group(1).strip()
        scratch = prompt.partition("## 已执行的推理轨迹")[2]
        pairs = self._parse_trajectory(scratch)
        done = [action for action, _ in pairs]
        tools_blob = prompt.partition("## 可用工具")[2].partition("##")[0]
        available = set(re.findall(r"- ([a-zA-Z][\w.\-]*)\s*:", tools_blob))

        # 候选术语只从「检索到的原文」里取，避免把工具回显的流程用语当成业务概念
        evidence = "\n".join(
            self._strip_observation_meta(obs)
            for action, obs in pairs
            if action in self._EVIDENCE_ACTIONS and not obs.startswith("失败")
        )
        proposed = {
            match.group(1)
            for action, obs in pairs
            if action == "ontology.propose_type" and obs.startswith("提案")
            for match in [re.search(r"label=([^|\s]+)", obs)]
            if match
        }
        # 用户在目标里用「」点名的概念优先，其次才是原文统计出的术语
        named = [(term, 30.0) for term in dict.fromkeys(re.findall(r"[「]([^」]{2,14})[」]", goal))]
        terms = named + (
            candidate_terms(evidence, max_terms=30, extra_suffixes=_term_suffixes(prompt))
            if evidence
            else []
        )
        domain_table = _cue_table(prompt)
        tag_table = _tag_table(prompt)
        definitions = {row["term"]: row["definition"] for row in extract_definitions(evidence, limit=40)}
        # 已经查重过或已经提案过的概念不再重复处理（含被工具拒绝的重名概念）
        considered = set(re.findall(r'"label":\s*"([^"]+)"', scratch)) | proposed
        top_terms = [
            (term, score)
            for term, score in terms
            if term not in considered
            and not any(term in p or p in term for p in considered)
            and not self._is_clause_fragment(term)
        ][:8]

        if "rag.search" in available and "rag.search" not in done:
            action, args = "rag.search", {"query": goal or " ".join(t for t, _ in top_terms), "top_k": 8}
        elif "doc.list" in available and "doc.list" not in done and "rag.search" in done:
            action, args = "doc.list", {}
        elif "ontology.list_types" in available and "ontology.list_types" not in done:
            action, args = "ontology.list_types", {"limit": 60}
        elif "ontology.find_similar" in available and top_terms and "ontology.find_similar" not in done:
            action, args = "ontology.find_similar", {"label": top_terms[0][0]}
        elif top_terms and "ontology.propose_type" in available and len(proposed) < 4:
            term, score = top_terms[0]
            definition = definitions.get(term) or self._describe_concept(term, evidence)
            action = "ontology.propose_type"
            args = {
                "label": term,
                "definition": definition,
                "business_domain": self._guess_domain(term, domain_table),
                "confidence": round(min(0.8, 0.35 + score / 30.0), 2),
                "tags": self._guess_tags(term, tag_table),
                "rationale": f"该概念在检索到的原文中反复出现（得分 {round(score, 1)}），具备独立建类的依据。",
            }
        elif "wiki.draft_page" in available and "wiki.draft_page" not in done and len(done) >= 3:
            action, args = "wiki.draft_page", {"topic": goal}
        else:
            action = "finish"
            summary_terms = "、".join(sorted(proposed)) or "、".join(term for term, _ in top_terms[:3]) or truncate(goal, 40)
            answer = (
                f"已完成推理（{len(done)} 步）。基于检索资料归纳出的核心概念：{summary_terms or '（无新增概念）'}。"
                f"本轮提交 {len(proposed)} 个候选对象类型提案，均通过 ChangeProposal 进入待人工确认队列，"
                "确认后将写入本体空间并触发 Wiki 页面重编译。"
            )
            args = {"answer": answer}
        thought = (
            f"目标是「{truncate(goal, 60)}」。已执行 {len(done)} 步，已提交提案 {len(proposed)} 个；"
            f"下一步调用 {action} 以获取或沉淀结构化信息。"
        )
        return json.dumps(
            {"thought": thought, "action": action, "action_input": args}, ensure_ascii=False
        )

    @staticmethod
    def _describe_concept(term: str, evidence: str) -> str:
        """没有「X 是指」句式时，用包含该术语的原文句子拼出保守的定义性描述。"""
        sentences = [s for s in split_sentences(evidence) if term in s]
        if sentences:
            best = max(sentences, key=len)
            return f"依据检索原文归纳：{truncate(best.strip(), 140)}（术语「{term}」在资料中作为独立业务概念出现）。"
        return f"依据检索资料识别出的业务概念「{term}」，原文中以其为限定对象被描述，需人工确认口径后正式建类。"

    # ----------------------------------------------------------- Wiki 页面
    def _wiki_page(self, prompt: str) -> str:
        payload = json_block(prompt)
        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            data = {}
        label = data.get("label", "未命名对象类型")
        definition = data.get("definition", "")
        props = data.get("properties", []) or []
        links = data.get("links", []) or []
        lines = [f"# {label}", "", "## 定义", definition or "（待补充）", ""]
        if props:
            lines += ["## 属性", "", "| 属性 | 数据类型 | 必填 | 说明 |", "| --- | --- | --- | --- |"]
            for p in props:
                lines.append(
                    f"| {p.get('label', '')} | {p.get('datatype', 'string')} | "
                    f"{'是' if p.get('required') else '否'} | {truncate(p.get('description', ''), 60)} |"
                )
            lines.append("")
        if links:
            lines += ["## 关联对象类型", ""]
            for l in links:
                lines.append(f"- **{l.get('relation', '关联')}** → {l.get('target', '')}：{truncate(l.get('description', ''), 80)}")
            lines.append("")
        ann = data.get("annotations", {}) or {}
        if ann:
            lines += ["## 领域注解", ""]
            for k, v in ann.items():
                lines.append(f"- **{k}**：{v}")
            lines.append("")
        lines += [
            "> 本页由本体编译器自动生成。人工编辑会生成新版本，可在版本列表中 diff 与回滚。",
        ]
        return "\n".join(lines)

    def _definition_refine(self, prompt: str) -> str:
        contexts = split_context(prompt)
        corpus = "\n".join(b for _, b in contexts) or prompt
        m = re.search(r"## 目标术语\s*\n(.+)", prompt)
        term = m.group(1).strip() if m else ""
        for d in extract_definitions(corpus, limit=20):
            if term and (term in d["term"] or d["term"] in term):
                return truncate(d["definition"], 300)
        sents = [s for s in split_sentences(corpus) if term and term in s]
        return truncate(" ".join(sents[:2]), 300) if sents else truncate(corpus, 200)

    def _memory_summary(self, prompt: str) -> str:
        body = prompt.partition("## 推理轨迹")[2]
        bullets = [truncate(s, 90) for s in split_sentences(body) if len(s) > 18][:4]
        return "；".join(bullets) if bullets else truncate(prompt, 180)

    def _rerank(self, prompt: str) -> str:
        m = re.search(r"## 问题\s*\n(.+)", prompt)
        question = m.group(1).strip() if m else ""
        items: list[dict] = []
        for idx, (ref, body) in enumerate(split_context(prompt)):
            items.append({"index": idx, "score": round(overlap(question, body), 4), "ref": ref})
        items.sort(key=lambda d: -d["score"])
        return json.dumps({"ranked": items}, ensure_ascii=False)

    def _generic(self, prompt: str) -> str:
        sents = split_sentences(prompt)
        return truncate(" ".join(sents[:3]) if sents else prompt, 300)


def usage_estimate(messages: list[ChatMessage], out: str) -> LLMUsage:
    return LLMUsage(sum(len(m.content) for m in messages) // 2, len(out) // 2)


__all__ = ["MockLLMProvider", "task_of", "split_context", "json_block", "usage_estimate"]
