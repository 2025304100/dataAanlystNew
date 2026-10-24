import { reconciliationText } from "./reconciliationPresentation";

export function repairObjectMessage(object: Record<string, unknown>, finished: boolean): string {
  const saved = [object.message, object.reason, object.error].find(value => typeof value === "string" && value.trim()) as string | undefined;
  if (saved === "数据修复失败，请查看来源任务后重试") return "原任务未保留具体来源错误；可核查本任务日志，或仅重试失败步骤以取得当前错误。";
  if (saved) return reconciliationText(saved, "本步骤未完成，请查看失败详情并核查原任务记录。");
  if (object.reinspection_status === "PASSED") return "目标范围已重新核验，本步骤没有剩余数据缺口。";
  if (object.status === "succeeded") return "产物已保存，数据是否有效以重检结论为准。";
  return finished ? "本步骤结果已保存，具体来源说明未保留。" : "正在等待本步骤的执行结果。";
}

export function repairNextAction(object: Record<string, unknown>): string {
  return reconciliationText(typeof object.next_action === "string" ? object.next_action : null,
    "核查失败原因后，回到本批结果仅重试失败与依赖受阻步骤；已成功的其他步骤保留。 ");
}
