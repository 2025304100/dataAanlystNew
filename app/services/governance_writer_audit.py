"""Fail-closed registration of SQL/ORM/warehouse write-capable boundaries.

This source audit detects new or changed boundaries; database privileges remain
the enforcement layer. It does not claim static analysis proves arbitrary SQL.
"""
import ast,hashlib,json
from pathlib import Path

SINKS={'execute','exec_driver_sql','executemany','executescript','sql','update','add','add_all','delete','merge','bulk_save_objects','bulk_insert_mappings','bulk_update_mappings','to_sql','upsert_daily_bars','upsert_records','upsert_frame','write'}


def inventory_writes(root):
    root=Path(root);entries=[]
    for folder in ('app','scripts'):
        for path in sorted((root/folder).rglob('*.py')):
            if '__pycache__' in path.parts:continue
            tree=ast.parse(path.read_text(encoding='utf-8-sig'));relative=path.relative_to(root).as_posix()
            def collect(nodes,prefix=''):
                for node in nodes:
                    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                        sinks=sorted({c.func.attr for c in ast.walk(node) if isinstance(c,ast.Call) and isinstance(c.func,ast.Attribute) and c.func.attr in SINKS})
                        if sinks:
                            name=prefix+node.name
                            entries.append({'path':relative,'qualname':name,'line':node.lineno,'sinks':sinks,
                                'sha256':hashlib.sha256(ast.dump(node,include_attributes=False).encode()).hexdigest(),
                                'scope':'canonical_fact_executor' if relative=='app/services/governance_writer.py' and name=='FactWriter.write' else 'governance_owner_or_admin' if Path(relative).name.startswith('governance_') else 'legacy_or_staging'})
                    elif isinstance(node,ast.ClassDef):collect(node.body,prefix+node.name+'.')
                    elif isinstance(node,(ast.If,ast.With,ast.Try,ast.For,ast.While)):
                        collect(getattr(node,'body',[]),prefix);collect(getattr(node,'orelse',[]),prefix)
            collect(tree.body)
            top=[node for node in tree.body if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
            if any(isinstance(c,ast.Call) and isinstance(c.func,ast.Attribute) and c.func.attr in SINKS for node in top for c in ast.walk(node)):
                entries.append({'path':relative,'qualname':'<module>','line':1,'sinks':['module_boundary'],
                    'sha256':hashlib.sha256(ast.dump(ast.Module(body=top,type_ignores=[]),include_attributes=False).encode()).hexdigest(),'scope':'legacy_or_staging'})
    return {'format':'governance-writer-inventory-v1','entries':sorted(entries,key=lambda r:(r['path'],r['qualname']))}


def audit_writes(root,baseline):
    if baseline.get('format')!='governance-writer-inventory-v1':raise ValueError('WRITER_INVENTORY_INVALID')
    actual=inventory_writes(root);registered={(r['path'],r['qualname']):r for r in baseline['entries']};violations=[]
    if len(registered)!=len(baseline['entries']):raise ValueError('WRITER_INVENTORY_INVALID')
    for entry in actual['entries']:
        known=registered.get((entry['path'],entry['qualname']))
        reason='UNREGISTERED_ENTRY' if known is None else 'CHANGED_ENTRY' if known['sha256']!=entry['sha256'] else None
        if reason:violations.append({'path':entry['path'],'qualname':entry['qualname'],'line':entry['line'],'reason':reason})
    return {'status':'fail' if violations else 'pass','entry_count':len(actual['entries']),'violations':violations}
