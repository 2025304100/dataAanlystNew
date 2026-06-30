import os
import re

root = r'D:\ai_project\dataAanlystNew'

# Garbled character patterns
garbled_chars = set('閺圱浠閿閸閳閼閹鎮鎺鎼鏍鏌闆')

# Common valid Chinese character range
chinese_char_pattern = re.compile(r'[\u4e00-\u9fff]')

def has_garbled(filepath):
    """Check if file contains garbled characters (outside valid Chinese chars)."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        # Check each Chinese-like character
        for char in content:
            if char in garbled_chars:
                return True
        return False
    except:
        return False

def check_file(filepath):
    """Return list of lines containing garbled chars."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        garbled_lines = []
        for i, line in enumerate(lines, 1):
            for char in line:
                if char in garbled_chars:
                    garbled_lines.append((i, line.rstrip()[:100]))
                    break
        return garbled_lines
    except Exception as e:
        return [(-1, str(e))]

print('Scanning for garbled content in all text files...\n')

found_files = {}
for dirpath, dirnames, filenames in os.walk(root):
    # Skip certain directories
    if any(x in dirpath for x in ['node_modules', '__pycache__', '.git', '.venv', 'venv']):
        continue
    for f in filenames:
        if f.endswith(('.py', '.ts', '.tsx', '.css', '.js', '.jsx', '.json', '.md', '.txt')):
            filepath = os.path.join(dirpath, f)
            garbled = check_file(filepath)
            if garbled:
                rel = os.path.relpath(filepath, root)
                # Filter out comment-only lines for CSS/JS
                non_comment = [(ln, txt) for ln, txt in garbled if not txt.strip().startswith('/*')]
                if non_comment or not f.endswith('.css'):
                    if f.endswith('.css'):
                        found_files[rel] = len(garbled)  # count all for CSS
                    else:
                        found_files[rel] = non_comment if non_comment else garbled

if found_files:
    print(f'Found {len(found_files)} files with garbled content:\n')
    for filepath, info in sorted(found_files.items()):
        if isinstance(info, int):
            print(f'  {filepath}: {info} garbled lines')
        else:
            print(f'  {filepath}:')
            for ln, txt in info[:5]:
                print(f'    Line {ln}: {txt}')
else:
    print('No garbled content found!')
