import os
import re

root = r'D:\ai_project\dataAanlystNew\app'
# Look for garbled patterns like "閺冦儲婀?: " - Chinese chars followed by garbled text and colon
pattern = re.compile(r'[\u4e00-\u9fff]{2,10}?\?')

found = {}
for dirpath, dirs, files in os.walk(root):
    if '__pycache__' in dirpath or 'node_modules' in dirpath:
        continue
    for f in files:
        if f.endswith('.py'):
            fp = os.path.join(dirpath, f)
            try:
                with open(fp, 'r', encoding='utf-8') as fh:
                    content = fh.read()
                # Find lines with garbled patterns (question marks in strings)
                lines_with_garble = []
                for i, line in enumerate(content.split('\n'), 1):
                    if '?' in line and ('":' in line or "?:" in line or '",' in line):
                        lines_with_garble.append((i, line.rstrip()))
                if lines_with_garble:
                    found[os.path.relpath(fp, root)] = lines_with_garble
            except Exception as e:
                print(f'Error reading {fp}: {e}')

if found:
    print(f'Found {len(found)} files with potential garbled content:\n')
    for k, v in sorted(found.items()):
        print(f'{k}:')
        for lineno, line in v:
            print(f'  Line {lineno}: {line[:100]}')
        print()
else:
    print('No garbled files found with question mark pattern')
