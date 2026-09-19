import { t } from "../../../../../i18n";

/**
 * 导入股票池面板（向导 §3.1 方式 A）。
 *
 * 口径：模板至少含 `symbol`（必填）、`name`/`market`/`note`（可选）；
 * 股票代码是唯一匹配键；未匹配代码**不能**进入候选池，可导出错误明细
 * 修正后重导。锁定态下上传与模板下载全部置灰。
 */
export interface PoolImportPanelProps {
  locked: boolean;
  onTemplate: () => void;
  onFile: (file: File) => void;
}

export default function PoolImportPanel({ locked, onTemplate, onFile }: PoolImportPanelProps) {
  return (
    <div className="mining-pool-import" data-pool-import-panel>
      <div className="mining-pool-import-actions">
        <button
          type="button"
          className="mining-pool-btn"
          disabled={locked}
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
            disabled={locked}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onFile(f);
            }}
          />
        </label>
      </div>
      <div className="mining-pool-import-result">
        <span>{t("miningPoolImportRows")}</span>
      </div>
    </div>
  );
}
