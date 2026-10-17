import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { setLocale } from "../../../../../i18n";
import PoolMetricHelp, { metricDisplayLabel } from "./PoolMetricHelp";

afterEach(() => setLocale("zh-CN"));

describe("pool metric guidance", () => {
  it("supports keyboard help without activating the surrounding filter", async () => {
    const outer = vi.fn();
    const page = render(<div onClick={outer}><PoolMetricHelp metric="total_market_cap" /></div>);
    const button = page.getByRole("button", { name: "了解总市值（元）" });
    act(() => button.focus());
    await waitFor(() => expect(button.getAttribute("aria-expanded")).toBe("true"));
    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    await waitFor(() => expect(page.getByText(/100 亿元输入 10,000,000,000/)).toBeTruthy());
    expect(page.getByText(/上下界均包含端点/)).toBeTruthy();
    expect(button.getAttribute("aria-describedby")).toBeTruthy();
    expect(outer).not.toHaveBeenCalled();
    fireEvent.keyDown(button, { key: "Escape" });
    await waitFor(() => expect(button.getAttribute("aria-expanded")).toBe("false"));
  });

  it("uses actual screening semantics and translates known metrics", async () => {
    setLocale("en-US");
    const page = render(<PoolMetricHelp metric="pe_ttm" />);
    fireEvent.focus(page.getByRole("button", { name: "Learn about Price-to-earnings PE (multiple)" }));
    await waitFor(() => expect(page.getByText(/does not automatically exclude non-positive values/)).toBeTruthy());
    expect(metricDisplayLabel("roe_ttm")).toBe("Latest announced ROE (%)");
    expect(metricDisplayLabel("custom_metric", "User metric")).toBe("User metric");
  });
});
