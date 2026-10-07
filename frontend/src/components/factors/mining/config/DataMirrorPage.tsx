import { useCallback, useEffect, useState } from "react";
import { t } from "../../../../i18n";
import { dataMirrorApi } from "../../../../api/dataMirror";
import type { DataMirrorStatus, DataMirrorTask } from "../../../../api/dataMirror";
import { normalizeBackendError } from "../../../../utils/errorRender";

/**
 * 数据中心 · 历史行情镜像管理页（C2，设计 §10.1）。
 *
 * 契约：`GET /data-mirror/status`（当前状态）+ `GET /data-mirror/tasks`
 * （最近任务列表）+ `POST /tasks`（创建镜像任务）+ `POST /tasks/{id}/cancel`（取消）。
 *
 * - 三态容错（加载中 / 空 / 错误），接口异常不崩页；
 * - 状态面板（区间 / 行数 / 标的数 + 低覆盖年份）+ 范围选择（5 年 / 10 年 / 全量）
 *   + 任务列表（分片进度条 + 取消）；
 * - P1-2：`available:false` 时展示**中文兜底文案**（不把后端英文底层错误
 *   直接甩给用户），错误详情折叠展示，且**禁用「创建镜像任务」**。
 *
 * ⚠️ 2026-10-02 勘误：原注释称「预计耗时 / 磁盘检查 / 占用空间」后端未提供、
 * 前端只能给口径说明——**该判断不准确**。实测 `mirror_task.estimate_rows()` 返回
 * `estimated_rows / estimated_symbols / estimated_seconds / estimated_bytes`，
 * `check_disk_space()` 在空间不足时抛中文说明（经路由转 400），且 `POST /tasks`
 * 的返回体就带完整 `estimated` 块（含 `note_zh`）。已接入：
 *   - 创建成功后展示「预计行数 / 预计耗时 / 预计占用」（**显式标注估算**）；
 *   - 创建失败不再静默吞掉，展示后端中文原因（典型：磁盘余量不足）。
 *
 * 仍缺一项能力（需后端补路由，前端不臆造）：**提交前**的估算与磁盘检查预览。
 * `estimate_rows` 目前只在 `POST /tasks` 内部调用，没有 GET 估算端点，
 * 所以「先看估算再决定建不建」还做不到——只能建完看到。详见下方注释。
 */
const PRESETS = [
  { value: "5y", labelKey: "dataMirrorPreset5y" },
  { value: "10y", labelKey: "dataMirrorPreset10y" },
  { value: "full", labelKey: "dataMirrorPresetFull" },
];

/** 任务状态 → 语义标签 */
function taskStatusChipClass(status: string | null | undefined): string {
  const v = String(status ?? "").toLowerCase();
  if (v === "done" || v === "succeeded" || v === "success") {
    return "mining-chip mining-chip--success";
  }
  if (v === "running" || v === "queued") return "mining-chip mining-chip--info";
  if (v === "failed" || v === "cancelled") return "mining-chip mining-chip--danger";
  return "mining-chip";
}

