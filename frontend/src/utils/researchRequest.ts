export type PendingResearchRequest = {
  version: 1; modelId: string; requestId: string; manifestSha256: string; note: string;
};
const key = (modelId: string) => `factor-research:pending:v1:${encodeURIComponent(modelId)}`;

export function readPendingResearch(modelId: string): PendingResearchRequest | null {
  const raw = localStorage.getItem(key(modelId));
  if (raw === null) return null;
  const value = JSON.parse(raw);
  if (value?.version !== 1 || value.modelId !== modelId || !/^[a-f0-9]{32}$/.test(value.requestId)
      || !/^[a-f0-9]{64}$/.test(value.manifestSha256) || typeof value.note !== "string" || value.note.length > 300) {
    throw new Error("invalid_research_request_receipt");
  }
  return { version: 1, modelId, requestId: value.requestId, manifestSha256: value.manifestSha256, note: value.note };
}

export function savePendingResearch(request: PendingResearchRequest) {
  const previous = readPendingResearch(request.modelId);
  if (previous && JSON.stringify(previous) !== JSON.stringify(request)) throw new Error("research_request_already_pending");
  localStorage.setItem(key(request.modelId), JSON.stringify(request));
}

export function clearPendingResearch(request: PendingResearchRequest) {
  if (readPendingResearch(request.modelId)?.requestId === request.requestId) localStorage.removeItem(key(request.modelId));
}
