import React, { useState, useEffect } from "react";
import { X, Shield, FlaskConical } from "lucide-react";
import { useApp } from "../../context/AppContext";
import { t, template } from "../../i18n";

/**
 * CreatePortfolioModal — 新建组合弹窗
 *
 * 一比一还原原型图「新建组合弹窗.html」：560px 居中模态框，
 * 背景模糊遮罩 rgba(0,0,0,0.6) + backdrop-filter blur(4px)。
 *
 * 三个分区：基本信息 / 资金与成本 / 风险约束。
 *
 * 创建逻辑复用 AppContext.createPortfolio（方法签名不变）：
 *   - name → payload.name
 *   - type(real/sim) → payload.account_type(manual/simulated)
 *   - 初始资金 → payload.total_capital
 *   - 现金保留比例(%) → payload.cash_reserve_ratio(0-1)；payload.investable_ratio = 1 - cash_reserve_ratio
 *   - 佣金率 / 单票仓位上限 / 基准指数：当前 createPortfolio 签名不支持，仅作前端表单态保留
 *
 * i18n key 前缀：portfolioTrading.create.*（由 Task 13 补全，缺失时 t() 返回 key 字符串，不阻塞编译）。
 */

interface CreatePortfolioModalProps {
  open: boolean;
  onClose: () => void;
  onSuccess?: (id: number) => void;
}

type PortfolioType = "real" | "sim";
type AssetScope = "stock" | "etf" | "mixed";

