"""Public FRED CSV transport, separate from the unconfigured keyed API."""
from io import BytesIO
import pandas as pd
import requests
from app.services.data_source_execution import source_boundary

CSV_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv'


@source_boundary(lambda series, *, raw=False: 'fred:' + series)
def fetch_fred_csv(series, *, raw=False):
    if series not in {'CPIAUCSL', 'CPILFESL', 'INDPRO', 'PAYEMS', 'PPIACO', 'UNRATE'}:
        raise ValueError('Unknown FRED CSV series')
    from app.services.data_source_control import current_controller
    if current_controller() is None and not raw:
        # Preserve the explicitly labelled legacy deployment during migration.
        return pd.read_csv(CSV_URL + '?id=' + series)
    options = {'params': {'id': series}, 'timeout': (5,15), 'allow_redirects': False}
    if raw: options['stream'] = True
    with requests.get(CSV_URL, **options) as response:
        response.raise_for_status()
        if raw:
            if response.status_code != 200: raise ValueError('FRED CSV response status invalid')
            parts = []; size = 0
            for part in response.iter_content(chunk_size=65536):
                size += len(part)
                if size > 16 * 1024 * 1024: raise ValueError('FRED CSV response size invalid')
                parts.append(part)
            body = b''.join(parts)
        else:
            body = response.content
        if not body or len(body) > 16 * 1024 * 1024:
            raise ValueError('FRED CSV response size invalid')
        return body if raw else pd.read_csv(BytesIO(body))
