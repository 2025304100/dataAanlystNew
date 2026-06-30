import os

# Check workbench.css for garbled content
css_path = r'D:\ai_project\dataAanlystNew\frontend\src\styles\workbench.css'
with open(css_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Find garbled comment lines
lines = content.split('\n')
garbled_lines = []
for i, line in enumerate(lines, 1):
    # Look for lines that contain garbled Chinese-like characters (閺, 圱, etc)
    if any(c in line for c in '閺圱浠閿閸閳閼閹鎮鎺鎼鏍鏌闆'):
        garbled_lines.append((i, line.rstrip()))

print(f'Found {len(garbled_lines)} garbled comment lines in workbench.css:\n')
for lineno, line in garbled_lines:
    print(f'  Line {lineno}: {line[:120]}')

# Also check for common garbled unicode sequences
import re
# Match garbled sequences like 閺冦儲婀? or similar patterns
garbled_pattern = re.compile(r'[\u4e00-\u9fff]{2}[\?\s:]{1,3}"[^"]*"')
matches = garbled_pattern.findall(content)
if matches:
    print(f'\nFound garbled dict-like patterns: {len(matches)}')
    for m in matches[:10]:
        print(f'  {m[:80]}')
