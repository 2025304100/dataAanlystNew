import pathlib

p = pathlib.Path(r"D:\ai_project\dataAanlystNew\alembic\env.py")
content = p.read_text(encoding="utf-8")

old = """    @functools.wraps(_orig_create_index)
    def _create_index(self, index_name, table_name, columns, **kw):
        bind = self.get_bind()
        if bind is not None:
            insp = _sqla_inspect(bind)
            if insp.has_table(table_name):
                existing_columns = {col[\"name\"] for col in insp.get_columns(table_name)}
                if any(str(column) not in existing_columns for column in columns):
                    return None
                idxs = insp.get_indexes(table_name)
                if any(i.get(\"name\") == index_name for i in idxs):
                    return None
        return _orig_create_index(self, index_name, table_name, columns, **kw)"""

new = """    @functools.wraps(_orig_create_index)
    def _create_index(self, index_name, table_name, columns, **kw):
        bind = self.get_bind()
        if bind is not None:
            insp = _sqla_inspect(bind)
            if insp.has_table(table_name):
                existing_columns = {col["name"] for col in insp.get_columns(table_name)}
                if any(str(column) not in existing_columns for column in columns):
                    return None
                idxs = insp.get_indexes(table_name)
                if any(i.get("name") == index_name for i in idxs):
                    return None
        # T3 修复：MySQL 不支持 CREATE INDEX IF NOT EXISTS 语法。
        # env.py 已用 insp.has_table + get_indexes 做了等价的前置存在性检查，
        # 此处必须剥离 if_not_exists kwarg，避免生成非法 SQL。
        kw.pop("if_not_exists", None)
        return _orig_create_index(self, index_name, table_name, columns, **kw)"""

if old in content:
    content = content.replace(old, new)
    p.write_text(content, encoding="utf-8")
    print("PASS: env.py _create_index stripped if_not_exists kwarg")
else:
    print("WARN: env.py _create_index old text not matched, trying alt...")
    # Use fuzzy search based on core pattern
    idx = content.find("return _orig_create_index(self, index_name, table_name, columns, **kw)")
    if idx >= 0:
        before_line = content.rfind("\n", 0, idx)
        indent = "        "
        insert_line = indent + "# T3 MySQL fix: strip if_not_exists after manual exists check\n"
        insert_line += indent + 'kw.pop("if_not_exists", None)\n'
        content = content[:idx] + insert_line + content[idx:]
        p.write_text(content, encoding="utf-8")
        print("PASS: env.py fuzzy patched")
    else:
        raise SystemExit("FAIL: cannot locate _orig_create_index return in env.py")
