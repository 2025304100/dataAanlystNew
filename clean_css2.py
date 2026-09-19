import re

css_path = r'D:\ai_project\dataAanlystNew\frontend\src\styles\workbench.css'
with open(css_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

garbled_chars = '閺圱浠閿閸閳閼閹鎮鎺鎼鏍鏌闆'

def has_garbled(text):
    return any(c in text for c in garbled_chars)

cleaned_lines = []
removed = 0

i = 0
while i < len(lines):
    line = lines[i]
    stripped = line.strip()

    # Check if this line starts a garbled comment block
    if stripped.startswith('/*') and has_garbled(stripped):
        # This line is a garbled comment start, skip until we find */
        j = i
        while j < len(lines) and '*/' not in lines[j]:
            j += 1
        # Remove all lines in this block
        removed += (j - i + 1)
        i = j + 1
        continue

    # Check if this is a regular line that might have garbled content
    if has_garbled(line):
        # It's a line inside a comment block without the opening /*
        # Check if it's a standalone garbled line
        if stripped.startswith('/*') or stripped.startswith('//'):
            j = i
            while j < len(lines) and '*/' not in lines[j]:
                j += 1
            removed += (j - i + 1)
            i = j + 1
            continue
        else:
            # Might be a line with garbled CSS property value - keep it but note it
            pass

    cleaned_lines.append(line)
    i += 1

cleaned_content = ''.join(cleaned_lines)

with open(css_path, 'w', encoding='utf-8') as f:
    f.write(cleaned_content)

print(f'Removed {removed} lines')
print(f'New file size: {len(cleaned_content)} chars')

# Verify braces
open_count = cleaned_content.count('{')
close_count = cleaned_content.count('}')
print(f'Braces: {open_count} open, {close_count} close - {"OK" if open_count == close_count else "MISMATCH"}')