const CreatePortfolioModal: React.FC<CreatePortfolioModalProps> = ({
  open,
  onClose,
  onSuccess,
}) => {
  const { portfolios, createPortfolio, showToast } = useApp();

  // ----- 表单状态 -----
  const [name, setName] = useState("");
  const [type, setType] = useState<PortfolioType>("sim");
  const [assetScope, setAssetScope] = useState<AssetScope>("stock");
  const [capital, setCapital] = useState<number>(50000);
  const [capitalDisplay, setCapitalDisplay] = useState<string>("50,000.00");
  const [buyFee, setBuyFee] = useState<string>("0.025");
  const [sellFee, setSellFee] = useState<string>("0.025");
  const [singleLimit, setSingleLimit] = useState<number>(30); // % 5-100
  const [cashReserve, setCashReserve] = useState<number>(10); // % 0-50
  const [benchmark, setBenchmark] = useState<string>("hs300");

  const [nameError, setNameError] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // 打开时重置表单（默认模拟组合，初始资金 5 万，佣金 0.025%，单票 30%，现金保留 10%）
  useEffect(() => {
    if (!open) return;
    setName("");
    setType("sim");
    setAssetScope("stock");
    setCapital(50000);
    setCapitalDisplay("50,000.00");
    setBuyFee("0.025");
    setSellFee("0.025");
    setSingleLimit(30);
    setCashReserve(10);
    setBenchmark("hs300");
    setNameError(false);
    setSubmitting(false);
  }, [open]);

  // ESC 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  // ----- 资金格式化（千分位 + 2 位小数）-----
  const formatCurrency = (n: number): string =>
    n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const parseCurrency = (s: string): number => parseFloat(s.replace(/,/g, "")) || 0;

  const handleCapitalFocus = () => setCapitalDisplay(capital > 0 ? String(capital) : "");
  const handleCapitalChange = (raw: string) => {
    setCapitalDisplay(raw);
    setCapital(parseCurrency(raw));
  };
  const handleCapitalBlur = () => {
    const n = parseCurrency(capitalDisplay);
    setCapital(n);
    setCapitalDisplay(formatCurrency(n));
  };

  const quickAmounts = [
    { label: t("portfolioTrading.create.quick.wan1"), value: 10000 },
    { label: t("portfolioTrading.create.quick.wan5"), value: 50000 },
    { label: t("portfolioTrading.create.quick.wan10"), value: 100000 },
    { label: t("portfolioTrading.create.quick.wan50"), value: 500000 },
    { label: t("portfolioTrading.create.quick.wan100"), value: 1000000 },
  ];

  const handleSubmit = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      // 名称空校验：红色高亮 + 抖动动画，不关闭弹窗
      setNameError(true);
      window.setTimeout(() => setNameError(false), 1500);
      return;
    }
    setSubmitting(true);
    try {
      // P1-FIX: 前端字段 → 后端 ratio 转换：
      //   buyFee "0.025" (%) → 0.00025 ratio
      //   sellFee "0.025" (%) → 0.00025 ratio
      //   singleLimit 30 (%) → 0.30 ratio
      //   benchmark code 别名映射：hs300/zz500/cyb/sz50/kc50 → 真实指数代码
      const buyFeeRatio = (parseFloat(buyFee) || 0) / 100;
      const sellFeeRatio = (parseFloat(sellFee) || 0) / 100;
      const singleLimitRatio = singleLimit / 100;
      const BENCHMARK_MAP: Record<string, string> = {
        hs300: "000300",
        zz500: "000905",
        cyb: "399006",
        sz50: "000016",
        kc50: "000688",
        custom: "000300",
      };
      const benchmarkCode = BENCHMARK_MAP[benchmark] ?? "000300";
      const newPortfolio = await createPortfolio({
        name: trimmed,
        account_type: type === "real" ? "manual" : "simulated",
        asset_scope: assetScope,
        total_capital: capital,
        investable_ratio: 1 - cashReserve / 100,
        cash_reserve_ratio: cashReserve / 100,
        // P1-FIX: 曾只保存在前端本地状态，现在一并提交落库
        buy_fee_pct: buyFeeRatio,
        sell_fee_pct: sellFeeRatio,
        benchmark_code: benchmarkCode,
        default_single_position_pct: singleLimitRatio,
      });
      if (!newPortfolio) {
        // AppContext.createPortfolio 内部已 toast 错误
        setSubmitting(false);
        return;
      }
      onSuccess?.(newPortfolio.id);
    } catch (error) {
      showToast("error", t("portfolioTrading.create.createFailed"));
      setSubmitting(false);
    }
  };

  const existingCount = portfolios.length;

  return (
    <>
      <style>{`
        @keyframes pt-cpm-fade-in { from { opacity: 0; } to { opacity: 1; } }
        @keyframes pt-cpm-scale-in {
          from { opacity: 0; transform: scale(0.94) translateY(-8px); }
          to { opacity: 1; transform: scale(1) translateY(0); }
        }
        @keyframes pt-cpm-shake {
          0%, 100% { transform: translateX(0); }
          20% { transform: translateX(-6px); }
          40% { transform: translateX(6px); }
          60% { transform: translateX(-4px); }
          80% { transform: translateX(4px); }
        }
        .pt-cpm-backdrop { animation: pt-cpm-fade-in 0.25s ease-out forwards; }
        .pt-cpm-panel { animation: pt-cpm-scale-in 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
        .pt-cpm-shake { animation: pt-cpm-shake 0.4s ease-in-out; }
        .pt-cpm-num::-webkit-outer-spin-button,
        .pt-cpm-num::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
        .pt-cpm-num { -moz-appearance: textfield; }
      `}</style>

      {/* 背景模糊遮罩 */}
      <div
        className="pt-cpm-backdrop"
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 1000,
          background: "rgba(0, 0, 0, 0.6)",
          backdropFilter: "blur(4px)",
          WebkitBackdropFilter: "blur(4px)",
        }}
      />

      {/* 居中模态框 */}
      <div
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 1001,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: 16,
          pointerEvents: "none",
        }}
      >
        <div
          className="pt-cpm-panel"
          style={{
            pointerEvents: "auto",
            width: 560,
            maxHeight: "calc(100vh - 80px)",
            display: "flex",
            flexDirection: "column",
            background: "var(--pt-surface-2)",
            border: "1px solid var(--pt-border)",
            borderRadius: 12,
            boxShadow:
              "0 20px 40px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(255, 255, 255, 0.02)",
          }}
        >
          {/* ===== Header ===== */}
          <div
            style={{
              display: "flex",
              alignItems: "flex-start",
              justifyContent: "space-between",
              padding: "20px 24px 16px",
              borderBottom: "1px solid var(--pt-border)",
            }}
          >
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <h2
                style={{
                  margin: 0,
                  fontSize: 18,
                  fontWeight: 600,
                  color: "var(--pt-white)",
                  letterSpacing: "-0.01em",
                }}
              >
                {t("portfolioTrading.create.title")}
              </h2>
              <p
                style={{
                  margin: 0,
                  fontSize: 12,
                  color: "var(--pt-muted-foreground)",
                }}
              >
                {t("portfolioTrading.create.subtitle")}
              </p>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label={t("portfolioTrading.create.close")}
              style={{
                width: 32,
                height: 32,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                borderRadius: "50%",
                background: "var(--pt-surface-3)",
                color: "var(--pt-muted-foreground)",
                border: "none",
                cursor: "pointer",
                flexShrink: 0,
                marginTop: 2,
                transition: "all 0.15s ease",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = "var(--pt-surface-4)";
                e.currentTarget.style.color = "var(--pt-foreground)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "var(--pt-surface-3)";
                e.currentTarget.style.color = "var(--pt-muted-foreground)";
              }}
            >
              <X size={16} />
            </button>
          </div>

          {/* ===== Form Body ===== */}
          <div
            className="thin-scrollbar"
            style={{ flex: 1, overflowY: "auto", minHeight: 0 }}
          >
            <div
              style={{
                padding: "20px 24px",
                display: "flex",
                flexDirection: "column",
                gap: 24,
              }}
            >
              {/* ----- Section 1: 基本信息 ----- */}
              <section style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <SectionTitle>{t("portfolioTrading.create.section.basic")}</SectionTitle>

                {/* 组合名称 */}
                <div>
                  <FieldLabel required>
                    {t("portfolioTrading.create.field.name")}
                  </FieldLabel>
                  <input
                    type="text"
                    className={`pt-input${nameError ? " pt-cpm-shake" : ""}`}
                    value={name}
                    maxLength={20}
                    placeholder={t("portfolioTrading.create.field.namePlaceholder")}
                    onChange={(e) => {
                      setName(e.target.value);
                      if (nameError) setNameError(false);
                    }}
                    style={{
                      width: "100%",
                      ...(nameError
                        ? {
                            borderColor: "var(--pt-state-error)",
                            boxShadow: "0 0 0 3px rgba(239, 68, 68, 0.12)",
                          }
                        : {}),
                    }}
                  />
                  <FieldHint>{t("portfolioTrading.create.field.nameHint")}</FieldHint>
                </div>

                {/* 组合类型 */}
                <div>
                  <FieldLabel>{t("portfolioTrading.create.field.type")}</FieldLabel>
                  <div style={{ display: "flex", gap: 12 }}>
                    <TypeCard
                      active={type === "real"}
                      onClick={() => setType("real")}
                      icon={<Shield size={20} />}
                      title={t("portfolioTrading.create.type.real")}
                      desc={t("portfolioTrading.create.type.realDesc")}
                    />
                    <TypeCard
                      active={type === "sim"}
                      onClick={() => setType("sim")}
                      icon={<FlaskConical size={20} />}
                      title={t("portfolioTrading.create.type.sim")}
                      desc={t("portfolioTrading.create.type.simDesc")}
                    />
                  </div>
                </div>

                {/* 标的范围：这是组合的硬约束，后端会同时用于候选、成员、回测与自动交易。 */}
                <div>
                  <FieldLabel>组合标的范围</FieldLabel>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
                    <AssetScopeCard active={assetScope === "stock"} onClick={() => setAssetScope("stock")} title="股票" desc="只允许股票；使用股票评分与策略。" />
                    <AssetScopeCard active={assetScope === "etf"} onClick={() => setAssetScope("etf")} title="ETF" desc="只允许 ETF；使用 ETF 专属数据和评分。" />
                    <AssetScopeCard active={assetScope === "mixed"} onClick={() => setAssetScope("mixed")} title="混合" desc="股票与 ETF 共存，需分别配置策略。" />
                  </div>
                  <FieldHint>创建后会作为候选、成员、持仓、回测和自动交易的统一资产边界。</FieldHint>
                </div>
              </section>

              {/* ----- Section 2: 资金与成本 ----- */}
              <section style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <SectionTitle>{t("portfolioTrading.create.section.capital")}</SectionTitle>

                {/* 初始资金 */}
                <div>
                  <FieldLabel>{t("portfolioTrading.create.field.initialCapital")}</FieldLabel>
                  <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
                    <span
                      style={{
                        position: "absolute",
                        left: 12,
                        fontSize: 13,
                        color: "var(--pt-muted-foreground)",
                        pointerEvents: "none",
                      }}
                    >
                      ¥
                    </span>
                    <input
                      type="text"
                      className="pt-input pt-mono"
                      value={capitalDisplay}
                      onChange={(e) => handleCapitalChange(e.target.value)}
                      onFocus={handleCapitalFocus}
                      onBlur={handleCapitalBlur}
                      style={{ width: "100%", paddingLeft: 28, textAlign: "left" }}
                    />
                  </div>
                  {/* 快捷金额 */}
                  <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                    {quickAmounts.map((qa) => (
                      <QuickAmountButton
                        key={qa.value}
                        label={qa.label}
                        onClick={() => {
                          setCapital(qa.value);
                          setCapitalDisplay(formatCurrency(qa.value));
                        }}
                      />
                    ))}
                  </div>
                </div>

                {/* 买入/卖出佣金率 */}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                  <div>
                    <FieldLabel>{t("portfolioTrading.create.field.buyFee")}</FieldLabel>
                    <InputWithSuffix suffix="%">
                      <input
                        type="text"
                        className="pt-input pt-mono"
                        value={buyFee}
                        onChange={(e) => setBuyFee(e.target.value)}
                        style={{ width: "100%", paddingRight: 28 }}
                      />
                    </InputWithSuffix>
                  </div>
                  <div>
                    <FieldLabel>{t("portfolioTrading.create.field.sellFee")}</FieldLabel>
                    <InputWithSuffix suffix="%">
                      <input
                        type="text"
                        className="pt-input pt-mono"
                        value={sellFee}
                        onChange={(e) => setSellFee(e.target.value)}
                        style={{ width: "100%", paddingRight: 28 }}
                      />
                    </InputWithSuffix>
                  </div>
                </div>
                <div style={{ fontSize: 11, color: "var(--pt-slate-500)", marginTop: -8 }}>
                  {t("portfolioTrading.create.hint.fee")}
                </div>
              </section>

              {/* ----- Section 3: 风险约束 ----- */}
              <section style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <SectionTitle>{t("portfolioTrading.create.section.risk")}</SectionTitle>

                {/* 单票仓位上限 / 现金保留比例 */}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                  <div>
                    <FieldLabel>{t("portfolioTrading.create.field.singleLimit")}</FieldLabel>
                    <SliderInput
                      min={5}
                      max={100}
                      value={singleLimit}
                      onChange={setSingleLimit}
                    />
                  </div>
                  <div>
                    <FieldLabel>{t("portfolioTrading.create.field.cashReserve")}</FieldLabel>
                    <SliderInput
                      min={0}
                      max={50}
                      value={cashReserve}
                      onChange={setCashReserve}
                    />
                  </div>
                </div>

                {/* 基准指数 */}
                <div>
                  <FieldLabel>{t("portfolioTrading.create.field.benchmark")}</FieldLabel>
                  <select
                    className="pt-select"
                    value={benchmark}
                    onChange={(e) => setBenchmark(e.target.value)}
                    style={{ width: "100%" }}
                  >
                    <option value="hs300">{t("portfolioTrading.create.benchmark.hs300")}</option>
                    <option value="zz500">{t("portfolioTrading.create.benchmark.zz500")}</option>
                    <option value="cyb">{t("portfolioTrading.create.benchmark.cyb")}</option>
                    <option value="sz50">{t("portfolioTrading.create.benchmark.sz50")}</option>
                    <option value="kc50">{t("portfolioTrading.create.benchmark.kc50")}</option>
                    <option value="custom">{t("portfolioTrading.create.benchmark.custom")}</option>
                  </select>
                </div>
              </section>
            </div>
          </div>

          {/* ===== Bottom Action Bar ===== */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "16px 24px",
              borderTop: "1px solid var(--pt-border)",
              background: "var(--pt-surface-2)",
              borderRadius: "0 0 12px 12px",
            }}
          >
            <div style={{ fontSize: 11, color: "var(--pt-slate-500)" }}>
              {template("portfolioTrading.create.existingCount", { count: existingCount })}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <button
                type="button"
                className="pt-btn pt-btn-ghost"
                onClick={onClose}
                disabled={submitting}
              >
                {t("portfolioTrading.create.cancel")}
              </button>
              <button
                type="button"
                className="pt-btn pt-btn-primary"
                onClick={handleSubmit}
                disabled={submitting}
              >
                {t("portfolioTrading.create.submit")}
              </button>
            </div>
          </div>
        </div>
      </div>
    </>
  );
};

