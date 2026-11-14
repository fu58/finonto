/* graph.js —— 图谱交互：平移、缩放、点击节点查看邻域
 *
 * 布局坐标由服务端（Python + numpy）算好，这里只改 transform —— 所以「同一张图坐标稳定」，
 * 也意味着前端没有布局算法、没有第三方库。
 */
(function () {
  "use strict";

  const wrap = document.getElementById("graph-wrap");
  if (!wrap) return;
  const svg = wrap.querySelector("svg");
  if (!svg) return;

  let scale = 1;
  let tx = 0;
  let ty = 0;
  let dragging = false;
  let startX = 0;
  let startY = 0;
  let originX = 0;
  let originY = 0;

  function apply() {
    svg.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
  }

  wrap.addEventListener("pointerdown", (event) => {
    if (event.target.closest("circle")) return; // 点节点不拖拽
    dragging = true;
    startX = event.clientX;
    startY = event.clientY;
    originX = tx;
    originY = ty;
    wrap.setPointerCapture(event.pointerId);
  });

  wrap.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    tx = originX + (event.clientX - startX);
    ty = originY + (event.clientY - startY);
    apply();
  });

  wrap.addEventListener("pointerup", (event) => {
    dragging = false;
    try { wrap.releasePointerCapture(event.pointerId); } catch (e) { /* noop */ }
  });

  wrap.addEventListener("wheel", (event) => {
    event.preventDefault();
    const delta = event.deltaY > 0 ? 0.92 : 1.08;
    const next = Math.min(4, Math.max(0.35, scale * delta));
    // 以鼠标位置为中心缩放
    const rect = wrap.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    tx = px - ((px - tx) * next) / scale;
    ty = py - ((py - ty) * next) / scale;
    scale = next;
    apply();
  }, { passive: false });

  // 点击节点 → 邻域子图
  svg.addEventListener("click", (event) => {
    const circle = event.target.closest("circle");
    if (!circle) return;
    const nodeId = circle.dataset.id;
    if (!nodeId) return;
    const space = wrap.dataset.space || "0";
    location.href = `/graph?space=${encodeURIComponent(space)}&mode=neighborhood&node=${encodeURIComponent(nodeId)}&depth=1`;
  });

  // hover 高亮
  svg.addEventListener("mouseover", (event) => {
    const circle = event.target.closest("circle");
    if (!circle) return;
    circle.setAttribute("stroke", "#c0caf5");
    circle.setAttribute("stroke-width", "2");
  });
  svg.addEventListener("mouseout", (event) => {
    const circle = event.target.closest("circle");
    if (!circle) return;
    circle.removeAttribute("stroke");
    circle.removeAttribute("stroke-width");
  });

  apply();
})();
