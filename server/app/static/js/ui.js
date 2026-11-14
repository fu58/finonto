/* ui.js —— 交互协议（约 150 行，替代 HTMX）
 *
 * 规则（模板里只写 data-* 属性）：
 *   data-autosubmit           select/input 变化后自动提交所在表单
 *   data-post="URL"           form 提交或按钮点击 → fetch POST → 提示结果
 *   data-get="URL"            按钮点击 → fetch GET JSON
 *   data-target="#sel"        结果渲染到这个元素
 *   data-render="#sel"        同上（组件页用）
 *   data-dialog-open="#id"    打开 <dialog>
 *   data-dialog-close         关闭最近的 <dialog>
 *   data-confirm="文案"       提交前确认
 *
 * 没有 JS 时：普通 <form method="post"> 仍能工作（服务端 303 跳转），这是有意保留的降级路径。
 */
(function () {
  "use strict";

  const FinOnto = {
    toast(message, kind) {
      const root = document.getElementById("toast-root");
      if (!root) return;
      const node = document.createElement("div");
      node.className = "toast " + (kind || "");
      node.textContent = message;
      root.appendChild(node);
      setTimeout(() => node.remove(), 4200);
    },

    async request(url, options) {
      const response = await fetch(url, options);
      const type = response.headers.get("content-type") || "";
      const payload = type.includes("application/json") ? await response.json() : await response.text();
      if (!response.ok) {
        const detail = payload && payload.detail ? payload.detail : "请求失败";
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return payload;
    },

    render(target, data) {
      if (!target) return;
      if (target.tagName === "PRE") {
        target.hidden = false;
        target.textContent = JSON.stringify(data, null, 2);
        return;
      }
      if (data && data.violation !== undefined) {
        renderValidation(target, data);
        return;
      }
      if (data && data.ok !== undefined) {
        target.innerHTML =
          '<div class="row"><span class="badge ' + (data.ok ? "ok" : "err") + '">' +
          (data.ok ? "OK" : "FAIL") + "</span><span>" +
          escapeHtml(String(data.detail || "")) + "</span></div>";
        return;
      }
      target.innerHTML = "<pre class='code-block tiny'>" + escapeHtml(JSON.stringify(data, null, 2)) + "</pre>";
    },
  };

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderValidation(target, data) {
    const levels = [
      ["violation", "err", "违规"],
      ["warning", "warn", "警告"],
      ["info", "info", "提示"],
    ];
    const parts = [];
    levels.forEach(([key, cls, label]) => {
      const items = data[key] || [];
      if (!items.length) return;
      parts.push(`<h4>${label}（${items.length}）</h4>`);
      items.forEach((item) => {
        parts.push(
          `<div class="row"><div class="row-main"><span class="badge ${cls}">${label}</span> ` +
          `${escapeHtml(item.type_label || "")}：${escapeHtml(item.message || "")}</div></div>`
        );
      });
    });
    target.innerHTML = parts.join("") || '<p class="muted">校验通过，没有问题。</p>';
  }

  // ---- 打开 / 关闭对话框 ----
  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-dialog-open]");
    if (opener) {
      event.preventDefault();
      const dialog = document.querySelector(opener.dataset.dialogOpen);
      if (dialog) dialog.showModal();
      return;
    }
    if (event.target.closest("[data-dialog-close]")) {
      event.preventDefault();
      const dialog = event.target.closest("dialog");
      if (dialog) dialog.close();
    }
  });

  // ---- GET 拉取并渲染 ----
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-get]");
    if (!button) return;
    event.preventDefault();
    const targetSelector = button.dataset.target || button.dataset.render;
    const target = targetSelector ? (targetSelector.startsWith("#") ? document.querySelector(targetSelector) : document.getElementById(targetSelector)) : null;
    button.disabled = true;
    try {
      const data = await FinOnto.request(button.dataset.get);
      FinOnto.render(target, data);
    } catch (error) {
      FinOnto.toast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  });

  // ---- POST 按钮（非表单） ----
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-post]");
    if (!button || button.closest("form")) return;
    event.preventDefault();
    if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
    button.disabled = true;
    try {
      const data = await FinOnto.request(button.dataset.post, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      FinOnto.toast(typeof data === "object" ? (data.status || "完成") : "完成", "ok");
      const target = button.dataset.target ? document.querySelector(button.dataset.target) : null;
      FinOnto.render(target, data);
      if (button.dataset.reload !== "false") setTimeout(() => location.reload(), 500);
    } catch (error) {
      FinOnto.toast(error.message, "err");
    } finally {
      button.disabled = false;
    }
  });

  // ---- 表单提交 ----
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const submitter = event.submitter || form;
    const confirmText = (submitter && submitter.dataset.confirm) || form.dataset.confirm;
    if (confirmText && !window.confirm(confirmText)) {
      event.preventDefault();
      return;
    }
    const action = form.dataset.post;
    if (!action) return; // 走原生提交（服务端 303）
    event.preventDefault();
    const submitButton = submitter.querySelector ? submitter : form.querySelector("button[type=submit]");
    if (submitButton) submitButton.disabled = true;
    try {
      const body = new FormData(form);
      const response = await fetch(action, { method: "POST", body });
      const type = response.headers.get("content-type") || "";
      if (!response.ok) {
        const payload = type.includes("json") ? await response.json() : await response.text();
        throw new Error(payload.detail || "提交失败");
      }
      FinOnto.toast("已提交", "ok");
      if (form.dataset.reload !== "false") setTimeout(() => location.assign(form.dataset.next || location.href), 400);
    } catch (error) {
      FinOnto.toast(error.message, "err");
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  });

  // ---- 选择即提交 ----
  document.addEventListener("change", (event) => {
    const control = event.target.closest("[data-autosubmit]");
    if (control && control.form) control.form.submit();
  });

  // ---- 引用角标联动 ----
  document.addEventListener("click", (event) => {
    const cite = event.target.closest(".cite");
    if (!cite) return;
    const tag = cite.dataset.tag;
    document.querySelectorAll(".citation").forEach((node) => {
      node.classList.toggle("hit", node.dataset.tag === tag);
    });
    const target = document.querySelector(`.citation[data-tag="${tag}"]`);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
  });

  // ---- 轮询（组件探针、健康状态这类「隔几秒自己更新」的卡片） ----
  function initPollers() {
    document.querySelectorAll("[data-poll]").forEach((node) => {
      const interval = Math.max(1000, parseInt(node.dataset.interval || "5000", 10));
      const tick = async () => {
        try {
          const data = await FinOnto.request(node.dataset.poll);
          FinOnto.render(node, data);
        } catch (error) {
          /* 轮询失败静默：内网抖动不该刷屏 toast */
        }
      };
      tick();
      setInterval(tick, interval);
    });
  }

  window.FinOnto = FinOnto;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initPollers);
  } else {
    initPollers();
  }
})();
