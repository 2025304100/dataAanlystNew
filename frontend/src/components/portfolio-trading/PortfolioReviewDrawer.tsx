import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CalendarDays,
  CheckCircle2,
  Clock3,
  Info,
  Loader2,
  NotebookPen,
  RefreshCw,
  X,
} from "lucide-react";
import { Button, DatePicker, Input } from "antd";
import dayjs from "dayjs";

import { api } from "../../api/client";
import { t } from "../../i18n";

/**
 * PortfolioReviewDrawer — 组合复盘记录抽屉（体检报告 §二十七）。
 *
 * 为什么是独立抽屉而不是塞进「总览」首屏：创建复盘时若不带 `report_snapshot`，
 * 后端会**实时计算整段归因报告**（`portfolios.py` create_portfolio_review），
 * 放进首屏等于让每次打开总屏都可能触发一次重算；抽屉按需拉取、离开即卸载。
 *
 * 能力来源：原 `PortfolioPerformancePanel`（从未被大改造挂载的孤儿组件）里的
 * 复盘分区。那份代码调 `createReview` 时用的是错契约（缺 start_date/end_date、
 * 字段名写成 attribution_snapshot），所以复盘此前从未创建成功（PT-DEF-26）。
 * 这里按后端 `ReviewCreate` 的真实字段实现。
 */

interface ReviewRow {
  id: number;
  portfolio_id: number;
  start_date: string;
  end_date: string;
  note?: string | null;
  title?: string | null;
  created_at?: string | null;
}

interface PortfolioReviewDrawerProps {
  open: boolean;
  onClose: () => void;
  portfolioId: number;
}

const DAY_MS = 86400000;

const isoDate = (d: Date) => d.toISOString().slice(0, 10);

/** 取最近 n 天窗口（含今天）。 */
function rangeOfLastDays(days: number): [string, string] {
  const end = new Date();
  const start = new Date(end.getTime() - (days - 1) * DAY_MS);
  return [isoDate(start), isoDate(end)];
}

/** 默认窗口：最近 30 个自然日（与后端"按范围算归因"的语义对齐）。 */
function defaultRange(): [string, string] {
  return rangeOfLastDays(30);
}

const PRESETS: Array<{ days: number; labelKey: string }> = [
  { days: 7, labelKey: "portfolioTrading.reviews.preset7" },
  { days: 30, labelKey: "portfolioTrading.reviews.preset30" },
  { days: 90, labelKey: "portfolioTrading.reviews.preset90" },
];

