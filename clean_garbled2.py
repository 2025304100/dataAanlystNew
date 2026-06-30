import re

# Garbled character set
garbled_chars = set('閺圱浠閿閸閳閼閹鎮鎺鎼鏍鏌闆')

def has_garbled(text):
    return any(c in text for c in garbled_chars)

# Fix types/index.ts
ts_path = r'D:\ai_project\dataAanlystNew\frontend\src\types\index.ts'
with open(ts_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

cleaned = []
removed = 0
for i, line in enumerate(lines, 1):
    stripped = line.strip()
    # Remove lines that are pure garbled comments (not useful)
    if stripped.startswith('//') and has_garbled(stripped):
        # Check if the garbled comment has any recognizable content
        # Keep comments that have a mix of garbled and valid content (like line 580)
        valid_chinese = re.findall(r'[\u4e00-\u9fff]', stripped)
        if len(valid_chinese) < 3:  # Too few valid Chinese chars, remove
            removed += 1
            continue
    cleaned.append(line)

with open(ts_path, 'w', encoding='utf-8') as f:
    f.writelines(cleaned)

print(f'Types index: removed {removed} garbled comment lines')

# Fix workbench.css - remove garbled comment blocks
css_path = r'D:\ai_project\dataAanlystNew\frontend\src\styles\workbench.css'
with open(css_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Remove /* ... */ comment blocks that contain garbled characters
lines = content.split('\n')
cleaned_lines = []
removed_css = 0

i = 0
while i < len(lines):
    line = lines[i]
    stripped = stripped = line.strip()

    # Check if this is a comment line with garbled content
    if stripped.startswith('/*') and has_garbled(stripped):
        # Find the end of this comment block
        j = i
        while j < len(lines) and '*/' not in lines[j]:
            j += 1
        # Check if the entire comment block is garbled
        comment_block = '\n'.join(lines[i:j+1])
        if has_garbled(comment_block):
            # Check if there's any meaningful non-garbled content
            # Extract content between /* and */
            match = re.search(r'/\*(.*?)\*/', comment_block, re.DOTALL)
            if match:
                comment_content = match.group(1)
                # Count valid Chinese or ASCII content
                valid_chars = len(re.findall(r'[\u4e00-\u9fff\w\s\-_.,;:!?()*/]', comment_content))
                garbled_chars_count = sum(1 for c in comment_content if c in garbled_chars)
                if garbled_chars_count > valid_chars * 0.5:  # Mostly garbled
                    removed_css += 1
                    i = j + 1
                    continue
        cleaned_lines.append(line)
        i += 1
    else:
        cleaned_lines.append(line)
        i += 1

cleaned_css = '\n'.join(cleaned_lines)

with open(css_path, 'w', encoding='utf-8') as f:
    f.write(cleaned_css)

print(f'Workbench CSS: removed {removed_css} garbled comment blocks')

# Verify CSS braces match
open_count = cleaned_css.count('{')
close_count = cleaned_css.count('}')
print(f'CSS braces: {open_count} open, {close_count} close - {"OK" if open_count == close_count else "MISMATCH"}')
