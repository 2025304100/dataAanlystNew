import React, { useCallback, useEffect, useState } from "react";

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

const isoDate = (d: Date) => d.toISOString().slice(0, 10);

/** 默认窗口：最近 30 个自然日（与后端"按范围算归因"的语义对齐）。 */
function defaultRange(): [string, string] {
  const end = new Date();
  const start = new Date(end.getTime() - 29 * 86400000);
  return [isoDate(start), isoDate(end)];
}

const PortfolioReviewDrawer: React.FC<PortfolioReviewDrawerProps> = ({
  open,
  onClose,
  portfolioId,
}) => {
  const [rows, setRows] = useState<ReviewRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [created, setCreated] = useState(false);
  const [range, setRange] = useState<[string, string]>(defaultRange);

  const load = useCallback(async () => {
    if (!portfolioId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await api.getReviews(portfolioId);
      setRows(Array.isArray(data) ? (data as ReviewRow[]) : []);
    } catch (err: any) {
      setError(String(err?.message ?? err));
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
      setRange(defaultRange());
    }
  }, [open, load]);

  const handleSubmit = useCallback(async () => {
    const trimmed = note.trim();
    if (!portfolioId || !trimmed || submitting) return;
    const [startDate, endDate] = range;
    if (!startDate || !endDate || startDate > endDate) {
      setError(t("portfolioTrading.reviews.invalidRange"));
      return;
    }
    setSubmitting(true);
    setError(null);
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
    } finally {
      setSubmitting(false);
    }
  }, [note, portfolioId, range, submitting, load]);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-label={t("portfolioTrading.reviews.title")}
      data-testid="review-drawer"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1200,
        display: "flex",
        justifyContent: "flex-end",
        background: "rgba(15,23,42,0.45)",
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 520,
          maxWidth: "92vw",
          height: "100%",
          overflowY: "auto",
          background: "var(--pt-card-bg, #fff)",
          borderLeft: "1px solid var(--pt-border, #e2e8f0)",
          padding: 16,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
          <h2 style={{ fontSize: 15, margin: 0 }}>{t("portfolioTrading.reviews.title")}</h2>
          <button type="button" className="pt-btn pt-btn-ghost pt-btn-sm" onClick={onClose} data-testid="review-drawer-close">
            {t("portfolioTrading.reviews.close")}
          </button>
        </div>

        <div style={{ border: "1px solid var(--pt-border, #e2e8f0)", borderRadius: 8, padding: 12, marginBottom: 14 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8, fontSize: 12 }}>
            <label>
              {t("portfolioTrading.reviews.startDate")}
              <input
                type="date"
                data-testid="review-start-date"
                value={range[0]}
                onChange={(e) => setRange([e.target.value, range[1]])}
                style={{ marginLeft: 6 }}
              />
            </label>
            <label>
              {t("portfolioTrading.reviews.endDate")}
              <input
                type="date"
                data-testid="review-end-date"
                value={range[1]}
                onChange={(e) => setRange([range[0], e.target.value])}
                style={{ marginLeft: 6 }}
              />
            </label>
          </div>
          <textarea
            data-testid="review-note-input"
            rows={3}
            value={note}
            placeholder={t("portfolioTrading.reviews.notePlaceholder")}
            onChange={(e) => { setNote(e.target.value); setCreated(false); }}
            style={{ width: "100%", boxSizing: "border-box", fontSize: 13 }}
          />
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 8 }}>
            <span style={{ fontSize: 11, color: "var(--pt-muted-foreground)" }}>{t("portfolioTrading.reviews.autoAttributionHint")}</span>
            <button
              type="button"
              className="pt-btn pt-btn-primary pt-btn-sm"
              data-testid="review-submit-button"
              disabled={!note.trim() || submitting}
              onClick={() => void handleSubmit()}
            >
              {submitting ? t("portfolioTrading.reviews.submitting") : t("portfolioTrading.reviews.submit")}
            </button>
          </div>
          {created && <div data-testid="review-created" style={{ fontSize: 12, marginTop: 6 }}>{t("portfolioTrading.reviews.created")}</div>}
        </div>

        {loading && <div data-testid="review-loading">{t("portfolioTrading.reviews.loading")}</div>}
        {error && !loading && (
          <div data-testid="review-error" style={{ fontSize: 12, color: "var(--pt-state-error)", marginBottom: 8 }}>
            {t("portfolioTrading.reviews.failed")}: {error}
          </div>
        )}
        {!loading && rows.length === 0 && !error && (
          <div data-testid="review-empty" style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
            {t("portfolioTrading.reviews.empty")}
          </div>
        )}
        <ul data-testid="review-list" style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 8 }}>
          {rows.map((row) => (
            <li key={row.id} data-testid="review-item" style={{ border: "1px solid var(--pt-border, #e2e8f0)", borderRadius: 8, padding: "8px 10px" }}>
              <div style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                {String(row.start_date)} ~ {String(row.end_date)}
                {row.created_at ? ` · ${String(row.created_at).slice(0, 16).replace("T", " ")}` : ""}
              </div>
              {row.title && <div style={{ fontSize: 13, fontWeight: 600 }}>{String(row.title)}</div>}
              <div style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>{String(row.note ?? "")}</div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
};

export default PortfolioReviewDrawer;
