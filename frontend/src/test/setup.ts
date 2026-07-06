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
