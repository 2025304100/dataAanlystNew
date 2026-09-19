"""Browser acceptance test for the mobile backtest layout.

The public interaction contract is the visible navigation path:
``组合交易`` -> ``回测中心`` -> either backtest mode.  Layout is verified in
an actual Chromium viewport because JSDOM cannot calculate scroll geometry.
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.e2e

MOBILE_VIEWPORT = {"width": 390, "height": 844}


def _open_backtest_center(page) -> None:
    page.set_viewport_size(MOBILE_VIEWPORT)
    page.get_by_role("button", name="组合交易", exact=True).click()
    page.get_by_role("button", name="回测中心", exact=True).click()
    page.get_by_role("button", name="组合回测", exact=True).wait_for()


def _assert_no_page_or_main_horizontal_overflow(page, mode: str) -> None:
    metrics = page.evaluate(
        """() => {
            const root = document.documentElement;
            const main = document.querySelector("main");
            return {
                documentClientWidth: root.clientWidth,
                documentScrollWidth: root.scrollWidth,
                mainClientWidth: main?.clientWidth ?? 0,
                mainScrollWidth: main?.scrollWidth ?? 0,
            };
        }"""
    )
    assert metrics["documentScrollWidth"] <= metrics["documentClientWidth"] + 1, (
        f"{mode} makes the document horizontally scrollable: {metrics}"
    )
    assert metrics["mainScrollWidth"] <= metrics["mainClientWidth"] + 1, (
        f"{mode} makes main horizontally scrollable: {metrics}"
    )


def test_backtest_center_has_no_mobile_horizontal_overflow(page):
    """The portfolio and single-symbol backtest views fit a 390x844 viewport."""
    _open_backtest_center(page)
    _assert_no_page_or_main_horizontal_overflow(page, "组合回测")

    page.get_by_role("button", name="单标的回测", exact=True).click()
    page.get_by_role("heading", name="单标的参数", exact=True).wait_for()
    _assert_no_page_or_main_horizontal_overflow(page, "单标的回测")
