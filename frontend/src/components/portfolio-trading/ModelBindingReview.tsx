import { useEffect, useRef, useState } from "react";
import { Button, Checkbox, Input } from "antd";
import { api } from "../../api/client";
import { useApp } from "../../context/AppContext";
import { t, template } from "../../i18n";
import type { ModelBindingDetail, ModelBindingReviewResponse } from "../../types";
import { requestPortfolioSubtab } from "../../utils/navigate";

interface Props {
  portfolioId: number;
  onResolved: () => void | Promise<void>;
  onNavigate?: (tab: string) => void;
}

export default function ModelBindingReview({ portfolioId, onResolved, onNavigate }: Props) {
  const { showToast } = useApp();
  const [review, setReview] = useState<ModelBindingReviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [ack, setAck] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const load = async (changed = false) => {
    const current = ++generation.current;
    setLoading(true);
    setAck(false);
    setNote("");
    setReview(null);
    setError(changed ? t("repairDrawer.binding.changed") : null);
    try {
      const result = await api.getPortfolioModelBindingReview(portfolioId);
      if (current === generation.current) setReview(result);
    } catch {
      if (current === generation.current) setError(t("repairDrawer.binding.loadFailed"));
    } finally {
      if (current === generation.current) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    return () => { generation.current += 1; };
  }, [portfolioId]);

  const confirm = async () => {
    if (!review?.can_confirm || !review.snapshot_id || !ack || busy || note.trim().length < 10) return;
    const current = generation.current;
    setBusy(true);
    setError(null);
    try {
      const result = await api.confirmPortfolioModelBinding(portfolioId, {
        snapshot_id: review.snapshot_id,
        model_run_id: review.model_run_id,
        ack: true,
        review_note: note.trim(),
      });
      if (current !== generation.current) return;
      if (!result.model_confirmed) {
        setError(t("repairDrawer.binding.confirmFailed"));
        return;
      }
      setAck(false);
      showToast("success", t("repairDrawer.binding.confirmed"));
      await onResolved();
    } catch (err) {
      if (current !== generation.current) return;
      if ((err as { status_code?: number }).status_code === 409) await load(true);
      else setError(t("repairDrawer.binding.confirmFailed"));
    } finally {
      if (current === generation.current) setBusy(false);
      // A conflict reload changes generation while keeping the same review form.
      else if (current + 1 === generation.current) setBusy(false);
    }
  };

  const renderBinding = (binding: ModelBindingDetail | null) => binding ? (
    <div style={{ fontSize: 12, lineHeight: 1.8 }}>
      <div>{t("repairDrawer.binding.snapshot")}: {template("repairDrawer.binding.version", { n: binding.snapshot_no ?? binding.rule_version ?? "—" })}</div>
      <div>{t("repairDrawer.binding.model")}: {binding.model_name || t(binding.model_run_id ? "repairDrawer.binding.boundModel" : "repairDrawer.binding.noModel")}</div>
      <details>
        <summary>{t("repairDrawer.techInfo")}</summary>
        <div>{binding.snapshot_id}</div>
        {binding.model_run_id && <div>{binding.model_run_id}</div>}
      </details>
    </div>
  ) : <div style={{ fontSize: 12 }}>{t("repairDrawer.binding.firstReview")}</div>;

  return (
    <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px dashed #334155", color: "var(--pt-foreground, #e2e8f0)" }}>
      {loading ? <div>{t("repairDrawer.binding.loading")}</div> : review && (
        <>
          <div style={{ fontSize: 12, marginBottom: 8 }}>{review.change_summary}</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
            <div><strong>{t("repairDrawer.binding.before")}</strong>{renderBinding(review.before)}</div>
            <div><strong>{t("repairDrawer.binding.after")}</strong>{renderBinding(review.after)}</div>
          </div>
          {review.can_confirm ? (
            <>
              <div style={{ marginTop: 12 }}>
                <Checkbox checked={ack} disabled={busy} onChange={(event) => setAck(event.target.checked)}>
                  {t("repairDrawer.binding.ack")}
                </Checkbox>
              </div>
              <Input.TextArea
                value={note}
                disabled={busy}
                onChange={(event) => setNote(event.target.value)}
                placeholder={t("repairDrawer.guide.notePlaceholder")}
                rows={2}
                style={{ width: "100%", marginTop: 8 }}
              />
              <Button size="small" type="primary" loading={busy} disabled={!ack || busy || note.trim().length < 10} onClick={() => void confirm()}>
                {t("repairDrawer.binding.confirm")}
              </Button>
            </>
          ) : (
            <div style={{ marginTop: 10, fontSize: 12 }}>
              <div>{t("repairDrawer.binding.invalid")}</div>
              {review.blockers.map((blocker, index) => <div key={index}>{blocker}</div>)}
              <Button size="small" onClick={() => onNavigate ? onNavigate("strategy") : requestPortfolioSubtab("strategy")}>
                {t("repairDrawer.binding.goStrategy")}
              </Button>
            </div>
          )}
        </>
      )}
      {error && <div role="alert" style={{ color: "#fca5a5", fontSize: 12, marginTop: 8 }}>{error}</div>}
      <Button size="small" loading={loading} disabled={busy} onClick={() => void load()} style={{ marginTop: 8 }}>
        {t("repairDrawer.refresh")}
      </Button>
    </div>
  );
}
