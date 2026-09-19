// T12.3: 安全错误渲染工具，杜绝 err.reason.split() / err.message.split() / string.split 崩溃。

export interface NormalizedBackendError {
  error_code: string;
  title_zh: string;
  detail_zh: string;
  correlation_id: string;
  impact: string;
  fix_link: string;
  retryable: boolean;
  blocking_reasons?: Array<Record<string, unknown>>;
  raw_message: string;
  deduced: boolean;
}

const FALLBACK_ERROR_CODE = "UNKNOWN_ERROR";
const FALLBACK_TITLE_ZH = "未知错误";
const FALLBACK_DETAIL_ZH = "发生了一个未分类的错误，请稍后重试或联系技术支持。";

function safeStringify(input: unknown): string {
  if (input == null) return "";
  if (typeof input === "string") return input;
  if (typeof input === "number" || typeof input === "boolean" || typeof input === "bigint") {
    return String(input);
  }
  if (input instanceof Date) {
    try {
      const iso = input.toISOString();
      return "[Date: " + iso + "]";
    } catch {
      return "[Date: Invalid]";
    }
  }
  if (typeof input === "function") {
    try {
      const fn = input as { name?: string };
      const name = fn.name || "anonymous";
      return "[Function: " + name + "]";
    } catch {
      return "[Function]";
    }
  }
  if (input instanceof Error) {
    const msg = input.message ? String(input.message) : "";
    const name = input.name ? String(input.name) : "Error";
    if (msg) return msg;
    if (input.stack) {
      const first = input.stack.split(/\r?\n/)[0];
      return first || name;
    }
    return name;
  }
  if (typeof input === "symbol") {
    return input.toString();
  }
  try {
    return JSON.stringify(input);
  } catch {
    try {
      return Object.prototype.toString.call(input);
    } catch {
      return "[Object]";
    }
  }
}

export function renderReasonToString(reason: unknown, maxLen = 400): string {
  try {
    if (reason == null) return "";
    if (typeof reason === "object") {
      const rec = reason as Record<string, unknown>;
      const hasStruct =
        ("error_code" in rec) ||
        ("title_zh" in rec) ||
        ("detail_zh" in rec) ||
        ("correlation_id" in rec) ||
        ("fix_link" in rec) ||
        ("retryable" in rec) ||
        ("impact" in rec);
      if (hasStruct) {
        const code = safeStringify(rec.error_code).trim();
        const title = safeStringify(rec.title_zh).trim();
        const detail = safeStringify(rec.detail_zh).trim();
        const message = safeStringify(rec.message).trim();
        const reasonField = safeStringify(rec.reason).trim();
        const body = detail || message || reasonField || title || FALLBACK_DETAIL_ZH;
        let prefix = "";
        if (code && title) prefix = "[" + code + "] " + title;
        else if (code) prefix = "[" + code + "]";
        else if (title) prefix = title;
        const full = prefix ? (prefix + ": " + body) : body;
        if (full.length > maxLen) {
          return full.slice(0, Math.max(0, maxLen - 3)) + "...";
        }
        return full;
      }
      const recN = reason as Record<string, unknown>;
      const msg =
        safeStringify(recN.message) ||
        safeStringify(recN.reason) ||
        safeStringify(recN.detail) ||
        safeStringify(recN.errorMessage) ||
        "";
      if (msg) {
        if (msg.length > maxLen) return msg.slice(0, Math.max(0, maxLen - 3)) + "...";
        return msg;
      }
    }
    const raw = safeStringify(reason);
    if (!raw) return "";
    if (raw.length > maxLen) return raw.slice(0, Math.max(0, maxLen - 3)) + "...";
    return raw;
  } catch {
    return FALLBACK_TITLE_ZH;
  }
}

function pickString(obj: Record<string, unknown>, keys: string[], fallback = ""): string {
  for (const k of keys) {
    if (k in obj && obj[k] != null) {
      const s = safeStringify(obj[k]);
      if (s) return s;
    }
  }
  return fallback;
}

