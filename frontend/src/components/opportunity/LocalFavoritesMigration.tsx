// WP2.5：本地收藏迁移工具
// 前端首次加载检测到 ic_favorites 时：
// 1. 读取 ic_favorites（实际存储格式为 number[]，即 symbol_id 数组）
// 2. 与后端 core 名单已存在观察项比对
// 3. 显示"将导入 N 个、已存在 N 个、无效 N 个"
// 4. 用户确认后批量幂等写入（POST /watchlists/{id}/observations/batch-import）
// 5. 成功后记录 ic_favorites_migrated_v1，暂不删除原值（保留回退期）
//
// 关键约束：
// - 迁移可重复执行且不产生重复项（后端 batch-import 幂等）
// - 不删除 ic_favorites 原值（至少保留一个版本回退期）
// - 后端不可用时静默跳过，不抛异常
import { useEffect, useState } from "react";
import { Alert, Button, Modal, Space, Typography, message } from "antd";
import { requestJson } from "../../api/client";
import { t, template } from "../../i18n";

// 名单条目（对齐 app.schemas.watchlist.WatchlistRead）
interface WatchlistItem {
  id: number;
  name: string;
  list_type: string;
  description: string | null;
  created_at: string;
}

// 观察项富读（仅用 symbol_id 比对，字段同 ObservationPool）
interface ObservationRead {
  watchlist_item_id: number;
  watchlist_id: number;
  symbol_id: number;
  status: string;
}

// 批量导入结果（对齐 app.schemas.watchlist.ObservationBatchImportResult）
interface MigrationResult {
  imported: number;
  existing: number;
  failed: number;
  errors: Array<Record<string, unknown>>;
}

interface MigrationStats {
  total: number;
  toImport: number;
  existing: number;
  invalid: number;
}

/**
 * 本地收藏迁移工具（WP2.5）。
 * 首次检测到 ic_favorites 且未迁移时弹窗提示用户确认。
 */
