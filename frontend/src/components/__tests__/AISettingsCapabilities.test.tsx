import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ getAIProfiles: vi.fn(), getAIHealth: vi.fn() }));
vi.mock("../../api/client", () => ({ api: mocks }));
vi.mock("../../i18n", () => ({ t: (key: string) => key }));
import AISettings from "../ai/AISettings";
import UnsupportedFeaturesToggle from "../settings/UnsupportedFeaturesToggle";

beforeEach(() => {
  localStorage.clear();
  mocks.getAIProfiles.mockResolvedValue([]);
  mocks.getAIHealth.mockResolvedValue([]);
});

it("hides unsupported choices by default and shows them disabled through the shared toggle", async () => {
  const user = userEvent.setup();
  render(<><UnsupportedFeaturesToggle /><AISettings embedded /></>);
  await user.click(await screen.findByRole("button", { name: /aiSettings.addProfile$/ }));
  await user.click(screen.getByLabelText("aiSettings.provider"));
  expect(screen.queryByText("Anthropic")).not.toBeInTheDocument();
  expect(screen.queryByText("Azure")).not.toBeInTheDocument();
  await user.keyboard("{Escape}");
  await user.click(screen.getByLabelText("aiSettings.purpose"));
  expect(screen.queryByText("aiPurposeDraft")).not.toBeInTheDocument();
  expect(screen.getByText("aiProfilePurposeMining")).toBeInTheDocument();
  await user.keyboard("{Escape}");
  await user.click(screen.getByRole("button", { name: "cancel" }));
  await user.click(screen.getByRole("switch", { name: "settingsShowUnsupported" }));
  await user.click(await screen.findByRole("button", { name: /aiSettings.addProfile$/ }));
  await user.click(screen.getByLabelText("aiSettings.provider"));
  expect(screen.getByText("Anthropic").closest(".ant-select-item-option")).toHaveClass("ant-select-item-option-disabled");
  await user.keyboard("{Escape}");
  await user.click(screen.getByLabelText("aiSettings.purpose"));
  expect(screen.getByText("aiProfilePurposeMining")).toBeInTheDocument();
  expect(screen.getByText("aiPurposeDraft").closest(".ant-select-item-option")).toHaveClass("ant-select-item-option-disabled");
});

it("keeps an existing unsupported selection visible with its reason while the toggle is off", async () => {
  mocks.getAIProfiles.mockResolvedValue([{ id: 1, name: "legacy-native", provider: "anthropic", purpose: "draft", model: "legacy", is_enabled: false, health_status: "unknown", priority: 0 }]);
  const user = userEvent.setup();
  render(<AISettings embedded />);
  await user.click(await screen.findByRole("button", { name: "edit" }));
  const dialog = screen.getByRole("dialog");
  expect(await within(dialog).findByText("Anthropic")).toBeInTheDocument();
  expect(within(dialog).getByText("aiPurposeDraft")).toBeInTheDocument();
  expect(await within(dialog).findByText("settingsUnsupportedReason")).toBeInTheDocument();
});
