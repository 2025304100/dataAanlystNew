/**
 * WPD-05: 任务错误消息显示工具。
 *
 * 解决 factor_pipeline 失败任务的中文 message 乱码问题：
 * 1. 优先用稳定 error_code → i18n 翻译（事实来源）
 * 2. error_code 存在但无翻译 → 回退到 task.message（可能含历史中文）
 * 3. 无 error_code 且 message 疑似乱码 → 回退到 UNKNOWN_ERROR 翻译
 */

/** getTaskErrorMessage 接受的最小输入形状（UnifiedTask / AsyncTaskRead 均可满足）。 */
interface TaskErrorMessageInput {
  message?: string | null;
  error_code?: string | null;
  errors?: Array<Record<string, unknown>> | null;
}

/**
 * 判断文本是否疑似乱码。
 *
 * 启发式规则（满足任一即判定为乱码）：
 * 1. 含 U+FFFD 替换符（UTF-8 解码失败的典型标志）
 * 2. 含连续 4 个以上非可打印控制字符（C0/C1 控制符区段）
 * 3. 含 BMP 之外的字符（代理对/非 BMP）且可打印 ASCII 占比 < 30%
 *    （正常中文文本中 ASCII 标点/数字占比不会这么低）
 */
export function isLikelyMojibake(text: string | null | undefined): boolean {
  if (text == null || text === "") {
    return false;
  }

  // 规则 1：含 U+FFFD 替换符
  if (text.includes("\uFFFD")) {
    return true;
  }

  // 规则 2：含连续 4 个以上非可打印控制字符
  // 排除常见的合法空白符（\t \n \r），仅检测 C0 控制符区段
  let consecutiveControls = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0)!;
    if (
      (code < 0x20 && code !== 0x09 && code !== 0x0a && code !== 0x0d) ||
      (code >= 0x7f && code < 0xa0)
    ) {
      consecutiveControls++;
      if (consecutiveControls >= 4) {
        return true;
      }
    } else {
      consecutiveControls = 0;
    }
  }

  // 规则 3：含非 BMP 字符且可打印 ASCII 占比 < 30%
  // 正常中文文本即使含少量 ASCII 标点，占比也不会低于 30%
  let hasNonBmp = false;
  let printableAscii = 0;
  let total = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0)!;
    total++;
    if (code > 0xffff) {
      hasNonBmp = true;
    }
    if (code >= 0x20 && code <= 0x7e) {
      printableAscii++;
    }
  }
  if (hasNonBmp && total > 0 && printableAscii / total < 0.3) {
    return true;
  }

  return false;
}

/**
 * 获取任务的显示消息。
 *
 * 优先级：
 * 1. task.error_code 存在且 i18n 有对应 error_code_${code} 翻译 → 返回翻译
 * 2. task.error_code 存在但 i18n 无翻译 → 返回 task.message（兜底，可能含历史中文）
 * 3. task.errors[0]?.error_code 存在 → 按规则 1-2 处理
 * 4. task.message 是有效 UTF-8 中文 → 返回 task.message
 * 5. task.message 疑似乱码 → 返回 t('error_code_UNKNOWN_ERROR')
 * 6. 全部为空 → 返回空字符串
 *
 * @param task 任务对象（可为 null/undefined）
 * @param t i18n 翻译函数（传入 key，返回翻译或 key 本身表示缺失）
 */
export function getTaskErrorMessage(
  task: TaskErrorMessageInput | null | undefined,
  t: (key: string) => string,
): string {
  if (task == null) {
    return "";
  }

  const message = task.message ?? "";
  const errorCode = task.error_code;

  // 规则 1-2：顶层 error_code 存在
  if (errorCode) {
    const translation = t(`error_code_${errorCode}`);
    // t() 在 key 缺失时返回 key 本身
    if (translation && translation !== `error_code_${errorCode}`) {
      return translation;
    }
    // error_code 存在但无翻译 → 回退到 message
    if (message) {
      return message;
    }
    // 既无翻译也无 message → 返回 UNKNOWN_ERROR 翻译
    return t("error_code_UNKNOWN_ERROR");
  }

  // 规则 3：errors[0].error_code 存在（顶层缺失时回退到 errors 数组）
  const errors = task.errors;
  if (errors && errors.length > 0) {
    const firstError = errors[0];
    if (firstError && typeof firstError === "object") {
      const nestedErrorCode = firstError.error_code;
      if (typeof nestedErrorCode === "string" && nestedErrorCode) {
        const translation = t(`error_code_${nestedErrorCode}`);
        if (translation && translation !== `error_code_${nestedErrorCode}`) {
          return translation;
        }
        // nested error_code 存在但无翻译 → 回退到 message
        if (message) {
          return message;
        }
        return t("error_code_UNKNOWN_ERROR");
      }
    }
  }

  // 规则 4-5：无 error_code，检查 message
  if (message) {
    if (isLikelyMojibake(message)) {
      // 规则 5：message 疑似乱码 → 回退到 UNKNOWN_ERROR 翻译
      return t("error_code_UNKNOWN_ERROR");
    }
    // 规则 4：message 是有效 UTF-8 → 原样返回
    return message;
  }

  // 规则 6：全部为空
  return "";
}
