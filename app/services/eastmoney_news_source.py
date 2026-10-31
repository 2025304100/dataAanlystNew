"""Eastmoney's existing public news contract over the accounted HTTP transport.

AkShare 1.18.30 uses curl_cffi here, which bypasses requests.Session.send. Keep
the six-column contract, but perform the request through the shared boundary.
"""
import json
from functools import partial

URL = 'https://search-api-web.eastmoney.com/search/jsonp'
COLUMNS = ['关键词', '新闻标题', '新闻内容', '发布时间', '文章来源', '新闻链接']


def fetch_stock_news(symbol: str = '603777'):
    import pandas as pd
    import requests
    payload = {'uid': '', 'keyword': symbol, 'type': ['cmsArticleWebOld'],
               'client': 'web', 'clientType': 'web', 'clientVersion': 'curr',
               'param': {'cmsArticleWebOld': {'searchScope': 'default', 'sort': 'default',
                         'pageIndex': 1, 'pageSize': 10, 'preTag': '<em>', 'postTag': '</em>'}}}
    callback = 'dataSourceNews'
    response = requests.get(URL, params={'cb': callback, 'param': json.dumps(payload)},
                            timeout=(5, 15), allow_redirects=False)
    response.raise_for_status()
    text = response.content.decode('utf-8').strip().removesuffix(';')
    if not text.startswith(callback + '(') or not text.endswith(')'):
        from app.services.data_source_contracts import DataContractError
        raise DataContractError('NEWS_JSONP_SHAPE')
    try:
        rows = json.loads(text[len(callback) + 1:-1])['result']['cmsArticleWebOld']
        if not isinstance(rows, list):
            raise ValueError('news result must be a list')
        def clean(value):
            return str(value or '').replace('(<em>', '').replace('</em>)', '').replace('<em>', '').replace('</em>', '').replace('\u3000', '').replace('\r\n', ' ')
        return pd.DataFrame([{'关键词': symbol, '新闻标题': clean(row['title']),
                    '新闻内容': clean(row['content']), '发布时间': row['date'],
                    '文章来源': row['mediaName'],
                    '新闻链接': f"http://finance.eastmoney.com/a/{row['code']}.html"}
                    for row in rows], columns=COLUMNS)
    except (ValueError, TypeError, KeyError) as exc:
        from app.services.data_source_contracts import DataContractError
        raise DataContractError('NEWS_RESULT_SHAPE') from exc


def accounted_supplier(endpoint, func):
    """Replace only the installed native SDK root, preserving injected callables."""
    if endpoint != 'stock_news_em':
        return func
    if isinstance(func, partial):
        return partial(accounted_supplier(endpoint, func.func), *func.args, **(func.keywords or {}))
    if getattr(func, '__module__', '').startswith('akshare.') and getattr(func, '__name__', '') == 'stock_news_em':
        return fetch_stock_news
    return func
