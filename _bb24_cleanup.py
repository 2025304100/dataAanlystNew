# -*- coding: utf-8 -*-
"""黑盒收尾：清理 BB23/BB24 测试数据（池/快照/模板/草稿）。"""
import requests

BASE = "http://127.0.0.1:8000/api/v1"

pools = requests.get(f"{BASE}/factor-mining/candidate-pools",
                     params={"page_size": 100}, timeout=30).json().get("items", [])
for p in pools:
    name = str(p.get("name", ""))
    if name.startswith(("BB23", "BB24", "因子挖掘候选池")):
        pid = p.get("id") or p.get("pool_id")
        requests.delete(f"{BASE}/factor-mining/candidate-pools/{pid}/snapshot/latest", timeout=30)
        rd = requests.delete(f"{BASE}/factor-mining/candidate-pools/{pid}", timeout=30)
        if rd.status_code >= 400:
            ru = requests.patch(f"{BASE}/factor-mining/candidate-pools/{pid}",
                                json={"status": "archived"}, timeout=30)
            print(pid, name, "DELETE", rd.status_code, "PATCH", ru.status_code)
        else:
            print(pid, name, "deleted")

tls = requests.get(f"{BASE}/factor-mining/templates", params={"limit": 300}, timeout=30).json()
items = tls if isinstance(tls, list) else tls.get("items", [])
for t in items:
    if str(t.get("name", "")).startswith("BB23"):
        tid = t.get("template_id") or t.get("id")
        rt = requests.delete(f"{BASE}/factor-mining/templates/{tid}", timeout=30)
        print("tpl", t.get("name"), rt.status_code)

drs = requests.get(f"{BASE}/factor-mining/drafts", timeout=30).json()
dlist = drs if isinstance(drs, list) else drs.get("items", [])
for d in dlist:
    if str(d.get("name", "")).startswith("BB23"):
        did = d.get("draft_id") or d.get("id")
        rd = requests.delete(f"{BASE}/factor-mining/drafts/{did}", timeout=30)
        print("draft", did, rd.status_code)
print("cleanup done")
