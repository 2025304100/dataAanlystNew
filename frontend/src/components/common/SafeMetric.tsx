import React from "react";
import { Tooltip } from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";

export interface SafeMetricProps {
  value: number | null | undefined;
  missingReason?: string | null;
  ruleCode?: string | null;
  suffix?: string;
  /** 保留小数位，默认 4 */
  precision?: number;
  className?: string;
}

const isBadNumber = (v: unknown): boolean =>
  v === null ||
  v === undefined ||
  typeof v === "number"
    ? Number.isNaN(v) || !Number.isFinite(v)
    : false;

export const SafeMetric: React.FC<SafeMetricProps> = ({
  value,
  missingReason,
  ruleCode,
  suffix,
  precision = 4,
  className,
}) => {
  if (isBadNumber(value)) {
    const reason =
      missingReason && String(missingReason).trim().length > 0
        ? missingReason
        : ruleCode && String(ruleCode).trim().length > 0
          ? `规则排除：${ruleCode}`
          : "数据缺失或未通过过滤";
    return (
      <span className={className}>
        {"-"}
        <Tooltip title={reason} placement="top">
          <QuestionCircleOutlined
            style={{ marginLeft: 4, color: "#999", fontSize: 12 }}
          />
        </Tooltip>
      </span>
    );
  }
  const num = Number(value);
  const fixed = Number.isInteger(precision) && precision >= 0 ? num.toFixed(precision) : String(num);
  return (
    <span className={className}>
      {fixed}
      {suffix ? ` ${suffix}` : ""}
    </span>
  );
};

export default SafeMetric;
