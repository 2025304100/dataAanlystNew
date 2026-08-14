import { Button, Input, Space, Tooltip, Typography } from "antd";
import {
  SearchOutlined,
  DownOutlined,
  RightOutlined,
  DatabaseOutlined,
  FunctionOutlined,
  BarChartOutlined,
  AppstoreOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { useMemo, useState } from "react";
import { t } from "../../i18n";

type FormulaCatalogItem = {
  key: string;
  labelZh: string;
  labelEn: string;
  snippet: string;
  descriptionZh: string;
  descriptionEn: string;
  category?: "math" | "rolling" | "cross_section";
  type?: "field" | "function" | "operator";
  fieldType?: string;
};

type FormulaExample = {
  key: string;
  nameZh: string;
  nameEn: string;
  descriptionZh: string;
  descriptionEn: string;
  formula: string;
};

type FactorFormulaBuilderProps = {
  isZh: boolean;
  onInsert: (snippet: string) => void;
  onUseExample: (formula: string) => void;
};

export const FACTOR_FORMULA_FIELDS: FormulaCatalogItem[] = [
  { key: "open", labelZh: "开盘价", labelEn: "Open", snippet: "open", descriptionZh: "当日开盘价", descriptionEn: "Daily open price", type: "field", fieldType: "price" },
  { key: "high", labelZh: "最高价", labelEn: "High", snippet: "high", descriptionZh: "当日最高价", descriptionEn: "Daily high price", type: "field", fieldType: "price" },
  { key: "low", labelZh: "最低价", labelEn: "Low", snippet: "low", descriptionZh: "当日最低价", descriptionEn: "Daily low price", type: "field", fieldType: "price" },
  { key: "close", labelZh: "收盘价", labelEn: "Close", snippet: "close", descriptionZh: "当日收盘价", descriptionEn: "Daily close price", type: "field", fieldType: "price" },
  { key: "volume", labelZh: "成交量", labelEn: "Volume", snippet: "volume", descriptionZh: "当日成交量", descriptionEn: "Daily trading volume", type: "field", fieldType: "volume" },
  { key: "amount", labelZh: "成交额", labelEn: "Amount", snippet: "amount", descriptionZh: "当日成交额", descriptionEn: "Daily trading amount", type: "field", fieldType: "amount" },
  { key: "turnover_rate", labelZh: "换手率", labelEn: "Turnover Rate", snippet: "turnover_rate", descriptionZh: "当日换手率", descriptionEn: "Daily turnover rate", type: "field", fieldType: "float" },
  { key: "prev_close", labelZh: "前收盘价", labelEn: "Previous Close", snippet: "prev_close", descriptionZh: "上一交易日收盘价", descriptionEn: "Previous close price", type: "field", fieldType: "price" },
  { key: "pe_ttm", labelZh: "市盈率 TTM", labelEn: "P/E TTM", snippet: "pe_ttm", descriptionZh: "滚动市盈率，按时点数据读取", descriptionEn: "Trailing P/E, point-in-time", type: "field", fieldType: "float" },
  { key: "pb", labelZh: "市净率", labelEn: "P/B", snippet: "pb", descriptionZh: "市净率，按时点数据读取", descriptionEn: "Price-to-book, point-in-time", type: "field", fieldType: "float" },
  { key: "main_net_inflow", labelZh: "主力净流入", labelEn: "Main Net Inflow", snippet: "main_net_inflow", descriptionZh: "主力资金净流入", descriptionEn: "Main capital net inflow", type: "field", fieldType: "amount" },
  { key: "roe_ttm", labelZh: "净资产收益率 TTM", labelEn: "ROE TTM", snippet: "roe_ttm", descriptionZh: "滚动净资产收益率", descriptionEn: "Trailing return on equity", type: "field", fieldType: "float" },
  { key: "lhb_institution_net", labelZh: "龙虎榜机构净额", labelEn: "Institution Net", snippet: "lhb_institution_net", descriptionZh: "龙虎榜机构席位净买入额", descriptionEn: "Institutional net amount from top-trader list", type: "field", fieldType: "amount" },
  { key: "hot_rank_pct", labelZh: "热度百分位", labelEn: "Hot Rank Percentile", snippet: "hot_rank_pct", descriptionZh: "市场热度排名百分位", descriptionEn: "Market popularity percentile", type: "field", fieldType: "float" },
  { key: "proxy_score", labelZh: "尾盘代理分数", labelEn: "Proxy Score", snippet: "proxy_score", descriptionZh: "尾盘代理评分", descriptionEn: "Tail-session proxy score", type: "field", fieldType: "float" },
];

export const FACTOR_FORMULA_FUNCTIONS: FormulaCatalogItem[] = [
  { key: "abs", labelZh: "绝对值", labelEn: "Absolute", snippet: "abs(close)", descriptionZh: "取表达式的绝对值", descriptionEn: "Absolute value", category: "math", type: "function" },
  { key: "min", labelZh: "最小值", labelEn: "Minimum", snippet: "min(close, open)", descriptionZh: "取两个表达式中的较小值", descriptionEn: "Minimum of two expressions", category: "math", type: "function" },
  { key: "max", labelZh: "最大值", labelEn: "Maximum", snippet: "max(close, open)", descriptionZh: "取两个表达式中的较大值", descriptionEn: "Maximum of two expressions", category: "math", type: "function" },
  { key: "round", labelZh: "四舍五入", labelEn: "Round", snippet: "round(close, 2)", descriptionZh: "按指定小数位四舍五入", descriptionEn: "Round to decimal digits", category: "math", type: "function" },
  { key: "log", labelZh: "自然对数", labelEn: "Log", snippet: "log(close)", descriptionZh: "计算自然对数，输入需为正数", descriptionEn: "Natural logarithm; input must be positive", category: "math", type: "function" },
  { key: "sqrt", labelZh: "平方根", labelEn: "Square Root", snippet: "sqrt(close)", descriptionZh: "计算平方根，输入不能为负数", descriptionEn: "Square root; input must be non-negative", category: "math", type: "function" },
  { key: "exp", labelZh: "指数", labelEn: "Exponential", snippet: "exp(close)", descriptionZh: "计算自然指数", descriptionEn: "Natural exponential", category: "math", type: "function" },
  { key: "sma", labelZh: "简单移动平均", labelEn: "Simple Moving Average", snippet: "sma(close, 20)", descriptionZh: "最近 N 个交易日的简单平均", descriptionEn: "Simple average over N trading days", category: "rolling", type: "function" },
  { key: "ema", labelZh: "指数移动平均", labelEn: "Exponential Moving Average", snippet: "ema(close, 20)", descriptionZh: "最近 N 个交易日的指数加权平均", descriptionEn: "Exponentially weighted average over N days", category: "rolling", type: "function" },
  { key: "stddev", labelZh: "滚动标准差", labelEn: "Rolling Std Dev", snippet: "stddev(close, 20)", descriptionZh: "最近 N 个交易日的标准差", descriptionEn: "Standard deviation over N days", category: "rolling", type: "function" },
  { key: "sum", labelZh: "滚动求和", labelEn: "Rolling Sum", snippet: "sum(volume, 20)", descriptionZh: "最近 N 个交易日求和", descriptionEn: "Sum over N trading days", category: "rolling", type: "function" },
  { key: "mean", labelZh: "滚动均值", labelEn: "Rolling Mean", snippet: "mean(close, 20)", descriptionZh: "最近 N 个交易日的均值", descriptionEn: "Mean over N trading days", category: "rolling", type: "function" },
  { key: "count", labelZh: "非空计数", labelEn: "Non-null Count", snippet: "count(close, 20)", descriptionZh: "最近 N 个交易日的非空数量", descriptionEn: "Non-null count over N trading days", category: "rolling", type: "function" },
  { key: "highest", labelZh: "滚动最高", labelEn: "Rolling Highest", snippet: "highest(high, 20)", descriptionZh: "最近 N 个交易日的最高值", descriptionEn: "Highest value over N trading days", category: "rolling", type: "function" },
  { key: "lowest", labelZh: "滚动最低", labelEn: "Rolling Lowest", snippet: "lowest(low, 20)", descriptionZh: "最近 N 个交易日的最低值", descriptionEn: "Lowest value over N trading days", category: "rolling", type: "function" },
  { key: "ref", labelZh: "历史引用", labelEn: "Historical Reference", snippet: "ref(close, 1)", descriptionZh: "引用 N 个交易日前的值", descriptionEn: "Value from N trading days ago", category: "rolling", type: "function" },
  { key: "pct_change", labelZh: "区间涨跌幅", labelEn: "Percentage Change", snippet: "pct_change(close, 20)", descriptionZh: "相对 N 个交易日前的涨跌幅", descriptionEn: "Percentage change versus N days ago", category: "rolling", type: "function" },
  // 截面函数
  { key: "rank_cs", labelZh: "截面排名", labelEn: "Cross-section Rank", snippet: "rank_cs(close)", descriptionZh: "横截面排名，返回 0-1 之间", descriptionEn: "Cross-sectional rank, returns 0-1", category: "cross_section", type: "function" },
  { key: "winsorize", labelZh: "去极值", labelEn: "Winsorize", snippet: "winsorize(close, 0.01)", descriptionZh: "上下 1% 缩尾处理", descriptionEn: "Winsorize at 1% tails", category: "cross_section", type: "function" },
  { key: "neutralize", labelZh: "中性化", labelEn: "Neutralize", snippet: "neutralize(x, industry, size)", descriptionZh: "对行业和市值中性化", descriptionEn: "Neutralize against industry and size", category: "cross_section", type: "function" },
];

const OPERATORS = [
  { key: "add", snippet: " + ", label: "+", descriptionZh: "加", descriptionEn: "Add" },
  { key: "sub", snippet: " - ", label: "−", descriptionZh: "减", descriptionEn: "Subtract" },
  { key: "mul", snippet: " * ", label: "×", descriptionZh: "乘", descriptionEn: "Multiply" },
  { key: "div", snippet: " / ", label: "÷", descriptionZh: "除", descriptionEn: "Divide" },
  { key: "mod", snippet: " % ", label: "%", descriptionZh: "取余", descriptionEn: "Modulo" },
  { key: "pow", snippet: " ** ", label: "幂", descriptionZh: "乘方", descriptionEn: "Power" },
  { key: "gt", snippet: " > ", label: ">", descriptionZh: "大于", descriptionEn: "Greater than" },
  { key: "gte", snippet: " >= ", label: "≥", descriptionZh: "大于等于", descriptionEn: "Greater than or equal" },
  { key: "lt", snippet: " < ", label: "<", descriptionZh: "小于", descriptionEn: "Less than" },
  { key: "lte", snippet: " <= ", label: "≤", descriptionZh: "小于等于", descriptionEn: "Less than or equal" },
  { key: "eq", snippet: " == ", label: "=", descriptionZh: "等于", descriptionEn: "Equal" },
  { key: "neq", snippet: " != ", label: "≠", descriptionZh: "不等于", descriptionEn: "Not equal" },
  { key: "and", snippet: " and ", label: "并且", descriptionZh: "两个条件同时成立", descriptionEn: "Both conditions are true" },
  { key: "or", snippet: " or ", label: "或者", descriptionZh: "任一条件成立", descriptionEn: "Either condition is true" },
  { key: "not", snippet: "not ", label: "非", descriptionZh: "条件取反", descriptionEn: "Negate condition" },
  { key: "paren", snippet: "()", label: "( )", descriptionZh: "括号，控制计算顺序", descriptionEn: "Parentheses" },
];

const EXAMPLES: FormulaExample[] = [
  { key: "earnings_yield", nameZh: "盈利收益率", nameEn: "Earnings Yield", descriptionZh: "市盈率的倒数，并规避无效市盈率", descriptionEn: "Inverse P/E with invalid values guarded", formula: "1 / pe_ttm if pe_ttm > 0 else 0" },
  { key: "momentum", nameZh: "20 日动量", nameEn: "20-day Momentum", descriptionZh: "20 个交易日的价格涨跌幅", descriptionEn: "Price change over 20 trading days", formula: "pct_change(close, 20)" },
  { key: "turnover_z", nameZh: "换手率标准分", nameEn: "Turnover Z-score", descriptionZh: "换手率相对 20 日均值的标准化偏离", descriptionEn: "Standardized turnover deviation from its 20-day mean", formula: "(turnover_rate - mean(turnover_rate, 20)) / stddev(turnover_rate, 20)" },
  { key: "ma_distance", nameZh: "均线乖离率", nameEn: "MA Distance", descriptionZh: "收盘价偏离 20 日均线的比例", descriptionEn: "Close price distance from the 20-day moving average", formula: "close / sma(close, 20) - 1" },
  { key: "breakout", nameZh: "20 日突破", nameEn: "20-day Breakout", descriptionZh: "收盘价是否达到近 20 日最高价", descriptionEn: "Whether close reaches the 20-day high", formula: "close >= highest(high, 20)" },
  { key: "quality_value", nameZh: "质量价值组合", nameEn: "Quality Value", descriptionZh: "净资产收益率与盈利收益率组合", descriptionEn: "ROE combined with earnings yield", formula: "roe_ttm + 1 / pe_ttm if pe_ttm > 0 else roe_ttm" },
];

// 字段分组
const FIELD_GROUPS = [
  {
    key: "price_volume",
    nameZh: "行情与基本面字段",
    nameEn: "Market & Fundamental Fields",
    icon: <DatabaseOutlined />,
    items: FACTOR_FORMULA_FIELDS,
  },
];

// 函数分组
const FUNCTION_GROUPS = [
  {
    key: "rolling",
    nameZh: "时间序列函数",
    nameEn: "Time-series Functions",
    icon: <BarChartOutlined />,
    items: FACTOR_FORMULA_FUNCTIONS.filter((f) => f.category === "rolling"),
  },
  {
    key: "cross_section",
    nameZh: "截面函数",
    nameEn: "Cross-section Functions",
    icon: <AppstoreOutlined />,
    items: FACTOR_FORMULA_FUNCTIONS.filter((f) => f.category === "cross_section"),
  },
  {
    key: "math",
    nameZh: "数学函数",
    nameEn: "Math Functions",
    icon: <FunctionOutlined />,
    items: FACTOR_FORMULA_FUNCTIONS.filter((f) => f.category === "math"),
  },
];

export default function FactorFormulaBuilder({ isZh, onInsert, onUseExample }: FactorFormulaBuilderProps) {
  const [keyword, setKeyword] = useState("");
  const normalizedKeyword = keyword.trim().toLowerCase();
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({
    price_volume: true,
    rolling: true,
    cross_section: true,
    math: false,
  });

  const toggleGroup = (key: string) => {
    setExpandedGroups((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  // 过滤字段
  const filteredFieldGroups = useMemo(() => {
    if (!normalizedKeyword) return FIELD_GROUPS;
    return FIELD_GROUPS.map((g) => ({
      ...g,
      items: g.items.filter((item) =>
        [item.key, item.labelZh, item.labelEn, item.descriptionZh, item.descriptionEn, item.snippet]
          .join(" ")
          .toLowerCase()
          .includes(normalizedKeyword),
      ),
    })).filter((g) => g.items.length > 0);
  }, [normalizedKeyword]);

  // 过滤函数
  const filteredFunctionGroups = useMemo(() => {
    if (!normalizedKeyword) return FUNCTION_GROUPS;
    return FUNCTION_GROUPS.map((g) => ({
      ...g,
      items: g.items.filter((item) =>
        [item.key, item.labelZh, item.labelEn, item.descriptionZh, item.descriptionEn, item.snippet]
          .join(" ")
          .toLowerCase()
          .includes(normalizedKeyword),
      ),
    })).filter((g) => g.items.length > 0);
  }, [normalizedKeyword]);

  // 字段类型标签颜色
  const fieldTypeLabel = (type?: string) => {
    if (!type) return "";
    const map: Record<string, string> = {
      price: "price",
      volume: "volume",
      amount: "amount",
      float: "float",
    };
    return map[type] || "field";
  };

  const totalFunctions = FACTOR_FORMULA_FUNCTIONS.length;

  return (
    <div className="formula-builder-redesign">
      {/* 搜索框 */}
      <div className="formula-builder-search">
        <Input
          allowClear
          size="middle"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          prefix={<SearchOutlined style={{ color: "#94a3b8" }} />}
          placeholder={isZh ? "请输入字段、函数或关键词" : "Search fields, functions or keywords"}
          className="formula-builder-search-input"
        />
      </div>

      {/* 树形列表 */}
      <div className="formula-builder-tree">
        {/* 字段分组 */}
        {filteredFieldGroups.map((group) => (
          <div key={group.key} className="formula-tree-group">
            <div
              className={`formula-tree-group-header ${expandedGroups[group.key] ? "expanded" : ""}`}
              onClick={() => toggleGroup(group.key)}
            >
              <span className="formula-tree-toggle-icon">
                {expandedGroups[group.key] ? <DownOutlined /> : <RightOutlined />}
              </span>
              <span className="formula-tree-group-icon">{group.icon}</span>
              <span className="formula-tree-group-name">
                {isZh ? group.nameZh : group.nameEn}
              </span>
              <span className="formula-tree-group-count">{group.items.length}</span>
            </div>
            {expandedGroups[group.key] ? (
              <div className="formula-tree-items">
                {group.items.map((item) => (
                  <Tooltip
                    key={item.key}
                    title={`${isZh ? item.descriptionZh : item.descriptionEn} · ${item.snippet}`}
                  >
                    <div
                      className="formula-tree-item"
                      onClick={() => onInsert(item.snippet)}
                    >
                      <span className="formula-tree-item-dot" />
                      <span className="formula-tree-item-name">
                        {isZh ? item.labelZh : item.labelEn}
                      </span>
                      <span className={`formula-tree-item-type type-${fieldTypeLabel(item.fieldType)}`}>
                        {item.key}
                      </span>
                    </div>
                  </Tooltip>
                ))}
              </div>
            ) : null}
          </div>
        ))}

        {/* 函数分组 */}
        {filteredFunctionGroups.map((group) => (
          <div key={group.key} className="formula-tree-group">
            <div
              className={`formula-tree-group-header ${expandedGroups[group.key] ? "expanded" : ""}`}
              onClick={() => toggleGroup(group.key)}
            >
              <span className="formula-tree-toggle-icon">
                {expandedGroups[group.key] ? <DownOutlined /> : <RightOutlined />}
              </span>
              <span className="formula-tree-group-icon func-icon">{group.icon}</span>
              <span className="formula-tree-group-name">
                {isZh ? group.nameZh : group.nameEn}
              </span>
              <span className="formula-tree-group-count">{group.items.length}</span>
            </div>
            {expandedGroups[group.key] ? (
              <div className="formula-tree-items">
                {group.items.map((item) => (
                  <Tooltip
                    key={item.key}
                    title={`${isZh ? item.descriptionZh : item.descriptionEn} · ${item.snippet}`}
                  >
                    <div
                      className="formula-tree-item formula-tree-item-func"
                      onClick={() => onInsert(item.snippet)}
                    >
                      <span className="formula-tree-item-func-mark">ƒ</span>
                      <span className="formula-tree-item-name">
                        {isZh ? item.labelZh : item.labelEn}
                      </span>
                      <span className="formula-tree-item-snippet">{item.key}</span>
                    </div>
                  </Tooltip>
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </div>

      {/* 底部统计 */}
      <div className="formula-builder-footer">
        <span className="formula-builder-footer-text">
          {isZh ? "点击插入公式编辑区" : "Click to insert into editor"}
        </span>
        <span className="formula-builder-footer-count">
          <FunctionOutlined /> {isZh ? `函数库 ${totalFunctions} 个` : `${totalFunctions} functions`}
        </span>
      </div>
    </div>
  );
}
