import { t } from "../../../../../i18n";
import type { PoolMember } from "./poolApi";

/**
 * 候选列表（向导 §3.6）。
 *
 * 支持搜索/排序/分页/全选/批量删除（**删除按钮在锁定态置灰**）；
 * 批量删除只解除池-标的关联，不删主数据，且必须二次确认（由父级弹出）。
 *
 * ⚠️ **列按数据可用性动态呈现**：后端 `list_members` 实测（2026-09-21）只返回
 * `symbol/name/asset_type/market/board/industry/is_st/is_active/included_at`；
 * 设计 §3.6 里的行情/估值/财务指标与「纳入来源 / 纳入原因 / 数据完整性」**当前不返回**。
 * 与其每行渲染一列 "-"（看起来像坏了），不如**整列隐藏**并给一行说明；
 * 后端补字段后该列会自动出现（判据 = 本页是否有任一成员带该字段）。
 * ⚠️ `data-pool-member-table` 与 `tbody input[type=checkbox]` 数量是测试钩子，不得改动。
 */
export interface PoolMemberTableProps {
  members: PoolMember[];
  selected: number[];
  locked: boolean;
  onToggle: (symbolId: number) => void;
  onToggleAll: () => void;
}

/** 完整性标记 → 语义标签（未知值按中性展示，不臆造含义） */
function integrityClass(value: string | null | undefined): string {
  const v = String(value ?? "").toLowerCase();
  if (!v) return "mining-chip--muted";
  if (v.includes("ok") || v.includes("complete") || v.includes("full")) return "mining-chip--success";
  if (v.includes("partial") || v.includes("warn")) return "mining-chip--warn";
  if (v.includes("miss") || v.includes("fail") || v.includes("low")) return "mining-chip--danger";
  return "mining-chip--muted";
}

function riskClass(value: string | null | undefined): string {
  const v = String(value ?? "").toUpperCase();
  if (!v) return "mining-chip--muted";
  if (v.includes("ST")) return "mining-chip--danger";
  return "mining-chip--warn";
}

/** 该字段在本页成员中是否有任一非空值（决定整列是否呈现） */
function hasAny(members: PoolMember[], pick: (m: PoolMember) => unknown): boolean {
  return members.some((m) => {
    const v = pick(m);
    return v != null && v !== "";
  });
}

export default function PoolMemberTable({
  members, selected, locked, onToggle, onToggleAll,
}: PoolMemberTableProps) {
  const selectedCount = members.filter((m, idx) =>
    selected.includes(m.symbol_id ?? idx),
  ).length;

  const showMarket = hasAny(members, (m) => m.market ?? m.board);
  const showSource = hasAny(members, (m) => m.source);
  const showIntegrity = hasAny(members, (m) => m.integrity);
  const showReason = hasAny(members, (m) => m.reason);

  const columnCount =
    5 + // 复选框 + 序号 + 代码 + 名称 + 风险标记
    (showMarket ? 1 : 0) +
    (showSource ? 1 : 0) +
    (showIntegrity ? 1 : 0) +
    (showReason ? 1 : 0);

  return (
    <div className="mining-pool-table-wrap">
      <div className="mining-card-head" style={{ padding: "12px 14px 0" }}>
        <span className="mining-card-title mining-card-title--plain">
          {t("miningPoolColSymbol")}
        </span>
        <span className="mining-card-sub" data-pool-member-count>
          {t("miningPoolSelectedCount").replace("{n}", String(selectedCount))}
          {locked ? ` · ${t("miningPoolLockedTableHint")}` : ""}
        </span>
      </div>
      <table className="mining-pool-table" data-pool-member-table>
        <thead>
          <tr>
            <th style={{ width: 40 }}>
              <input
                type="checkbox"
                aria-label="select all"
                disabled={locked}
                onChange={onToggleAll}
              />
            </th>
            <th style={{ width: 56 }}>{t("miningPoolColIndex")}</th>
            <th>{t("miningPoolColSymbol")}</th>
            <th>{t("miningPoolColName")}</th>
            <th>{t("miningPoolColRisk")}</th>
            {showMarket && <th>{t("miningPoolColMarket")}</th>}
            {showSource && <th>{t("miningPoolColSource")}</th>}
            {showIntegrity && <th>{t("miningPoolColIntegrity")}</th>}
            {showReason && <th>{t("miningPoolColReason")}</th>}
          </tr>
        </thead>
        <tbody>
          {members.length === 0 && (
            <tr className="mining-table-empty">
              <td colSpan={columnCount}>{t("miningPoolEmpty")}</td>
            </tr>
          )}
          {members.map((m, idx) => {
            const id = m.symbol_id ?? idx;
            // 风险标记读**真实字段** `is_st`（后端没有 risk_flag）；risk_flag 仅作未来兼容
            const stFlag = m.risk_flag
              ? String(m.risk_flag)
              : m.is_st === 1
                ? "ST"
                : null;
            const marketText = [m.market, m.board].filter(Boolean).join(" / ");
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
                <td className="mining-cell-idx">{idx + 1}</td>
                <td className="mining-cell-formula">{m.symbol}</td>
                <td>{m.name ?? "-"}</td>
                <td>
                  {stFlag ? (
                    <span className={`mining-chip ${riskClass(stFlag)}`} data-pool-member-risk>
                      {stFlag}
                    </span>
                  ) : (
                    <span className="mining-cell-muted">-</span>
                  )}
                </td>
                {showMarket && (
                  <td>{marketText || <span className="mining-cell-muted">-</span>}</td>
                )}
                {showSource && <td>{m.source ?? "-"}</td>}
                {showIntegrity && (
                  <td>
                    {m.integrity ? (
                      <span className={`mining-chip ${integrityClass(m.integrity)}`}>
                        {m.integrity}
                      </span>
                    ) : (
                      <span className="mining-cell-muted">-</span>
                    )}
                  </td>
                )}
                {showReason && <td className="mining-cell-muted">{m.reason ?? "-"}</td>}
              </tr>
            );
          })}
        </tbody>
      </table>
      {/* 契约缺口说明：不渲染空列，但把原因讲清楚 */}
      {members.length > 0 && !showSource && (
        <p className="mining-hint" style={{ padding: "8px 14px 12px" }} data-pool-member-gap-note>
          {t("miningPoolMemberGapNote")}
        </p>
      )}
    </div>
  );
}
