// WP5.1：研究情景参数面板
//
// 设计说明：
// - 任务清单 #6：研究情景参数（正式风控读 PortfolioRule，本组件只展示研究情景参数）
// - 从 InvestmentCenter.tsx 抽出 renderRiskDashboard JSX 块
// - 风险指标计算（rrRatio/stopDistancePct/concentrationLevel 等）保留在 Shell
// - riskSettings 持久化逻辑（localStorage）保留在 Shell
//
// 关键约束：
// - 正式风控读 PortfolioRule，本组件只展示研究情景参数
// - 不重写风控规则（ATR 倍数/集中度阈值/最大亏损比例算法不动）
// - riskSettings 通过 props 传入，更新通过 onUpdateSettings 回调
//
// WP5.2：
// - 标注"研究情景参数" Tag + 提示语（不再冒充组合风控）
// - 新增"前往组合风控配置"按钮，跳转到组合 PortfolioRule 配置
import { Button, InputNumber, Progress, Tag } from "antd";
import { t } from "../../i18n";
import { money, percent, score } from "../../utils/format";
import type { RiskReferencePanelProps } from "./types";

/**
 * 研究情景参数面板：展示风险指标 + 研究情景参数设置。
 * - riskMetrics 数据由 Shell 计算生成
 * - riskSettings 通过 props 传入，支持就地编辑
 * - WP5.2：明确标注"研究情景参数"，提供跳转到组合 PortfolioRule 入口
 */