function pickBlocking(candidate: unknown): Array<Record<string, unknown>> | undefined {
  if (Array.isArray(candidate) && candidate.length > 0) {
    const result: Array<Record<string, unknown>> = [];
    for (const x of candidate) {
      if (typeof x === "object" && x != null) {
        result.push(x as Record<string, unknown>);
      } else {
        result.push({ value: x });
      }
    }
    return result;
  }
  return undefined;
}

export function normalizeBackendError(resp: unknown): NormalizedBackendError {
  const original = resp;
  const fallback: NormalizedBackendError = {
    error_code: FALLBACK_ERROR_CODE,
    title_zh: FALLBACK_TITLE_ZH,
    detail_zh: FALLBACK_DETAIL_ZH,
    correlation_id: "",
    impact: "",
    fix_link: "",
    retryable: false,
    raw_message: renderReasonToString(original),
    deduced: true,
  };
  if (resp == null) return fallback;

  let root: unknown = resp;
  try {
    const recAny = resp as Record<string, unknown>;
    if (typeof recAny.response === "object" && recAny.response != null) {
      const innerResp = recAny.response as Record<string, unknown>;
      if (typeof innerResp.data === "object" && innerResp.data != null) {
        root = innerResp.data;
      } else {
        root = innerResp;
      }
    } else if (typeof recAny.data === "object" && recAny.data != null) {
      root = recAny.data;
    }
  } catch {
    root = resp;
  }

  let payload: Record<string, unknown> = {};
  let found = false;
  try {
    if (typeof root === "object" && root != null) {
      const rootRec = root as Record<string, unknown>;
      payload = { ...rootRec } as Record<string, unknown>;
      let current: unknown = rootRec.detail;
      for (let depth = 0; depth < 2 && current != null && typeof current === "object"; depth++) {
        const curRec = current as Record<string, unknown>;
        payload = { ...payload, ...curRec };
        if ("error_code" in curRec || ("title_zh" in curRec && "detail_zh" in curRec)) {
          found = true;
        }
        current = curRec.detail;
      }
      if ("error_code" in payload || "title_zh" in payload) found = true;
    }
  } catch {
    return { ...fallback, raw_message: renderReasonToString(original) };
  }

  let blockingReasons: Array<Record<string, unknown>> | undefined;
  blockingReasons =
    pickBlocking((payload as Record<string, unknown>).blocking_reasons) ??
    pickBlocking((root as Record<string, unknown> | null | undefined)?.blocking_reasons as unknown) ??
    undefined;

  const payloadR = payload as Record<string, unknown>;
  let error_code = pickString(payloadR, ["error_code", "code", "reason_code", "errorCode"], found ? "" : FALLBACK_ERROR_CODE);
  let title_zh = pickString(payloadR, ["title_zh", "title", "user_message", "userTitle"], "");
  if (!title_zh) title_zh = FALLBACK_TITLE_ZH;
  let detail_zh = pickString(payloadR, ["detail_zh", "detail", "message", "reason", "error_message"], "");
  if (!detail_zh) {
    if (found) detail_zh = "";
    else detail_zh = renderReasonToString(original) || FALLBACK_DETAIL_ZH;
  }
  const correlation_id = pickString(payloadR, ["correlation_id", "correlationId", "trace_id", "request_id"], "");
  const impact = pickString(payloadR, ["impact"], "");
  const fix_link = pickString(payloadR, ["fix_link", "fixLink", "doc_link"], "");
  let retryable = false;
  try {
    if ("retryable" in payloadR) retryable = Boolean(payloadR.retryable);
  } catch {
    retryable = false;
  }
  if (!error_code) error_code = FALLBACK_ERROR_CODE;

  const deduced = !found;
  const raw_message = renderReasonToString(original);
  const result: NormalizedBackendError = {
    error_code,
    title_zh,
    detail_zh,
    correlation_id,
    impact,
    fix_link,
    retryable,
    raw_message,
    deduced,
  };
  if (blockingReasons && blockingReasons.length > 0) {
    result.blocking_reasons = blockingReasons;
  }
  return result;
}
