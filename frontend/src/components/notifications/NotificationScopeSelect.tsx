import { useEffect, useRef, useState } from "react";
import { Alert, Select } from "antd";
import { requestJson } from "../../api/client";
import { t } from "../../i18n";

type Option = { value: number; label: string };

export default function NotificationScopeSelect({ scope, value = [], onChange, id }: {
  scope: string; value?: number[]; onChange?: (ids: number[]) => void; id?: string;
}) {
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState<Option[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const target = useRef(scope);
  useEffect(() => {
    let current = true;
    if (target.current !== scope) { target.current = scope; setOptions([]); setQuery(""); }
    setLoading(true);
    setError("");
    const timer = window.setTimeout(() => {
      const url = scope === "symbol"
        ? `/api/v1/symbols?page=1&page_size=50&keyword=${encodeURIComponent(query)}`
        : "/api/v1/portfolios";
      requestJson<Array<{ id: number; name: string; symbol?: string }>>(url)
        .then((rows) => {
          if (!current) return;
          setOptions((previous) => {
            const merged = new Map(previous.map((option) => [option.value, option]));
            (Array.isArray(rows) ? rows : []).forEach((row) => merged.set(row.id, { value: row.id, label: `${row.symbol ? `${row.symbol} · ` : ""}${row.name} (#${row.id})` }));
            return [...merged.values()];
          });
        })
        .catch((reason) => { if (current) setError(reason instanceof Error ? reason.message : t("loadFailed")); })
        .finally(() => { if (current) setLoading(false); });
    }, scope === "symbol" ? 200 : 0);
    return () => { current = false; window.clearTimeout(timer); };
  }, [scope, query]);
  const known = new Set(options.map((option) => option.value));
  return <div>
    <Select id={id} mode="multiple" value={value} onChange={onChange} loading={loading}
      showSearch onSearch={scope === "symbol" ? setQuery : undefined} filterOption={scope === "symbol" ? false : (input, option) => String(option?.label).toLowerCase().includes(input.toLowerCase())}
      options={[...options, ...value.filter((id) => !known.has(id)).map((id) => ({ value: id, label: `#${id}` }))]}
      placeholder={t("settingsNotificationScopeIds")} style={{ width: "100%" }} />
    {error && <Alert type="error" message={error} style={{ marginTop: 8 }} />}
  </div>;
}
