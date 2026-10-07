import { useCallback, useEffect, useState } from "react";
import { t } from "../../../../i18n";
import { factorExperienceApi } from "../../../../api/factorExperience";
import type { F1Experience } from "../../../../api/factorExperience";

/**
 * F1 历史经验库列表页（B3 挂载；对外契约 GET /factor-experience）。
 *
 * - 三态容错（加载中 / 空 / 错误），接口异常不崩页；
 * - 列表列：公式模板 / 类别 / 来源 / 平均 ICIR / 使用次数 / 成功率 / 负样本标记 / 状态；
 * - 归档按钮：POST /{id}/archive（抽取与抽样硬化排除），操作后刷新。
 *
 * ⚠️ 2026-10-02 勘误：原注释称「使用次数 / 成功率」契约未返回、待后端补字段——**该判断有误**。
 * 实测 `app/services/factors/experience/service.py` 早就在输出 `use_count / success_count /
 * success_rate`，前端类型 `F1Experience` 也已声明；缺的是**本页渲染**。已补齐两列。
 *
 * 设计 §6.9.7 剩余未接入项：`last_used_at`（后端确未提供，本页不臆造）、
 * 筛选/排序 UI、详情弹窗（`GET /{id}` 已存在，可直连）。
 */
export interface F1ExperiencePageProps {
  /** 页签激活时才拉取（与 MiningShell 批次列表同款，避免隐藏挂载即请求） */
  active?: boolean;
}

/** 状态 → 语义标签（未知状态按中性展示，不改写后端取值） */
function statusChipClass(status: string | null | undefined): string {
  const v = String(status ?? "").toLowerCase();
  if (v === "premium") return "mining-chip mining-chip--success";
  if (v === "archived") return "mining-chip mining-chip--muted";
  if (v === "need_field" || v === "unavailable") return "mining-chip mining-chip--warn";
  return "mining-chip";
}

/**
 * 成功率展示口径：
 * - `use_count=0`（从未被抽样）→ 成功率是 0/0，无意义，显示 "-"（**不能显示 0.0%**，
 *   那会被读成"用过且全失败"）；
 * - 用过且有值 → 百分比保留 1 位；
 * - 契约未返回 → "-"。
 */
function rateText(exp: F1Experience): string {
  const used = Number(exp.use_count ?? 0);
  if (!used || exp.success_rate == null) return "-";
  return `${(Number(exp.success_rate) * 100).toFixed(1)}%`;
}

export default function F1ExperiencePage({ active = true }: F1ExperiencePageProps) {
  const [items, setItems] = useState<F1Experience[]>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const page = await factorExperienceApi.list({ page: 1, page_size: 50 });
      setItems(Array.isArray(page?.items) ? page.items : []);
    } catch {
      setItems([]);
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  const archive = async (experienceId: string) => {
    try {
      await factorExperienceApi.archive(experienceId);
      await load();
    } catch {
      // 归档失败保持原列表
    }
  };

  const negativeCount = items.filter((e) => Boolean(e.is_negative_sample)).length;
  const archivedCount = items.filter((e) => e.status === "archived").length;

  return (
    <div className="mining-exp-page" data-f1-exp-page>
      <div className="mining-page-head">
        <div className="mining-page-head-text">
          <span className="mining-page-title">{t("miningExpPageTitle")}</span>
          <span className="mining-page-sub">{t("miningExpDesc")}</span>
        </div>
      </div>

      {!loading && !failed && items.length > 0 && (
        <div className="mining-tiles">
          <div className="mining-tile">
            <span className="mining-tile-label">{t("miningExpColTemplate")}</span>
            <span className="mining-tile-value">{items.length}</span>
          </div>
          <div className="mining-tile">
            <span className="mining-tile-label">{t("miningExpNegative")}</span>
            <span className="mining-tile-value">{negativeCount}</span>
          </div>
          <div className="mining-tile">
            <span className="mining-tile-label">{t("miningExpArchive")}</span>
            <span className="mining-tile-value">{archivedCount}</span>
          </div>
        </div>
      )}

      {loading && <p data-f1-exp-loading>{t("miningExpLoading")}</p>}
      {!loading && failed && <p data-f1-exp-error>{t("miningExpError")}</p>}
      {!loading && !failed && items.length === 0 && (
        <p data-f1-exp-empty>{t("miningExpEmpty")}</p>
      )}
      {!loading && !failed && items.length > 0 && (
        <div className="mining-exp-table-wrap">
          <table className="mining-exp-table" data-f1-exp-table>
            <thead>
              <tr>
                <th style={{ width: 56 }}>{t("miningPoolColIndex")}</th>
                <th>{t("miningExpColTemplate")}</th>
                <th>{t("miningExpColCategory")}</th>
                <th>{t("miningExpColSource")}</th>
                <th className="mining-cell-num">{t("miningExpColIc")}</th>
                <th className="mining-cell-num">{t("miningExpColUseCount")}</th>
                <th className="mining-cell-num">{t("miningExpColSuccessRate")}</th>
                <th>{t("miningExpColStatus")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((exp, idx) => (
                <tr key={exp.experience_id}>
                  <td className="mining-cell-idx">{idx + 1}</td>
                  <td className="mining-cell-formula">
                    {exp.formula_template}
                    {Boolean(exp.is_negative_sample) && (
                      <span className="mining-exp-negative" data-f1-exp-negative>
                        {t("miningExpNegative")}
                      </span>
                    )}
                  </td>
                  <td>
                    <span className="mining-chip mining-chip--brand">
                      {exp.category ?? "-"}
                    </span>
                  </td>
                  <td>{exp.source ?? "-"}</td>
                  <td className="mining-cell-num">
                    {exp.avg_icir != null ? Number(exp.avg_icir).toFixed(4) : "-"}
                  </td>
                  <td className="mining-cell-num" data-f1-exp-use-count>
                    {exp.use_count != null ? exp.use_count : "-"}
                  </td>
                  <td className="mining-cell-num" data-f1-exp-success-rate>
                    {rateText(exp)}
                  </td>
                  <td>
                    <span className={statusChipClass(exp.status)}>{exp.status ?? "-"}</span>
                  </td>
                  <td>
                    <button
                      type="button"
                      className="mining-pool-btn"
                      data-f1-exp-archive={exp.experience_id}
                      disabled={exp.status === "archived"}
                      onClick={() => void archive(exp.experience_id)}
                    >
                      {t("miningExpArchive")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
