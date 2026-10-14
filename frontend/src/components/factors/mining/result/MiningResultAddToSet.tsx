import { useEffect, useState } from "react";
import { Modal } from "antd";
import { api, type FactorSet } from "../../../../api/client";
import { factorMiningApi } from "../../../../api/factorMining";
import { t } from "../../../../i18n";
import type { MiningCandidate } from "../../../../types/mining";

export interface MiningResultAddToSetProps {
  candidates: MiningCandidate[];
  onClose: () => void;
  onAdded: (factorSetId: string, added: number) => void;
}

/** Candidates enter a mutable research collection. Freezing and activation remain explicit later steps. */
export default function MiningResultAddToSet({ candidates, onClose, onAdded }: MiningResultAddToSetProps) {
  const [sets, setSets] = useState<FactorSet[]>([]);
  const [target, setTarget] = useState("");
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(candidates);
  const [addedCount, setAddedCount] = useState(0);

  const loadSets = async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await api.listFactorSets("any", 200);
      const mutable = rows.filter((row) => row.status === "draft");
      setSets(mutable);
      setTarget(mutable[0]?.id ?? "new");
    } catch (e) {
      setError(e instanceof Error ? e.message : t("miningResultSetLoadFailed"));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void loadSets(); }, []);

  const submit = async () => {
    if (busy || loading || !target || (target === "new" && !name.trim())) return;
    setBusy(true);
    setError(null);
    try {
      let factorSetId = target;
      if (target === "new") {
        const created = await api.createFactorSet({ name: name.trim(), asset_type: "STOCK", actor: "mining:result" });
        factorSetId = created.id;
        setSets((rows) => [created, ...rows]);
        setTarget(factorSetId);
      }
      const unsubmitted = pending.filter((candidate) => !candidate.factor_version_id);
      let ready = pending;
      const failures: string[] = [];
      if (unsubmitted.length) {
        const approved = await factorMiningApi.batchReview(unsubmitted.map((candidate) => ({ candidate_id: candidate.id, action: "approve" })));
        const approvedIds = new Set(approved.results.filter((item) => item.status === "submitted" || item.status === "already").map((item) => item.candidate_id));
        ready = pending.filter((candidate) => candidate.factor_version_id || approvedIds.has(candidate.id));
        failures.push(...approved.results.filter((item) => item.status === "failed").map((item) => `${item.candidate_id.slice(0, 8)}: ${item.error ?? item.error_code}`));
      }
      const result = ready.length ? await factorMiningApi.batchReview(ready.map((candidate) => ({ candidate_id: candidate.id, action: "add_to_set", factor_set_id: factorSetId }))) : { results: [], counts: { added: 0 } };
      const addedIds = new Set(result.results.filter((item) => item.status === "added").map((item) => item.candidate_id));
      failures.push(...result.results.filter((item) => item.status === "failed").map((item) => `${item.candidate_id.slice(0, 8)}: ${item.error ?? item.error_code}`));
      setPending((rows) => rows.filter((candidate) => !addedIds.has(candidate.id)));
      setAddedCount((count) => count + addedIds.size);
      if (addedIds.size) onAdded(factorSetId, addedIds.size);
      if (failures.length || addedIds.size < pending.length) {
        setError(failures.join("；") || t("miningResultSetIncomplete"));
      } else {
        onClose();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("miningResultSetFailed"));
    } finally {
      setBusy(false);
    }
  };

  return <Modal open title={t("miningResultBatchAdd")} onCancel={() => !busy && onClose()} footer={null} closable={!busy} maskClosable={!busy}>
    <p>{t("miningResultSetHint").replace("{count}", String(pending.length))}</p>
    {loading ? <p role="status">{t("miningResultLoading")}</p> : <>
      <label className="mining-set-field">
        <span>{t("miningResultSetTarget")}</span>
        <select aria-label={t("miningResultSetTarget")} value={target} disabled={busy} onChange={(event) => setTarget(event.target.value)}>
          <option value="new">{t("miningResultSetNew")}</option>
          {sets.map((set) => <option key={set.id} value={set.id}>{set.name || set.id}</option>)}
        </select>
      </label>
      {target === "new" && <label className="mining-set-field"><span>{t("miningResultSetName")}</span><input aria-label={t("miningResultSetName")} value={name} disabled={busy} onChange={(event) => setName(event.target.value)} /></label>}
    </>}
    {addedCount > 0 && <p role="status">{t("miningResultSetAdded").replace("{count}", String(addedCount))}</p>}
    {error && <div role="alert" className="mining-run-op-error">{error}{!target && <button type="button" onClick={() => void loadSets()}>{t("miningResultRetry")}</button>}</div>}
    <div className="mining-run-actions">
      <button type="button" disabled={busy} onClick={onClose}>{t("miningResultCancel")}</button>
      <button type="button" className="mining-pool-btn primary" data-mining-set-confirm disabled={busy || loading || !target || !pending.length || (target === "new" && !name.trim())} onClick={() => void submit()}>{busy ? t("miningSubmitPending") : t("miningResultConfirm")}</button>
    </div>
  </Modal>;
}
