"""Dump OpenAPI schema, monkey-patching pydantic so that ForwardRef('date')
evaluates against datetime.date (instead of being left dangling due to
`from __future__ import annotations` inside route modules).
"""
import datetime as _dt
import json
import typing as _t
from pathlib import Path

import pydantic as _p
from pydantic import TypeAdapter as _TA

_ta_orig_init = _TA.__init__

_DEFAULT_NS: dict[str, type] = {
    "date": _dt.date,
    "datetime": _dt.datetime,
    "time": _dt.time,
    "timedelta": _dt.timedelta,
}


def _ta_new_init(self, type, /, *args, **kwargs):
    # 如果用户已显式传 types_namespace，优先用它；否则 merge 默认
    user_ns = kwargs.pop("types_namespace", None) or {}
    merged = {**_DEFAULT_NS, **user_ns}
    kwargs["types_namespace"] = merged
    try:
        _ta_orig_init(self, type, *args, **kwargs)
    except TypeError:
        # 老版本签名没有 types_namespace
        if "types_namespace" in kwargs:
            kwargs.pop("types_namespace")
        _ta_orig_init(self, type, *args, **kwargs)


_TA.__init__ = _ta_new_init  # type: ignore[assignment]

# BaseModel .model_rebuild 也用默认 ns
_orig_rebuild = _p.BaseModel.model_rebuild


def _patched_rebuild(cls, *a, **kw):
    kw.setdefault("_types_namespace", dict(_DEFAULT_NS))
    try:
        return _orig_rebuild.__func__(cls, *a, **kw)
    except Exception:
        return None


_p.BaseModel.model_rebuild = classmethod(_patched_rebuild)  # type: ignore[assignment]

from app.main import app

schema = app.openapi()
out = Path("openapi_schema.json")
out.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Wrote {out.resolve()}")
print(f"  info.title    = {schema['info']['title']}")
print(f"  info.version  = {schema['info'].get('version')}")
paths = schema.get("paths", {})
print(f"  paths.count   = {len(paths)}")
print(f"  schemas.count = {len(schema.get('components', {}).get('schemas', {}))}")
print()
print("G3/G4 governance paths:")
for p in sorted(paths.keys()):
    lp = p.lower()
    if ("portfolio" in lp and (
        "audit" in lp or "status" in lp or "reconcile" in lp
        or "preflight" in lp or "auto-simulation" in lp
        or "transition-state" in lp
    )) or "/audit-events" in p:
        methods = ",".join(sorted(paths[p].keys())).upper()
        print(f"  [{methods:^6}] {p}")
