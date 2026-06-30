import codecs

with codecs.open(r'D:\ai_project\dataAanlystNew\app\services\market_data.py', 'r', 'utf-8') as f:
    content = f.read()

# Fix corrupted lines - mapping of corrupted prefix to correct Chinese
fixes = [
    ('"閺冦儲婀?: "trade_date"', '"日期": "trade_date"'),
    ('"瀵嵃钃嶉敍锟?: "open"', '"开盘": "open"'),
    ('"閺堚偓妤?: "high"', '"最高": "high"'),
    ('"閺堚偓娴?: "low"', '"最低": "low"'),
    ('"閺€鍓佹磸": "close"', '"收盘": "close"'),
    ('"閹存劒姘﹂柌?: "volume"', '"成交量": "volume"'),
    ('"閹存劒姘︽０?: "amount"', '"成交额": "amount"'),
    ('"閹广垺澧滈悳?: "turnover_rate"', '"换手率": "turnover_rate"'),
]

for old, new in fixes:
    if old in content:
        content = content.replace(old, new)
        print(f'Fixed: {old[:25]}')
    else:
        print(f'NOT FOUND: {old[:25]}')

with codecs.open(r'D:\ai_project\dataAanlystNew\app\services\market_data.py', 'w', 'utf-8') as f:
    f.write(content)
print('Done')
