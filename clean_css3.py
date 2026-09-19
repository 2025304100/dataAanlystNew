css_path = r'D:\ai_project\dataAanlystNew\frontend\src\styles\workbench.css'
with open(css_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

garbled_chars = '閺圱浠閿閸閳閼閹鎮鎺鎼鏍鏌闆'

def has_garbled(text):
    return any(c in text for c in garbled_chars)

# Find all garbled line indices
garbled_indices = set()
for i, line in enumerate(lines):
    if has_garbled(line):
        garbled_indices.add(i)

print(f'Found {len(garbled_indices)} garbled lines: {sorted(garbled_indices)[:10]}...')

# Group garbled lines and find their surrounding comment blocks
# Strategy: for each garbled line, find the enclosing comment block (/* */)
# and mark all lines in that block for removal

lines_to_remove = set()

for i in garbled_indices:
    # Look backwards for opening /*
    start = i
    while start > 0 and '/*' not in lines[start]:
        start -= 1
    # Check if there's a /* before line i
    if start >= 0 and '/*' in lines[start]:
        # Find closing */
        end = i
        while end < len(lines) and '*/' not in lines[end]:
            end += 1
        # Mark all lines in this block for removal
        for j in range(start, min(end + 1, len(lines))):
            lines_to_remove.add(j)
        print(f'Garbled line {i} is in comment block [{start}, {end}]')
    else:
        print(f'Garbled line {i} has no enclosing comment: {lines[i][:50]}')

# Create cleaned content
cleaned_lines = [line for i, line in enumerate(lines) if i not in lines_to_remove]
cleaned_content = ''.join(cleaned_lines)

with open(css_path, 'w', encoding='utf-8') as f:
    f.write(cleaned_content)

print(f'\nRemoved {len(lines_to_remove)} lines')
print(f'New file size: {len(cleaned_content)} chars')

# Verify braces
open_count = cleaned_content.count('{')
close_count = cleaned_content.count('}')
print(f'Braces: {open_count} open, {close_count} close - {"OK" if open_count == close_count else "MISMATCH"}')

# Check if any garbled remain
remaining = [(i, lines[i].rstrip()[:60]) for i in lines_to_remove if has_garbled(lines[i])]
print(f'Garbled lines removed: {len(remaining)}')
