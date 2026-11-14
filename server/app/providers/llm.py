"""LLM 组件：本抽取式 mock（零密钥）+ 任意 OpenAI 兼容厂商。

Mock 不是占位实现
----------------
它能真正完成四类任务：抽取式问答（带引用）、本体草案 JSON、ReAct 下一步决策、pointwise 重排。
做法是**识别我们自己约定的提示词小节**（``【资料】``/``【语料】``/``【可用工具】`` …）并据此产出结构化结果。
这些小节对真实模型同样有效（它们本来就是给模型读的上下文），所以切换厂商不需要改提示词。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from ..core.text import extract_terms, normalize, split_sentences, term_set, truncate
from .base import Message

# --------------------------------------------------------------------------- #
# 厂商目录：加一行就出现在组件中心（OpenAI 兼容协议）
# --------------------------------------------------------------------------- #
VENDORS: dict[str, dict[str, str]] = {
    "dashscope": {
        "display": "通义千问（阿里云百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "openai": {
        "display": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "deepseek": {
        "display": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "moonshot": {
        "display": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    "zhipu": {
        "display": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
    "volcengine": {
        "display": "火山方舟（豆包）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-pro-32k",
    },
    "vllm": {
        "display": "自建 vLLM",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "local-model",
    },
    "ollama": {
        "display": "本机 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
    },
}


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文按 1 字≈1 token，英文按 4 字符≈1 token。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = max(0, len(text) - cjk)
    return int(cjk + other / 4) + 1


# --------------------------------------------------------------------------- #
# 提示词小节解析（mock 与调试共用）
# --------------------------------------------------------------------------- #
_SECTION_RE = re.compile(r"【([^】]{1,20})】\s*(.*?)(?=【[^】]{1,20}】|\Z)", re.S)


def sections(prompt: str) -> dict[str, str]:
    return {m.group(1).strip(): m.group(2).strip() for m in _SECTION_RE.finditer(prompt)}


def task_of(messages: list[Message]) -> str:
    """从提示词里取 ``# 任务：xxx``，没有则返回空串。"""
    for message in messages:
        match = re.search(r"#\s*任务[：:]\s*([a-z_]+)", message.content)
        if match:
            return match.group(1)
    return ""


def _split_list(raw: str) -> list[str]:
    parts = re.split(r"[、,，;；\n]+", raw or "")
    return [p.strip(" -*•\t") for p in parts if p.strip(" -*•\t")]


# --------------------------------------------------------------------------- #
# Mock LLM
# --------------------------------------------------------------------------- #
_KIND_BY_SUFFIX: tuple[tuple[str, str], ...] = (
    ("合同", "agreement"),
    ("协议", "agreement"),
    ("契约", "agreement"),
    ("制度", "document"),
    ("办法", "document"),
    ("规定", "document"),
    ("规程", "document"),
    ("手册", "document"),
    ("标准", "document"),
    ("账户", "role"),
    ("角色", "role"),
    ("岗位", "role"),
    ("客户", "role"),
    ("机构", "organization"),
    ("部门", "organization"),
    ("系统", "asset"),
    ("平台", "asset"),
    ("设备", "asset"),
    ("装置", "asset"),
    ("模型", "asset"),
    ("指标", "metric"),
    ("比率", "metric"),
    ("余额", "metric"),
    ("额度", "metric"),
    ("事件", "event"),
    ("事故", "event"),
    ("报警", "event"),
    ("流程", "process"),
    ("程序", "process"),
    ("作业", "process"),
    ("计划", "process"),
    ("检查", "process"),
    ("审批", "process"),
    ("变更", "process"),
    ("工单", "process"),
    ("记录", "document"),
    ("报表", "document"),
    ("通知", "document"),
    ("清单", "document"),
    ("策略", "document"),
    ("基准", "document"),
    ("模板", "document"),
    ("分类", "class"),
    ("接口", "class"),
    ("链路", "class"),
    ("子网", "class"),
    ("台账", "asset"),
    ("数据集", "asset"),
    ("备件", "asset"),
    ("工具", "asset"),
    ("知识库", "asset"),
    ("智能体", "asset"),
    ("故障", "event"),
    ("机房", "organization"),
)

