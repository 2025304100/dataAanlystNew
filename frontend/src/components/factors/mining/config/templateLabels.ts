import type { FactorTemplate } from "../../../../api/factorTemplates";
import { t } from "../../../../i18n";

// System template IDs are generated during seeding. These exact system names
// identify the immutable presets from initial_population.CLASSIC_TEMPLATES.
const SYSTEM_PRESETS: Array<[string, string, string]> = [
  ["MaTrend", "均线趋势", "短期均线上穿长期均线，捕捉趋势启动"],
  ["PriceMomentum", "价格动量", "过去 N 日涨跌幅，捕捉价格动量"],
  ["ChannelBreakout", "通道突破", "价格在 N 日通道中的位置，突破上沿看多"],
  ["Macd", "MACD", "快慢均线差值，趋势强度指标"],
  ["MaSlope", "均线斜率", "短期均线相对长期均线的斜率，趋势加速度"],
  ["ShortReversal", "短期反转", "过去 N 日跌幅越大，未来反弹概率越高"],
  ["OverboughtOversold", "超买超卖", "类似 RSI，超卖（<0）看多、超买（>0）看空"],
  ["MeanReversion", "均值回归", "价格偏离均线越远，回归概率越高"],
  ["PriceVolumeDivergence", "量价背离", "价格下跌但成交量放大，可能是底部信号"],
  ["VolContraction", "波动率收缩", "当前波动率相对历史波动率的位置，收缩后可能突破"],
  ["VolBreakout", "波动率突破", "波动率突然放大，可能是趋势启动信号"],
  ["AtrRatio", "ATR比率", "真实波幅占价格比例，波动率水平"],
  ["BollingerPosition", "布林带位置", "价格在布林带中的位置，±1 为上下轨"],
  ["PeReversal", "PE反转", "低 PE 股票未来收益可能更高（价值效应）"],
  ["PbReversal", "PB反转", "低 PB 股票未来收益可能更高（价值效应）"],
  ["RelativeValuation", "估值比价", "PE 和 PB 的相对位置，寻找估值错配"],
  ["EarningsYield", "盈利收益率", "盈利收益率（E/P），价值因子的另一种表达"],
  ["RoeChange", "ROE变化", "ROE 提升的公司未来收益可能更好（盈利动量）"],
  ["EarningsAcceleration", "盈利加速度", "ROE 变化的变化，盈利加速的公司更优"],
  ["GrossMarginChange", "毛利率变化", "毛利率提升说明竞争力增强"],
  ["AssetTurnover", "资产周转率", "资产周转率提升说明运营效率改善"],
  ["UnusualTurnover", "换手率异常", "当前换手率相对历史均值的偏离，异常放量可能有行情"],
  ["VolumeTrend", "成交量趋势", "短期成交量相对长期的变化，放量看涨"],
  ["VolumePriceDivergence", "量价背离(量价类)", "成交量大但价格不涨，可能是出货信号"],
  ["MoneyFlowStrength", "资金流强度", "当前成交额相对于历史均值的强度"],
];

function preset(item: FactorTemplate) {
  return item.scope === "system" ? SYSTEM_PRESETS.find(([, name]) => item.name === name) : undefined;
}

export function templateName(item: FactorTemplate): string {
  const match = preset(item);
  return match ? t(`factorTplPreset${match[0]}Name`) : item.name;
}

export function templateEconomy(item: FactorTemplate): string {
  const original = item.rule_config?.economy_logic_zh ?? item.description ?? "-";
  const match = preset(item);
  // A personal copy or an edited description is user content, even if its name
  // happens to match a system preset. Never replace that content by a translation.
  return match && original === match[2] ? t(`factorTplPreset${match[0]}Logic`) : original;
}

export function templateScope(scope: string | null | undefined): string {
  if (scope === "system") return t("factorTplScopeSystem");
  if (scope === "personal") return t("factorTplScopePersonal");
  return scope || "-";
}
