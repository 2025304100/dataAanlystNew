import type { ETFTrainingRequest } from '../api/client';

const key = 'factor-etf-training:pending:v1';
export const sameETFTrainingRequest = (left: ETFTrainingRequest, right: ETFTrainingRequest) =>
  ['request_id', 'factor_set_id', 'dataset_id', 'manifest_sha256', 'note'].every(
    field => left[field as keyof ETFTrainingRequest] === right[field as keyof ETFTrainingRequest]);

export function readETFTrainingRequest(): ETFTrainingRequest | null {
  const raw = localStorage.getItem(key);
  if (raw === null) return null;
  const value = JSON.parse(raw);
  const request = value?.request;
  if (value?.version !== 1 || !request || !/^[a-f0-9]{32}$/.test(request.request_id)
      || typeof request.factor_set_id !== 'string' || !request.factor_set_id || request.factor_set_id.length > 64
      || !/^[a-z0-9][a-z0-9_-]{0,79}$/.test(request.dataset_id)
      || !/^[a-f0-9]{64}$/.test(request.manifest_sha256)
      || typeof request.note !== 'string' || request.note.length > 300
      || Object.keys(request).sort().join(',') !== 'dataset_id,factor_set_id,manifest_sha256,note,request_id') {
    throw new Error('ETF训练记录读取失败');
  }
  return request;
}

export function saveETFTrainingRequest(request: ETFTrainingRequest) {
  const previous = readETFTrainingRequest();
  if (previous && !sameETFTrainingRequest(previous, request)) throw new Error('还有一次ETF训练需要确认');
  localStorage.setItem(key, JSON.stringify({ version: 1, request }));
}

export function clearETFTrainingRequest(request: ETFTrainingRequest) {
  const previous = readETFTrainingRequest();
  if (!previous || !sameETFTrainingRequest(previous, request)) throw new Error('ETF训练记录已变化');
  localStorage.removeItem(key);
}
