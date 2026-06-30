with open(r'D:\ai_project\dataAanlystNew\frontend\src\styles\workbench.css', 'r', encoding='utf-8') as f:
    content = f.read()

print(f'CSS file size: {len(content)} chars')
print(f'Has fadeSlideUp: {"fadeSlideUp" in content}')
print(f'Has focus-visible: {"focus-visible" in content}')
print(f'Has backdrop-filter: {"backdrop-filter" in content}')
