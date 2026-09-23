import { useCallback, useEffect, useState } from "react";
import { t } from "../../../../i18n";
import { factorExperienceApi } from "../../../../api/factorExperience";
import type { F1Experience } from "../../../../api/factorExperience";

/**
 * F1 历史经验库列表页（B3 挂载；对外契约 GET /factor-experience）。
 *
 * - 三态容错（加载中 / 空 / 错误），接口异常不崩页；
 * - 列表列：公式模板 / 类别 / 来源 / 平均 ICIR / 负样本标记 / 状态；
 * - 归档按钮：POST /{id}/archive（抽取与抽样硬化排除），操作后刷新。
 *
 * ⚠️ 设计 §6.9.7 还要求「使用次数 / 成功率 / 最后使用 / 筛选 / 排序 / 详情弹窗」，
 * 当前契约（F1Experience）未返回这些字段，**前端不臆造**，待后端补字段后接入。
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
      <p className="mining-exp-desc">{t("miningExpDesc")}</p>

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
