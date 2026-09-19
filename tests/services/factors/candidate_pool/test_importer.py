"""T10 · 外部导入 + 逐行匹配 的契约测试。

测试策略（与 T09 一致）
======================
- 解析 / 匹配 / 模板 / 错误明细 都是**纯逻辑**（不碰数仓），用内存字节串覆盖。
- 写库路径用 `db_session`（SQLite）并预置 `Symbol` 主数据行。
- HTTP 层用最小 FastAPI app + 覆盖 `get_db`。
- **不依赖真实 MySQL / 6.2GB DuckDB**，毫秒级确定性。
"""
from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.symbol import Symbol
from app.schemas.errors import FactorSevenError
from app.services.factors.candidate_pool import importer as I
from app.services.factors.candidate_pool import service as pool_service


# ══════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════

#: 预置主数据：`code -> (name, market, asset_type, is_active)`
SEED: dict[str, tuple[str, str, str, int]] = {
    "000001": ("平安银行", "sz", "stock", 1),
    "600519": ("贵州茅台", "sh", "stock", 1),
    "000002": ("万  科Ａ", "sz", "stock", 1),
    "300001": ("特锐德", "sz", "stock", 1),
    "688001": ("华兴源创", "sh", "stock", 1),
    "920001": ("北交所样本", "bj", "stock", 1),
    "159919": ("沪深300ETF", "sz", "etf", 1),     # 非股票
    "600193": ("退市创兴", "sh", "stock", 0),      # is_active=0
}


@pytest.fixture
def seeded(db_session):
    for code, (name, market, asset_type, is_active) in SEED.items():
        db_session.add(Symbol(symbol=code, name=name, asset_type=asset_type,
                              market=market, board="main", is_st=0,
                              is_active=is_active))
    db_session.flush()
    return set(SEED)


def csv_bytes(rows, header=("symbol", "name", "market", "note")):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def xlsx_bytes(rows, header=("symbol", "name", "market", "note")):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _status_of(results, code):
    for r in results:
        if r.symbol_raw == code:
            return r.status
    return None


