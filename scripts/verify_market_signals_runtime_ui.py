"""Validate the deployed signal UI with real API rule writes only.

Requires APP_VALIDATION_MODE=1 and global signal processing disabled. Creates
two clearly labelled shadow rules and pauses them; never analyzes or sends.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    origin = args.origin.rstrip("/")
    if urlsplit(origin).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Local acceptance only")
    folder = args.evidence_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if (folder/"ui-result.json").exists():
        parser.error("Use a new evidence directory")
    opener = build_opener(ProxyHandler({}))
    checks = []
    for path in ("/health", "/api/v1/market-events?limit=1", "/api/v1/market-signals/settings",
                 "/api/v1/market-signals/rules", "/api/v1/market-signals/metadata",
                 "/api/v1/market-signals/evidence?limit=1", "/api/v1/market-signals/assessments",
                 "/api/v1/market-signals/predictions", "/api/v1/market-signals/dashboard"):
        with opener.open(origin+path, timeout=30) as response:
            body = json.load(response)
            checks.append({"path":path, "status":response.status,
                           "validation_mode":response.headers.get("X-App-Validation-Mode")})
            assert response.status==200 and response.headers.get("X-App-Validation-Mode")=="1", checks[-1]
            if path.endswith("/settings"):
                assert not body["enabled"] and body["ai_profile_id"] is None
    (folder/"http-checks.json").write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding="utf-8")

    report = {"passed":False, "started_at":datetime.now(timezone.utc).isoformat()}
    responses, errors, blocked, writes = [], [], [], []
    created = []
    attempted_names = set()
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width":1512,"height":982},locale="zh-CN",timezone_id="Asia/Shanghai")
        def restrict(route):
            request = route.request
            path = urlsplit(request.url).path
            read = request.method in {"GET","HEAD","OPTIONS"}
            rule_write = ((request.method=="POST" and path=="/api/v1/market-signals/rules") or
                (request.method=="PUT" and re.fullmatch(r"/api/v1/market-signals/rules/[0-9]+",path)))
            if urlsplit(request.url).netloc != urlsplit(origin).netloc or not (read or rule_write):
                blocked.append({"url":request.url,"method":request.method})
                route.abort()
            else:
                if not read:
                    writes.append({"path":path,"method":request.method})
                route.continue_()
        context.route("**/*",restrict)
        context.tracing.start(screenshots=True,snapshots=True,sources=True)
        page = context.new_page()
        page.set_default_timeout(30000)
        page.on("pageerror",lambda error:errors.append(str(error)))
        page.on("response",lambda response:responses.append({"path":urlsplit(response.url).path,
            "status":response.status,"method":response.request.method}) if "/api/" in response.url else None)

        def read(path):
            response = page.request.get(origin+"/api/v1/market-signals"+path)
            assert response.status==200, response.text()
            return response.json()

        def center():
            page.goto(origin+"/?tab=news",wait_until="domcontentloaded")
            page.get_by_role("button",name="行情异动提醒",exact=True).click()
            expect(page.locator(".msc-page")).to_be_visible()
            expect(page.locator(".msc-page .ant-spin-spinning")).to_have_count(0)
            page.get_by_role("tab",name="关注设置",exact=True).click()

        def row(name):
            return page.get_by_role("row").filter(has=page.get_by_role("cell",name=re.compile("^"+re.escape(name))))

        try:
            center()
            page.screenshot(path=str(folder/"entry.png"))
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            for sector in (False, True):
                name = f"验收专用-{'半导体' if sector else '股票'}-{stamp}"
                attempted_names.add(name)
                page.get_by_role("button",name="新增关注",exact=True).click()
                dialog = page.get_by_role("dialog",name="新增关注",exact=True)
                dialog.get_by_label("规则名称",exact=True).fill(name)
                if sector:
                    dialog.get_by_label("关注类型",exact=True).select_option("sector")
                    dialog.get_by_label("关注板块",exact=True).select_option("semiconductor")
                    dialog.get_by_label("验证行情类型",exact=True).select_option("index")
                    dialog.get_by_label("验证行情代码",exact=True).fill("000300")
                else:
                    dialog.get_by_label("搜索股票代码或名称",exact=True).fill("000001")
                    expect(dialog.get_by_label("关注股票",exact=True).locator('option[value="000001"]')).to_have_count(1)
                    dialog.get_by_label("关注股票",exact=True).select_option("000001")
                dialog.get_by_label("最低评分",exact=True).fill("97" if sector else "99")
                dialog.get_by_label("观察窗口",exact=True).select_option("3" if sector else "1")
                dialog.get_by_label("最低波动幅度（%）",exact=True).fill("4" if sector else "3")
                dialog.get_by_label("按历史波动提高门槛",exact=True).uncheck()
                dialog.get_by_label("运行模式",exact=True).select_option("shadow")
                dialog.get_by_role("button",name="保存关注",exact=True).click()
                expect(dialog).not_to_be_visible()
                row(name).get_by_role("button",name=f"暂停 {name}",exact=True).click()
                expect(row(name).get_by_role("button",name=f"启用 {name}",exact=True)).to_be_visible()
                rule = next(value for value in read("/rules") if value["name"]==name)
                created.append({"id":rule["id"],"name":name})
                assert not rule["enabled"] and rule["mode"]=="shadow" and rule["revision"]==2
                if not sector:
                    row(name).get_by_role("button",name=f"编辑 {name}",exact=True).click()
                    editing = page.get_by_role("dialog",name="新增关注",exact=True)
                    editing.get_by_label("最低评分",exact=True).fill("98")
                    editing.get_by_label("观察窗口",exact=True).select_option("5")
                    editing.get_by_role("button",name="保存关注",exact=True).click()
                    expect(editing).not_to_be_visible()
            center()  # A fresh page fetch must load persisted, independent settings.
            rules = read("/rules")
            saved = [next(value for value in rules if value["id"]==item["id"]) for item in created]
            stock,sector = saved
            assert (stock["min_score"],stock["window_days"],stock["min_move_pct"],stock["revision"])==(98,5,3,3)
            assert (sector["min_score"],sector["window_days"],sector["min_move_pct"],sector["revision"])==(97,3,4,2)
            versions = {}
            for rule in saved:
                assert not rule["enabled"] and rule["mode"]=="shadow"
                expect(row(rule["name"])).to_contain_text(str(rule["min_score"]))
                expect(row(rule["name"])).to_contain_text(f'{rule["window_days"]} 个交易日')
                expect(row(rule["name"]).get_by_role("button",name=f'启用 {rule["name"]}',exact=True)).to_be_visible()
                versions[str(rule["id"])] = read(f'/rules/{rule["id"]}/versions')
                assert len(versions[str(rule["id"])])==rule["revision"]
            page.screenshot(path=str(folder/"rules-persisted.png"))
            row(stock["name"]).get_by_role("button",name="查看版本",exact=True).click()
            expect(page.get_by_role("dialog",name=stock["name"]+" · 版本",exact=True)).to_be_visible()
            page.screenshot(path=str(folder/"rule-versions.png"))
            assert not errors and not blocked, {"errors":errors,"blocked":blocked}
            assert not [value for value in responses if value["status"]>=400], responses
            assert not read("/settings")["enabled"]
            report.update(passed=True,rules=saved,versions=versions,real_model_calls=0,notifications_sent=0)
        except Exception as exc:
            report["error"] = str(exc)
            # Preserve acceptance records but leave every successfully created one paused.
            for rule in read("/rules"):
                if rule["name"] in attempted_names and rule["enabled"]:
                    response = page.request.put(origin+f'/api/v1/market-signals/rules/{rule["id"]}',data={"enabled":False})
                    assert response.status==200
            page.screenshot(path=str(folder/"failure.png"))
        finally:
            report.update(finished_at=datetime.now(timezone.utc).isoformat(),responses=responses,page_errors=errors,
                          blocked_requests=blocked,rule_writes=writes,created_rules=created)
            (folder/"ui-result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            context.tracing.stop(path=str(folder/"ui-trace.zip"))
            context.close()
            browser.close()
    print(json.dumps({key:report[key] for key in ("passed","created_rules","error") if key in report},ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__=="__main__":
    raise SystemExit(main())
