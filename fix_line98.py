with open(r'D:\ai_project\dataAanlystNew\app\services\market_data.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Print current line 98 to understand the encoding
print('Line 98 before:', repr(lines[97]))

# Replace line 98 (index 97) with correct content
lines[97] = '            "开盘": "open",\n'

with open(r'D:\ai_project\dataAanlystNew\app\services\market_data.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
print('Fixed line 98')
print('New line 98:', repr(lines[97]))