def _build_client(db_session) -> TestClient:
    from app.api.routes import mining_candidate_pool as route_mod

    app = FastAPI()
    app.include_router(route_mod.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


# ══════════════════════════════════════════════════════════
# 1. 模板
# ══════════════════════════════════════════════════════════


class TestTemplate:
    @pytest.mark.parametrize("fmt,ext,mime_kind", [
        (I.FORMAT_CSV, "csv", "csv"),
        (I.FORMAT_XLSX, "xlsx", "spreadsheetml"),
    ])
    def test_template_generated_and_parseable(self, fmt, ext, mime_kind):
        payload, filename, media_type = I.build_template(fmt)
        assert payload
        assert filename.endswith("." + ext)
        assert mime_kind in media_type
        # 模板本身必须能被本模块的解析器读回（否则用户下下来也不会填）
        rows = I.parse_import_file(payload, filename=filename)
        assert len(rows) == 2
        assert all(r.symbol_raw for r in rows)

    def test_template_contains_required_columns(self):
        payload, filename, _ = I.build_template(I.FORMAT_CSV)
        header = payload.decode("utf-8-sig").splitlines()[0]
        assert "symbol" in header
        for col in I.OPTIONAL_COLUMNS:
            assert col in header

    def test_unsupported_template_format(self):
        with pytest.raises(FactorSevenError):
            I.build_template("pdf")


# ══════════════════════════════════════════════════════════
# 2. 格式判定
# ══════════════════════════════════════════════════════════


class TestDetectFormat:
    @pytest.mark.parametrize("filename,declared,expect", [
        ("a.csv", None, "csv"),
        ("a.xlsx", None, "xlsx"),
        ("a.xls", None, "xls"),
        ("A.XLSX", None, "xlsx"),   # 大小写不敏感
        ("a.csv", "xlsx", "xlsx"),  # 显式声明优先
        ("a.xlsx", "csv", "csv"),
        ("noext", "csv", "csv"),
    ])
    def test_detected(self, filename, declared, expect):
        assert I.detect_format(filename, declared) == expect

    def test_unknown_extension_raises(self):
        with pytest.raises(FactorSevenError) as ei:
            I.detect_format("a.txt", None)
        assert ei.value.error_code == "VALIDATION_ERROR"


# ══════════════════════════════════════════════════════════
# 3. 解析
# ══════════════════════════════════════════════════════════


class TestParse:
    def test_basic_and_blank_rows_skipped(self):
        rows = I.parse_import_file(csv_bytes([
            ["000001", "平安银行", "sz", "x"],
            ["", "", "", ""],
            ["600519", "贵州茅台", "sh", ""],
        ]), filename="a.csv")
        assert len(rows) == 2
        assert [r.symbol_raw for r in rows] == ["000001", "600519"]
        # 行号与 Excel 一致（表头占第 1 行）
        assert rows[0].row_no == 2

    def test_header_case_space_and_underscore_insensitive(self):
        raw = csv_bytes([["000001", "n", "sz", "x"]],
                        header=("Symbol", " Name ", "market_x", "note"))
        rows = I.parse_import_file(raw, filename="a.csv")
        assert len(rows) == 1
        assert rows[0].symbol_raw == "000001"

    def test_symbol_aliases(self):
        """`code` / `代码` / `股票代码` 都当 symbol（用户文件列名很随意）。"""
        for header in (("code",), ("代码",), ("股票代码",)):
            raw = csv_bytes([["000001"]], header=header)
            rows = I.parse_import_file(raw, filename="a.csv")
            assert len(rows) == 1 and rows[0].symbol_raw == "000001"

    def test_extra_columns_are_ignored(self):
        raw = csv_bytes([["000001", "n", "sz", "note", "extra1", "extra2"]],
                        header=("symbol", "name", "market", "note", "pe", "pb"))
        rows = I.parse_import_file(raw, filename="a.csv")
        assert len(rows) == 1 and rows[0].symbol_raw == "000001"

    def test_missing_symbol_column_raises(self):
        raw = csv_bytes([["000001"]], header=("ticker",))
        with pytest.raises(FactorSevenError) as ei:
            I.parse_import_file(raw, filename="a.csv")
        assert ei.value.error_code == "VALIDATION_ERROR"
        assert "symbol" in (ei.value.detail_zh or "")

    def test_empty_file_raises(self):
        with pytest.raises(FactorSevenError):
            I.parse_import_file(b"", filename="a.csv")

    def test_gbk_encoded_csv(self):
        raw = "symbol,name,market,note\n000001,平安银行,sz,测试\n".encode("gbk")
        rows = I.parse_import_file(raw, filename="a.csv")
        assert len(rows) == 1
        assert rows[0].symbol_raw == "000001"
        assert rows[0].name == "平安银行"

    def test_excel_read_as_text_keeps_leading_zeros(self):
        rows = I.parse_import_file(xlsx_bytes([["000001", "平安银行"]]),
                                   filename="a.xlsx")
        assert rows[0].symbol_raw == "000001"

    def test_excel_numeric_cell_becomes_short_string(self):
        """Excel 把 `000001` 存成数字 1 时，我们**不自动补零** —— 会静默改用户意图。
        而是原样读出，交给 `format_error` 给出可操作的提示。"""
        rows = I.parse_import_file(xlsx_bytes([[1, "x"]]), filename="a.xlsx")
        assert rows[0].symbol_raw == "1"

    def test_row_limit(self):
        big = csv_bytes([[f"{i:06d}"] for i in range(I.MAX_IMPORT_ROWS + 1)],
                        header=("symbol",))
        with pytest.raises(FactorSevenError) as ei:
            I.parse_import_file(big, filename="a.csv")
        assert "上限" in (ei.value.detail_zh or "")


# ══════════════════════════════════════════════════════════
# 4. 逐行匹配：六种状态
# ══════════════════════════════════════════════════════════


class TestMatchRows:
    def test_all_six_statuses(self, db_session, seeded):
        raw = csv_bytes([
            ["000001", "平安银行", "sz", ""],      # success
            ["600519", "贵州茅台", "sh", ""],      # success
            ["999999", "不存在", "sz", ""],        # unmatched
            ["000001", "平安银行", "sz", "再来一次"],  # duplicate
            ["1", "前导零丢失", "sz", ""],         # format_error
            ["159919", "沪深300ETF", "sz", ""],    # non_stock
            ["600193", "退市创兴", "sh", ""],      # suspended
        ])
        rows = I.parse_import_file(raw, filename="a.csv")
        results = I.match_rows(db_session, rows)

        assert _status_of(results, "000001") == I.RowStatus.SUCCESS
        assert _status_of(results, "600519") == I.RowStatus.SUCCESS
        assert _status_of(results, "999999") == I.RowStatus.UNMATCHED
        assert _status_of(results, "1") == I.RowStatus.FORMAT_ERROR
        assert _status_of(results, "159919") == I.RowStatus.NON_STOCK
        assert _status_of(results, "600193") == I.RowStatus.SUSPENDED

        dup = [r for r in results if r.status == I.RowStatus.DUPLICATE]
        assert len(dup) == 1
        assert dup[0].row_no == 5           # 第 2 次出现的那一行
        assert "第 2 行" in dup[0].reason_zh  # 指向首次出现的行号

    def test_only_success_is_admitted(self, db_session, seeded):
        raw = csv_bytes([["000001"], ["999999"], ["159919"], ["600193"], ["1"]],
                        header=("symbol",))
        results = I.match_rows(db_session, I.parse_import_file(raw, filename="a.csv"))
        admitted = [r for r in results if r.admitted]
        assert [r.symbol for r in admitted] == ["000001"]

    def test_counts(self, db_session, seeded):
        raw = csv_bytes([["000001"], ["600519"], ["999999"], ["000001"]],
                        header=("symbol",))
        results = I.match_rows(db_session, I.parse_import_file(raw, filename="a.csv"))
        counts = I._count_by_status(results)
        assert counts[I.RowStatus.SUCCESS] == 2
        assert counts[I.RowStatus.UNMATCHED] == 1
        assert counts[I.RowStatus.DUPLICATE] == 1

    def test_format_error_hint_mentions_leading_zero(self, db_session, seeded):
        raw = csv_bytes([["1"]], header=("symbol",))
        results = I.match_rows(db_session, I.parse_import_file(raw, filename="a.csv"))
        assert "前导零" in results[0].reason_zh
        # 只提示，**不自动补零**
        assert results[0].symbol != "000001"

    def test_empty_matches_are_not_admitted(self, db_session, seeded):
        raw = csv_bytes([[""]], header=("symbol",))
        rows = I.parse_import_file(raw, filename="a.csv")
        # 空代码 + 空其它列 → 整行跳过；这里 note 为空所以会被跳过
        assert rows == [] or all(not r.admitted
                                 for r in I.match_rows(db_session, rows))

    def test_no_rows_returns_empty(self, db_session):
        assert I.match_rows(db_session, []) == []


# ══════════════════════════════════════════════════════════
# 5. 名称/市场只提示，绝不回写主数据
# ══════════════════════════════════════════════════════════


class TestNoMasterDataWrite:
    def test_mismatch_only_warns(self, db_session, seeded):
        raw = csv_bytes([["000001", "错误名字", "sh", ""]])
        results = I.match_rows(db_session, I.parse_import_file(raw, filename="a.csv"))
        row = results[0]
        assert row.status == I.RowStatus.SUCCESS          # 不影响匹配
        assert row.name_in_system == "平安银行"
        assert row.market_in_system == "sz"
        assert any("名称" in w for w in row.warnings)
        assert any("市场" in w for w in row.warnings)

    def test_master_data_unchanged(self, db_session, seeded):
        raw = csv_bytes([["000001", "错误名字", "sh", ""]])
        I.match_rows(db_session, I.parse_import_file(raw, filename="a.csv"))
        db_session.expire_all()
        sym = db_session.query(Symbol).filter_by(symbol="000001").one()
        assert sym.name == "平安银行"
        assert sym.market == "sz"

    def test_unknown_market_value_is_recorded_not_fatal(self, db_session, seeded):
        raw = csv_bytes([["000001", "", "hk", ""]])
        results = I.match_rows(db_session, I.parse_import_file(raw, filename="a.csv"))
        assert results[0].status == I.RowStatus.SUCCESS
        assert any("不是已知值" in w for w in results[0].warnings)


# ══════════════════════════════════════════════════════════
# 6. 错误明细 CSV
# ══════════════════════════════════════════════════════════


class TestErrorCsv:
    def _result(self, db_session, seeded) -> I.ImportResult:
        raw = csv_bytes([
            ["000001", "平安银行", "sz", "成功"],
            ["999999", "不存在", "sz", "未匹配"],
            ["1", "x", "sz", "格式错误"],
            ["159919", "ETF", "sz", "非股票"],
        ])
        rows = I.parse_import_file(raw, filename="a.csv")
        results = I.match_rows(db_session, rows)
        return I.ImportResult(pool_id=None, import_batch_id="testbatch",
                              total_rows=len(results), rows=results,
                              counts=I._count_by_status(results))

    def test_only_errors_are_exported(self, db_session, seeded):
        res = self._result(db_session, seeded)
        blob = I.build_error_csv(res).decode("utf-8-sig")
        reader = list(csv.reader(io.StringIO(blob)))
        header, body = reader[0], reader[1:]
        assert header[:4] == ["row_no", "status", "status_label_zh", "symbol"]
        assert len(body) == 3                       # 只有 3 条失败
        assert all(r[1] != I.RowStatus.SUCCESS for r in body)

    def test_error_rows_keep_user_original_data(self, db_session, seeded):
        """错误明细要能**改完直接重导** —— 必须回写用户原始数据 + 行号 + 原因。"""
        res = self._result(db_session, seeded)
        body = list(csv.reader(io.StringIO(I.build_error_csv(res).decode("utf-8-sig"))))[1:]
        by_code = {r[3]: r for r in body}
        assert by_code["999999"][0] == "3"          # 行号（Excel 行号）
        assert by_code["999999"][1] == I.UNMATCHED if hasattr(I, "UNMATCHED") else True
        assert by_code["999999"][1] == I.RowStatus.UNMATCHED
        assert by_code["999999"][4] == "不存在"      # 用户在文件里写的名称
        assert by_code["999999"][7]                 # 原因非空

    def test_empty_errors_still_has_header(self, db_session, seeded):
        raw = csv_bytes([["000001"]], header=("symbol",))
        results = I.match_rows(db_session, I.parse_import_file(raw, filename="a.csv"))
        res = I.ImportResult(pool_id=None, import_batch_id="b", total_rows=1,
                             rows=results, counts=I._count_by_status(results))
        blob = I.build_error_csv(res).decode("utf-8-sig")
        assert blob.splitlines()[0].startswith("row_no")
        assert len(blob.splitlines()) == 1


# ══════════════════════════════════════════════════════════
# 7. 预览 / 提交
# ══════════════════════════════════════════════════════════


class TestImportMembers:
    def _pool(self, db_session, source_type=pool_service.SOURCE_TYPE_IMPORT):
        return pool_service.create_pool(
            db_session, name="导入池", source_type=source_type,
            import_batch_id="batch-x" if source_type == pool_service.SOURCE_TYPE_IMPORT else None,
            filter_config={"markets": ["sh"]} if source_type == pool_service.SOURCE_TYPE_FILTER else None,
        )

    def test_preview_writes_nothing(self, db_session, seeded):
        pool = self._pool(db_session)
        raw = csv_bytes([["000001"], ["600519"], ["999999"]], header=("symbol",))
        res = I.import_members(db_session, pool_id=pool.id, file_bytes=raw,
                               filename="a.csv", commit=False)
        assert res.committed is False
        assert res.admitted_count == 2
        assert res.error_count == 1
        assert res.member_write == {}
        # 池成员数仍为 0
        assert pool_service.get_pool_detail(db_session, pool.id)["member_count"] == 0

    def test_commit_writes_only_success_rows(self, db_session, seeded):
        pool = self._pool(db_session)
        raw = csv_bytes([
            ["000001"], ["600519"], ["000002"], ["300001"], ["688001"],
            ["999999"],       # unmatched → 不入池
            ["159919"],       # etf → 不入池
            ["600193"],       # 退市 → 不入池
            ["1"],            # 格式错误 → 不入池
        ], header=("symbol",))
        res = I.import_members(db_session, pool_id=pool.id, file_bytes=raw,
                               filename="a.csv", commit=True)
        assert res.committed is True
        assert res.admitted_count == 5
        assert res.member_write["requested"] == 5
        assert res.member_write["added"] == 5
        detail = pool_service.get_pool_detail(db_session, pool.id)
        assert detail["member_count"] == 5

    def test_filter_pool_rejects_import(self, db_session, seeded):
        """两种方式互斥（向导 §3.1）：`filter` 池不能导入。"""
        pool = self._pool(db_session, source_type=pool_service.SOURCE_TYPE_FILTER)
        raw = csv_bytes([["000001"]], header=("symbol",))
        with pytest.raises(FactorSevenError) as ei:
            I.import_members(db_session, pool_id=pool.id, file_bytes=raw,
                             filename="a.csv", commit=True)
        assert ei.value.error_code == "BUSINESS_BLOCKED"
        assert ei.value.extras["reason"] == "SOURCE_TYPE_IMMUTABLE"

    def test_unknown_pool_raises(self, db_session, seeded):
        raw = csv_bytes([["000001"]], header=("symbol",))
        with pytest.raises(FactorSevenError) as ei:
            I.import_members(db_session, pool_id="nope", file_bytes=raw,
                             filename="a.csv", commit=True)
        assert ei.value.error_code == "NOT_FOUND"

    def test_batch_id_is_reused_when_given(self, db_session, seeded):
        pool = self._pool(db_session)
        raw = csv_bytes([["000001"]], header=("symbol",))
        res = I.import_members(db_session, pool_id=pool.id, file_bytes=raw,
                               filename="a.csv", import_batch_id="my-batch",
                               commit=True)
        assert res.import_batch_id == "my-batch"

    def test_batch_id_is_generated_when_absent(self, db_session, seeded):
        pool = self._pool(db_session)
        raw = csv_bytes([["000001"]], header=("symbol",))
        res = I.import_members(db_session, pool_id=pool.id, file_bytes=raw,
                               filename="a.csv", commit=True)
        assert len(res.import_batch_id) == 32      # uuid4().hex

    def test_duplicates_are_written_once(self, db_session, seeded):
        pool = self._pool(db_session)
        raw = csv_bytes([["000001"], ["000001"], ["000001"]], header=("symbol",))
        res = I.import_members(db_session, pool_id=pool.id, file_bytes=raw,
                               filename="a.csv", commit=True)
        assert res.admitted_count == 1
        assert res.member_write["requested"] == 1
        assert pool_service.get_pool_detail(db_session, pool.id)["member_count"] == 1

    def test_result_to_dict_shape(self, db_session, seeded):
        raw = csv_bytes([["000001"], ["999999"]], header=("symbol",))
        res = I.import_members(db_session, pool_id=None, file_bytes=raw,
                               filename="a.csv", commit=False)
        d = res.to_dict()
        for key in ("total_rows", "admitted_count", "error_count", "has_errors",
                    "counts", "counts_label_zh", "rows", "import_batch_id"):
            assert key in d
        assert d["has_errors"] is True
        assert set(d["counts_label_zh"]) == set(d["counts"])


# ══════════════════════════════════════════════════════════
# 8. HTTP 层
# ══════════════════════════════════════════════════════════


class TestHttpLayer:
    def test_static_routes_precede_dynamic_pool_id(self):
        from app.api.routes import mining_candidate_pool as m

        entries = [getattr(r, "path", "") for r in m.router.routes
                   if "/candidate-pools" in getattr(r, "path", "")]
        idx = {p: i for i, p in enumerate(entries)}
        first_dyn = next(i for i, p in enumerate(entries)
                         if p.rsplit("/", 1)[-1] == "{pool_id}")
        for name in ("import-template", "import-preview", "import-errors"):
            path = f"/factor-mining/candidate-pools/{name}"
            assert path in idx, f"{name} 未注册"
            assert idx[path] < first_dyn, f"{name} 必须排在 {{pool_id}} 之前"

    @pytest.mark.parametrize("fmt,ext", [("csv", "csv"), ("xlsx", "xlsx")])
    def test_download_template(self, db_session, fmt, ext):
        client = _build_client(db_session)
        r = client.get(f"/factor-mining/candidate-pools/import-template?fmt={fmt}")
        assert r.status_code == 200
        assert ext in r.headers["content-disposition"]
        assert r.content

    def test_preview_via_multipart(self, db_session, seeded):
        client = _build_client(db_session)
        raw = csv_bytes([["000001"], ["600519"], ["999999"]], header=("symbol",))
        r = client.post(
            "/factor-mining/candidate-pools/import-preview",
            files={"file": ("a.csv", raw, "text/csv")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["admitted_count"] == 2
        assert body["error_count"] == 1
        assert body["committed"] is False
        assert len(body["rows"]) == 3

    def test_preview_via_raw_body(self, db_session, seeded):
        """不依赖 `python-multipart` 的上传通道（body 直接放文件 + X-Filename）。

        这条测试保证：即使部署环境缺 python-multipart，导入功能**仍然可用**，
        而不是整站 503。
        """
        client = _build_client(db_session)
        raw = csv_bytes([["000001"], ["999999"]], header=("symbol",))
        r = client.post(
            "/factor-mining/candidate-pools/import-preview",
            content=raw,
            headers={"X-Filename": "a.csv"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["admitted_count"] == 1

    def test_download_error_csv(self, db_session, seeded):
        client = _build_client(db_session)
        raw = csv_bytes([["000001"], ["999999"]], header=("symbol",))
        r = client.post(
            "/factor-mining/candidate-pools/import-errors",
            files={"file": ("a.csv", raw, "text/csv")},
        )
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "import_errors_" in r.headers["content-disposition"]
        body = r.content.decode("utf-8-sig")
        assert "999999" in body
        assert "000001" not in body          # 成功行不出现在错误明细里

    def test_import_into_pool(self, db_session, seeded):
        pool = pool_service.create_pool(
            db_session, name="导入池",
            source_type=pool_service.SOURCE_TYPE_IMPORT,
            import_batch_id="batch-1",
        )
        client = _build_client(db_session)
        raw = csv_bytes([["000001"], ["600519"], ["000002"], ["999999"]],
                        header=("symbol",))
        r = client.post(
            f"/factor-mining/candidate-pools/{pool.id}/import",
            files={"file": ("a.csv", raw, "text/csv")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["committed"] is True
        assert body["admitted_count"] == 3
        assert body["pool"]["member_count"] == 3
        assert body["error_csv_endpoint"].endswith("/import-errors")

    def test_import_into_filter_pool_returns_4xx(self, db_session, seeded):
        pool = pool_service.create_pool(
            db_session, name="筛选池",
            source_type=pool_service.SOURCE_TYPE_FILTER,
            filter_config={"markets": ["sh"]},
        )
        client = _build_client(db_session)
        raw = csv_bytes([["000001"]], header=("symbol",))
        r = client.post(
            f"/factor-mining/candidate-pools/{pool.id}/import",
            files={"file": ("a.csv", raw, "text/csv")},
        )
        assert 400 <= r.status_code < 500
        assert r.json()["detail"]["error_code"] == "BUSINESS_BLOCKED"

    def test_import_missing_symbol_column_returns_4xx(self, db_session, seeded):
        client = _build_client(db_session)
        raw = csv_bytes([["000001"]], header=("ticker",))
        r = client.post(
            "/factor-mining/candidate-pools/import-preview",
            files={"file": ("a.csv", raw, "text/csv")},
        )
        assert 400 <= r.status_code < 500
        assert r.json()["detail"]["error_code"] == "VALIDATION_ERROR"

    def test_empty_upload_returns_4xx(self, db_session, seeded):
        client = _build_client(db_session)
        r = client.post(
            "/factor-mining/candidate-pools/import-preview",
            files={"file": ("a.csv", b"", "text/csv")},
        )
        assert 400 <= r.status_code < 500
