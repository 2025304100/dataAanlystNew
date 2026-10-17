import { useCallback, useEffect, useRef, useState } from "react";
import { api, type FactorDefinition, type FactorVersionListItem } from "../../api/client";
import { factorLabel, t } from "../../i18n";

export interface FactorLabSelectionProps {
  initialFactorCode?: string;
  initialVersionId?: number;
  onSelectionChange?: (selection: { factorCode?: string; versionId?: number }) => void;
}

/** Resolve a version inside its factor; user-facing selectors never accept unrelated IDs. */
export function useFactorLabSelection({ initialFactorCode = "", initialVersionId, onSelectionChange }: FactorLabSelectionProps) {
  const [factorCode, setCode] = useState(initialFactorCode);
  const [versionId, setVersionId] = useState<number | undefined>(initialVersionId);
  const [factors, setFactors] = useState<FactorDefinition[]>([]);
  const [versions, setVersions] = useState<FactorVersionListItem[]>([]);
  const [versionsFor, setVersionsFor] = useState("");
  const [factorLoading, setFactorLoading] = useState(true);
  const [versionLoading, setVersionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const versionRequest = useRef(0);
  const wantedVersion = useRef(initialVersionId);
  const selectionListener = useRef(onSelectionChange);
  selectionListener.current = onSelectionChange;

  useEffect(() => {
    // Parent feedback acknowledges a user choice; it must not reset that choice
    // or restart a version request merely because the callback updated context.
    if (initialFactorCode === factorCode && initialVersionId === versionId) return;
    wantedVersion.current = initialVersionId;
    setCode(initialFactorCode);
    setVersionId(initialVersionId);
    if (initialFactorCode && initialFactorCode === factorCode && !versions.some(item => item.id === initialVersionId)) {
      setReload(value => value + 1);
    }
    // Only incoming selection changes synchronize from the parent. Local choices
    // are sent upwards explicitly, after they have been made or resolved.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialFactorCode, initialVersionId]);

  useEffect(() => {
    let cancelled = false;
    setFactorLoading(true);
    setError(null);
    void (async () => {
      const all: FactorDefinition[] = [];
      let page = 1;
      while (true) {
        const result = await api.scoringListFactorDefinitions({ page, page_size: 100 });
        if (cancelled) return;
        const items: FactorDefinition[] = result.items || [];
        all.push(...items);
        if (items.length < 100 || all.length >= Number(result.total || 0)) break;
        page += 1;
      }
      if (!cancelled) setFactors(all);
    })().catch((err: unknown) => {
      if (!cancelled) setError(err instanceof Error ? err.message : t("factorLabSelectionFailed"));
    }).finally(() => { if (!cancelled) setFactorLoading(false); });
    return () => { cancelled = true; };
  }, [reload]);

  useEffect(() => {
    const request = ++versionRequest.current;
    setVersions([]);
    setVersionsFor("");
    if (!factorCode) { setVersionId(undefined); setVersionLoading(false); return; }
    setVersionLoading(true);
    setError(null);
    void api.listFactorVersions(factorCode).then((items) => {
      if (versionRequest.current !== request) return;
      const usable = items.filter(item => Number.isInteger(item.id) && Number(item.id) > 0);
      setVersions(usable);
      setVersionsFor(factorCode);
      const requested = wantedVersion.current;
      const chosen = requested == null
        ? usable.find(item => item.is_latest) || usable[0]
        : usable.find(item => item.id === requested);
      setVersionId(chosen?.id);
      if (chosen?.id) selectionListener.current?.({ factorCode, versionId: chosen.id });
      if (items.length > 0 && usable.length === 0) setError(t("factorCenterLibraryServicePendingDesc"));
      else if (requested != null && !chosen) setError(t("factorLabVersionMismatch"));
    }).catch((err: unknown) => {
      if (versionRequest.current === request) {
        setVersionId(undefined);
        setError(err instanceof Error ? err.message : t("factorLabSelectionFailed"));
      }
    }).finally(() => {
      if (versionRequest.current === request) setVersionLoading(false);
    });
    return () => { versionRequest.current += 1; };
  }, [factorCode, reload]);

  const selectFactor = useCallback((code: string | undefined) => {
    wantedVersion.current = undefined;
    versionRequest.current += 1;
    setCode(code || "");
    setVersionId(undefined);
    setVersions([]);
    selectionListener.current?.({ factorCode: code || undefined, versionId: undefined });
  }, []);
  const selectVersion = useCallback((id: number) => {
    if (versionsFor !== factorCode || !versions.some(item => item.id === id)) return;
    wantedVersion.current = id;
    setError(null);
    setVersionId(id);
    selectionListener.current?.({ factorCode, versionId: id });
  }, [factorCode, versions, versionsFor]);
  return {
    factorCode, versionId, factor: factors.find(factor => factor.code === factorCode),
    version: versionsFor === factorCode ? versions.find(version => version.id === versionId) : undefined,
    factorOptions: factors.map(factor => ({ value: factor.code, label: `${factorLabel(factor.code, factor.name)} (${factor.code})` })),
    versionOptions: versionsFor === factorCode ? versions.map(version => {
      const statusKey = `factorValidation_${version.validation_status || "pending"}`;
      const status = t(statusKey);
      return { value: version.id!, label: `v${version.version} · ${status === statusKey ? t("factorValidation_pending") : status}` };
    }) : [],
    factorLoading, versionLoading, error, selectFactor, selectVersion, retry: () => setReload(value => value + 1),
  };
}
