const fs = require("fs");
const P = "src/components/portfolio-trading/__tests__/PortfolioBacktestCenter.test.tsx";
let s = "";
const w = (t) => { s += t + "\n"; };

w(`import { describe, expect, it, vi, beforeEach } from "vitest";`);
w(`import { fireEvent, render, screen, waitFor, act } from "@testing-library/react";`);
w(``);
w(`const { mockApi, mockToast } = vi.hoisted(() => ({`);
w(`  mockApi: {`);
w(`    getPortfolioBacktestSourceStatus: vi.fn(),`);
w(`    getBacktestRuns: vi.fn(),`);
w(`    runPortfolioBacktest: vi.fn(),`);
w(`    getBacktestRun: vi.fn(),`);
w(`    getBacktestTrades: vi.fn(),`);
w(`    getBacktestEvidence: vi.fn(),`);
w(`    getBacktestPositions: vi.fn(),`);
w(`    getCurrentFactorUsage: vi.fn(),`);
w(`    evaluatePortfolioDecision: vi.fn(),`);
w(`    postBacktestPrecheck: vi.fn(),`);
w(`  },`);
w(`  mockToast: vi.fn(),`);
w(`}));`);
w(``);
w(`vi.mock("../../../api/client", () => ({ api: mockApi }));`);
w(`vi.mock("../../../context/AppContext", () => ({`);
w(`  useApp: () => ({ showToast: mockToast, setActiveTab: vi.fn() }),`);
w(`}));`);
w(`vi.mock("../../../i18n", () => ({ t: (key) => key }));`);
w(``);
// antd mock
w(`vi.mock("antd", () => {`);
w(`  const React = require("react");`);
w(`  return {`);
w(`    DatePicker: { RangePicker: (p) => React.createElement("div", { "data-testid": p["data-testid"], "data-value": JSON.stringify(p.value ?? null) }) },`);
w(`    App: { useApp: () => ({ message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }) },`);
w(`    Modal: { warning: vi.fn() },`);
w(`    Space: (p) => React.createElement(React.Fragment, null, p.children),`);
w(`    Tag: (p) => React.createElement("span", { "data-antd-tag": p.color ?? "default" }, p.children),`);
w(`    Tooltip: (p) => React.createElement("span", { "data-antd-tooltip": typeof p.title === "string" ? p.title : undefined }, p.children),`);
w(`    Table: (p) => React.createElement("table", { "data-testid": "antd-table" },`);
w(`      React.createElement("tbody", null, (p.dataSource ?? []).map((r, i) => React.createElement("tr", { key: i, "data-row": JSON.stringify(r) })))),`);
w(`    Button: (p) => React.createElement("button", { type: "button", "data-testid": p["data-testid"], disabled: p.disabled, onClick: p.onClick }, p.children),`);
w(`  };`);
w(`});`);
w(``);
w(`vi.mock("../DecisionEvidenceDrawer", () => {`);
w(`  const React = require("react");`);
w(`  return { default: (p) => p.open ? React.createElement("div", { "data-testid": "decision-evidence-drawer" },`);
w(`    React.createElement("span", null, p.decisionRunId ?? ""),`);
w(`    React.createElement("span", { "data-testid": "selected-evidence-id" }, p.selectedEvidenceId ?? "")) : null };`);
w(`});`);
w(``);
w(`import PortfolioBacktestCenter from "../PortfolioBacktestCenter";`);

fs.writeFileSync("build_test_writer_step1.mjs",
  `import fs from "fs"; const lines = [\n${JSON.stringify(s.split("\n"))}\n].flat(); fs.writeFileSync(process.argv[1], lines.join("\\n"));`,
  "utf8");
console.log("step1 done, str length:", s.length);
