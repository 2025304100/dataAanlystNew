"""Navigate by the current visible UI and wait for render readiness."""
import re
from playwright.sync_api import expect


def click_named(scope, labels, *, role="button"):
    locator = scope.get_by_role(role, name=re.compile(r"(?:^|\s)(?:" + "|".join(re.escape(label) for label in labels) + ")$")).first
    expect(locator).to_be_visible(timeout=20000)
    locator.click()
    return locator
