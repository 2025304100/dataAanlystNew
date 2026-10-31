"""Bounded Sina transport for the strict three-response history parser.

The caller keeps the stock_zh_a_daily registry policy and retry wrapper. This
helper never invokes the SDK's outer-join/forward-fill/drop-duplicates path.
"""
from __future__ import annotations

from datetime import datetime
import re

import pandas as pd
import requests

from app.services.akshare_utils import _DEFAULT_TIMEOUT
from app.services.data_source_execution import source_boundary
from app.services.market_data_sources.sources.sina_history_evidence import parse_sina_history_bundle


SINA_HISTORY_TIMEOUT = _DEFAULT_TIMEOUT
SINA_HISTORY_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _read_response(url: str) -> bytes:
    response = requests.get(url, timeout=SINA_HISTORY_TIMEOUT, allow_redirects=False, stream=True)
    try:
        if response.status_code != 200:
            response.raise_for_status()
            raise ValueError("sina_history_unexpected_http_status")
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > SINA_HISTORY_MAX_RESPONSE_BYTES:
            raise ValueError("sina_history_response_too_large")
        chunks, count = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            count += len(chunk)
            if count > SINA_HISTORY_MAX_RESPONSE_BYTES:
                raise ValueError("sina_history_response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.close()


@source_boundary('stock_zh_a_daily')
def fetch_sina_history_raw(*, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    """Return strict standard columns for RAW or qfq; hfq fails before I/O.

    `raw` in this helper's name denotes the original HTTP response boundary,
    as in the Tencent helper. The returned price mode is explicit in attrs.
    """
    if not isinstance(symbol, str) or re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol) is None:
        raise ValueError("sina_history_invalid_symbol")
    if adjust not in {"", "qfq"}:
        raise ValueError("sina_history_unsupported_adjustment")
    try:
        start = datetime.strptime(start_date, "%Y%m%d").date()
        end = datetime.strptime(end_date, "%Y%m%d").date()
        if start_date != start.strftime("%Y%m%d") or end_date != end.strftime("%Y%m%d") or start > end:
            raise ValueError("non-canonical range")
    except (TypeError, ValueError) as exc:
        raise ValueError("sina_history_invalid_request_dates") from exc
    urls = {
        "history": f"https://finance.sina.com.cn/realstock/company/{symbol}/hisdata_klc2/klc_kl.js",
        "shares": f"https://stock.finance.sina.com.cn/stock/api/jsonp.php/var%20KKE_ShareAmount_{symbol}=/StockService.getAmountBySymbol?_=20&symbol={symbol}",
        "qfq": f"https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js",
    }
    payloads = {name: _read_response(url) for name, url in urls.items()}
    frame = parse_sina_history_bundle(symbol, payloads["history"], payloads["shares"], payloads["qfq"],
                                      start, end, adjust=adjust)
    frame.attrs["history_response"] = {
        "provider": "sina", "endpoints": urls, "symbol": symbol,
        "requested_adjustment": adjust, "actual_adjustment": adjust,
        "adjustment_evidence": "raw_prices" if adjust == "" else "bound_qfq_factor_backward_asof",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "response_sha256": frame.attrs["sina_evidence"]["response_sha256"],
        "raw_response_archived": False,
    }
    return frame
