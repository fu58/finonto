/* editor.js —— Wiki 编辑器增强：Tab 缩进、Ctrl+S 保存、离开提醒
 *
 * 只做三件事，保持「编辑器就是一个 textarea」的可控性。
 */
(function () {
  "use strict";

  const editor = document.getElementById("wiki-editor");
  if (!editor) return;

  let dirty = false;
  const initial = editor.value;

  editor.addEventListener("input", () => { dirty = editor.value !== initial; });

  editor.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      event.preventDefault();
      const start = editor.selectionStart;
      const end = editor.selectionEnd;
      editor.setRangeText("  ", start, end, "end");
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      const form = editor.closest("form");
      if (form) form.requestSubmit();
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  const form = editor.closest("form");
  if (form) {
    form.addEventListener("submit", () => { dirty = false; });
  }
})();
