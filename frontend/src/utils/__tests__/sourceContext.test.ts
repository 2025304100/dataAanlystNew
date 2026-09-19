// WP5.3：来源上下文工具单元测试
//
// 覆盖：
// - parseSourceContextFromQuery：URL query params 解析与旧路由兼容
// - normalizeSourceContext：默认值补全与枚举校验
// - resolveSourceContext：prop > URL > sessionStorage > 默认 legacy 优先级
// - saveReturnState / peekReturnState / popReturnState：返回状态持久化与一次性消费
// - tabForReturnTo：return_to 到 activeTab 的映射
// - buildSourceContext：工厂函数
// - clearSourceContext：清理
// - navigateToResearch：跳转主流程（保存滚动位置 + 写入 SourceContext + 加载标的 + 切换 tab）
//
// 约束：
// - jsdom 提供 sessionStorage / window.location / window.scrollTo
// - 每个 it 前清理 sessionStorage 与 location.search，避免用例间状态污染
import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  SOURCE_CONTEXT_STORAGE_KEY,
  RETURN_STATE_STORAGE_KEY_PREFIX,
  parseSourceContextFromQuery,
  normalizeSourceContext,
  readSourceContext,
  writeSourceContext,
  clearSourceContext,
  resolveSourceContext,
  saveReturnState,
  peekReturnState,
  popReturnState,
  tabForReturnTo,
  buildSourceContext,
  navigateToResearch,
} from "../sourceContext";