/* ------------------------------------------------------------------ */
/* 子组件                                                              */
/* ------------------------------------------------------------------ */

/** 区块标题：青色竖条 + 标题 */
const SectionTitle: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <h3
    style={{
      display: "flex",
      alignItems: "center",
      gap: 8,
      margin: 0,
      fontSize: 14,
      fontWeight: 600,
      color: "var(--pt-foreground)",
    }}
  >
    <span
      style={{
        display: "inline-block",
        width: 3,
        height: 14,
        background: "var(--pt-primary)",
        borderRadius: 2,
      }}
    />
    {children}
  </h3>
);

/** 字段标签 */
const FieldLabel: React.FC<{ children: React.ReactNode; required?: boolean }> = ({
  children,
  required,
}) => (
  <label
    style={{
      display: "block",
      fontSize: 12,
      fontWeight: 500,
      color: "var(--pt-muted-foreground)",
      marginBottom: 6,
    }}
  >
    {children}
    {required && (
      <span style={{ color: "var(--pt-state-error)", marginLeft: 2 }}>*</span>
    )}
  </label>
);

/** 字段提示 */
const FieldHint: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div style={{ fontSize: 11, color: "var(--pt-slate-500)", marginTop: 4 }}>
    {children}
  </div>
);

/** 带后缀的输入框容器（% 等） */
const InputWithSuffix: React.FC<{ suffix: string; children: React.ReactElement }> = ({
  suffix,
  children,
}) => (
  <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
    {children}
    <span
      style={{
        position: "absolute",
        right: 12,
        fontSize: 13,
        color: "var(--pt-muted-foreground)",
        pointerEvents: "none",
      }}
    >
      {suffix}
    </span>
  </div>
);

