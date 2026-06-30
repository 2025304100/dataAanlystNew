import re

css_path = r'D:\ai_project\dataAanlystNew\frontend\src\styles\workbench.css'

with open(css_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Characters that indicate garbled content in comments
garbled_chars = '閺圱浠閿閸閳閼閹鎮鎺鎼鏍鏌闆'

def is_garbled_line(line):
    """Check if a line contains garbled Chinese characters."""
    # Remove the comment markers first
    stripped = line.strip()
    if stripped.startswith('/*'):
        # Check if comment content has garbled chars
        comment_content = stripped[2:-2] if stripped.endswith('*/') else stripped[2:]
        return any(c in comment_content for c in garbled_chars)
    return False

lines = content.split('\n')
cleaned_lines = []
removed_count = 0

for line in lines:
    if is_garbled_line(line):
        removed_count += 1
    else:
        cleaned_lines.append(line)

cleaned_content = '\n'.join(cleaned_lines)

with open(css_path, 'w', encoding='utf-8') as f:
    f.write(cleaned_content)

print(f'Removed {removed_count} garbled comment lines')
print(f'New file size: {len(cleaned_content)} chars')

# Verify the file is valid CSS by checking for obvious syntax errors
print('\nVerifying CSS syntax...')
open_braces = cleaned_content.count('{')
close_braces = cleaned_content.count('}')
print(f'Open braces: {open_braces}, Close braces: {close_braces}')
if open_braces == close_braces:
    print('✓ Brace count matches')
else:
    print('⚠ Brace count mismatch - review manually')