describe("WP5.3 sourceContext 工具", () => {
  beforeEach(() => {
    sessionStorage.clear();
    // 重置 location.search 到无 SourceContext 状态
    window.history.replaceState(null, "", "/");
    vi.restoreAllMocks();
  });

  // 1. URL query params 解析（含完整 SourceContext 字段）
  describe("parseSourceContextFromQuery", () => {
    it("完整 query params 解析为 SourceContext", () => {
      const search = "?symbol_id=42&source_type=candidate&source_id=7&portfolio_id=3&return_to=observation";
      const ctx = parseSourceContextFromQuery(search);
      expect(ctx).not.toBeNull();
      expect(ctx?.symbol_id).toBe(42);
      expect(ctx?.source_type).toBe("candidate");
      expect(ctx?.source_id).toBe(7);
      expect(ctx?.portfolio_id).toBe(3);
      expect(ctx?.return_to).toBe("observation");
    });

    it("旧路由 ?symbol=X 兼容为 legacy source_type", () => {
      const search = "?symbol=600519";
      const ctx = parseSourceContextFromQuery(search);
      expect(ctx).not.toBeNull();
      expect(ctx?.source_type).toBe("legacy");
      expect(ctx?.return_to).toBe("research");
      // 旧路由不携带 symbol_id（仅 symbol 字符串）
      expect(ctx?.symbol_id).toBeUndefined();
    });

    it("无相关参数时返回 null", () => {
      const search = "?tab=portfolio";
      const ctx = parseSourceContextFromQuery(search);
      expect(ctx).toBeNull();
    });

    it("非法 source_type 回退到默认 legacy", () => {
      const search = "?symbol_id=1&source_type=invalid_type";
      const ctx = parseSourceContextFromQuery(search);
      expect(ctx?.source_type).toBe("legacy");
    });

    it("非法 return_to 回退到默认 research", () => {
      const search = "?symbol_id=1&source_type=candidate&return_to=invalid_return";
      const ctx = parseSourceContextFromQuery(search);
      expect(ctx?.return_to).toBe("research");
    });
  });

  // 2. normalizeSourceContext 默认值补全
  describe("normalizeSourceContext", () => {
    it("null/undefined 返回默认 legacy 上下文", () => {
      const ctx = normalizeSourceContext(null);
      expect(ctx.source_type).toBe("legacy");
      expect(ctx.return_to).toBe("research");
      expect(ctx.source_id).toBeUndefined();
      expect(ctx.portfolio_id).toBeUndefined();
    });

    it("非法 source_type 回退到 legacy", () => {
      const ctx = normalizeSourceContext({ source_type: "invalid" as any, return_to: "candidate" });
      expect(ctx.source_type).toBe("legacy");
      expect(ctx.return_to).toBe("candidate");
    });

    it("保留有效字段并补全可选字段为 undefined", () => {
      const ctx = normalizeSourceContext({
        symbol_id: 99,
        source_type: "observation",
        return_to: "observation",
      });
      expect(ctx.symbol_id).toBe(99);
      expect(ctx.source_type).toBe("observation");
      expect(ctx.source_id).toBeUndefined();
      expect(ctx.portfolio_id).toBeUndefined();
      expect(ctx.return_to).toBe("observation");
    });
  });

  // 3. resolveSourceContext 优先级：prop > URL > sessionStorage > 默认
  describe("resolveSourceContext", () => {
    it("prop 优先级最高", () => {
      // 同时设置 URL 与 sessionStorage，prop 应胜出
      window.history.replaceState(null, "", "/?symbol_id=1&source_type=candidate");
      writeSourceContext({
        symbol_id: 2,
        source_type: "observation",
        return_to: "observation",
      });
      const resolved = resolveSourceContext({
        symbol_id: 999,
        source_type: "portfolio_member",
        source_id: 10,
        portfolio_id: 5,
        return_to: "portfolio",
      });
      expect(resolved.symbol_id).toBe(999);
      expect(resolved.source_type).toBe("portfolio_member");
      expect(resolved.source_id).toBe(10);
      expect(resolved.portfolio_id).toBe(5);
      expect(resolved.return_to).toBe("portfolio");
    });

    it("URL 优先级高于 sessionStorage", () => {
      window.history.replaceState(null, "", "/?symbol_id=1&source_type=candidate&return_to=candidate");
      writeSourceContext({
        symbol_id: 2,
        source_type: "observation",
        return_to: "observation",
      });
      const resolved = resolveSourceContext(null);
      expect(resolved.symbol_id).toBe(1);
      expect(resolved.source_type).toBe("candidate");
      expect(resolved.return_to).toBe("candidate");
    });

    it("sessionStorage 优先级高于默认值", () => {
      // 无 URL，使用 sessionStorage
      const resolved = resolveSourceContext(null);
      // sessionStorage 为空时回退到默认 legacy
      expect(resolved.source_type).toBe("legacy");
      expect(resolved.return_to).toBe("research");

      // 写入 sessionStorage 后应被读取
      writeSourceContext({
        symbol_id: 42,
        source_type: "position",
        return_to: "portfolio",
      });
      const resolved2 = resolveSourceContext(null);
      expect(resolved2.symbol_id).toBe(42);
      expect(resolved2.source_type).toBe("position");
      expect(resolved2.return_to).toBe("portfolio");
    });

    it("三者都为空时回退到默认 legacy 上下文", () => {
      const resolved = resolveSourceContext(null);
      expect(resolved.source_type).toBe("legacy");
      expect(resolved.return_to).toBe("research");
    });
  });

  // 4. saveReturnState / peekReturnState / popReturnState
  describe("返回状态持久化", () => {
    it("saveReturnState + peekReturnState 不清理数据", () => {
      saveReturnState("portfolio", { scrollY: 200, statusFilter: "active" });
      const peeked = peekReturnState("portfolio");
      expect(peeked).not.toBeNull();
      expect(peeked?.scrollY).toBe(200);
      expect(peeked?.statusFilter).toBe("active");
      // 再次 peek 仍可读取
      const peekedAgain = peekReturnState("portfolio");
      expect(peekedAgain).not.toBeNull();
    });

    it("popReturnState 一次性消费后清理", () => {
      saveReturnState("observation", { scrollY: 500 });
      const popped = popReturnState("observation");
      expect(popped?.scrollY).toBe(500);
      // 再次 pop 应为 null
      const poppedAgain = popReturnState("observation");
      expect(poppedAgain).toBeNull();
    });

    it("不同 return_to 分桶存储互不干扰", () => {
      saveReturnState("candidate", { scrollY: 100 });
      saveReturnState("portfolio", { scrollY: 300 });
      expect(popReturnState("candidate")?.scrollY).toBe(100);
      expect(popReturnState("portfolio")?.scrollY).toBe(300);
    });

    it("不存在的 return_to 返回 null", () => {
      expect(peekReturnState("nonexistent")).toBeNull();
      expect(popReturnState("nonexistent")).toBeNull();
    });
  });

  // 5. tabForReturnTo 映射
  describe("tabForReturnTo", () => {
    it("candidate 与 observation 映射到 opportunity tab", () => {
      expect(tabForReturnTo("candidate")).toBe("opportunity");
      expect(tabForReturnTo("observation")).toBe("opportunity");
    });

    it("portfolio 映射到 portfolio tab", () => {
      expect(tabForReturnTo("portfolio")).toBe("portfolio");
    });

    it("alert 映射到 decision tab", () => {
      expect(tabForReturnTo("alert")).toBe("decision");
    });

    it("backtest 与 research 映射到 investment tab", () => {
      expect(tabForReturnTo("backtest")).toBe("investment");
      expect(tabForReturnTo("research")).toBe("investment");
    });

    it("home 映射到 decision tab", () => {
      expect(tabForReturnTo("home")).toBe("decision");
    });

    it("未知 return_to 兜底为 decision", () => {
      expect(tabForReturnTo("unknown_value")).toBe("decision");
    });
  });

  // 6. buildSourceContext 工厂
  describe("buildSourceContext", () => {
    it("构造完整 SourceContext", () => {
      const ctx = buildSourceContext({
        symbol_id: 100,
        source_type: "portfolio_member",
        source_id: 5,
        portfolio_id: 2,
        return_to: "portfolio",
      });
      expect(ctx.symbol_id).toBe(100);
      expect(ctx.source_type).toBe("portfolio_member");
      expect(ctx.source_id).toBe(5);
      expect(ctx.portfolio_id).toBe(2);
      expect(ctx.return_to).toBe("portfolio");
    });

    it("可选字段缺省时补 undefined", () => {
      const ctx = buildSourceContext({
        source_type: "manual",
        return_to: "research",
      });
      expect(ctx.symbol_id).toBeUndefined();
      expect(ctx.source_id).toBeUndefined();
      expect(ctx.portfolio_id).toBeUndefined();
    });
  });

  // 7. clearSourceContext 清理
  describe("clearSourceContext", () => {
    it("写入后清理使 sessionStorage 不再包含 SourceContext", () => {
      writeSourceContext({
        symbol_id: 1,
        source_type: "candidate",
        return_to: "candidate",
      });
      expect(sessionStorage.getItem(SOURCE_CONTEXT_STORAGE_KEY)).not.toBeNull();
      clearSourceContext();
      expect(sessionStorage.getItem(SOURCE_CONTEXT_STORAGE_KEY)).toBeNull();
    });
  });

  // 8. navigateToResearch 主流程
  describe("navigateToResearch", () => {
    it("保存滚动位置 + 写入 SourceContext + 加载标的 + 切换 tab", () => {
      const scrollSpy = vi.spyOn(window, "scrollY", "get").mockReturnValue(450);
      const loadSymbolDetail = vi.fn(async () => undefined);
      const setActiveTab = vi.fn();
      const ctx = {
        loadSymbolDetail,
        setActiveTab,
        activeTab: "portfolio", // 当前不在 investment，应触发切换
      };

      navigateToResearch(
        ctx,
        {
          symbol_id: 42,
          source_type: "candidate",
          source_id: 7,
          portfolio_id: undefined,
          return_to: "candidate",
        },
        {
          returnState: { statusFilter: "active" },
        },
      );

      // 1. SourceContext 已写入 sessionStorage
      const written = readSourceContext();
      expect(written).not.toBeNull();
      expect(written?.symbol_id).toBe(42);
      expect(written?.source_type).toBe("candidate");
      expect(written?.source_id).toBe(7);

      // 2. 返回状态已写入（含 scrollY 与额外字段）
      const saved = peekReturnState("candidate");
      expect(saved?.scrollY).toBe(450);
      expect(saved?.statusFilter).toBe("active");

      // 3. 已加载标的
      expect(loadSymbolDetail).toHaveBeenCalledWith(42, { focus: true, barLimit: 500 });

      // 4. 已切换到 investment tab
      expect(setActiveTab).toHaveBeenCalledWith("investment");

      scrollSpy.mockRestore();
    });

    it("当前已在 investment tab 时不重复 setActiveTab", () => {
      const scrollSpy = vi.spyOn(window, "scrollY", "get").mockReturnValue(0);
      const loadSymbolDetail = vi.fn(async () => undefined);
      const setActiveTab = vi.fn();
      const ctx = {
        loadSymbolDetail,
        setActiveTab,
        activeTab: "investment", // 已在 investment
      };

      navigateToResearch(ctx, {
        symbol_id: 1,
        source_type: "manual",
        return_to: "research",
      });

      expect(setActiveTab).not.toHaveBeenCalled();
      scrollSpy.mockRestore();
    });

    it("symbol_id 缺省时不调用 loadSymbolDetail", () => {
      const scrollSpy = vi.spyOn(window, "scrollY", "get").mockReturnValue(0);
      const loadSymbolDetail = vi.fn(async () => undefined);
      const setActiveTab = vi.fn();
      const ctx = { loadSymbolDetail, setActiveTab, activeTab: "opportunity" };

      navigateToResearch(ctx, {
        source_type: "search",
        return_to: "research",
      });

      expect(loadSymbolDetail).not.toHaveBeenCalled();
      // 但 SourceContext 仍写入 sessionStorage
      expect(readSourceContext()?.source_type).toBe("search");
      scrollSpy.mockRestore();
    });
  });

  // 9. sessionStorage 异常容错
  describe("sessionStorage 异常容错", () => {
    it("readSourceContext 在 JSON.parse 失败时返回 null", () => {
      sessionStorage.setItem(SOURCE_CONTEXT_STORAGE_KEY, "{invalid json");
      expect(readSourceContext()).toBeNull();
    });

    it("peekReturnState 在 JSON.parse 失败时返回 null", () => {
      sessionStorage.setItem(`${RETURN_STATE_STORAGE_KEY_PREFIX}test`, "{broken");
      expect(peekReturnState("test")).toBeNull();
    });
  });
});
