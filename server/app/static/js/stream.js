/* stream.js —— SSE 消费（RAG 问答 + Agent 推理）
 *
 * 为什么用 EventSource 而不是 fetch 流：SSE 自动重连、实现最短（约 20 行），
 * 而本项目只需要服务端→浏览器单向推送。
 */
(function () {
  "use strict";

  const space = new URLSearchParams(location.search).get("space") || "0";

  function el(id) { return document.getElementById(id); }

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ------------------------------------------------------------------ //
  // 问答
  // ------------------------------------------------------------------ //
  const askForm = el("ask-form");
  if (askForm) {
    const input = el("ask-input");
    const answerPanel = el("answer-panel");
    const retrievalPanel = el("retrieval-panel");
    const answerBox = el("answer");
    const status = el("ask-status");

    function ask(question) {
      if (!question || !question.trim()) return;
      input.value = question;
      answerPanel.hidden = false;
      retrievalPanel.hidden = true;
      answerBox.textContent = "";
      el("plan-row").innerHTML = "";
      el("citations").innerHTML = "";
      el("retrieval").innerHTML = "";
      status.innerHTML = '<span class="spinner"></span> 检索与生成中…';

      const url = `/api/rag/stream?space=${encodeURIComponent(space)}&q=${encodeURIComponent(question)}`;
      const source = new EventSource(url);
      let answer = "";

      source.onmessage = (event) => {
        if (event.data === "[DONE]") { source.close(); return; }
        let payload;
        try { payload = JSON.parse(event.data); } catch (e) { return; }

        if (payload.type === "plan") {
          const plan = payload.data;
          const chips = [
            `<span class="badge info">意图 ${escapeHtml(plan.intent)}</span>`,
            plan.aligned_types && plan.aligned_types.length
              ? `<span class="badge ghost">对齐 ${plan.aligned_types.map((t) => escapeHtml(t.label)).join("、")}</span>` : "",
            plan.expanded_terms && plan.expanded_terms.length
              ? `<span class="badge ghost">扩展 ${plan.expanded_terms.map(escapeHtml).join("、")}</span>` : "",
            plan.filters && Object.keys(plan.filters).length
              ? `<span class="badge ghost">过滤 ${escapeHtml(JSON.stringify(plan.filters))}</span>` : "",
          ];
          el("plan-row").innerHTML = chips.join("");
        }

        if (payload.type === "retrieval") {
          const items = payload.data || [];
          retrievalPanel.hidden = items.length === 0;
          el("retrieval").innerHTML = items.map((item, index) => `
            <div class="row">
              <div class="row-main">
                <div><span class="badge ghost">c${index + 1}</span>
                  <strong>${escapeHtml(item.doc_title)}</strong>
                  <span class="muted small">${escapeHtml(item.locator)}</span></div>
                <p>${escapeHtml(item.text).slice(0, 260)}…</p>
              </div>
              <div class="row-meta">
                <span class="badge ghost">${(item.score || 0).toFixed(3)}</span>
                <span class="muted small">${escapeHtml(Object.keys(item.signals.sources || {}).join("+"))}</span>
              </div>
            </div>`).join("");
          status.innerHTML = `已召回 ${items.length} 段，生成中…`;
        }

        if (payload.type === "token") {
          answer += payload.text;
          answerBox.textContent = answer;
        }

        if (payload.type === "citations") {
          el("citations").innerHTML = (payload.data || []).map((item) => `
            <div class="citation" data-tag="${escapeHtml(item.tag)}">
              <div class="meta">${escapeHtml(item.tag)} ${escapeHtml(item.doc_title)}｜${escapeHtml(item.locator)}</div>
              <div>${escapeHtml(item.quote)}</div>
            </div>`).join("");
        }

        if (payload.type === "done") {
          const data = payload.data || {};
          el("answer-meta").textContent =
            `${data.provider || ""} · ${data.latency_ms || 0}ms · trace #${data.trace_id || "-"}`;
          status.textContent = "完成";
          source.close();
        }

        if (payload.type === "error") {
          status.textContent = "出错：" + payload.message;
          source.close();
        }
      };

      source.onerror = () => {
        status.textContent = "连接中断";
        source.close();
      };
    }

    askForm.addEventListener("submit", (event) => {
      event.preventDefault();
      ask(input.value);
    });

    document.querySelectorAll("[data-ask]").forEach((button) => {
      button.addEventListener("click", () => ask(button.dataset.ask));
    });

    // 按本题生成本体草案（只产出提案）
    el("draft-btn")?.addEventListener("click", async () => {
      const question = input.value.trim();
      if (!question) { window.FinOnto.toast("先输入一个问题", "err"); return; }
      const button = el("draft-btn");
      button.disabled = true;
      button.textContent = "生成中…";
      try {
        const response = await fetch(
          `/api/ontology/${space}/draft?question=${encodeURIComponent(question)}`,
          { method: "POST" }
        );
        const data = await response.json();
        el("draft-panel").hidden = false;
        el("draft-result").innerHTML = (data.proposals || []).map((item) => `
          <div class="row">
            <div class="row-main">
              <strong>#${item.proposal_id} ${escapeHtml(item.label)}</strong>
              <span class="badge ghost">${escapeHtml(item.payload.kind || "")}</span>
              <span class="muted small">${escapeHtml((item.payload.definition || "").slice(0, 110))}</span>
              ${item.similar && item.similar.length
                ? `<span class="badge warn">疑似重复：${escapeHtml(item.similar[0].label)} (${item.similar[0].score})</span>` : ""}
            </div>
            <div class="row-meta"><span class="muted small">${item.evidence_count} 条证据</span></div>
          </div>`).join("") || '<p class="muted">没有产出候选类型。</p>';
        window.FinOnto.toast(`产出 ${(data.proposals || []).length} 条提案`, "ok");
      } catch (error) {
        window.FinOnto.toast("生成失败：" + error.message, "err");
      } finally {
        button.disabled = false;
        button.textContent = "按本题生成本体草案";
      }
    });
  }

  // ------------------------------------------------------------------ //
  // Agent
  // ------------------------------------------------------------------ //
  const agentForm = el("agent-form");
  if (agentForm) {
    const stepsPanel = el("steps-panel");
    const stepsList = el("agent-steps");
    const status = el("agent-status");

    agentForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const formData = new FormData(agentForm);
      const goal = (formData.get("goal") || "").toString().trim();
      if (!goal) { window.FinOnto.toast("请填写目标", "err"); return; }
      const skill = formData.get("skill") || "doc-extraction";
      const maxSteps = formData.get("max_steps") || "8";

      stepsPanel.hidden = false;
      stepsList.innerHTML = "";
      el("agent-result").hidden = true;
      status.innerHTML = '<span class="spinner"></span> 推理中…';

      const url = `/api/agent/stream?space=${encodeURIComponent(space)}&goal=${encodeURIComponent(goal)}` +
        `&skill=${encodeURIComponent(skill)}&max_steps=${encodeURIComponent(maxSteps)}`;
      const source = new EventSource(url);

      source.onmessage = (event) => {
        if (event.data === "[DONE]") { source.close(); return; }
        let payload;
        try { payload = JSON.parse(event.data); } catch (e) { return; }

        if (payload.type === "start") {
          el("agent-meta").textContent =
            `run #${payload.data.run_id} · 技能 ${payload.data.skill} · 预算 ${payload.data.max_steps} 步`;
        }

        if (payload.type === "step") {
          const step = payload.data;
          const li = document.createElement("li");
          const observation = step.observation || {};
          li.innerHTML = `
            <div class="thought"><span class="badge ghost">${step.ordinal}</span> ${escapeHtml(step.thought)}</div>
            <div class="action">Action: ${escapeHtml(step.action)} ${escapeHtml(JSON.stringify(step.action_input))}</div>
            <div class="obs">${escapeHtml(JSON.stringify(observation).slice(0, 600))}</div>`;
          stepsList.appendChild(li);
          status.innerHTML = `已完成 ${step.ordinal} 步…`;
        }

        if (payload.type === "done") {
          const data = payload.data;
          el("agent-result").hidden = false;
          el("agent-conclusion").textContent = data.conclusion || "（无结论）";
          el("agent-proposals").innerHTML = (data.proposal_ids || []).map((id) => `
            <a class="row link-row" href="/proposals?space=${encodeURIComponent(space)}">
              <div class="row-main"><strong>提案 #${id}</strong><span class="muted small">等待人工确认</span></div>
              <div class="row-meta"><span class="badge warn">待确认</span></div>
            </a>`).join("");
          status.textContent = `${data.status} · ${data.tokens} tokens`;
          source.close();
        }

        if (payload.type === "error") {
          status.textContent = "出错：" + payload.message;
          source.close();
        }
      };

      source.onerror = () => { status.textContent = "连接中断"; source.close(); };
    });
  }
})();
