import pytest

from aeeo.agents.base import extract_json
from aeeo.inspector import index_repo, rank_components
from aeeo.llm import strict_json_schema
from aeeo.models import DevOutput
from aeeo.workspace import ScopeViolation, Workspace, changeset_hash


def test_extract_json_handles_fences_and_preamble():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": 2}') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_strict_schema_closes_objects():
    s = strict_json_schema(DevOutput.model_json_schema())
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    edit = s["$defs"]["FileEdit"]
    assert edit["additionalProperties"] is False


def test_workspace_rejects_path_escape(tmp_path):
    ws = Workspace(tmp_path, "x")
    for bad in ("../etc/passwd", "/etc/passwd", ".git/config"):
        with pytest.raises(ScopeViolation):
            ws._resolve(bad)


def test_scope_check_protects_files(tmp_path):
    ws = Workspace(tmp_path, "x")
    with pytest.raises(ScopeViolation):
        ws.check_scope(["tests/t.py"], {"tests/t.py"}, protected={"tests/t.py"})


def test_tool_allowlist(tmp_path):
    with pytest.raises(PermissionError):
        Workspace(tmp_path, "x").run_tool("pip", ["install", "x"])


def test_changeset_hash_order_independent():
    assert changeset_hash([("a", "1"), ("b", "2")]) == changeset_hash([("b", "2"), ("a", "1")])


def test_inspector_ranks_pricing_first():
    files = {
        "services/pricing/pricing.py": '"""Order pricing: subtotal, tax and total."""\ndef calculate_total(): pass\n',
        "services/inventory/stock.py": '"""Stock levels."""\ndef reserve(): pass\n',
        "services/orders/checkout.py": "from services.pricing.pricing import calculate_total\n",
    }
    ranked = rank_components("discount applies to the subtotal before tax in the total", index_repo(files))
    assert ranked[0]["path"] == "services/pricing/pricing.py"
    assert ranked[0]["imported_by"] == ["services/orders/checkout.py"]
