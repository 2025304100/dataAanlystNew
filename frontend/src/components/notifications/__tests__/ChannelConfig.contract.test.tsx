import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../../../api/client", () => ({ requestJson: request }));
vi.mock("../../../i18n", () => ({ t: (key: string) => key }));
import { ChannelConfig } from "../ChannelConfig";

beforeEach(() => { request.mockReset(); request.mockResolvedValue([]); });
afterEach(cleanup);

it("creates a WxPusher channel using typed recipient arrays accepted by the backend", async () => {
  const user = userEvent.setup();
  render(<ChannelConfig />);
  await user.click(await screen.findByText("addChannel"));
  await user.type(screen.getByLabelText("channelColumnName"), "typed recipients");
  await user.click(screen.getByLabelText("channelColumnType"));
  await user.click(await screen.findByText("channelTypeWxPusher", { selector: ".ant-select-item-option-content" }));
  await user.type(screen.getByLabelText("channelConfigAppToken"), "FAKE_TOKEN");
  await user.type(screen.getByLabelText("channelConfigUid"), "UID_audit{Enter}");
  await user.click(screen.getByText("create", { selector: "button span" }));
  await waitFor(() => {
    const submission = request.mock.calls.find(([, options]) => options?.method === "POST");
    expect(submission).toBeTruthy();
    expect(JSON.parse(submission![1].body).config).toMatchObject({ app_token: "FAKE_TOKEN", uids: ["UID_audit"] });
  });
});

it("uses a single SMTP transport choice and submits STARTTLS with recipient arrays", async () => {
  const user = userEvent.setup();
  render(<ChannelConfig />);
  await user.click(await screen.findByText("addChannel"));
  await user.type(screen.getByLabelText("channelColumnName"), "mail");
  await user.click(screen.getByLabelText("channelColumnType"));
  await user.click(await screen.findByText("channelTypeEmail", { selector: ".ant-select-item-option-content" }));
  await user.click(screen.getByLabelText("settingsNotificationTransport"));
  await user.click(await screen.findByText("STARTTLS", { selector: ".ant-select-item-option-content" }));
  await user.type(screen.getByLabelText("channelConfigSmtpHost"), "smtp.example.invalid");
  await user.type(screen.getByLabelText("channelConfigUsername"), "sender");
  await user.type(screen.getByLabelText("channelConfigPassword"), "FAKE_PASSWORD");
  await user.type(screen.getByLabelText("channelConfigFromAddr"), "sender@example.invalid");
  await user.type(screen.getByLabelText("channelConfigToAddrs"), "receiver@example.invalid{Enter}");
  await user.click(screen.getByText("create", { selector: "button span" }));
  await waitFor(() => {
    const submission = request.mock.calls.find(([, options]) => options?.method === "POST");
    expect(JSON.parse(submission![1].body).config).toMatchObject({ use_ssl: false, use_tls: true, to_addrs: ["receiver@example.invalid"] });
  });
});
