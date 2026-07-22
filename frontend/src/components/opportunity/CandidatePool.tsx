// WP1.2：机会中心-候选池子组件
// 候选列表，复用现有 Discovery 组件，不重写候选数据获取逻辑
//
// 设计说明：
// - Discovery.tsx 是无 props 的单体组件，直接从 AppContext 获取数据
// - WP1.1 要求"第一版组合现有 Discovery 数据，不改扫描算法"
// - WP1.2 要求"复用 Discovery 结果表抽成可复用组件"
// - 因此 CandidatePool 直接渲染 <Discovery />，保证候选数据与旧入口一致
// - 未来 WP5 标的研究收口后可逐步抽出共享候选表格组件
import Discovery from "../Discovery";

export interface CandidatePoolProps {
  /** 自定义 className */
  className?: string;
}

/** 机会中心候选池：直接复用 Discovery 组件 */
export default function CandidatePool({ className }: CandidatePoolProps) {
  return (
    <div
      className={`opportunity-candidate-pool ${className ?? ""}`}
      data-opportunity-tab="candidate"
    >
      <Discovery />
    </div>
  );
}
