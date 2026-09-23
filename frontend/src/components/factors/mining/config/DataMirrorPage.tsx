import { useCallback, useEffect, useState } from "react";
import { t } from "../../../../i18n";
import { dataMirrorApi } from "../../../../api/dataMirror";
import type { DataMirrorStatus, DataMirrorTask } from "../../../../api/dataMirror";

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
 * ⚠️ 设计 §10.1 的「预计耗时（须标注估算）/ 磁盘检查 / 完成后提示占用空间」依赖
 * 后端补充字段，当前契约未提供，**前端不臆造数值**，仅在界面给出估算口径说明。
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

export default function DataMirrorPage() {
  const [status, setStatus] = useState<DataMirrorStatus | null>(null);
  const [tasks, setTasks] = useState<DataMirrorTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [preset, setPreset] = useState("5y");

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
    try {
      await dataMirrorApi.createTask({ preset });
      await load();
    } catch {
      // 创建失败保持现状（不崩页）
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
      <p className="mining-exp-desc">{t("dataMirrorDesc")}</p>

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
