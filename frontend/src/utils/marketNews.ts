import type { MarketEvent, MarketEventListResponse, MarketEventQuery } from "../types";

export const CONCEPT_DAYS = { "7d": 7, "30d": 30, "90d": 90, "365d": 365 } as const;
const CREDIBILITY: Record<string, number> = {
  cctv: 5, "baidu-report": 4, baidu: 3, "eastmoney-global": 4, caixin: 4, "futures-shmet": 3, manual: 4,
};

// Naive timestamps from the service represent UTC, including on Shanghai browsers.
export function marketTime(value: string | null | undefined): number {
  if (!value) return 0;
  return Date.parse(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`);
}

export function storedSectorIds(event: MarketEvent, legacyMatch: (event: MarketEvent) => string[]): string[] {
  return event.affected_sectors != null
    ? [...new Set(event.affected_sectors.split(",").map((s) => s.trim()).filter(Boolean))]
    : legacyMatch(event);
}

export function aggregateMarketSectors(events: MarketEvent[], days: number,
    getIds: (event: MarketEvent) => string[], now = Date.now()) {
  const buckets = new Map<string, { id: string; bull: number; bear: number; neutral: number; maxLevel: number }>();
  const cutoff = now - days * 86400_000;
  for (const event of events) {
    const published = marketTime(event.published_at);
    if (!event.published_at || !Number.isFinite(published) || published < cutoff) continue;
    for (const id of new Set(getIds(event))) {
      const bucket = buckets.get(id) ?? { id, bull: 0, bear: 0, neutral: 0, maxLevel: 0 };
      if (event.sentiment === "positive") bucket.bull++;
      else if (event.sentiment === "negative") bucket.bear++;
      else bucket.neutral++;
      bucket.maxLevel = Math.max(bucket.maxLevel, event.importance_level);
      buckets.set(id, bucket);
    }
  }
  return [...buckets.values()].sort((a, b) =>
    (b.bull + b.bear + b.neutral) - (a.bull + a.bear + a.neutral) || b.maxLevel - a.maxLevel || a.id.localeCompare(b.id));
}

export function sortMarketEvents(events: MarketEvent[], sortBy?: string): MarketEvent[] {
  return [...events].sort((a, b) => {
    if (sortBy === "importance_level") {
      const difference = b.importance_level - a.importance_level || (CREDIBILITY[b.source] ?? 2) - (CREDIBILITY[a.source] ?? 2);
      if (difference) return difference;
    }
    return (marketTime(b.published_at) || 0) - (marketTime(a.published_at) || 0)
      || (marketTime(b.created_at) || 0) - (marketTime(a.created_at) || 0) || b.id - a.id;
  });
}

/** Read every matching page. Favorite IDs are split to keep GET URLs bounded.
 * Freeze the insert ceiling and expiration clock from the first response.
 * Updates/deletes can still change a live dataset: abort rather than silently
 * export missing/duplicated rows if page counts no longer agree.
 */
export async function fetchCompleteMarketEvents(
    fetchPage: (query: MarketEventQuery) => Promise<MarketEventListResponse>, query: MarketEventQuery,
    favoriteIds?: number[], isCurrent: () => boolean = () => true): Promise<MarketEventListResponse> {
  const batches: Array<number[] | undefined> = [];
  if (favoriteIds === undefined) batches.push(undefined);
  else {
    const unique = [...new Set(favoriteIds.filter((id) => Number.isSafeInteger(id) && id > 0))];
    for (let offset = 0; offset < unique.length; offset += 100) batches.push(unique.slice(offset, offset + 100));
  }
  const rows: MarketEvent[] = [];
  const seen = new Set<number>();
  let first: MarketEventListResponse | undefined;
  let expired = 0;
  let snapshot = { as_of: query.as_of, snapshot_max_id: query.snapshot_max_id };
  for (const ids of batches) {
    let offset = 0;
    let total: number | undefined;
    do {
      if (!isCurrent()) throw new Error("market_news_request_superseded");
      const page = await fetchPage({ ...query, ...snapshot, event_ids: ids, limit: 200, offset });
      if (!isCurrent()) throw new Error("market_news_request_superseded");
      first ??= page;
      snapshot = { as_of: first.as_of ?? snapshot.as_of, snapshot_max_id: first.snapshot_max_id ?? snapshot.snapshot_max_id };
      total ??= page.total;
      if (page.total !== total || (page.events.length === 0 && offset < total)) throw new Error("market_news_data_changed");
      if (offset === 0) expired += page.expired_total ?? 0;
      for (const event of page.events) {
        if (seen.has(event.id)) throw new Error("market_news_data_changed");
        seen.add(event.id);
        rows.push(event);
      }
      offset += page.events.length;
      if (offset > total) throw new Error("market_news_data_changed");
    } while (offset < total!);
  }
  return { ...first, events: sortMarketEvents(rows, query.sort_by), total: rows.length,
    by_scope: first?.by_scope ?? {}, by_level: first?.by_level ?? {}, expired_total: expired };
}

export function marketEventsCsv(rows: MarketEvent[]): string {
  const escape = (value: unknown) => {
    let text = String(value ?? "");
    // Spreadsheet software can evaluate formula-like external news text.
    if (typeof value === "string" && /^\s*[=+\-@]/.test(text)) text = `'${text}`;
    return `"${text.replace(/"/g, '""')}"`;
  };
  const lines = ["published_at,impact_scope,importance_level,sentiment,source,title,summary,source_url"];
  for (const event of rows) lines.push([
    event.published_at ?? "", event.impact_scope, event.importance_level, event.sentiment,
    event.source, event.title, (event.summary ?? "").replace(/[\r\n]+/g, " "), event.source_url ?? "",
  ].map(escape).join(","));
  return "\uFEFF" + lines.join("\r\n");
}
