const fs = require('fs');

function patchFile(path, transforms) {
  let text = fs.readFileSync(path, 'utf8');
  for (const [from, to] of transforms) {
    if (!text.includes(from)) {
      throw new Error(`Pattern not found in ${path}: ${from.slice(0, 80)}`);
    }
    text = text.replace(from, to);
  }
  fs.writeFileSync(path, text, 'utf8');
}

patchFile('frontend/src/components/HistoryInitSection.tsx', [
  [
    '  const [failedFilterStage, setFailedFilterStage] = useState<"all" | "sync_bars" | "calc_scores">("all");\r\n',
    '  const [failedFilterStage, setFailedFilterStage] = useState<"all" | "sync_bars" | "calc_scores">("all");\r\n  const [failedCompareScope, setFailedCompareScope] = useState<"all" | "new_only">("all");\r\n'
  ],
  [
    '  const renderRunDetails = (run: HistoryInitializationRunRecord) => {\r\n',
    '  const failureItemCompareKey = (item: HistoryInitializationFailureItem) => `${item.symbol_id}:${item.stage}`;\r\n\r\n  const renderRunDetails = (run: HistoryInitializationRunRecord, runIndex: number) => {\r\n'
  ],
  [
    '    const stages = run.stages;\r\n    const failedItems = run.failed_items ?? [];\r\n    const syncFailedItems = failedItems.filter((item) => item.stage === "sync_bars");\r\n    const scoreFailedItems = failedItems.filter((item) => item.stage === "calc_scores");\r\n    const normalizedKeyword = failedFilterKeyword.trim().toLowerCase();\r\n    const filteredItems = failedItems.filter((item) => {\r\n      const stageMatched = failedFilterStage === "all" || item.stage === failedFilterStage;\r\n      if (!stageMatched) return false;\r\n      if (!normalizedKeyword) return true;\r\n      return [item.symbol, item.name ?? "", item.message]\r\n        .join(" ")\r\n        .toLowerCase()\r\n        .includes(normalizedKeyword);\r\n    });\r\n    const filteredSyncFailedItems = filteredItems.filter((item) => item.stage === "sync_bars");\r\n    const filteredScoreFailedItems = filteredItems.filter((item) => item.stage === "calc_scores");\r\n    const hasFailureFilter = failedFilterStage !== "all" || normalizedKeyword.length > 0;\r\n    const retryable = failedItems.length > 0 && !isRunning;\r\n',
    '    const stages = run.stages;\r\n    const failedItems = run.failed_items ?? [];\r\n    const previousRun = recentRuns[runIndex + 1] ?? null;\r\n    const previousFailureKeys = new Set((previousRun?.failed_items ?? []).map(failureItemCompareKey));\r\n    const newFailedItems = failedItems.filter((item) => !previousFailureKeys.has(failureItemCompareKey(item)));\r\n    const comparedFailedItems = failedCompareScope === "new_only" && previousRun ? newFailedItems : failedItems;\r\n    const syncFailedItems = failedItems.filter((item) => item.stage === "sync_bars");\r\n    const scoreFailedItems = failedItems.filter((item) => item.stage === "calc_scores");\r\n    const comparedSyncFailedItems = comparedFailedItems.filter((item) => item.stage === "sync_bars");\r\n    const comparedScoreFailedItems = comparedFailedItems.filter((item) => item.stage === "calc_scores");\r\n    const normalizedKeyword = failedFilterKeyword.trim().toLowerCase();\r\n    const filteredItems = comparedFailedItems.filter((item) => {\r\n      const stageMatched = failedFilterStage === "all" || item.stage === failedFilterStage;\r\n      if (!stageMatched) return false;\r\n      if (!normalizedKeyword) return true;\r\n      return [item.symbol, item.name ?? "", item.message]\r\n        .join(" ")\r\n        .toLowerCase()\r\n        .includes(normalizedKeyword);\r\n    });\r\n    const filteredSyncFailedItems = filteredItems.filter((item) => item.stage === "sync_bars");\r\n    const filteredScoreFailedItems = filteredItems.filter((item) => item.stage === "calc_scores");\r\n    const hasFailureFilter = failedFilterStage !== "all" || normalizedKeyword.length > 0 || failedCompareScope !== "all";\r\n    const retryable = failedItems.length > 0 && !isRunning;\r\n'
  ],
  [
    '                  <Tag>{failedItems.length}</Tag>\r\n                  <Tag color="orange">{`${t("histStageSyncBars")} ${syncFailedItems.length}`}</Tag>\r\n                  <Tag color="purple">{`${t("histStageCalcScores")} ${scoreFailedItems.length}`}</Tag>\r\n',
    '                  <Tag>{failedItems.length}</Tag>\r\n                  <Tag color="orange">{`${t("histStageSyncBars")} ${syncFailedItems.length}`}</Tag>\r\n                  <Tag color="purple">{`${t("histStageCalcScores")} ${scoreFailedItems.length}`}</Tag>\r\n                  {previousRun && <Tag color="volcano">{template("histFailedNewCount", { count: newFailedItems.length })}</Tag>}\r\n'
  ],
  [
    '                    <Radio.Group\r\n                      size="small"\r\n                      optionType="button"\r\n                      buttonStyle="solid"\r\n                      value={failedFilterStage}\r\n                      onChange={(e) => setFailedFilterStage(e.target.value)}\r\n                    >\r\n                      <Radio.Button value="all">{t("histFailedFilterAll")}</Radio.Button>\r\n                      <Radio.Button value="sync_bars">{t("histFailedFilterSync")}</Radio.Button>\r\n                      <Radio.Button value="calc_scores">{t("histFailedFilterScore")}</Radio.Button>\r\n                    </Radio.Group>\r\n',
    '                    <Radio.Group\r\n                      size="small"\r\n                      optionType="button"\r\n                      buttonStyle="solid"\r\n                      value={failedCompareScope}\r\n                      onChange={(e) => setFailedCompareScope(e.target.value)}\r\n                    >\r\n                      <Radio.Button value="all">{t("histFailedCompareAll")}</Radio.Button>\r\n                      <Radio.Button value="new_only">{t("histFailedCompareNew")}</Radio.Button>\r\n                    </Radio.Group>\r\n                    <Radio.Group\r\n                      size="small"\r\n                      optionType="button"\r\n                      buttonStyle="solid"\r\n                      value={failedFilterStage}\r\n                      onChange={(e) => setFailedFilterStage(e.target.value)}\r\n                    >\r\n                      <Radio.Button value="all">{t("histFailedFilterAll")}</Radio.Button>\r\n                      <Radio.Button value="sync_bars">{t("histFailedFilterSync")}</Radio.Button>\r\n                      <Radio.Button value="calc_scores">{t("histFailedFilterScore")}</Radio.Button>\r\n                    </Radio.Group>\r\n'
  ],
  [
    '                          <Tag color="orange">{filteredSyncFailedItems.length}</Tag>\r\n',
    '                          <Tag color="orange">{filteredSyncFailedItems.length}</Tag>\r\n                          {failedCompareScope === "new_only" && previousRun && <Tag>{comparedSyncFailedItems.length}</Tag>}\r\n'
  ],
  [
    '                          <Tag color="purple">{filteredScoreFailedItems.length}</Tag>\r\n',
    '                          <Tag color="purple">{filteredScoreFailedItems.length}</Tag>\r\n                          {failedCompareScope === "new_only" && previousRun && <Tag>{comparedScoreFailedItems.length}</Tag>}\r\n'
  ],
  [
    '            {recentRuns.map((run) => renderRunDetails(run))}\r\n',
    '            {recentRuns.map((run, index) => renderRunDetails(run, index))}\r\n'
  ]
]);

