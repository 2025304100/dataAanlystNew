import os
import re

# UTF-8 mojibake patterns - these appear when UTF-8 Chinese is decoded as wrong encoding
GARBLED_PATTERNS = [
    '閺', '圱', '浠', '閿', '閸', '閳', '閼', '閹', '鎮', '鎺', '鎼',
    '鏍', '鏌', '闆', '闂', '闃', '闄', '闅', '闆', '閾', '閽',
    '閼', '閻', '閷', '閸', '閹', '闀', '闁', '闘', '闃', '闄'
]

# Common correct Chinese characters that get garbled
CORRECT_MAP = {
    '日期': None, '开盘': None, '最高': None, '最低': None, '收盘': None,
    '成交量': None, '成交额': None, '换手率': None, '建议': None,
    '当前': None, '止损': None, '目标': None, '盈利': None, '亏损': None,
    '数量': None, '上限': None, '最新': None, '近期': None, '金叉': None, '死叉': None,
    '已存计划': None, '计划名称': None, '逻辑': None, '保存计划': None, '删除计划': None,
    '重置': None, '添加规则': None, '规则': None, '选择指标': None, '运算符': None,
    '阈值': None, '目标值': None, '删除规则': None, '自定义': None, '未知': None,
    '指标': None, '布尔': None, '数值': None, '请选择': None, '删除分组': None,
    '添加条件': None, '添加分组': None, '命中指标': None, '条条件': None,
}

def find_garbled_files(root_dir):
    """Find all Python files containing garbled Chinese patterns."""
    garbled_files = {}
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Skip node_modules and __pycache__
        if 'node_modules' in dirpath or '__pycache__' in dirpath:
            continue
        for filename in filenames:
            if filename.endswith('.py'):
                filepath = os.path.join(dirpath, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                    found_garbled = []
                    for char in GARBLED_PATTERNS:
                        if char in content:
                            # Count occurrences
                            count = content.count(char)
                            found_garbled.append((char, count))
                    if found_garbled:
                        garbled_files[filepath] = found_garbled
                except Exception as e:
                    print(f'Error reading {filepath}: {e}')
    return garbled_files

def main():
    root = r'D:\ai_project\dataAanlystNew\app'
    print('Scanning for garbled Chinese characters...')
    garbled = find_garbled_files(root)
    
    if not garbled:
        print('No garbled files found!')
        return
    
    print(f'\nFound {len(garbled)} files with garbled content:\n')
    for filepath, chars in sorted(garbled.items()):
        rel_path = os.path.relpath(filepath, root)
        char_info = ', '.join([f"'{c}'({n})" for c, n in chars])
        print(f'  {rel_path}: {char_info}')

if __name__ == '__main__':
    main()
