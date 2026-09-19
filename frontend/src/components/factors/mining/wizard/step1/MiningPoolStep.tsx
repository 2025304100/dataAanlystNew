import { useCallback, useEffect, useRef, useState } from "react";
import { t } from "../../../../../i18n";
import PoolAnalysisModal from "./PoolAnalysisModal";
import PoolFilterPanel from "./PoolFilterPanel";
import PoolImportPanel from "./PoolImportPanel";
import PoolLockBanner from "./PoolLockBanner";
import PoolMemberTable from "./PoolMemberTable";
import {
  MIN_POOL_SIZE,
  createSnapshot,
  deleteLatestSnapshot,
  fetchMembers,
  previewPool,
  removeMembers,
} from "./poolApi";
import type { PoolMember, PoolPreview, PoolSnapshot } from "./poolApi";

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
}

export default function MiningPoolStep({ poolId = "", onNext }: MiningPoolStepProps) {
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

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const blocked = Boolean(preview?.blocked) ||
    (preview != null && preview.matched < MIN_POOL_SIZE);

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
  const onGenerateClick = async () => {
    if (blocked) return;
    if (locked) {
      setShowModal(true);
      return;
    }
    setGenerating(true);
    try {
      const snap = await createSnapshot(poolId, { analyze: true });
      setSnapshot(snap);
      setLocked(Boolean(snap?.is_locked));
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
          onTemplate={() => undefined}
          onFile={() => undefined}
        />
      )}

      {preview && (
        <div className="mining-pool-preview" data-pool-preview-stats>
          <span>
            {t("miningPoolPreviewTotal")}: {preview.total}
          </span>
          <span>
            {t("miningPoolPreviewMatched")}: {preview.matched}
          </span>
          <span>
            {t("miningPoolPreviewExcluded")}: {preview.excluded}
          </span>
        </div>
      )}

      {blocked && (
        <div className="mining-pool-blocked" data-pool-blocked role="alert">
          <strong>{t("miningPoolBlocked")}</strong>
          <span>
            {preview?.blocked?.detail_zh ?? t("miningPoolBlockedTooSmall")}
          </span>
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
        <button
          type="button"
          className="mining-pool-btn"
          data-pool-bulk-delete
          disabled={locked || selected.length === 0}
          title={locked ? t("miningPoolLockedHint") : undefined}
          onClick={() => setConfirmDelete(true)}
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

      {/* 切换入口确认 */}
      {pendingEntry && (
        <div className="mining-pool-confirm" role="dialog">
          <div>
            <strong>{t("miningPoolSwitchTitle")}</strong>
            <p>{t("miningPoolSwitchDesc")}</p>
            <button
              type="button"
              data-pool-switch-cancel
              onClick={() => setPendingEntry(null)}
            >
              {t("miningPoolSwitchCancel")}
            </button>
            <button type="button" data-pool-switch-confirm onClick={doSwitch}>
              {t("miningPoolSwitchConfirm")}
            </button>
          </div>
        </div>
      )}

      {/* 批量删除二次确认 */}
      {confirmDelete && (
        <div className="mining-pool-confirm" role="dialog">
          <div>
            <strong>{t("miningPoolDeleteTitle")}</strong>
            <p>{t("miningPoolDeleteDesc")}</p>
            <button
              type="button"
              data-pool-delete-cancel
              onClick={() => setConfirmDelete(false)}
            >
              {t("miningPoolSwitchCancel")}
            </button>
            <button type="button" data-pool-delete-confirm onClick={onDeleteConfirmed}>
              {t("miningPoolDeleteConfirm")}
            </button>
          </div>
        </div>
      )}

      {/* 重新选择二次确认 */}
      {confirmReselect && (
        <div className="mining-pool-confirm" role="dialog">
          <div>
            <strong>{t("miningPoolReselectTitle")}</strong>
            <button
              type="button"
              data-pool-reselect-cancel
              onClick={() => setConfirmReselect(false)}
            >
              {t("miningPoolSwitchCancel")}
            </button>
            <button
              type="button"
              data-pool-reselect-confirm
              onClick={onReselectConfirmed}
            >
              {t("miningPoolReselectConfirm")}
            </button>
          </div>
        </div>
      )}

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