patchFile('frontend/src/i18n/index.ts', [
  [
    '    histExportFailed: "导出失败列表",\r\n',
    '    histExportFailed: "导出失败列表",\r\n    histFailedSearchPlaceholder: "搜索代码 / 名称 / 原因",\r\n    histFailedFilterAll: "全部阶段",\r\n    histFailedFilterSync: "仅补K线",\r\n    histFailedFilterScore: "仅算评分",\r\n    histFailedCompareAll: "全部失败",\r\n    histFailedCompareNew: "只看本次新增失败",\r\n    histFailedNewCount: "新增 {count}",\r\n    histNoFilteredFailedItems: "当前筛选下没有失败项",\r\n'
  ],
  [
    '    histExportFailed: "Export Failed List",\r\n',
    '    histExportFailed: "Export Failed List",\r\n    histFailedSearchPlaceholder: "Search symbol / name / reason",\r\n    histFailedFilterAll: "All stages",\r\n    histFailedFilterSync: "Sync bars only",\r\n    histFailedFilterScore: "Score calc only",\r\n    histFailedCompareAll: "All failures",\r\n    histFailedCompareNew: "New in this run only",\r\n    histFailedNewCount: "New {count}",\r\n    histNoFilteredFailedItems: "No failed items match the current filter",\r\n'
  ]
]);
