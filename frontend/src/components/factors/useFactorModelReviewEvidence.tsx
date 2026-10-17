import { useEffect, useRef, useState } from "react";
import { Alert, Button } from "antd";
import { api, type FactorModelRun } from "../../api/client";
import { t } from "../../i18n";

type ReviewEvidence = { modelId: string; status: "loading" | "ready" | "error"; model?: FactorModelRun; error?: string };

/** Read evidence for one confirmation session; never replace activation eligibility. */
export default function useFactorModelReviewEvidence() {
  const [state, setState] = useState<ReviewEvidence | null>(null);
  const generation = useRef(0);
  const target = useRef<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; generation.current += 1; target.current = null; };
  }, []);
  const read = async (modelId: string, knownDetail?: FactorModelRun | null) => {
    const request = ++generation.current;
    target.current = modelId;
    setState({ modelId, status: "loading" });
    const current = () => mounted.current && request === generation.current && target.current === modelId;
    try {
      const model = knownDetail?.id === modelId ? knownDetail : await api.getFactorModel(modelId);
      if (!current()) return;
      if (model?.id !== modelId) throw new Error(t("factorModelRelationsTargetMismatch"));
      if (!model.hyperparameters || typeof model.hyperparameters !== "object" || Array.isArray(model.hyperparameters)) {
        throw new Error(t("factorModelReviewEvidenceIncomplete"));
      }
      setState({ modelId, status: "ready", model });
    } catch (error) {
      if (current()) setState({ modelId, status: "error", error: error instanceof Error ? error.message : String(error) });
    }
  };
  const clear = () => { generation.current += 1; target.current = null; setState(null); };
  return {
    state, read, clear,
    retry: () => { if (target.current) void read(target.current); },
    readyFor: (modelId?: string) => !!modelId && target.current === modelId && state?.modelId === modelId && state.status === "ready",
  };
}

export function FactorModelReviewEvidenceNotice({ state, onRetry }: { state: ReviewEvidence | null; onRetry: () => void }) {
  if (!state || state.status === "ready") return null;
  return <Alert showIcon type={state.status === "error" ? "error" : "info"} style={{ marginBottom: 12 }}
    message={t(state.status === "error" ? "factorModelReviewEvidenceFailed" : "factorModelReviewEvidenceLoading")}
    description={state.error}
    action={state.status === "error" ? <Button onClick={onRetry}>{t("factorModelReviewEvidenceRetry")}</Button> : undefined} />;
}