#: 看起来像「一句话」而不是「一个概念」的候选名，抽取后要剔掉
_NOT_A_CONCEPT = re.compile(
    r"合称|是指|指的是|应当|必须|不得|禁止|以下|下列|上述|本条|本办法|其中|包括|分别为"
    r"|指|一次|相应|开展|进行|配置|[与和及或、，,；;]"
)
_VERB_PREFIX = (
    "为", "在", "对", "按", "将", "把", "被", "由", "以", "于", "从", "向", "并", "且",
    "指", "规范", "加强", "完善", "建立", "健全", "明确", "要求", "执行", "落实",
    "余", "各", "该", "本", "此", "无", "非", "如", "则", "即", "凡", "经", "自",
)


def looks_like_concept(label: str) -> bool:
    """粗筛候选名：长度合理、不像整句、不含并列连词、不以动词/虚词/残词开头。"""
    label = (label or "").strip()
    if not (2 <= len(label) <= 12):
        return False
    if _NOT_A_CONCEPT.search(label):
        return False
    if label.startswith(_VERB_PREFIX):
        return False
    if label.endswith(("的", "了", "着", "等", "所")):
        return False
    return True


class MockLLM:
    """确定性、可复现、零外呼的抽取式实现。"""

    name = "mock"
    kind = "llm"

    def __init__(self, model: str = "finonto-mock-1") -> None:
        self.model = model
        self._calls = 0

    # ------------------------------------------------------------------ #
    def info(self) -> dict[str, Any]:
        return {
            "name": self.model,
            "provider": "mock",
            "kind": self.kind,
            "needs_key": False,
            "capabilities": ["answer", "ontology_draft", "react", "rerank"],
            "calls": self._calls,
        }

    def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        self._calls += 1
        return "".join(self.chat_stream(messages, temperature=temperature, max_tokens=max_tokens))

    def chat_stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        self._calls += 1
        task = task_of(messages)
        prompt = "\n".join(m.content for m in messages if m.role != "system")
        system = "\n".join(m.content for m in messages if m.role == "system")
        whole = system + "\n" + prompt

        if task == "ontology_draft":
            text = self._draft(whole)
        elif task == "react":
            text = self._react(whole)
        elif task == "rerank":
            text = self._rerank(whole)
        elif task == "query_understanding":
            text = self._query_understanding(whole)
        else:
            text = self._answer(whole)

        # 逐块吐字，模拟流式（按句切，前端能看出渐进效果）
        for piece in re.findall(r"[^。！？\n]*[。！？\n]?", text):
            if piece:
                yield piece

    # ------------------------------------------------------------------ #
    # 任务 1：抽取式问答（必须带 [cN] 引用）
    # ------------------------------------------------------------------ #
    def _answer(self, prompt: str) -> str:
        parts = sections(prompt)
        question = parts.get("问题", "").strip() or prompt[-200:]
        context = parts.get("资料", "")
        if not context:
            return "资料库里没有检索到相关内容，建议先入库文档或换个问法。"

        blocks = re.findall(r"\[(c\d+)\]\s*(.*?)(?=\n\[c\d+\]|\Z)", context, re.S)
        if not blocks:
            return "检索结果为空，请先上传文档后再提问。"

        q_terms = term_set(question)
        scored: list[tuple[float, str, str, str]] = []
        for tag, block in blocks:
            lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
            if not lines:
                continue
            head = lines[0]
            body = "\n".join(lines[1:])
            sentences = split_sentences(body) or [body]
            best_sentence, best_score = "", 0.0
            for sentence in sentences:
                st = term_set(sentence)
                if not st:
                    continue
                score = len(q_terms & st) / (len(q_terms) + 1e-6)
                score += 0.1 * len(q_terms & st)
                if score > best_score:
                    best_score, best_sentence = score, sentence
            if best_sentence:
                scored.append((best_score, tag, head, best_sentence))

        if not scored:
            return "资料中没有找到能回答该问题的句子。"

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:2]
        lead = "根据检索到的资料："
        body_parts: list[str] = []
        for score, tag, head, sentence in top:
            source = head.split("｜")[0].strip()
            body_parts.append(f"{truncate(sentence, 220)}[{tag}]")
        answer = lead + "".join(body_parts)
        if len(top) == 1:
            answer += "（其余资料相关度较低，未作引用）"
        return answer

    # ------------------------------------------------------------------ #
    # 任务 2：本体草案 JSON
    # ------------------------------------------------------------------ #
    def _draft(self, prompt: str) -> str:
        parts = sections(prompt)
        corpus = parts.get("语料", "") or prompt
        suffixes = tuple(_split_list(parts.get("术语后缀", "")))
        cue_markers = tuple(_split_list(parts.get("句式线索", ""))) or ("第", "是指")
        domains = _split_list(parts.get("子域线索", ""))
        tag_cues = _split_list(parts.get("标签线索", ""))
        existing = {
            line.strip(" -•")
            for line in re.split(r"[、,\n]", parts.get("已有类型", ""))
            if line.strip(" -•")
        }

        terms = extract_terms(
            corpus,
            suffixes=suffixes or ("系统", "流程", "指标", "制度"),
            cue_markers=cue_markers,
            max_terms=20,
        )
        terms = [term for term in terms if looks_like_concept(term)]
        # 只保留「以领域后缀结尾」的候选：合同/账户/额度/规程/工单/策略… 这类才像对象类型
        all_suffixes = tuple(suffixes) + tuple(suffix for suffix, _kind in _KIND_BY_SUFFIX)
        with_suffix = [term for term in terms if term.endswith(all_suffixes)]
        if with_suffix:
            terms = with_suffix[:8]
        else:
            terms = terms[:8]
        draft_types: list[dict[str, Any]] = []
        for index, term in enumerate(terms):
            if term in existing:
                continue
            kind = next((k for suffix, k in _KIND_BY_SUFFIX if term.endswith(suffix)), "class")
            domain = domains[index % len(domains)] if domains else ""
            quotes = self._evidence_for(corpus, term)
            definition = quotes[0] if quotes else f"{term}，见相关条款。"
            draft_types.append(
                {
                    "label": term,
                    "label_en": "",
                    "kind": kind,
                    "definition": truncate(definition, 180),
                    "business_domain": domain,
                    "aliases": [],
                    "tags": [tag_cues[index % len(tag_cues)]] if tag_cues else [],
                    "confidence": round(0.45 + 0.1 * min(len(quotes), 3), 2),
                    "properties": self._props_for(corpus, term),
                    "relations": self._relations_for(term, terms, index),
                    "constraints": self._constraints_for(corpus, term),
                    "evidence": [
                        {"quote": truncate(q, 160), "locator": f"§{term}"} for q in quotes[:2]
                    ],
                }
            )
        payload = {
            "summary": f"从语料中抽取到 {len(draft_types)} 个候选对象类型"
            + (f"（子域：{('、'.join(domains))}）" if domains else ""),
            "object_types": draft_types,
            "rationale": "基于术语后缀、定义句式与结构化标记的抽取式结果，需人工确认。",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _evidence_for(self, corpus: str, term: str) -> list[str]:
        hits: list[str] = []
        for sentence in split_sentences(corpus):
            if term in sentence and 8 <= len(sentence) <= 220:
                hits.append(sentence)
            if len(hits) >= 3:
                break
        return hits

    def _props_for(self, corpus: str, term: str) -> list[dict[str, Any]]:
        props: list[dict[str, Any]] = []
        seen: set[str] = set()
        window = ""
        for sentence in split_sentences(corpus):
            if term in sentence:
                window += sentence + "\n"
        for match in re.finditer(r"([\u4e00-\u9fff]{2,8})(?:应当|应|须|需)(?:包含|包括|载明|记录|登记)", window):
            label = match.group(1)
            if label not in seen:
                seen.add(label)
                props.append({"label": label, "data_type": "string", "required": True})
        for match in re.finditer(r"([\u4e00-\u9fff]{2,8})(?:金额|比例|比率|利率|期限|日期|数量|编号)", window):
            label = match.group(1)
            data_type = "decimal" if re.search(r"金额|比例|比率|利率", label) else "string"
            if label not in seen:
                seen.add(label)
                props.append({"label": label, "data_type": data_type, "required": False})
        return props[:4]

    def _relations_for(self, term: str, terms: list[str], index: int) -> list[dict[str, str]]:
        relations: list[dict[str, str]] = []
        for offset, other in enumerate(terms):
            if offset == index:
                continue
            relations.append({"relation": "关联于", "target": other})
            if len(relations) >= 2:
                break
        return relations

    def _constraints_for(self, corpus: str, term: str) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for sentence in split_sentences(corpus):
            if term not in sentence:
                continue
            if re.search(r"不得|禁止|应当|必须", sentence):
                out.append(
                    {
                        "kind": "codependency",
                        "severity": "violation",
                        "expression": truncate(sentence, 120),
                        "message": f"{term} 的相关条款约束",
                    }
                )
            if len(out) >= 2:
                break
        return out

    # ------------------------------------------------------------------ #
    # 任务 3：ReAct 下一步
    # ------------------------------------------------------------------ #
    def _react(self, prompt: str) -> str:
        parts = sections(prompt)
        goal = parts.get("目标", "").strip()
        tools = _split_list(re.sub(r"\s+—.*?(?=\n|$)", "", parts.get("可用工具", "")))
        done_raw = parts.get("已完成步骤", "")
        used = set(re.findall(r"Action:\s*([a-zA-Z_.]+)", done_raw))
        observations = parts.get("最近观察", "")

        plan = [
            ("rag.search", {"query": goal or "制度条款"}),
            ("doc.list", {}),
            ("ontology.list_types", {}),
            ("find_similar", {"label": self._guess_label(goal, observations)}),
            (
                "propose_type",
                {"draft": self._draft_payload(goal, done_raw)},
            ),
            ("wiki.read_page", {"slug": "index"}),
        ]
        for tool, payload in plan:
            if tool not in tools:
                continue
            if tool in used:
                continue
            return (
                f"Thought: 还缺 {tool} 这一步的信息，先调用它收集依据。\n"
                f"Action: {tool}\n"
                f"Action Input: {json.dumps(payload, ensure_ascii=False)}"
            )
        return (
            "Thought: 已完成检索、对齐与提案，可以给出结论。\n"
            f"Final Answer: 基于检索与已有本体对齐，已产出待确认提案。{truncate(goal, 80)}"
        )

    def _guess_label(self, goal: str, observations: str) -> str:
        """优先取引号里的短语，其次取常见名词后缀，最后退回最长的中文片段。"""
        for pattern in (r"[「『\"“]([^」』\"”]{2,20})[」』\"”]", r"《([^》]{2,20})》"):
            match = re.search(pattern, goal)
            if match:
                return match.group(1).strip()
        candidates = extract_terms(
            goal,
            suffixes=("计划", "检查", "管理", "制度", "规程", "规范", "模型", "设备", "合同",
                      "接口", "策略", "指标", "系统", "流程", "账户", "报表", "通知"),
            max_terms=3,
        )
        if candidates:
            return candidates[0]
        runs = [run for run in re.findall(r"[\u4e00-\u9fff]{4,10}", goal)]
        if runs:
            return max(runs, key=len)
        return "候选对象类型"

    def _draft_payload(self, goal: str, observations: str) -> dict[str, Any]:
        """以「目标里点名的那个概念」为主体产出草案，语料只用来补定义/属性/证据。

        这样 Agent 提议的永远是它被要求提议的东西，而不是语料里出现频率最高的词。
        """
        label = self._guess_label(goal, observations)
        corpus = observations or goal
        kind = next((k for suffix, k in _KIND_BY_SUFFIX if label.endswith(suffix)), "class")
        quotes = self._evidence_for(corpus, label)
        definition = quotes[0] if quotes else truncate(goal, 140)
        evidence = [{"quote": truncate(q, 160), "locator": "检索结果"} for q in quotes[:2]]
        if not evidence:
            evidence = [{"quote": truncate(corpus, 200), "locator": "检索结果"}]
        return {
            "label": label,
            "kind": kind,
            "definition": truncate(definition, 180),
            "business_domain": "",
            "aliases": [],
            "tags": [],
            "confidence": 0.55,
            "properties": self._props_for(corpus, label),
            "relations": [],
            "constraints": self._constraints_for(corpus, label),
            "evidence": evidence,
        }

    # ------------------------------------------------------------------ #
    # 任务 4：pointwise 重排
    # ------------------------------------------------------------------ #
    def _rerank(self, prompt: str) -> str:
        parts = sections(prompt)
        query = parts.get("问题", "")
        items = re.findall(r"\[(c\d+)\]\s*(.*?)(?=\n\[c\d+\]|\Z)", parts.get("候选", ""), re.S)
        q_terms = term_set(query)
        scored: list[tuple[float, int]] = []
        for index, (_tag, text) in enumerate(items):
            st = term_set(text)
            scored.append((len(q_terms & st) / (len(q_terms) + 1e-6), index + 1))
        scored.sort(reverse=True)
        return " ".join(str(idx) for _score, idx in scored) or "1"

    # ------------------------------------------------------------------ #
    # 任务 5：查询理解增强（规则已够用，这里只补同义词）
    # ------------------------------------------------------------------ #
    def _query_understanding(self, prompt: str) -> str:
        parts = sections(prompt)
        question = parts.get("问题", "")
        synonyms = {
            "维保": ["维护", "保养"],
            "预防性维护": ["计划性维护", "定期维护"],
            "合同": ["协议", "契约"],
            "额度": ["限额", "授信额度"],
        }
        hits = {k: v for k, v in synonyms.items() if k in question}
        intent = "definition"
        if re.search(r"多少|比例|金额|占比|几个", question):
            intent = "quantitative"
        elif re.search(r"对比|区别|差异|相比", question):
            intent = "comparison"
        elif re.search(r"生成|设计|建设|抽取", question) and "本体" in question:
            intent = "ontology_generation"
        return json.dumps({"intent": intent, "synonyms": hits}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# OpenAI 兼容实现（覆盖目录里全部厂商）
# --------------------------------------------------------------------------- #
class OpenAICompatLLM:
    """任何提供 ``/chat/completions`` 的服务都能接：百炼 / OpenAI / DeepSeek / Ollama / vLLM。"""

    kind = "llm"

    def __init__(
        self,
        *,
        vendor: str = "dashscope",
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout: float = 60.0,
    ) -> None:
        profile = VENDORS.get(vendor, {})
        self.vendor = vendor
        self.base_url = (base_url or profile.get("base_url", "")).rstrip("/")
        self.api_key = api_key
        self.model = model or profile.get("model", "")
        self.timeout = timeout
        self.name = f"openai_compat:{vendor}"

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": "openai_compat",
            "vendor": self.vendor,
            "model": self.model,
            "base_url": self.base_url,
            "needs_key": True,
            "key_configured": bool(self.api_key),
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        import httpx

        payload = {
            "model": self.model,
            "messages": [m.as_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=self._headers(), json=payload)
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"]["content"] or ""

    def chat_stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> Iterator[str]:
        import httpx

        payload = {
            "model": self.model,
            "messages": [m.as_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", url, headers=self._headers(), json=payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    text = delta.get("content") or ""
                    if text:
                        yield text
