import { useCallback, useEffect, useRef, useState } from "react";
import { t } from "../../../../../i18n";
import { Modal } from "antd";
import PoolAnalysisModal from "./PoolAnalysisModal";
import PoolFilterPanel from "./PoolFilterPanel";
import PoolImportPanel from "./PoolImportPanel";
import PoolLockBanner from "./PoolLockBanner";
import PoolMemberTable from "./PoolMemberTable";
import {
  MIN_POOL_SIZE,
  createPool,
  createPoolFromFilter,
  createSnapshot,
  deleteLatestSnapshot,
  downloadImportTemplate,
  exportImportErrors,
  fetchMembers,
  importPoolMembers,
  previewImport,
  previewPool,
  removeMembers,
} from "./poolApi";
import type {
  ImportPreviewResult,
  PoolMember,
  PoolPreview,
  PoolSnapshot,
} from "./poolApi";

/**
 * Step1 候选池（向导 §3）—— 入口互斥 + 防抖预览 + 生成物料 + 锁定 + 看板。
 *
 * 关键口径：
 * - 两种入口（条件筛选 / 导入）**严格互斥**，切换前必须确认清空未保存配置；
 * - 条件变化 **300ms 防抖** 调预览；**防抖预览只用于展示，不得作为挖掘快照**
 *   （快照只由后端按数据截止日重新完整计算并冻结）；
 * - 「生成挖掘物料」= `createSnapshot(analyze=true)`，**分析完成才锁定**；
 *   锁定后筛选/导入/批量删除全部置灰，[重新选择] 二次确认后解锁并清分析数据；
 * - 有效标的 **<50 硬阻断**（系统下限，前端不得降低）。
 */
const DEBOUNCE_MS = 300;

export interface MiningPoolStepProps {
  poolId?: string;
  onNext?: () => void;
  /** 快照生成/重选后向上上报（A4：MiningShell 组装挖掘 payload 用） */
  onSnapshot?: (snap: PoolSnapshot | null) => void;
  /** P0-1：按筛选条件物化候选池后上报池 id（MiningShell 持有并下发，避免双斜杠 405） */
  onPoolCreated?: (poolId: string) => void;
}

