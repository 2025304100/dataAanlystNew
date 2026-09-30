import "@testing-library/jest-dom";

// antd 与部分浏览器 API 在 jsdom 中缺失，统一 mock
if (!window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (!window.ResizeObserver) {
  (window as any).ResizeObserver = ResizeObserverStub;
}

class IntersectionObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (!window.IntersectionObserver) {
  (window as any).IntersectionObserver = IntersectionObserverStub;
}

// ECharts 在 jsdom 中无法绘制 canvas，mock 为空函数避免报错
if (!window.HTMLCanvasElement.prototype.getContext) {
  window.HTMLCanvasElement.prototype.getContext = () => null as any;
}

// jsdom 未实现 Element.prototype.scrollIntoView，mock 为空函数
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// jsdom 未完整实现 window.getComputedStyle 的 2 参数形式（pseudoElt），
// antd 的 rc-util/Dom/scrollLocker 会调用 getComputedStyle(elt, pseudoElt) 并产生大量噪声。
// 用 polyfill 包装：忽略 pseudoElt 参数，委托给原始实现。
const originalGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = ((elt: Element, pseudoElt?: string | null) => {
  return originalGetComputedStyle(elt);
}) as typeof window.getComputedStyle;

// 抑制 antd 在 jsdom 下的已知警告噪声（Spin tip、Modal destroyOnClose/destroyOnHidden 等）
const originalConsoleWarn = console.warn;
const suppressedWarnPatterns = [
  "destroyOnClose is deprecated",
  "`destroyOnClose` is deprecated",
  "destroyOnHidden is deprecated",
  "`destroyOnHidden` is deprecated",
  "Spin `tip`",
  "Static function can not consume context",
  "The ticks may be not readable when set min: 0, max: 100 and alignTicks: true",
];
console.warn = (...args: unknown[]) => {
  const msg = String(args[0] ?? "");
  if (suppressedWarnPatterns.some((p) => msg.includes(p))) return;
  originalConsoleWarn(...(args as Parameters<typeof console.warn>));
};

// 抑制 jsdom "Not implemented" 噪声（getComputedStyle 已由上方 polyfill 解决，
// 但 antd 其他路径可能仍触发 not-implemented warning）
const originalConsoleError = console.error;
const suppressedErrorPatterns = ["Not implemented: window.getComputedStyle"];
console.error = (...args: unknown[]) => {
  const msg = String(args[0] ?? "");
  if (suppressedErrorPatterns.some((p) => msg.includes(p))) return;
  originalConsoleError(...(args as Parameters<typeof console.error>));
};

// ── 防止“api mock 抄漏方法 → 被组件 catch 吞掉 → 测试仍绿”的假绿 ──────────
//
// 背景（拟真走查实测）：前端测试绝大多数自己手写局部 api mock，要用几个写几个。
// 某测试挂的通知链路调 `api.getInboxNotifications`，而那份 mock 未声明该方法；
// 运行时就是 `... is not a function`，但组件用 try/catch 把它降级成了一行日志，
// 于是测试实际跑的是“取数失败时的降级路径”，却照样通过（851 全绿里看不出来）。
//
// 规则：把这类消息收集起来，afterEach 直接判失败 —— mock 缺方法必须让测试变红。
// 确实在测“接口不可用”分支的用例，调 __allowMissingApiMethod() 显式声明，
// 把隐式绕过变成显式选择。
const missingApiMethodMessages: string[] = [];
// 只抓“函数不存在”这一类；不抓普通断言错误与 React 告警，避免造噪声守卫
const MISSING_API_FN_RE = /\bis not a function\b/;
let allowMissingApiMethodOnce = false;

(globalThis as any).__allowMissingApiMethod = () => {
  allowMissingApiMethodOnce = true;
};

function recordMissingApiMethod(args: unknown[]): void {
  const text = args.map((a) => (typeof a === "string" ? a : String(a))).join(" ");
  if (MISSING_API_FN_RE.test(text) && !missingApiMethodMessages.includes(text)) {
    missingApiMethodMessages.push(text.slice(0, 300));
  }
}

const guardedConsoleError = console.error;
console.error = (...args: unknown[]) => {
  recordMissingApiMethod(args);
  return guardedConsoleError(...args);
};
const guardedConsoleWarn = console.warn;
console.warn = (...args: unknown[]) => {
  recordMissingApiMethod(args);
  return guardedConsoleWarn(...args);
};

afterEach(() => {
  const collected = missingApiMethodMessages.splice(0);
  const allowed = allowMissingApiMethodOnce;
  allowMissingApiMethodOnce = false;
  if (collected.length === 0 || allowed) return;
  throw new Error(
    "测试中出现 “xxx is not a function”：通常是本用例的 api mock 没声明该方法，\n"
    + "而组件把它当成普通错误记了一行日志 —— 于是测试验证的是“取数失败时的降级”，不是真实行为。\n"
    + "修法：在 mock 里补上这个方法（及返回值）；若确实在测异常分支，\n"
    + "调 (globalThis as any).__allowMissingApiMethod() 显式声明。\n"
    + "捕获到的消息：\n  - " + collected.join("\n  - ")
  );
});