/** 组合类型卡片单选 */
const TypeCard: React.FC<{
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  title: string;
  desc: string;
}> = ({ active, onClick, icon, title, desc }) => {
  const [hover, setHover] = useState(false);
  const bg = active
    ? "rgba(6, 182, 212, 0.06)"
    : hover
      ? "var(--pt-surface-3)"
      : "var(--pt-surface-2)";
  const border = active
    ? "var(--pt-primary)"
    : hover
      ? "var(--pt-slate-500)"
      : "var(--pt-border)";
  const shadow = active
    ? "0 0 0 3px rgba(6, 182, 212, 0.08), 0 2px 8px rgba(6, 182, 212, 0.1)"
    : "none";
  const iconBg = active ? "rgba(6, 182, 212, 0.15)" : "var(--pt-surface-3)";
  const iconColor = active ? "var(--pt-primary)" : "var(--pt-muted-foreground)";
  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        flex: 1,
        cursor: "pointer",
        padding: 14,
        border: `1px solid ${border}`,
        borderRadius: 8,
        background: bg,
        boxShadow: shadow,
        transition: "all 0.2s ease",
      }}
    >
      <div
        style={{
          width: 32,
          height: 32,
          borderRadius: 8,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: iconBg,
          color: iconColor,
          marginBottom: 10,
          transition: "all 0.2s ease",
        }}
      >
        {icon}
      </div>
      <div
        style={{
          fontSize: 13,
          fontWeight: 600,
          color: "var(--pt-foreground)",
          marginBottom: 2,
        }}
      >
        {title}
      </div>
      <div style={{ fontSize: 11, color: "var(--pt-slate-500)", lineHeight: 1.4 }}>{desc}</div>
    </div>
  );
};

