import { useCallback, useEffect, useMemo, useState } from "react";
import { t } from "../../../../i18n";
import { factorTemplatesApi } from "../../../../api/factorTemplates";
import type { FactorTemplate } from "../../../../api/factorTemplates";
import { ALL_CATEGORIES, categoryLabel } from "../factorCategory";

/**
 * 因子模板配置页（C2 / B4，设计 §6.3.11）。
 *
 * 契约：`GET /factor-mining/templates`（列表）+ `POST /templates/seed`
 * （25 系统模板幂等 seed）+ `POST /templates/{id}/copy`（复制）+
 * `POST /templates/{id}/enabled`（启停）。
 *
 * - 三态容错（加载中 / 空 / 错误），接口异常不崩页；
 * - 列表列：名称 / 类型 / 公式结构 / 优先级 / 适用范围 / 启用 / 版本 / 创建时间；
 * - 工具条：按名称或公式搜索 · 按类型筛选 · 按优先级/名称/创建时间排序
 *   （三者均在前端完成——列表一次拉全量 limit=200，25 条量级无分页必要）；
 * - 详情弹窗（**只读**，不含任何 input/select/textarea）：公式结构 / 经济逻辑 /
 *   参数范围 / 依赖字段 / 适用范围 / 优先级 / 版本 / 创建时间；
 * - 「重置系统模板」按钮触发幂等 seed，操作后刷新。
 *
 * ⚠️ 2026-10-02 勘误：原注释称 §6.3.11 所需字段（factor_type / priority /
 * formula / economic_logic）**后端未返回、待补**——该判断有误。实测
 * `template_service._to_view` 把这些字段放在 `rule_config` 内层返回（字段名是
 * `category` 而非 `factor_type`），25 条系统模板 100% 齐备。缺的是**本页渲染**。
 *
 * 仍未接入（确需后端补，前端不臆造）：
 * - 创建来源三分类（系统预设 / AI生成保存 / 手动创建）——契约只有 `scope`
 *   (system|personal) 与 `owner`，无法区分"AI生成保存"与"手动创建"；
 * - 个人模板的编辑 / 删除 / 调优先级——契约只有 create / copy / enabled，
 *   无 update / delete 路由。
 * `scope` 原值照常展示（测试与审计都按原值核对），译名只在详情里给出。
 */
export interface TemplateConfigPageProps {
  /** 页签激活时才拉取（与 F1ExperiencePage 同款门控） */
  active?: boolean;
}

type SortKey = "priority" | "name" | "created";

/** 排序键 → 标签 key（工具条下拉用） */
const SORT_LABEL_KEY: Record<SortKey, string> = {
  priority: "factorTplColPriority",
  name: "factorTplColName",
  created: "factorTplColCreated",
};

/** 优先级缺省值：排到最后（后端 1~5，数值越小优先级越高） */
const PRIORITY_FALLBACK = Number.MAX_SAFE_INTEGER;

/**
 * 参数范围 → 可读文本。
 * 形如 `{n1:[14,20], n2:[26]}` → `n1: 14~20 · n2: 26`；空对象返回 "-"（不臆造）。
 */
function paramsText(params?: Record<string, unknown>): string {
  if (!params) return "-";
  const parts = Object.entries(params).map(([k, v]) => {
    if (Array.isArray(v) && v.length > 0) {
      return v.length === 1 ? `${k}: ${v[0]}` : `${k}: ${v[0]}~${v[v.length - 1]}`;
    }
    if (v == null || v === "") return `${k}: -`;
    return `${k}: ${String(v)}`;
  });
  return parts.length > 0 ? parts.join(" · ") : "-";
}

/** 依赖字段数组 → 文本 */
function fieldsText(fields?: string[]): string {
  return fields && fields.length > 0 ? fields.join(" · ") : "-";
}