/** 字节 → 人话（换算到 KB/MB/GB/TB 保留 1 位）；非有限值返回 "-" */
export function humanBytes(bytes?: number | null): string {
  if (bytes == null || !Number.isFinite(Number(bytes))) return "-";
  const b = Number(bytes);
  if (b < 1024) return `${Math.round(b)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = b / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

/** 秒 → 人话；非有限值返回 "-" */
export function humanSeconds(sec?: number | null): string {
  if (sec == null || !Number.isFinite(Number(sec))) return "-";
  const s = Number(sec);
  if (s < 60) return `${s.toFixed(1)} ${t("dataMirrorUnitSecond")}`;
  if (s < 3600) {
    return `${(s / 60).toFixed(1)} ${t("dataMirrorUnitMinute")}`;
  }
  return `${(s / 3600).toFixed(1)} ${t("dataMirrorUnitHour")}`;
}

/** 后端 note_zh 里带 markdown 强调符（`**估算值**`），上屏前剥掉 */
function plainText(s?: string | null): string {
  return (s ?? "").replace(/\*\*/g, "").trim();
}

export default function DataMirrorPage() {
  const [status, setStatus] = useState<DataMirrorStatus | null>(null);
  const [tasks, setTasks] = useState<DataMirrorTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [preset, setPreset] = useState("5y");
  /** 创建成功后的返回体（含 estimated 块）；用于展示估算行数/耗时/占用 */
  const [created, setCreated] = useState<DataMirrorTask | null>(null);
  /** 创建失败的中文原因（磁盘不足 / 写锁被占 / 数仓不可用…） */
  const [createErr, setCreateErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const [nextStatus, page] = await Promise.all([
        dataMirrorApi.getStatus(),
        dataMirrorApi.getTasks(20),
      ]);
      setStatus(nextStatus);
      setTasks(Array.isArray(page?.items) ? page.items : []);
    } catch {
      setTasks([]);
      setStatus(null);
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async () => {
    setBusy(true);
    setCreateErr(null);
    try {
      // 返回体含 estimated 块 —— 拿它展示估算（否则用户看不到任何反馈）
      const res = await dataMirrorApi.createTask({ preset });
      setCreated(res);
      await load();
    } catch (err) {
      // 不再静默吞掉：磁盘不足/写锁被占都会带中文 detail_zh，必须让用户看见
      setCreated(null);
      const detail = (err as { detail?: unknown })?.detail;
      const norm = normalizeBackendError(detail ?? err);
      setCreateErr(
        plainText(norm.detail_zh) || plainText(norm.title_zh) ||
          (err instanceof Error ? err.message : "") || t("dataMirrorCreateFailed"),
      );
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (taskId: string) => {
    setBusy(true);
    try {
      await dataMirrorApi.cancelTask(taskId);
      await load();
    } catch {
      // 取消失败保持现状
    } finally {
      setBusy(false);
    }
  };

  const statusSummary = (s: DataMirrorStatus) =>
    s.available
      ? t("dataMirrorStatusSource") +
        ": " +
        `${s.mirrored_from ?? "-"} ~ ${s.mirrored_to ?? "-"} · ${s.total_rows ?? 0} ${t("dataMirrorRowCount")} · ${s.total_symbols ?? 0} ${t("dataMirrorSymbolCount")}`
      : t("dataMirrorUnavailable");

  const warehouseDown = Boolean(status && !status.available);

  return (
    <div className="mining-exp-page" data-mirror-page>
      <div className="mining-page-head">
        <div className="mining-page-head-text">
          <span className="mining-page-title">{t("dataMirrorPageTitle")}</span>
          <span className="mining-page-sub">{t("dataMirrorDesc")}</span>
        </div>
      </div>

      {loading && <p data-mirror-loading>{t("dataMirrorLoading")}</p>}
      {!loading && failed && <p data-mirror-error>{t("dataMirrorError")}</p>}

      {!loading && !failed && status && (
        <div
          className={status.available ? "mining-card" : "mining-pool-blocked"}
          data-mirror-status
          data-available={String(status.available)}
        >
          {status.available ? (
            <>
              <div className="mining-tiles">
                <div className="mining-tile mining-tile--brand">
                  <span className="mining-tile-label">{t("dataMirrorRangeLabel")}</span>
                  <span className="mining-tile-value mining-tile-value--sm">
                    {status.mirrored_from ?? "-"} ~ {status.mirrored_to ?? "-"}
                  </span>
                </div>
                <div className="mining-tile">
                  <span className="mining-tile-label">{t("dataMirrorRowsLabel")}</span>
                  <span className="mining-tile-value">
                    {status.total_rows != null
                      ? Number(status.total_rows).toLocaleString()
                      : "-"}
                  </span>
                </div>
                <div className="mining-tile">
                  <span className="mining-tile-label">{t("dataMirrorSymbolsLabel")}</span>
                  <span className="mining-tile-value">
                    {status.total_symbols != null
                      ? Number(status.total_symbols).toLocaleString()
                      : "-"}
                  </span>
                </div>
              </div>
              <p className="mining-hint">{statusSummary(status)}</p>
            </>
          ) : (
            <div>{statusSummary(status)}</div>
          )}

          {/* P1-2：不可用时给中文兜底提示，底层错误折叠展示 */}
          {warehouseDown && (
            <>
              <p className="mining-hint" data-mirror-unavailable-hint>
                {t("dataMirrorUnavailableHint")}
              </p>
              {status.error && (
                <details className="mining-mirror-error-detail" data-mirror-error-detail>
                  <summary>{t("dataMirrorUnavailableDetail")}</summary>
                  <pre>{status.error}</pre>
                </details>
              )}
            </>
          )}
          {!!status.low_coverage_years?.length && (
            <div className="mining-banner mining-banner--warn">
              <div className="mining-banner-body">
                <span className="mining-banner-title">{t("dataMirrorLowCoverage")}</span>
                <span>{status.low_coverage_years.map((y) => y.year).join(", ")}</span>
              </div>
            </div>
          )}
        </div>
      )}

      {!loading && !failed && (
        <div className="mining-card mining-card--tint">
          <div className="mining-evo-row">
            <span className="mining-evo-field-label">{t("dataMirrorPresetLabel")}</span>
            <div className="mining-segmented" role="group">
              {PRESETS.map((p) => (
                <button
                  key={p.value}
                  type="button"
                  className={preset === p.value ? "on" : undefined}
                  data-mirror-preset={p.value}
                  aria-pressed={preset === p.value}
                  onClick={() => setPreset(p.value)}
                >
                  {t(p.labelKey)}
                </button>
              ))}
            </div>
          </div>
          <p className="mining-hint">{t("dataMirrorEstimateNote")}</p>
          <div className="mining-toolbar">
            <button
              type="button"
              className="mining-pool-btn primary"
              data-mirror-create
              disabled={busy || warehouseDown}
              title={warehouseDown ? t("dataMirrorUnavailableHint") : undefined}
              onClick={() => void create()}
            >
              {t("dataMirrorCreate")}
            </button>
            <button
              type="button"
              className="mining-pool-btn"
              data-mirror-refresh
              onClick={() => void load()}
            >
              {t("dataMirrorRefresh")}
            </button>
          </div>

          {/* 创建失败：把后端中文原因显示出来（此前是静默无反应） */}
          {createErr && (
            <div className="mining-banner mining-banner--danger" data-mirror-create-error>
              <div className="mining-banner-body">
                <span className="mining-banner-title">{t("dataMirrorCreateFailed")}</span>
                <span>{createErr}</span>
              </div>
            </div>
          )}

          {/* 创建成功：展示估算块（行数 / 耗时 / 占用），并显式标注「估算」 */}
          {created?.estimated && (
            <div className="mining-banner mining-banner--info" data-mirror-create-info>
              <div className="mining-banner-body">
                <span className="mining-banner-title">{t("dataMirrorCreateOk")}</span>
                <span className="mining-chip mining-chip--warn" data-mirror-estimate-tag>
                  {t("dataMirrorEstimatedTag")}
                </span>
                <span data-mirror-est-rows>
                  {t("dataMirrorEstimateRows")}：
                  {Number(created.estimated.estimated_rows ?? 0).toLocaleString()}
                </span>
                <span data-mirror-est-seconds>
                  {t("dataMirrorEstimateSeconds")}：
                  {humanSeconds(created.estimated.estimated_seconds)}
                </span>
                <span data-mirror-est-bytes>
                  {t("dataMirrorEstimateSize")}：
                  {humanBytes(created.estimated.estimated_bytes)}
                </span>
                {created.estimated.note_zh && (
                  <span className="mining-hint">
                    {plainText(created.estimated.note_zh)}
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {!loading && !failed && tasks.length === 0 && (
        <p data-mirror-empty>{t("dataMirrorEmpty")}</p>
      )}

      {!loading && !failed && tasks.length > 0 && (
        <div className="mining-table-wrap">
          <table className="mining-runs-table" data-mirror-task-table>
            <thead>
              <tr>
                <th>{t("dataMirrorColTask")}</th>
                <th>{t("dataMirrorColStatus")}</th>
                <th>{t("dataMirrorColPreset")}</th>
                <th style={{ minWidth: 180 }}>{t("dataMirrorProgressLabel")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => {
                const done = task.completed_chunks ?? 0;
                const total = task.total_chunks ?? 0;
                const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
                return (
                  <tr key={task.task_id}>
                    <td className="mining-cell-formula">{task.task_id}</td>
                    <td>
                      <span className={taskStatusChipClass(task.status)}>
                        {task.status ?? "-"}
                      </span>
                    </td>
                    <td>
                      <span className="mining-chip">{task.preset ?? "-"}</span>
                    </td>
                    <td>
                      <div style={{ display: "grid", gap: 4, minWidth: 140 }}>
                        <span className="mining-meter mining-meter--sm">
                          <span
                            className="mining-meter-fill"
                            style={{ width: `${percent}%` }}
                          />
                        </span>
                        <span className="mining-card-sub">
                          {done}
                          {total ? ` / ${total}` : ""}
                          {total ? ` · ${percent}%` : ""}
                        </span>
                      </div>
                    </td>
                    <td>
                      {(task.status === "queued" || task.status === "running") && (
                        <button
                          type="button"
                          className="mining-pool-btn"
                          data-mirror-cancel={task.task_id}
                          disabled={busy}
                          onClick={() => void cancel(task.task_id)}
                        >
                          {t("dataMirrorCancel")}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