export const LocalFavoritesMigration: React.FC = () => {
  const [visible, setVisible] = useState(false);
  const [stats, setStats] = useState<MigrationStats | null>(null);
  const [migrating, setMigrating] = useState(false);
  const [result, setResult] = useState<MigrationResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // 静默检查是否需要迁移；任何异常都不抛出
    checkMigrationNeeded().catch(() => {
      // 静默忽略
    });
  }, []);

  /**
   * 检查是否需要迁移：
   * 1. 已迁移标记存在 → 跳过
   * 2. ic_favorites 为空 → 跳过
   * 3. 后端不可用 → 静默跳过
   * 4. 否则计算 stats 并弹窗
   */
  const checkMigrationNeeded = async () => {
    // 1. 检查迁移标记（记录时间戳，存在即已迁移）
    if (localStorage.getItem("ic_favorites_migrated_v1")) {
      return;
    }
    // 2. 读取 ic_favorites（实际存储为 number[]，兼容字符串数组）
    let favorites: unknown[] = [];
    try {
      favorites = JSON.parse(localStorage.getItem("ic_favorites") || "[]");
    } catch {
      favorites = [];
    }
    if (!Array.isArray(favorites) || favorites.length === 0) {
      return;
    }
    // 仅保留有效 symbol_id（number 或数字字符串）
    const symbolIds: number[] = favorites
      .map((v) => (typeof v === "number" ? v : Number(v)))
      .filter((v) => Number.isFinite(v) && v > 0);
    if (symbolIds.length === 0) {
      return;
    }

    // 3. 查询后端 core 名单
    try {
      const allLists = await requestJson<WatchlistItem[]>("/api/v1/watchlists");
      const coreList = (allLists ?? []).find((w) => w.name === "core");
      if (!coreList) {
        // 无 core 名单：静默跳过（首次启动后端会自动创建 core，下次再迁移）
        return;
      }

      // 4. 查询 core 已存在观察项（active + archived 合并，确保幂等对比）
      const activeItems = await requestJson<ObservationRead[]>(
        `/api/v1/watchlists/${coreList.id}/observations?limit=1000`,
      );
      let archivedItems: ObservationRead[] = [];
      try {
        archivedItems = await requestJson<ObservationRead[]>(
          `/api/v1/watchlists/${coreList.id}/observations?status=archived&limit=1000`,
        );
      } catch {
        // archived 查询失败时仅按 active 对比，不影响主流程
        archivedItems = [];
      }
      const existingSymbolIds = new Set<number>();
      for (const item of [...(activeItems ?? []), ...(archivedItems ?? [])]) {
        existingSymbolIds.add(item.symbol_id);
      }

      let toImport = 0;
      let existing = 0;
      let invalid = 0;
      for (const symId of symbolIds) {
        if (existingSymbolIds.has(symId)) {
          existing += 1;
        } else {
          // 不预先查询 symbol 有效性，由 batch-import 报告 failed
          toImport += 1;
        }
      }
      // invalid = 0（预先不验证），后端 batch-import 会返回 failed
      invalid = 0;

      setStats({ total: symbolIds.length, toImport, existing, invalid });
      setResult(null);
      setError(null);
      setVisible(true);
    } catch {
      // 后端不可用：静默跳过
    }
  };

  /**
   * 执行迁移：批量幂等写入 core 名单。
   * 后端 batch-import 已实现 (watchlist_id, symbol_id) 唯一性检查，可重复执行。
   */
  const handleMigrate = async () => {
    if (!stats) return;
    setMigrating(true);
    setError(null);
    try {
      // 重新读取 favorites（用户可能期间有变更）
      let favorites: unknown[] = [];
      try {
        favorites = JSON.parse(localStorage.getItem("ic_favorites") || "[]");
      } catch {
        favorites = [];
      }
      const symbolIds: number[] = (Array.isArray(favorites) ? favorites : [])
        .map((v) => (typeof v === "number" ? v : Number(v)))
        .filter((v) => Number.isFinite(v) && v > 0);

      if (symbolIds.length === 0) {
        message.warning(t("icMigrationFailed"));
        setMigrating(false);
        return;
      }

      // 查询 core 名单 id
      const allLists = await requestJson<WatchlistItem[]>("/api/v1/watchlists");
      const coreList = (allLists ?? []).find((w) => w.name === "core");
      if (!coreList) {
        message.error(t("icMigrationFailed"));
        setError(t("icMigrationFailed"));
        setMigrating(false);
        return;
      }

      // 构造批量导入项（origin_type=legacy_manual_unknown 标记迁移来源）
      const items = symbolIds.map((symId) => ({
        symbol_id: symId,
        origin_type: "legacy_manual_unknown",
        note: t("icMigrationNote"),
      }));

      // 批量幂等写入
      const importResult = await requestJson<MigrationResult>(
        `/api/v1/watchlists/${coreList.id}/observations/batch-import`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ watchlist_id: coreList.id, items }),
        },
      );

      setResult(importResult);

      // 记录迁移完成时间戳，暂不删除原值（保留回退期）
      localStorage.setItem("ic_favorites_migrated_v1", new Date().toISOString());

      message.success(t("icMigrationSuccess"));
    } catch (err) {
      // 错误消息不暴露敏感信息
      const msg = err instanceof Error ? err.message : "";
      setError(msg || t("icMigrationFailed"));
      message.error(t("icMigrationFailed"));
    } finally {
      setMigrating(false);
    }
  };

  const handleClose = () => {
    setVisible(false);
  };

  return (
    <Modal
      open={visible}
      title={t("icMigrationTitle")}
      onCancel={handleClose}
      footer={
        result
          ? [
              <Button key="done" type="primary" onClick={handleClose}>
                {t("icMigrationDone")}
              </Button>,
            ]
          : [
              <Button key="later" onClick={handleClose} disabled={migrating}>
                {t("icMigrationLater")}
              </Button>,
              <Button
                key="migrate"
                type="primary"
                loading={migrating}
                onClick={handleMigrate}
              >
                {t("icMigrationConfirm")}
              </Button>,
            ]
      }
    >
      {!result && stats && (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Typography.Paragraph>
            {template("icFavoritesMigrationDesc", { count: stats.total })}
          </Typography.Paragraph>
          {/* WP9.2：本地收藏已停用，提示用户后续使用观察池 */}
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            {t("wp9.favoritesDeprecated")}
          </Typography.Paragraph>
          <Alert
            type="info"
            showIcon
            message={
              <Space direction="vertical" size={2}>
                <span>{template("icMigrationStatsToImport", { count: stats.toImport })}</span>
                <span>{template("icMigrationStatsExisting", { count: stats.existing })}</span>
                <span>{template("icMigrationStatsInvalid", { count: stats.invalid })}</span>
              </Space>
            }
          />
          {error && <Alert type="error" showIcon message={error} />}
        </Space>
      )}
      {result && (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Alert
            type="success"
            showIcon
            message={t("icMigrationSuccess")}
            description={
              <Space direction="vertical" size={2}>
                <span>{template("icMigrationResultImported", { count: result.imported })}</span>
                <span>{template("icMigrationResultExisting", { count: result.existing })}</span>
                <span>{template("icMigrationResultFailed", { count: result.failed })}</span>
              </Space>
            }
          />
          <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
            {t("icMigrationKeepLocalHint")}
          </Typography.Paragraph>
        </Space>
      )}
    </Modal>
  );
};