const PortfolioReviewDrawer: React.FC<PortfolioReviewDrawerProps> = ({
  open,
  onClose,
  portfolioId,
}) => {
  const [rows, setRows] = useState<ReviewRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 加载/保存失败可重试；反向区间是表单校验错误，不显示重试。
  const [errorRetryable, setErrorRetryable] = useState(false);
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [created, setCreated] = useState(false);
  const [range, setRange] = useState<[string, string]>(defaultRange);

  const load = useCallback(async () => {
    if (!portfolioId) return;
    setLoading(true);
    setError(null);
    setErrorRetryable(false);
    try {
      const data = await api.getReviews(portfolioId);
      setRows(Array.isArray(data) ? (data as ReviewRow[]) : []);
    } catch (err: any) {
      setError(String(err?.message ?? err));
      setErrorRetryable(true);
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [portfolioId]);

  // 抽屉关闭时不保留上一次的表单内容，避免"以为还没提交"
  useEffect(() => {
    if (open) {
      setCreated(false);
      void load();
    } else {
      setNote("");
      setError(null);
      setErrorRetryable(false);
      setRange(defaultRange());
    }
  }, [open, load]);

  // Esc 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 区间天数（含首尾），非法区间不显示
  const rangeDays = useMemo(() => {
    const [s, e] = range;
    if (!s || !e || s > e) return null;
    const ms = new Date(`${e}T00:00:00`).getTime() - new Date(`${s}T00:00:00`).getTime();
    if (!Number.isFinite(ms) || ms < 0) return null;
    return Math.round(ms / DAY_MS) + 1;
  }, [range]);

  // 命中哪个快捷区间（rangeOfLastDays 依赖"今天"，每次渲染比对即可，计算很轻）
  const activePresetDays = PRESETS.find(({ days }) => {
    const [ps, pe] = rangeOfLastDays(days);
    return ps === range[0] && pe === range[1];
  })?.days ?? null;

  const handleSubmit = useCallback(async () => {
    const trimmed = note.trim();
    if (!portfolioId || !trimmed || submitting) return;
    const [startDate, endDate] = range;
    if (!startDate || !endDate || startDate > endDate) {
      setError(t("portfolioTrading.reviews.invalidRange"));
      setErrorRetryable(false);
      return;
    }
    setSubmitting(true);
    setError(null);
    setErrorRetryable(false);
    try {
      // 只传后端认识的字段：start_date / end_date 必填；
      // 不传 report_snapshot → 服务端按该时间范围实时计算归因快照。
      await api.createReview(portfolioId, {
        start_date: startDate,
        end_date: endDate,
        note: trimmed,
      });
      setNote("");
      setCreated(true);
      await load();
    } catch (err: any) {
      setError(String(err?.message ?? err));
      setErrorRetryable(true);
    } finally {
      setSubmitting(false);
    }
  }, [note, portfolioId, range, submitting, load]);

  if (!open) return null;

  return (
    <>
      <style>{`
        @keyframes pt-review-fade-in { from { opacity: 0; } to { opacity: 1; } }
        @keyframes pt-review-slide-in {
          from { transform: translateX(100%); }
          to { transform: translateX(0); }
        }
        .pt-review-scrim { animation: pt-review-fade-in 0.2s ease-out; }
        .pt-review-panel { animation: pt-review-slide-in 0.25s cubic-bezier(0.16, 1, 0.3, 1); }
      `}</style>

      {/* 遮罩 */}
      <div
        className="pt-review-scrim"
        onClick={onClose}
        style={{ position: "fixed", inset: 0, zIndex: 1200, background: "rgba(0,0,0,0.6)" }}
      />

      {/* 抽屉面板 */}
      <aside
        role="dialog"
        aria-label={t("portfolioTrading.reviews.title")}
        data-testid="review-drawer"
        className="pt-review-panel"
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          zIndex: 1201,
          width: 560,
          maxWidth: "92vw",
          height: "100%",
          overflowY: "auto",
          padding: "20px 22px 28px",
        }}
      >
        {/* 头部 */}
        <div className="pt-review-head">
          <div className="pt-review-head-left">
            <span className="pt-review-head-icon">
              <NotebookPen size={19} />
            </span>
            <div style={{ minWidth: 0 }}>
              <div className="pt-review-head-title">{t("portfolioTrading.reviews.title")}</div>
              <div className="pt-review-head-sub">{t("portfolioTrading.reviews.subtitle")}</div>
            </div>
          </div>
          <Button
            type="text"
            shape="circle"
            onClick={onClose}
            data-testid="review-drawer-close"
            aria-label={t("portfolioTrading.reviews.close")}
            title={t("portfolioTrading.reviews.close")}
            icon={<X size={17} />}
          />
        </div>

        {/* 新建复盘 */}
        <div className="pt-review-form">
          <div>
            <label className="pt-review-field-label" htmlFor="review-start-date">
              {t("portfolioTrading.reviews.rangeLabel")}
            </label>
            <div className="pt-review-range-row">
              <DatePicker
                id="review-start-date"
                data-testid="review-start-date"
                format="YYYY-MM-DD"
                allowClear={false}
                value={range[0] ? dayjs(range[0]) : null}
                onChange={(d) => { setRange([d ? d.format("YYYY-MM-DD") : "", range[1]]); setCreated(false); }}
              />
              <span className="pt-review-range-sep">—</span>
              <DatePicker
                data-testid="review-end-date"
                format="YYYY-MM-DD"
                allowClear={false}
                value={range[1] ? dayjs(range[1]) : null}
                onChange={(d) => { setRange([range[0], d ? d.format("YYYY-MM-DD") : ""]); setCreated(false); }}
              />
              {rangeDays != null && (
                <span className="pt-review-range-days">
                  {t("portfolioTrading.reviews.days").replace("{n}", String(rangeDays))}
                </span>
              )}
            </div>
            <div className="pt-review-presets" style={{ marginTop: 9 }}>
              {PRESETS.map(({ days, labelKey }) => (
                <Button
                  key={days}
                  type={activePresetDays === days ? "primary" : "default"}
                  size="small"
                  shape="round"
                  ghost={activePresetDays === days}
                  onClick={() => { setRange(rangeOfLastDays(days)); setCreated(false); }}
                >
                  {t(labelKey)}
                </Button>
              ))}
            </div>
          </div>

          <div>
            <label className="pt-review-field-label" htmlFor="review-note-input">
              {t("portfolioTrading.reviews.noteLabel")}
            </label>
            <Input.TextArea
              id="review-note-input"
              data-testid="review-note-input"
              rows={3}
              value={note}
              placeholder={t("portfolioTrading.reviews.notePlaceholder")}
              onChange={(e) => { setNote(e.target.value); setCreated(false); }}
            />
          </div>

          <div className="pt-review-form-foot">
            <span className="pt-review-hint">
              <Info size={13} style={{ flexShrink: 0, marginTop: 1 }} />
              {t("portfolioTrading.reviews.autoAttributionHint")}
            </span>
            <Button
              type="primary"
              data-testid="review-submit-button"
              disabled={!note.trim() || submitting}
              loading={submitting}
              onClick={() => void handleSubmit()}
              style={{ minWidth: 108 }}
            >
              {!submitting && <CheckCircle2 size={14} />}
              {submitting ? t("portfolioTrading.reviews.submitting") : t("portfolioTrading.reviews.submit")}
            </Button>
          </div>

          {created && (
            <div className="pt-review-success" data-testid="review-created">
              <CheckCircle2 size={14} />
              {t("portfolioTrading.reviews.created")}
            </div>
          )}
        </div>

        {/* 错误条：加载/保存失败带重试；区间校验错误不带 */}
        {error && !loading && (
          <div className="pt-review-error" data-testid="review-error" style={{ marginTop: 14 }}>
            <span style={{ display: "inline-flex", alignItems: "flex-start", gap: 7 }}>
              <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 1 }} />
              <span>
                {errorRetryable ? `${t("portfolioTrading.reviews.failed")}: ` : ""}
                {error}
              </span>
            </span>
            {errorRetryable && (
              <Button size="small" icon={<RefreshCw size={12} />} onClick={() => void load()}>
                {t("portfolioTrading.reviews.retry")}
              </Button>
            )}
          </div>
        )}

        {/* 历史复盘 */}
        {rows.length > 0 && (
          <div className="pt-review-list-head">
            <CalendarDays size={14} style={{ color: "var(--pt-primary)" }} />
            {t("portfolioTrading.reviews.history")}
            <span className="pt-review-list-count">{rows.length}</span>
          </div>
        )}

        {loading && (
          <div className="pt-review-loading" data-testid="review-loading">
            <Loader2 size={15} className="pt-review-spin" />
            {t("portfolioTrading.reviews.loading")}
          </div>
        )}

        {!loading && rows.length === 0 && !error && (
          <div className="pt-review-empty" data-testid="review-empty" style={{ marginTop: 8 }}>
            <span className="pt-review-empty-icon">
              <NotebookPen size={26} />
            </span>
            <span className="pt-review-empty-title">{t("portfolioTrading.reviews.empty")}</span>
            <span className="pt-review-empty-sub">{t("portfolioTrading.reviews.emptyHint")}</span>
          </div>
        )}

        <ul className="pt-review-timeline" data-testid="review-list">
          {rows.map((row) => (
            <li key={row.id} className="pt-review-card" data-testid="review-item">
              <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap" }}>
                <span className="pt-review-card-range">
                  <CalendarDays size={11} />
                  {String(row.start_date)} ~ {String(row.end_date)}
                </span>
                {row.created_at && (
                  <span className="pt-review-card-time">
                    <Clock3 size={10} style={{ display: "inline", verticalAlign: "-1px", marginRight: 3 }} />
                    {String(row.created_at).slice(0, 16).replace("T", " ")}
                  </span>
                )}
              </div>
              {row.title && <div className="pt-review-card-title">{String(row.title)}</div>}
              <div className="pt-review-card-note">{String(row.note ?? "")}</div>
            </li>
          ))}
        </ul>
      </aside>
    </>
  );
};

export default PortfolioReviewDrawer;