export default function RiskReferencePanel({
  riskMetrics,
  riskSettings,
  settingsOpen,
  entryPrice,
  quantity,
  setup,
  portfolioId,
  onToggleSettings,
  onUpdateSettings,
  onGoToPortfolioRule,
}: RiskReferencePanelProps) {
  if (!riskMetrics) return null;
  const rm = riskMetrics;
  const concColor =
    rm.concentrationLevel === "high"
      ? "#b42318"
      : rm.concentrationLevel === "medium"
        ? "#f59e0b"
        : "#0f766e";
  const concLabel =
    rm.concentrationLevel === "high"
      ? t("riskHigh")
      : rm.concentrationLevel === "medium"
        ? t("riskMedium")
        : t("riskLow");
  const rrColor = rm.rrRatio >= 2 ? "#0f766e" : rm.rrRatio >= 1 ? "#f59e0b" : "#b42318";
  const stopColor =
    rm.currentStopDist <= 5 ? "#b42318" : rm.currentStopDist <= 10 ? "#f59e0b" : "#0f766e";

  return (
    <section className="ic__section ic__risk-section symbol-research-risk-reference">
      <div className="panel">
        <div className="detail-card-head">
          <h3>
            {t("riskDashboard")}
            <Tag
              color="blue"
              style={{ marginLeft: 8, fontSize: 11 }}
              data-testid="research-only-params-tag"
            >
              {t("symbolResearchResearchOnlyParams")}
            </Tag>
          </h3>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Button
              size="small"
              type="default"
              onClick={onGoToPortfolioRule}
              disabled={!portfolioId || !onGoToPortfolioRule}
              data-testid="go-to-portfolio-rule-button"
            >
              {t("symbolResearchGoToPortfolioRule")}
            </Button>
            <Button size="small" onClick={onToggleSettings}>
              {t("icRiskSettings")}
            </Button>
          </div>
        </div>
        <div
          className="item-subline"
          style={{ marginTop: 4, marginBottom: 8, fontSize: 12 }}
          data-testid="research-only-hint"
        >
          <Tag color="default" style={{ marginRight: 4 }}>
            {t("symbolResearchRiskSettingsLegacyTag")}
          </Tag>
          {t("symbolResearchResearchOnlyHint")}
        </div>
        {settingsOpen && (
          <div className="ic__settings-grid ic__settings-grid--compact">
            <label>
              <span>{t("icAtrMultiplier")}</span>
              <InputNumber
                size="small"
                min={0.5}
                max={5}
                step={0.5}
                value={riskSettings.atrMultiplier}
                onChange={(v) => onUpdateSettings({ atrMultiplier: Number(v ?? 2) })}
              />
            </label>
            <label>
              <span>{t("icMediumPosition")}</span>
              <InputNumber
                size="small"
                min={0}
                max={100}
                value={riskSettings.concentrationMediumPct}
                onChange={(v) =>
                  onUpdateSettings({ concentrationMediumPct: Number(v ?? 10) })
                }
              />
            </label>
            <label>
              <span>{t("icHighPosition")}</span>
              <InputNumber
                size="small"
                min={0}
                max={100}
                value={riskSettings.concentrationHighPct}
                onChange={(v) =>
                  onUpdateSettings({ concentrationHighPct: Number(v ?? 20) })
                }
              />
            </label>
            <label>
              <span>{t("icMaxLossPct")}</span>
              <InputNumber
                size="small"
                min={0}
                max={100}
                step={0.5}
                value={riskSettings.maxLossPct}
                onChange={(v) => onUpdateSettings({ maxLossPct: Number(v ?? 2) })}
              />
            </label>
          </div>
        )}
        <div className="ic__risk-grid">
          {/* 盈亏比 */}
          <div className="ic__risk-card">
            <span className="ic__risk-label">{t("riskRewardRatio")}</span>
            <span className="ic__risk-value" style={{ color: rrColor }}>
              {rm.rrRatio >= 0 ? rm.rrRatio.toFixed(2) : "-"}
              <small>:1</small>
            </span>
            <div className="ic__risk-detail">
              <span>
                {t("reward")}: +{percent(rm.reward / entryPrice)}
              </span>
              <span>
                {t("risk")}: -{percent(rm.riskAmount / entryPrice)}
              </span>
            </div>
          </div>

          {/* 止损距离 */}
          <div className="ic__risk-card">
            <span className="ic__risk-label">{t("stopLossDistance")}</span>
            <span className="ic__risk-value" style={{ color: stopColor }}>
              {rm.currentStopDist.toFixed(1)}%
            </span>
            <div className="ic__risk-detail">
              <span>
                {t("icCurrent")}: {score(rm.currentPrice)}
              </span>
              <span>
                {t("icStopLossLabel")}: {score(rm.stop)}
              </span>
              {rm.atrStopRef != null && (
                <span>
                  {riskSettings.atrMultiplier}×ATR: {score(rm.atrStopRef)}
                </span>
              )}
            </div>
          </div>

          {/* 仓位集中度 */}
          <div className="ic__risk-card">
            <span className="ic__risk-label">{t("positionConcentration")}</span>
            <span className="ic__risk-value" style={{ color: concColor }}>
              {percent(setup?.recommended_position_pct ?? 0)}
            </span>
            <div className="ic__risk-bar">
              <Progress
                percent={Math.min(
                  100,
                  ((setup?.recommended_position_pct ?? 0) * 100 /
                    Math.max(riskSettings.concentrationHighPct, 1)) *
                    100,
                )}
                size="small"
                strokeColor={concColor}
                showInfo={false}
              />
              <span className="ic__risk-level">{concLabel}</span>
            </div>
          </div>

          {/* 单笔最大亏损 */}
          <div className="ic__risk-card">
            <span className="ic__risk-label">{t("maxLossPerTrade")}</span>
            <span
              className="ic__risk-value"
              style={{
                color:
                  rm.maxLossLimitAmount > 0 && rm.maxLossAmount > rm.maxLossLimitAmount
                    ? "#b42318"
                    : rm.maxLossAmount > 0
                      ? "#f59e0b"
                      : "#6b7280",
              }}
            >
              {rm.maxLossAmount > 0 ? `-¥${rm.maxLossAmount.toFixed(0)}` : "-"}
            </span>
            <div className="ic__risk-detail">
              <span>
                {t("icLossPerShare")}: {score(rm.maxLossPerShare)}
              </span>
              <span>
                {t("icQuantityLabel")}: {quantity}
              </span>
              {rm.maxLossLimitAmount > 0 && (
                <span>
                  {t("icUpperLimit")}: {money(rm.maxLossLimitAmount)}
                </span>
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
