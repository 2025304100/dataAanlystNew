import { describe, expect, it, vi } from 'vitest';
import { requestJson } from '../client';
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