export default function MiningPoolStep({
  poolId = "",
  onNext,
  onSnapshot,
  onPoolCreated,
}: MiningPoolStepProps) {
  const [entry, setEntry] = useState<"filter" | "import">("filter");
  const [pendingEntry, setPendingEntry] = useState<"filter" | "import" | null>(null);
  const [filterConfig, setFilterConfig] = useState<Record<string, unknown>>({});
  const [preview, setPreview] = useState<PoolPreview | null>(null);
  const [locked, setLocked] = useState(false);
  const [snapshot, setSnapshot] = useState<PoolSnapshot | null>(null);
  const [generating, setGenerating] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [confirmReselect, setConfirmReselect] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [members, setMembers] = useState<PoolMember[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  // P1-6：导入链路（预览结果 / 待入池文件 / 进行中 / 结构化错误）
  const [importResult, setImportResult] = useState<ImportPreviewResult | null>(null);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // P0-4：blocked 以 `can_generate` 为准（后端已综合命中/覆盖/范围冲突等全部阻断），
  // 并叠加 hits<50 兜底（系统硬下限，前端不得降低）。
  const blocked = Boolean(preview && (!preview.can_generate || preview.hits < MIN_POOL_SIZE));
  const firstBlocker = preview?.blocking_issues?.[0];

  // ── 防抖预览（仅展示，不落库）─────────────────────────────────────
  const requestPreview = useCallback((config: Record<string, unknown>) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      void previewPool(config).then(setPreview).catch(() => setPreview(null));
    }, DEBOUNCE_MS);
  }, []);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  // ── 成员列表（池已存在时拉取；锁定态仍可查看，只是不能改）─────────
  useEffect(() => {
    if (!poolId) return;
    void fetchMembers(poolId, { page: 1, page_size: 20 })
      .then((page) => setMembers(page?.items ?? []))
      .catch(() => setMembers([]));
  }, [poolId]);

  const onFilterChange = (next: Record<string, unknown>) => {
    setFilterConfig(next);
    requestPreview(next);
  };

  // ── 入口互斥（切换前确认清空）─────────────────────────────────────
  const trySwitch = (target: "filter" | "import") => {
    if (target === entry) return;
    setPendingEntry(target);
  };

  const doSwitch = () => {
    if (pendingEntry) setEntry(pendingEntry);
    setPendingEntry(null);
    setFilterConfig({});
    setPreview(null);
  };

  // ── 生成挖掘物料 / 查看看板 ───────────────────────────────────────
  // P0-1：无池时先 `createPoolFromFilter`（命中<50/覆盖不足/范围冲突 → 4xx
  // 且不落库，按契约展示阻断），拿到 pool.id 再 `createSnapshot(analyze=true)`；
  // 避免空 id 拼出 `/candidate-pools//snapshot` → 405。
  const onGenerateClick = async () => {
    if (blocked) return;
    if (locked) {
      setShowModal(true);
      return;
    }
    setGenerating(true);
    try {
      let pid = poolId;
      if (!pid) {
        const pool = await createPoolFromFilter({
          name: "因子挖掘候选池",
          filter_config: filterConfig,
        });
        pid = pool.id;
        onPoolCreated?.(pool.id);
      }
      const snap = await createSnapshot(pid, { analyze: true });
      setSnapshot(snap);
      setLocked(Boolean(snap?.is_locked));
      onSnapshot?.(snap);
    } catch (e) {
      // 结构化阻断（后端 4xx 带 detail.error_code/detail_zh）落到预览阻断区展示
      const detail = (e as { detail?: unknown })?.detail;
      const err =
        detail != null && typeof detail === "object"
          ? (detail as { error_code?: string; detail_zh?: string })
          : null;
      if (err?.detail_zh) {
        const detailZh: string = err.detail_zh;
        setPreview((prev) =>
          prev
            ? { ...prev, can_generate: false, blocking_issues: [{ error_code: err.error_code ?? "VALIDATION_ERROR", detail_zh: detailZh }] }
            : {
                universe_size: 0,
                hits: 0,
                excluded_total: 0,
                min_pool_size: MIN_POOL_SIZE,
                can_generate: false,
                by_rule: [],
                warnings: [],
                blocking_issues: [{ error_code: err.error_code ?? "VALIDATION_ERROR", detail_zh: detailZh }],
              },
        );
      }
    } finally {
      setGenerating(false);
    }
  };

  const onReselectConfirmed = async () => {
    await deleteLatestSnapshot(poolId).catch(() => undefined);
    setLocked(false);
    setSnapshot(null);
    setShowModal(false);
    setConfirmReselect(false);
    onSnapshot?.(null);
  };

  const onDeleteConfirmed = async () => {
    if (selected.length > 0) {
      await removeMembers(poolId, selected).catch(() => undefined);
    }
    setSelected([]);
    setConfirmDelete(false);
  };

  const toggleOne = (id: number) =>
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]);

  const toggleAll = () =>
    setSelected((prev) =>
      prev.length > 0 ? [] : members.map((m, i) => m.symbol_id ?? i));

  // ── P1-6：导入股票池（模板 / 预览 / 入池 / 错误明细）──────────────
  const handleDownloadTemplate = () => {
    void downloadImportTemplate().catch(() => undefined);
  };

  const handleImportFile = async (file: File) => {
    setImportFile(file);
    setImportError(null);
    setImportResult(null);
    setImportBusy(true);
    try {
      const res = await previewImport(file);
      setImportResult(res);
    } catch (e) {
      const detail = (e as { detail?: unknown })?.detail;
      setImportError(
        detail != null && typeof detail === "object"
          ? ((detail as { detail_zh?: string }).detail_zh ?? t("miningPoolImportFailed"))
          : t("miningPoolImportFailed"),
      );
    } finally {
      setImportBusy(false);
    }
  };

  const handleConfirmImport = async () => {
    if (!importFile) return;
    setImportBusy(true);
    setImportError(null);
    try {
      let pid = poolId;
      if (!pid) {
        // 后端 create_pool 对 source_type=import 强制要求 import_batch_id（取预览结果）
        const pool = await createPool({
          name: "导入候选池",
          source_type: "import",
          import_batch_id: importResult?.import_batch_id ?? null,
        });
        pid = pool.id;
        onPoolCreated?.(pool.id);
      }
      const res = await importPoolMembers(pid, importFile, importResult?.import_batch_id);
      setImportResult(res);
      // 入池成功后刷新成员（新池时 pid 在本组件内未更新，需主动拉一次）
      const page = await fetchMembers(pid, { page: 1, page_size: 20 }).catch(() => null);
      if (page) setMembers(page?.items ?? []);
    } catch (e) {
      const detail = (e as { detail?: unknown })?.detail;
      setImportError(
        detail != null && typeof detail === "object"
          ? ((detail as { detail_zh?: string }).detail_zh ?? t("miningPoolImportFailed"))
          : t("miningPoolImportFailed"),
      );
    } finally {
      setImportBusy(false);
    }
  };

  const handleDownloadErrors = () => {
    if (!importFile) return;
    void exportImportErrors(importFile).catch(() => undefined);
  };

  return (
    <div className="mining-pool-step" data-pool-step>
      <div className="mining-pool-tabs">
        <button
          type="button"
          className={`sub-tab ${entry === "filter" ? "active" : ""}`}
          data-pool-tab="filter"
          aria-current={entry === "filter" ? "page" : undefined}
          aria-disabled={locked ? "true" : undefined}
          disabled={locked}
          title={locked ? t("miningPoolLockedHint") : undefined}
          onClick={() => trySwitch("filter")}
        >
          {t("miningPoolTabFilter")}
        </button>
        <button
          type="button"
          className={`sub-tab ${entry === "import" ? "active" : ""}`}
          data-pool-tab="import"
          aria-current={entry === "import" ? "page" : undefined}
          aria-disabled={locked ? "true" : undefined}
          disabled={locked}
          title={locked ? t("miningPoolLockedHint") : undefined}
          onClick={() => trySwitch("import")}
        >
          {t("miningPoolTabImport")}
        </button>
      </div>

      {locked && <PoolLockBanner />}

      {entry === "filter" ? (
        <PoolFilterPanel locked={locked} value={filterConfig} onChange={onFilterChange} />
      ) : (
        <PoolImportPanel
          locked={locked}
          busy={importBusy}
          result={importResult}
          error={importError}
          onTemplate={handleDownloadTemplate}
          onFile={handleImportFile}
          onDownloadErrors={handleDownloadErrors}
          onConfirmImport={handleConfirmImport}
        />
      )}

      {/* 底部实时统计（设计 §3.2：命中/排除 + 按分类统计 + 硬下限提示） */}
      {preview && (
        <div className="mining-pool-preview" data-pool-preview-stats>
          <div className="mining-pool-preview-item">
            <span className="mining-pool-preview-label">
              {t("miningPoolPreviewTotal")}
            </span>
            <span className="mining-pool-preview-value">{preview.universe_size}</span>
          </div>
          <div className="mining-pool-preview-item mining-pool-preview-item--hit">
            <span className="mining-pool-preview-label">
              {t("miningPoolPreviewMatched")}
            </span>
            <span
              className="mining-pool-preview-value mining-pool-preview-value--hit"
              data-pool-preview-hits
            >
              {preview.hits}
            </span>
            <span className="mining-pool-preview-note">
              {t("miningPoolStatsHitRate")}{" "}
              {preview.universe_size > 0
                ? `${((preview.hits / preview.universe_size) * 100).toFixed(1)}%`
                : "-"}
            </span>
          </div>
          <div className="mining-pool-preview-item mining-pool-preview-item--excluded">
            <span className="mining-pool-preview-label">
              {t("miningPoolPreviewExcluded")}
            </span>
            <span className="mining-pool-preview-value mining-pool-preview-value--excluded">
              {preview.excluded_total}
            </span>
          </div>
          <div className="mining-pool-preview-item">
            <span className="mining-pool-preview-label">
              {t("miningPoolStatsGuard")}
            </span>
            <span className="mining-pool-preview-value">
              {preview.min_pool_size ?? MIN_POOL_SIZE}
            </span>
            <span className="mining-pool-preview-note">
              {t("miningPoolStatsGuardNote").replace(
                "{min}",
                String(preview.min_pool_size ?? MIN_POOL_SIZE),
              )}
            </span>
          </div>
          {preview.as_of_date ? (
            <div className="mining-pool-preview-item">
              <span className="mining-pool-preview-label">
                {t("miningPoolStatsAsOf")}
              </span>
              <span className="mining-pool-preview-value mining-pool-preview-value--sm">
                {preview.as_of_date}
              </span>
            </div>
          ) : null}

          <div className="mining-pool-preview-detail">
            <span className="mining-pool-preview-detail-title">
              {t("miningPoolStatsCategory")}
            </span>
            {(preview.categories ?? []).length === 0 ? (
              <span className="mining-pool-preview-note">
                {t("miningPoolStatsNoRule")}
              </span>
            ) : (
              <div className="mining-pool-preview-rules" data-pool-preview-rules>
                {(preview.categories ?? []).map((c) => (
                  <span
                    className="mining-pool-preview-rule"
                    key={c.category}
                    title={c.detail_zh ?? undefined}
                  >
                    {c.label_zh || c.category}
                    <span className="mining-pool-preview-rule-num">
                      {t("miningPoolStatsExcludedShort")} {c.excluded}
                    </span>
                    <span className="mining-pool-preview-note">
                      {t("miningPoolStatsEvaluated")} {c.evaluated}
                    </span>
                  </span>
                ))}
              </div>
            )}
            <span className="mining-pool-preview-note">
              {t("miningPoolPreviewHint")}
            </span>
          </div>

          {(preview.samples ?? []).length > 0 && (
            <div className="mining-pool-preview-detail" data-pool-preview-samples>
              <span className="mining-pool-preview-detail-title">
                {t("miningPoolStatsSamples")}
              </span>
              <div className="mining-pool-preview-rules">
                {(preview.samples ?? []).map((s) => (
                  <span className="mining-chip" key={s.symbol}>
                    {s.symbol}
                    {s.name ? ` ${s.name}` : ""}
                  </span>
                ))}
              </div>
            </div>
          )}

          {Object.keys(preview.field_coverage ?? {}).length > 0 && (
            <div className="mining-pool-preview-detail" data-pool-preview-coverage>
              <span className="mining-pool-preview-detail-title">
                {t("miningPoolStatsCoverage")}
              </span>
              <div className="mining-pool-preview-rules">
                {Object.entries(preview.field_coverage ?? []).map(([code, ratio]) => {
                  const pct = Number(ratio) <= 1 ? Number(ratio) * 100 : Number(ratio);
                  const low = pct < 80;
                  return (
                    <span
                      className={`mining-chip ${low ? "mining-chip--danger" : ""}`}
                      key={code}
                    >
                      {code} {pct.toFixed(1)}%
                    </span>
                  );
                })}
              </div>
            </div>
          )}

          {(preview.warnings ?? []).length > 0 && (
            <div className="mining-pool-preview-detail">
              <span className="mining-pool-preview-detail-title">
                {t("miningPoolStatsWarn")}
              </span>
              {(preview.warnings ?? []).map((w, i) => (
                <span className="mining-pool-preview-note" key={i}>
                  {w}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {blocked && (
        <div className="mining-pool-blocked" data-pool-blocked role="alert">
          <strong>{t("miningPoolBlocked")}</strong>
          {preview?.blocking_issues && preview.blocking_issues.length > 0 ? (
            preview.blocking_issues.map((issue) => (
              <span key={issue.error_code}>{issue.detail_zh}</span>
            ))
          ) : (
            <span>{firstBlocker?.detail_zh ?? t("miningPoolBlockedTooSmall")}</span>
          )}
        </div>
      )}

      <PoolMemberTable
        members={members}
        selected={selected}
        locked={locked}
        onToggle={toggleOne}
        onToggleAll={toggleAll}
      />

      <div className="mining-pool-actions">
        {/* 空池时不渲染批量删除：对空表给删除按钮既无用又像坏了 */}
        <button
          type="button"
          className="mining-pool-btn danger"
          data-pool-bulk-delete
          disabled={locked || selected.length === 0}
          title={locked ? t("miningPoolLockedHint") : undefined}
          onClick={() => setConfirmDelete(true)}
          style={members.length === 0 ? { display: "none" } : undefined}
        >
          {t("miningPoolBulkDelete")}
        </button>
        <button
          type="button"
          className="mining-pool-btn primary"
          data-pool-generate
          disabled={blocked || generating}
          onClick={onGenerateClick}
        >
          {generating
            ? t("miningPoolGenerating")
            : locked
              ? t("miningPoolViewBoard")
              : t("miningPoolGenerate")}
        </button>
      </div>

      {/* A2：二次确认统一 antd Modal（切换入口/批量删除/重新选择） */}
      <Modal
        open={Boolean(pendingEntry)}
        title={t("miningPoolSwitchTitle")}
        onCancel={() => setPendingEntry(null)}
        okText={t("miningPoolSwitchConfirm")}
        cancelText={t("miningPoolSwitchCancel")}
        onOk={doSwitch}
      >
        <p>{t("miningPoolSwitchDesc")}</p>
      </Modal>

      <Modal
        open={confirmDelete}
        title={t("miningPoolDeleteTitle")}
        onCancel={() => setConfirmDelete(false)}
        okText={t("miningPoolDeleteConfirm")}
        cancelText={t("miningPoolSwitchCancel")}
        okButtonProps={{ danger: true }}
        onOk={onDeleteConfirmed}
      >
        <p>{t("miningPoolDeleteDesc")}</p>
      </Modal>

      <Modal
        open={confirmReselect}
        title={t("miningPoolReselectTitle")}
        onCancel={() => setConfirmReselect(false)}
        okText={t("miningPoolReselectConfirm")}
        cancelText={t("miningPoolSwitchCancel")}
        onOk={onReselectConfirmed}
      >
        <p>{t("miningPoolReselectDesc")}</p>
      </Modal>

      {showModal && (
        <PoolAnalysisModal
          snapshot={snapshot}
          onClose={() => setShowModal(false)}
          onReselect={() => setConfirmReselect(true)}
          onNext={() => onNext?.()}
        />
      )}
    </div>
  );
}
