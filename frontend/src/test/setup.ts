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