/** 标的范围卡片：不只是前端筛选，提交后会成为服务端交易边界。 */
const AssetScopeCard: React.FC<{
  active: boolean;
  onClick: () => void;
  title: string;
  desc: string;
}> = ({ active, onClick, title, desc }) => (
  <button
    type="button"
    onClick={onClick}
    style={{
      minHeight: 76,
      padding: "10px 9px",
      textAlign: "left",
      color: active ? "var(--pt-foreground)" : "var(--pt-muted-foreground)",
      background: active ? "rgba(6, 182, 212, 0.1)" : "var(--pt-surface-3)",
      border: `1px solid ${active ? "var(--pt-primary)" : "var(--pt-border)"}`,
      borderRadius: 7,
      cursor: "pointer",
    }}
  >
    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 5 }}>{title}</div>
    <div style={{ fontSize: 11, lineHeight: 1.45 }}>{desc}</div>
  </button>
);

/** 快捷金额按钮（一比一对齐原型 .quick-amount-btn） */
const QuickAmountButton: React.FC<{ label: string; onClick: () => void }> = ({
  label,
  onClick,
}) => {
  const [hover, setHover] = useState(false);
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        flex: 1,
        height: 28,
        fontSize: 11,
        fontWeight: 500,
        color: hover ? "var(--pt-primary)" : "var(--pt-muted-foreground)",
        background: hover ? "rgba(6, 182, 212, 0.08)" : "var(--pt-surface-3)",
        border: `1px solid ${hover ? "var(--pt-primary)" : "transparent"}`,
        borderRadius: 4,
        cursor: "pointer",
        transition: "all 0.15s ease",
      }}
    >
      {label}
    </button>
  );
};

