/* 原型交互脚本 · 仅做 Tab / 分段控件切换
 * 约束：零依赖、离线可用、不读写存储、不发请求。
 * 用法：
 *   <nav class="tabs2" data-tabs>
 *     <button class="tab2 is-active" data-tab="a" aria-selected="true">候选池</button>
 *     <button class="tab2" data-tab="b" aria-selected="false">观察池</button>
 *   </nav>
 *   <div data-tab-scope>
 *     <section data-panel="a">…</section>
 *     <section data-panel="b" hidden>…</section>
 *   </div>
 * 说明：data-tab-scope 可省略，缺省时以触发组件的父元素为查找范围。
 */
document.addEventListener("click", function (event) {
  var trigger = event.target.closest("[data-tab]");
  if (!trigger) return;

  var group = trigger.closest("[data-tabs]");
  if (!group) return;

  var key = trigger.getAttribute("data-tab");

  var items = group.querySelectorAll("[data-tab]");
  for (var i = 0; i < items.length; i++) {
    var isSelf = items[i] === trigger;
    items[i].classList.toggle("is-active", isSelf);
    items[i].setAttribute("aria-selected", isSelf ? "true" : "false");
  }

  var scope = group.closest("[data-tab-scope]") || group.parentElement;
  if (!scope) return;

  var panels = scope.querySelectorAll("[data-panel]");
  for (var j = 0; j < panels.length; j++) {
    panels[j].hidden = panels[j].getAttribute("data-panel") !== key;
  }

  event.preventDefault();
});

/* 键盘可达性：左右方向键在 tab 组内移动焦点 */
document.addEventListener("keydown", function (event) {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  var current = document.activeElement;
  if (!current || !current.hasAttribute("data-tab")) return;

  var group = current.closest("[data-tabs]");
  if (!group) return;

  var items = Array.prototype.slice.call(group.querySelectorAll("[data-tab]"));
  var index = items.indexOf(current);
  if (index < 0) return;

  var next = event.key === "ArrowRight" ? index + 1 : index - 1;
  if (next < 0) next = items.length - 1;
  if (next >= items.length) next = 0;

  items[next].focus();
  event.preventDefault();
});
