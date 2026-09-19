// WP5.1：单标的回测面板
//
// 设计说明：
// - 任务清单 #9：事件驱动回测 + 规则模板 + 成本配置 + 结果详情 + 结果应用到组合
// - 直接复用 BacktestConfig + BacktestResult 现有组件，不重写回测引擎
// - 状态（backtestResult）留在 SymbolResearchShell，通过 props 传入
// - sourceContext 用于后续 WP5.3 结果保存来源追溯（当前仅透传，不发送请求）
//
// WP5.2：
// - 单股回测结果保存来源上下文（source_context）到 localStorage
//   后端 BacktestRunRequest 暂不接受 source_context，前端持久化以备后续追溯
// - 在结果详情显示"来源：标的研究" Tag，便于区分来自组合回测还是研究页
import { Button, Tag } from "antd";
import { t } from "../../i18n";
import BacktestConfig from "../BacktestConfig";
import BacktestResult from "../BacktestResult";
import type { SingleSymbolBacktestPanelProps } from "./types";

// localStorage key：保存最近一次单股回测的来源上下文
const BACKTEST_SOURCE_CONTEXT_KEY = "ic_backtest_source_context";

/**
 * 单标的回测面板：复用 BacktestConfig + BacktestResult 组件。
 * - 不重写回测引擎、规则模板、成本配置逻辑
 * - 通过 sourceContext 透传来源上下文（供 WP5.3 结果保存追溯）
 * - WP5.2：回测启动时持久化 source_context 到 localStorage（fallback，后端暂不接收）
 */
export default function SingleSymbolBacktestPanel({
  portfolioId,
  activeSymbolId,
  sourceContext,
  backtestResult,
  onSetBacktestResult,
  onAppliedToPortfolio,
}: SingleSymbolBacktestPanelProps) {
  if (!portfolioId) return null;

  // WP5.2：包装 onSetBacktestResult，回测完成时同步持久化 source_context 到 localStorage
  const handleSetBacktestResult = (result: import("../../types").BacktestRun | null) => {
    if (result) {
      // 构造 source_context（标的研究入口）
      const ctx = {
        symbol_id: activeSymbolId ?? null,
        source_type: "research" as const,
        source_id: null,
        portfolio_id: portfolioId ?? null,
        return_to: "research" as const,
        run_id: result.id,
        run_name: result.run_name,
        saved_at: new Date().toISOString(),
      };
      try {
        localStorage.setItem(BACKTEST_SOURCE_CONTEXT_KEY, JSON.stringify(ctx));
      } catch {
        /* ignore quota errors */
      }
    }
    onSetBacktestResult(result);
  };

  return (
    <section
      className="ic__section ic__backtest-section symbol-research-backtest"
      data-source-type={sourceContext?.source_type ?? ""}
      data-source-id={sourceContext?.source_id ?? ""}
    >
      <div className="panel">
        <div className="detail-card-head">
          <h3>
            {t("backtestValidation")}
            <Tag
              color="blue"
              style={{ marginLeft: 8, fontSize: 11 }}
              data-testid="backtest-source-tag"
            >
              {t("symbolResearchBacktestSourceLabel")}: {t("symbolResearchBacktestSourceResearch")}
            </Tag>
          </h3>
          <div className="detail-actions">
            {backtestResult && (
              <Button size="small" onClick={() => handleSetBacktestResult(null)}>
                {t("clear")}
              </Button>
            )}
          </div>
        </div>
        <div className="ic__backtest-layout">
          <BacktestConfig
            portfolioId={portfolioId}
            activeSymbolId={activeSymbolId}
            onResult={handleSetBacktestResult}
          />
          <BacktestResult
            result={backtestResult}
            portfolioId={portfolioId}
            onAppliedToPortfolio={onAppliedToPortfolio}
          />
        </div>
      </div>
    </section>
  );
}
