import { openSettingsSection } from './navigate';
import type { ETFTrainingSelection } from '../api/client';

export const ETF_TRAINING_OPEN_EVENT = 'factor-etf-training:open';
export const ETF_TRAINING_SELECTION_KEY = 'factor-etf-training:history:v1';
export const ETF_HISTORY_FOCUS_EVENT = 'etf-training-history:focus';
export type ETFHistoryFocus = 'codes' | 'dates';

export function openETFHistory(focus: ETFHistoryFocus = 'codes') {
  try {
    localStorage.setItem('settings_data_center_tab', 'market');
    localStorage.setItem(ETF_HISTORY_FOCUS_EVENT, focus);
  } catch { /* The mounted page can still receive the destination. */ }
  openSettingsSection('data-center');
  window.dispatchEvent(new CustomEvent('data-center:navigate', { detail: 'market' }));
  window.dispatchEvent(new CustomEvent(ETF_HISTORY_FOCUS_EVENT, { detail: focus }));
}

export function readETFTrainingSelection(raw: string): ETFTrainingSelection | undefined {
  if (raw === '1') return undefined;
  const value = JSON.parse(raw);
  const selected = value?.selection;
  if (value?.version !== 1 || !selected
      || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(selected.dataset_id)
      || !/^[a-f0-9]{64}$/.test(selected.manifest_sha256) || !/^[a-f0-9]{64}$/.test(selected.list_sha256)
      || !/^\d{4}-\d{2}-\d{2}$/.test(selected.start_date) || !/^\d{4}-\d{2}-\d{2}$/.test(selected.end_date)
      || selected.start_date > selected.end_date || !Number.isInteger(selected.trade_date_count) || selected.trade_date_count < 1
      || !Number.isInteger(selected.symbol_count) || selected.symbol_count < 1) throw new Error('ETF训练资料记录不完整');
  return selected;
}

/** Open the existing checks dialog; submitting training remains an explicit action. */
export function openETFTrainingChecks(selection?: ETFTrainingSelection) {
  const raw = selection ? JSON.stringify({ version: 1, selection }) : '1';
  try {
    localStorage.setItem('settings_factor_center_subtab', 'model');
    localStorage.setItem(ETF_TRAINING_OPEN_EVENT, raw);
  } catch { /* An already mounted model page can still handle the event. */ }
  openSettingsSection('factor-center');
  window.dispatchEvent(new CustomEvent('factor-center:navigate', { detail: { target: 'model' } }));
  window.dispatchEvent(new CustomEvent(ETF_TRAINING_OPEN_EVENT, { detail: raw }));
}
