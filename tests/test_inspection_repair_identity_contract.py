"""Pin the frontend lookup identities to actual backend expressions without importing app/DB."""
import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app/services/portfolio_inspection_repair.py"
DIAGNOSTICS = ROOT / "app/services/portfolio_inspection_diagnostics.py"
VECTORS = [(1, "inspection-contract-test", "e8a0f849aeab5a2a8106dd73899e98c4"),
           (42, "inspection-retry-contract", "274b90b117cd045e1b7d203364a13bf6")]


def backend_identity(function_name, pid, request_key):
    hash_node = next(node for node in ast.parse(DIAGNOSTICS.read_text(encoding="utf-8")).body
                     if isinstance(node, ast.FunctionDef) and node.name == "_hash")
    namespace = {"hashlib": hashlib, "json": json, "Any": object}
    exec(compile(ast.Module(body=[hash_node], type_ignores=[]), str(DIAGNOSTICS), "exec"), namespace)
    function = next(node for node in ast.parse(SERVICE.read_text(encoding="utf-8")).body
                    if isinstance(node, ast.FunctionDef) and node.name == function_name)
    assignment = next(node for node in function.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "task_id" for target in node.targets))
    namespace.update(portfolio_id=pid, key=request_key, request=SimpleNamespace(idempotency_key=request_key))
    return eval(compile(ast.Expression(body=assignment.value), str(SERVICE), "eval"), namespace)


class InspectionRepairIdentityContract(unittest.TestCase):
    def test_submit_identity_matches_frontend_lookup_vectors(self):
        for pid, key, expected in VECTORS:
            self.assertEqual(backend_identity("submit_inspection_repair", pid, key), expected)

    def test_persist_and_retry_identity_matches_the_same_lookup_vectors(self):
        for pid, key, expected in VECTORS:
            self.assertEqual(backend_identity("_persist_and_start", pid, key), expected)


if __name__ == "__main__":
    unittest.main()