export default function TemplateConfigPage({ active = true }: TemplateConfigPageProps) {
  const [items, setItems] = useState<FactorTemplate[]>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  // 工具条状态（纯前端）
  const [query, setQuery] = useState("");
  const [catFilter, setCatFilter] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("priority");
  const [sortAsc, setSortAsc] = useState(true);
  const [detail, setDetail] = useState<FactorTemplate | null>(null);

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

  /** 筛选下拉里只列**实际出现过**的类别（避免选了必然为空的项） */
  const presentCategories = useMemo(() => {
    const seen = new Set(
      items.map((i) => String(i.rule_config?.category ?? "")).filter(Boolean),
    );
    const known = ALL_CATEGORIES.filter((c) => seen.has(c));
    const unknown = [...seen].filter(
      (c) => !(ALL_CATEGORIES as readonly string[]).includes(c),
    );
    return [...known, ...unknown.sort()];
  }, [items]);

  /** 过滤 + 排序后的可见列表 */
  const view = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = items.filter((i) => {
      const cat = String(i.rule_config?.category ?? "");
      if (catFilter && cat !== catFilter) return false;
      if (!q) return true;
      const hay = `${i.name ?? ""} ${i.rule_config?.formula ?? ""}`.toLowerCase();
      return hay.includes(q);
    });

    const dir = sortAsc ? 1 : -1;
    return [...filtered].sort((a, b) => {
      if (sortKey === "priority") {
        const pa = Number(a.rule_config?.priority ?? PRIORITY_FALLBACK);
        const pb = Number(b.rule_config?.priority ?? PRIORITY_FALLBACK);
        if (pa !== pb) return (pa - pb) * dir;
        // 优先级并列时按名称稳定排序，避免顺序抖动
        return String(a.name ?? "").localeCompare(String(b.name ?? ""));
      }
      if (sortKey === "created") {
        const d = String(a.created_at ?? "").localeCompare(String(b.created_at ?? ""));
        if (d !== 0) return d * dir;
        return String(a.name ?? "").localeCompare(String(b.name ?? ""));
      }
      return String(a.name ?? "").localeCompare(String(b.name ?? "")) * dir;
    });
  }, [items, query, catFilter, sortKey, sortAsc]);

  const enabledCount = items.filter((i) => Boolean(i.enabled)).length;
  const systemCount = items.filter((i) => String(i.scope ?? "") === "system").length;
  const personalCount = items.length - systemCount;
  const filtering = Boolean(query.trim()) || Boolean(catFilter);

  return (
    <div className="mining-exp-page" data-tpl-page>
      <div className="mining-page-head">
        <div className="mining-page-head-text">
          <span className="mining-page-title">{t("factorTplPageTitle")}</span>
          <span className="mining-page-sub">{t("factorTplDesc")}</span>
        </div>
      </div>

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
            <span className="mining-tile-label">{t("factorTplScopeSystem")}</span>
            <span className="mining-tile-value">{systemCount}</span>
          </div>
          <div className="mining-tile">
            <span className="mining-tile-label">{t("factorTplScopePersonal")}</span>
            <span className="mining-tile-value">{personalCount}</span>
          </div>
          <div className="mining-tile">
            <span className="mining-tile-label">{t("factorTplColEnabled")}</span>
            <span className="mining-tile-value">{enabledCount}</span>
          </div>
        </div>
      )}

      {!loading && !failed && items.length > 0 && (
        <div className="mining-toolbar mining-tpl-toolbar">
          <input
            type="search"
            className="mining-tpl-search"
            data-tpl-search
            value={query}
            placeholder={t("factorTplSearch")}
            aria-label={t("factorTplSearch")}
            onChange={(e) => setQuery(e.target.value)}
          />
          <select
            className="mining-tpl-select"
            data-tpl-cat
            value={catFilter}
            aria-label={t("factorTplColCategory")}
            onChange={(e) => setCatFilter(e.target.value)}
          >
            <option value="">{t("factorTplFilterAll")}</option>
            {presentCategories.map((c) => (
              <option key={c} value={c}>
                {categoryLabel(c, t)}
              </option>
            ))}
          </select>
          <select
            className="mining-tpl-select"
            data-tpl-sort
            value={sortKey}
            aria-label={t("factorTplSortBy")}
            onChange={(e) => setSortKey(e.target.value as SortKey)}
          >
            {(Object.keys(SORT_LABEL_KEY) as SortKey[]).map((k) => (
              <option key={k} value={k}>
                {t(SORT_LABEL_KEY[k])}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="mining-pool-btn"
            data-tpl-sort-dir
            aria-label={t("factorTplSortToggle")}
            onClick={() => setSortAsc((v) => !v)}
          >
            {sortAsc ? t("factorTplSortAsc") : t("factorTplSortDesc")}
          </button>
          {filtering && (
            <span className="mining-tpl-match" data-tpl-match>
              {t("factorTplMatch")
                .replace("{n}", String(view.length))
                .replace("{total}", String(items.length))}
            </span>
          )}
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

      {/* 没有 seed 过：库是空的（与"筛选后无匹配"区分开） */}
      {!loading && !failed && items.length === 0 && (
        <p data-tpl-empty>{t("factorTplEmpty")}</p>
      )}

      {!loading && !failed && items.length > 0 && view.length === 0 && (
        <p data-tpl-no-match>{t("factorTplNoMatch")}</p>
      )}

      {!loading && !failed && view.length > 0 && (
        <div className="mining-table-wrap">
          <table className="mining-runs-table" data-tpl-table>
            <thead>
              <tr>
                <th style={{ width: 56 }}>{t("miningPoolColIndex")}</th>
                <th>{t("factorTplColName")}</th>
                <th>{t("factorTplColCategory")}</th>
                <th>{t("factorTplColFormula")}</th>
                <th className="mining-cell-num">{t("factorTplColPriority")}</th>
                <th>{t("factorTplColScope")}</th>
                <th>{t("factorTplColEnabled")}</th>
                <th>{t("factorTplColVersion")}</th>
                <th>{t("factorTplColCreated")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {view.map((item, idx) => (
                <tr key={item.template_id}>
                  <td className="mining-cell-idx">{idx + 1}</td>
                  <td>{item.name}</td>
                  <td data-tpl-cell="category">
                    <span className="mining-chip mining-chip--brand">
                      {categoryLabel(item.rule_config?.category, t)}
                    </span>
                  </td>
                  <td className="mining-cell-formula" data-tpl-cell="formula">
                    {item.rule_config?.formula ?? "-"}
                  </td>
                  <td className="mining-cell-num" data-tpl-cell="priority">
                    {item.rule_config?.priority ?? "-"}
                  </td>
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
                  <td className="mining-cell-muted" data-tpl-cell="created">
                    {item.created_at ?? "-"}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="mining-pool-btn"
                      data-tpl-detail={item.template_id}
                      onClick={() => setDetail(item)}
                    >
                      {t("factorTplDetail")}
                    </button>
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

      {/* 详情弹窗：只读，禁 input/select/textarea（与 PoolAnalysisModal 同款约束） */}
      {detail && (
        <div className="mining-pool-modal-mask" role="dialog" aria-modal="true">
          <div className="mining-pool-modal" data-tpl-detail-modal>
            <header className="mining-pool-modal-head">
              <h3>
                {t("factorTplDetailTitle")}
                <span className="mining-cell-muted"> · {detail.name}</span>
              </h3>
              <button
                type="button"
                className="mining-pool-modal-close"
                data-tpl-detail-close
                aria-label={t("factorTplClose")}
                onClick={() => setDetail(null)}
              >
                ×
              </button>
            </header>

            <div className="mining-tpl-detail">
              <div className="mining-tpl-detail-row">
                <span className="mining-tpl-detail-label">
                  {t("factorTplColFormula")}
                </span>
                <code className="mining-tpl-detail-code" data-tpl-detail-formula>
                  {detail.rule_config?.formula ?? "-"}
                </code>
              </div>
              <div className="mining-tpl-detail-row">
                <span className="mining-tpl-detail-label">
                  {t("factorTplDetailEconomy")}
                </span>
                <span data-tpl-detail-economy>
                  {detail.rule_config?.economy_logic_zh ?? detail.description ?? "-"}
                </span>
              </div>
              <div className="mining-tpl-detail-row">
                <span className="mining-tpl-detail-label">
                  {t("factorTplDetailParams")}
                </span>
                <span data-tpl-detail-params>
                  {paramsText(detail.rule_config?.params)}
                </span>
              </div>
              <div className="mining-tpl-detail-row">
                <span className="mining-tpl-detail-label">
                  {t("factorTplDetailFields")}
                </span>
                <span data-tpl-detail-fields>
                  {fieldsText(detail.rule_config?.required_fields)}
                </span>
              </div>
              <div className="mining-tpl-detail-row">
                <span className="mining-tpl-detail-label">
                  {t("factorTplColCategory")}
                </span>
                <span>{categoryLabel(detail.rule_config?.category, t)}</span>
              </div>
              <div className="mining-tpl-detail-row">
                <span className="mining-tpl-detail-label">
                  {t("factorTplColPriority")}
                </span>
                <span>{detail.rule_config?.priority ?? "-"}</span>
              </div>
              <div className="mining-tpl-detail-row">
                <span className="mining-tpl-detail-label">
                  {t("factorTplDetailScope")}
                </span>
                <span>
                  {String(detail.scope ?? "") === "system"
                    ? t("factorTplScopeSystem")
                    : t("factorTplScopePersonal")}
                  <span className="mining-cell-muted"> ({detail.scope ?? "-"})</span>
                </span>
              </div>
              <div className="mining-tpl-detail-row">
                <span className="mining-tpl-detail-label">
                  {t("factorTplDetailOwner")}
                </span>
                <span>{detail.owner ?? "-"}</span>
              </div>
              <div className="mining-tpl-detail-row">
                <span className="mining-tpl-detail-label">
                  {t("factorTplColVersion")}
                </span>
                <span>{detail.version ?? "-"}</span>
              </div>
              <div className="mining-tpl-detail-row">
                <span className="mining-tpl-detail-label">
                  {t("factorTplColCreated")}
                </span>
                <span>{detail.created_at ?? "-"}</span>
              </div>
            </div>

            <footer className="mining-pool-modal-foot">
              <button
                type="button"
                className="mining-pool-btn primary"
                data-tpl-detail-close
                onClick={() => setDetail(null)}
              >
                {t("factorTplClose")}
              </button>
            </footer>
          </div>
        </div>
      )}
    </div>
  );
}
