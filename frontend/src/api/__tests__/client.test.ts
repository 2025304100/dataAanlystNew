import { describe, expect, it, vi } from 'vitest';
import { api, requestJson, _scoringFactorSetsToFactorSets } from '../client';
import { setLocale } from '../../i18n';

describe('requestJson deduplication', () => {
  it('shares an in-flight GET', async () => {
    const response = { ok: true, json: async () => ({ value: 42 }) } as Response;
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal('fetch', fetchMock);
    const result = await Promise.all([requestJson('/test'), requestJson('/test')]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result).toEqual([{ value: 42 }, { value: 42 }]);
    vi.unstubAllGlobals();
  });

  it('starts a fresh GET after settlement', async () => {
    const response = { ok: true, json: async () => ({ ok: true }) } as Response;
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal('fetch', fetchMock);
    await requestJson('/test');
    await requestJson('/test');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.unstubAllGlobals();
  });

  it('does not deduplicate mutations', async () => {
    const response = { ok: true, json: async () => ({ ok: true }) } as Response;
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal('fetch', fetchMock);
    await Promise.all([
      requestJson('/test', { method: 'POST' }),
      requestJson('/test', { method: 'POST' }),
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.unstubAllGlobals();
  });

  it('localizes generic HTTP status text without hiding business details', async () => {
    setLocale('zh-CN');
    const genericResponse = {
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: async () => ({ detail: 'Internal Server Error' }),
    } as Response;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(genericResponse));
    await expect(requestJson('/generic-error')).rejects.toMatchObject({
      message: '服务暂时不可用，请稍后重试',
      status_code: 500,
    });

    const businessResponse = {
      ok: false,
      status: 409,
      statusText: 'Conflict',
      json: async () => ({ detail: '该任务正在运行' }),
    } as Response;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(businessResponse));
    await expect(requestJson('/business-error')).rejects.toMatchObject({
      message: '该任务正在运行',
      status_code: 409,
    });
    vi.unstubAllGlobals();
  });
});

describe('daily backtest position ledger client', () => {
  it('keeps the existing pagination and position-filter query contract', async () => {
    const response = { ok: true, json: async () => ({ items: [] }) } as Response;
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal('fetch', fetchMock);

    await api.getBacktestPositions(42, {
      page: 2,
      pageSize: 50,
      asOfDate: '2025-01-31',
      status: 'CLOSED',
    });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/backtest/runs/42/positions?page=2&page_size=50&as_of_date=2025-01-31&status=CLOSED',
      expect.any(Object),
    );
    vi.unstubAllGlobals();
  });
});

// ═══════════════════════════════════════════════════════════════════════════
// T1 Test Requirements — factor-center-chain-closure-20260902 / Task 1
// ═══════════════════════════════════════════════════════════════════════════

describe('T1 TR-1.1: createFactorSet request contract (name / description / no factor_id manual numeric)', () => {
  it('POSTs correct name and description; URL and body never carry a raw user factor_id numeric value', async () => {
    let capturedUrl: string = '';
    let capturedBody: string = '';
    let capturedInit: RequestInit | undefined;
    const fetchMock = vi.fn().mockImplementation((url: any, init: any) => {
      capturedUrl = String(url);
      capturedInit = init;
      if (init?.body) capturedBody = String(init.body);
      return Promise.resolve({ ok: true, json: async () => ({ id: 'FS-1', name: 'My Set', n_members: 0, status: 'draft' }) } as Response);
    });
    vi.stubGlobal('fetch', fetchMock);

    await api.createFactorSet({ name: 'MyCollection', description: '测试描述', asset_type: 'STOCK' });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    // 1a) URL 正确：POST /api/v1/factor-sets
    expect(capturedUrl).toBe('/api/v1/factor-sets');
    expect(capturedInit?.method).toBe('POST');

    // 1b) POST body 字段 name / description 值正确
    const payload = JSON.parse(capturedBody || '{}');
    expect(payload.name).toBe('MyCollection');
    expect(payload.description).toBe('测试描述');

    // 1c) URL 和 payload 中绝对不出现调用方手填的原生 factor_id 数字值
    //     (createFactorSet 的请求契约不含 factor_id 字段，前端不暴露数字 ID 输入)
    expect(payload).not.toHaveProperty('factor_id');
    expect(/\bfactor_id\b/.test(capturedUrl)).toBe(false);
    // 同时严格：payload 的任何字段值不能是可疑的手动数字 factor_id 形式（例如 ≥3 位纯数字暗示手填 ID）
    const numericFingerprint = /factor_id.*[0-9]{3,}/;
    expect(numericFingerprint.test(JSON.stringify(payload))).toBe(false);
    expect(numericFingerprint.test(capturedUrl)).toBe(false);

    vi.unstubAllGlobals();
  });
});

describe('T1 TR-1.2: _scoringFactorSetsToFactorSets DTO label↔name / n_members↔member_count 双向映射', () => {
  it('返回 items[0].id === 1 && label === "N" && member_count === 3', () => {
    // 严格匹配 TR-1.2 规则原文的输入形状
    const input = { items: [{ id: 1, name: 'N', n_members: 3 }] };
    const result = _scoringFactorSetsToFactorSets(input as any);

    expect(Array.isArray(result)).toBe(true);
    expect(result.length).toBeGreaterThanOrEqual(1);
    expect(result[0].id).toBe(1);
    expect(result[0].label).toBe('N');
    expect(result[0].member_count).toBe(3);

    // 同时断言双向映射的另一侧字段也被同步（DTO 保持双写兼容）
    expect(result[0].name).toBe('N');
    expect(result[0].n_members).toBe(3);
  });
});
