import { t } from "../../../../../i18n";
import type { ImportPreviewResult } from "./poolApi";

/**
 * 导入股票池面板（向导 §3.1 方式 A）。
 *
 * 口径：模板至少含 `symbol`（必填）、`name`/`market`/`note`（可选）；
 * 股票代码是唯一匹配键；未匹配代码**不能**进入候选池，可导出错误明细
 * 修正后重导。锁定态下上传与模板下载全部置灰。
 *
 * P1-6（2026-09-21）：接真实接口 —— 选文件后先 `previewImport` 展示
 * 逐行匹配结果（成功/未匹配/重复/格式错误/非股票/已停牌），确认后再入池。
 */
export interface PoolImportPanelProps {
  locked: boolean;
  busy: boolean;
  onTemplate: () => void;
  onFile: (file: File) => void;
  result: ImportPreviewResult | null;
  onDownloadErrors: () => void;
  onConfirmImport: () => void;
  /** P1-6：预览/入池失败的结构化错误文案（后端 detail_zh） */
  error?: string | null;
}

export default function PoolImportPanel({
  locked,
  busy,
  onTemplate,
  onFile,
  result,
  onDownloadErrors,
  onConfirmImport,
  error = null,
}: PoolImportPanelProps) {
  const admitted = result?.admitted_count ?? 0;
  const errors = result?.error_count ?? 0;
  const hasSuccess = admitted > 0;
  const hasErrors = Boolean(result?.has_errors);

  return (
    <div className="mining-pool-import" data-pool-import-panel>
      <div className="mining-pool-import-actions">
        <button
          type="button"
          className="mining-pool-btn"
          disabled={locked || busy}
          title={locked ? t("miningPoolLockedHint") : undefined}
          onClick={onTemplate}
        >
          {t("miningPoolImportTemplate")}
        </button>
        <label className="mining-pool-file">
          <span>{t("miningPoolImportUpload")}</span>
          <input
            type="file"
            accept=".csv,.xlsx,.xls"
            disabled={locked || busy}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onFile(f);
            }}
          />
        </label>
      </div>

      {busy && (
        <p className="mining-pool-import-busy" data-import-busy>
          {t("miningPoolImportAnalyzing")}
        </p>
      )}

      {error && (
        <p className="mining-pool-import-error" data-import-error role="alert">
          <strong>{t("miningPoolImportFailed")}:</strong> {error}
        </p>
      )}

      {result && (
        <div className="mining-pool-import-result" data-import-result>
          <div className="mining-pool-import-stats" data-import-stats>
            <span>
              {t("miningPoolImportTotal")}: {result.total_rows}
            </span>
            <span>
              {t("miningPoolImportAdmitted")}: {admitted}
            </span>
            <span>
              {t("miningPoolImportErrors")}: {errors}
            </span>
            {result.counts_label_zh &&
              Object.entries(result.counts_label_zh).map(([status, label]) => (
                <span key={status} data-import-count={status}>
                  {label}: {result.counts?.[status] ?? 0}
                </span>
              ))}
          </div>

          {result.rows.length > 0 && (
            <table className="mining-pool-import-table" data-import-rows>
              <thead>
                <tr>
                  <th>{t("miningPoolImportColRow")}</th>
                  <th>{t("miningPoolImportColStatus")}</th>
                  <th>{t("miningPoolImportColSymbol")}</th>
                  <th>{t("miningPoolImportColName")}</th>
                </tr>
              </thead>
              <tbody>
                {result.rows.map((r) => (
                  <tr key={r.row_no}>
                    <td>{r.row_no}</td>
                    <td>{r.status_label_zh ?? r.status}</td>
                    <td>{r.symbol}</td>
                    <td>{r.name ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="mining-pool-import-ops">
            {hasErrors && (
              <button
                type="button"
                className="mining-pool-btn"
                data-import-download-errors
                disabled={busy}
                onClick={onDownloadErrors}
              >
                {t("miningPoolImportDownloadErrors")}
              </button>
            )}
            {hasSuccess && (
              <button
                type="button"
                className="mining-pool-btn primary"
                data-import-confirm
                disabled={busy}
                onClick={onConfirmImport}
              >
                {t("miningPoolImportConfirm")}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