/** 滑块 + 数字输入（pt-input + 自定义 track + 透明 range 覆盖层） */
const SliderInput: React.FC<{
  min: number;
  max: number;
  value: number;
  onChange: (v: number) => void;
}> = ({ min, max, value, onChange }) => {
  const pct = max > min ? ((value - min) / (max - min)) * 100 : 0;
  const clamp = (v: number) => Math.max(min, Math.min(max, v));
  return (
    <div>
      <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
        <input
          type="number"
          className="pt-input pt-mono pt-cpm-num"
          min={min}
          max={max}
          value={value}
          onChange={(e) => onChange(clamp(Number(e.target.value) || 0))}
          style={{ width: "100%", paddingRight: 28, textAlign: "right" }}
        />
        <span
          style={{
            position: "absolute",
            right: 12,
            fontSize: 13,
            color: "var(--pt-muted-foreground)",
            pointerEvents: "none",
          }}
        >
          %
        </span>
      </div>
      <div
        style={{
          position: "relative",
          height: 14,
          marginTop: 8,
          display: "flex",
          alignItems: "center",
        }}
      >
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            height: 4,
            background: "var(--pt-surface-3)",
            borderRadius: 2,
          }}
        />
        <div
          style={{
            position: "absolute",
            left: 0,
            height: 4,
            width: `${pct}%`,
            background: "var(--pt-primary)",
            borderRadius: 2,
          }}
        />
        <div
          style={{
            position: "absolute",
            left: `${pct}%`,
            top: "50%",
            transform: "translate(-50%, -50%)",
            width: 14,
            height: 14,
            background: "var(--pt-primary)",
            border: "2px solid var(--pt-surface-2)",
            borderRadius: "50%",
            boxShadow: "0 0 0 2px rgba(6, 182, 212, 0.2)",
            pointerEvents: "none",
          }}
        />
        <input
          type="range"
          min={min}
          max={max}
          step={1}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          style={{
            position: "absolute",
            inset: 0,
            width: "100%",
            height: "100%",
            opacity: 0,
            cursor: "pointer",
            margin: 0,
            padding: 0,
          }}
        />
      </div>
    </div>
  );
};

export default CreatePortfolioModal;
