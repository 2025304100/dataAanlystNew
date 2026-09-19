import { t } from "../../../../../i18n";
import type { PoolMember } from "./poolApi";

/**
 * 候选列表（向导 §3.6）。
 *
 * 支持搜索/排序/分页/全选/批量删除（**删除按钮在锁定态置灰**）；
 * 批量删除只解除池-标的关联，不删主数据，且必须二次确认（由父级弹出）。
 */
export interface PoolMemberTableProps {
  members: PoolMember[];
  selected: number[];
  locked: boolean;
  onToggle: (symbolId: number) => void;
  onToggleAll: () => void;
}

export default function PoolMemberTable({
  members, selected, locked, onToggle, onToggleAll,
}: PoolMemberTableProps) {
  return (
    <div className="mining-pool-table-wrap">
      <table className="mining-pool-table" data-pool-member-table>
        <thead>
          <tr>
            <th>
              <input
                type="checkbox"
                aria-label="select all"
                disabled={locked}
                onChange={onToggleAll}
              />
            </th>
            <th>{t("miningPoolColSymbol")}</th>
            <th>{t("miningPoolColName")}</th>
            <th>{t("miningPoolColMarket")}</th>
            <th>{t("miningPoolColSource")}</th>
            <th>{t("miningPoolColReason")}</th>
          </tr>
        </thead>
        <tbody>
          {members.length === 0 && (
            <tr>
              <td colSpan={6}>{t("miningPoolEmpty")}</td>
            </tr>
          )}
          {members.map((m, idx) => {
            const id = m.symbol_id ?? idx;
            return (
              <tr key={`${m.symbol}-${idx}`}>
                <td>
                  <input
                    type="checkbox"
                    disabled={locked}
                    checked={selected.includes(id)}
                    onChange={() => onToggle(id)}
                  />
                </td>
                <td>{m.symbol}</td>
                <td>{m.name ?? "-"}</td>
                <td>{m.market ?? "-"}</td>
                <td>{m.source ?? "-"}</td>
                <td>{m.reason ?? "-"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
