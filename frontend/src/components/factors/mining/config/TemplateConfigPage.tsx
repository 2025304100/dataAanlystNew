import { useCallback, useEffect, useState } from "react";
import { t } from "../../../../i18n";
import { factorTemplatesApi } from "../../../../api/factorTemplates";
import type { FactorTemplate } from "../../../../api/factorTemplates";

/**
 * 因子模板配置页（C2 / B4，设计 §6.3.11）。
 *
 * 契约：`GET /factor-mining/templates`（列表）+ `POST /templates/seed`
 * （25 系统模板幂等 seed）+ `POST /templates/{id}/copy`（复制）+
 * `POST /templates/{id}/enabled`（启停）。
 *
 * - 三态容错（加载中 / 空 / 错误），接口异常不崩页；
 * - 列表含 scope / enabled 列，附「复制」「启停」操作；
 * - 「重置系统模板」按钮触发幂等 seed，操作后刷新。
 *
 * ⚠️ 设计 §6.3.11 的「按类型筛选 / 名称搜索 / 优先级排序 / 模板详情」需要后端补充
 * 字段（factor_type / priority / formula / economic_logic），当前契约未返回，
 * 前端不臆造；scope 原值照常展示（测试与审计都按原值核对）。
 */
export interface TemplateConfigPageProps {
  /** 页签激活时才拉取（与 F1ExperiencePage 同款门控） */
  active?: boolean;
}

export default function TemplateConfigPage({ active = true }: TemplateConfigPageProps) {
  const [items, setItems] = useState<FactorTemplate[]>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const page = await factorTemplatesApi.list({ limit: 200 });
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

  const seed = async () => {
    setBusy(true);
    try {
      await factorTemplatesApi.seed();
      await load();
    } catch {
      // seed 失败保持现状
    } finally {
      setBusy(false);
    }
  };

  const copy = async (templateId: string) => {
    setBusy(true);
    try {
      await factorTemplatesApi.copy(templateId);
      await load();
    } catch {
      // 复制失败保持现状
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (templateId: string, enabled: number) => {
    setBusy(true);
    try {
      await factorTemplatesApi.setEnabled(templateId, enabled ? 0 : 1);
      await load();
    } catch {
      // 启停失败保持现状
    } finally {
      setBusy(false);
    }
  };

  const enabledCount = items.filter((i) => Boolean(i.enabled)).length;
  const systemCount = items.filter((i) => String(i.scope ?? "") === "system").length;
  const personalCount = items.length - systemCount;

  return (
    <div className="mining-exp-page" data-tpl-page>
      <p className="mining-exp-desc">{t("factorTplDesc")}</p>

      {loading && <p data-tpl-loading>{t("factorTplLoading")}</p>}
      {!loading && failed && <p data-tpl-error>{t("factorTplError")}</p>}

      {!loading && !failed && (
        <div className="mining-tiles">
          <div className="mining-tile mining-tile--brand">
            <span className="mining-tile-label">{t("factorTplColName")}</span>
            <span className="mining-tile-value">{items.length}</span>
            <span className="mining-tile-note">
              {t("factorTplSummary")
                .replace("{total}", String(items.length))
                .replace("{enabled}", String(enabledCount))}
            </span>
          </div>
          <div className="mining-tile">
            <span className="mining-tile-label">{t("factorTplColScope")} system</span>
            <span className="mining-tile-value">{systemCount}</span>
          </div>
          <div className="mining-tile">
            <span className="mining-tile-label">{t("factorTplColScope")} personal</span>
            <span className="mining-tile-value">{personalCount}</span>
          </div>
          <div className="mining-tile">
            <span className="mining-tile-label">{t("factorTplColEnabled")}</span>
            <span className="mining-tile-value">{enabledCount}</span>
          </div>
        </div>
      )}

      {!loading && !failed && (
        <div className="mining-toolbar" style={{ margin: "12px 0" }}>
          <button
            type="button"
            className="mining-pool-btn primary"
            data-tpl-seed
            disabled={busy}
            onClick={() => void seed()}
          >
            {t("factorTplSeed")}
          </button>
        </div>
      )}

      {!loading && !failed && items.length === 0 && (
        <p data-tpl-empty>{t("factorTplEmpty")}</p>
      )}

      {!loading && !failed && items.length > 0 && (
        <div className="mining-table-wrap">
          <table className="mining-runs-table" data-tpl-table>
            <thead>
              <tr>
                <th style={{ width: 56 }}>{t("miningPoolColIndex")}</th>
                <th>{t("factorTplColName")}</th>
                <th>{t("factorTplColScope")}</th>
                <th>{t("factorTplColEnabled")}</th>
                <th>{t("factorTplColVersion")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((item, idx) => (
                <tr key={item.template_id}>
                  <td className="mining-cell-idx">{idx + 1}</td>
                  <td>{item.name}</td>
                  <td>
                    <span
                      className={
                        String(item.scope ?? "") === "system"
                          ? "mining-chip mining-chip--info"
                          : "mining-chip mining-chip--brand"
                      }
                    >
                      {item.scope ?? "-"}
                    </span>
                  </td>
                  <td>
                    <span
                      className={
                        item.enabled
                          ? "mining-chip mining-chip--success"
                          : "mining-chip mining-chip--muted"
                      }
                    >
                      {item.enabled ? t("factorTplEnabledOn") : t("factorTplEnabledOff")}
                    </span>
                  </td>
                  <td className="mining-cell-muted">{item.version ?? "-"}</td>
                  <td>
                    <button
                      type="button"
                      className="mining-pool-btn"
                      data-tpl-copy={item.template_id}
                      disabled={busy}
                      onClick={() => void copy(item.template_id)}
                    >
                      {t("factorTplCopy")}
                    </button>
                    <button
                      type="button"
                      className="mining-pool-btn"
                      data-tpl-toggle={item.template_id}
                      disabled={busy}
                      onClick={() => void toggle(item.template_id, item.enabled ?? 0)}
                    >
                      {item.enabled ? t("factorTplDisable") : t("factorTplEnable")}
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
