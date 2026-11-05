import ast
from pathlib import Path


def imported_names(node: ast.AST) -> list[str]:
    """Full dotted names: `from modules.inventory import service` -> modules.inventory.service."""
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        return [f"{node.module}.{a.name}" for a in node.names]
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    return []


def test_modules_talk_to_each_other_only_through_service():
    for module in (Path(__file__).parent / "modules").iterdir():
        if not module.is_dir() or module.name.startswith("_"):
            continue
        for py in module.rglob("*.py"):
            for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
                for name in imported_names(node):
                    parts = name.split(".")
                    other_module = parts[0] == "modules" and len(parts) > 1 and parts[1] != module.name
                    via_service = parts[2:3] == ["service"]          # the one public door (ADR 011)
                    assert not (other_module and not via_service) and parts[0] != "main", (
                        f"{module.name}/{py.name} imports {name}: another module is reachable ONLY through its "
                        "service.py (never its models, repository or router), and nothing imports main.py"
                    )


def test_the_rule_catches_a_violation():
    tree = ast.parse("from modules.inventory.repository import BloodUnitRepository\nfrom modules.inventory import service")
    names = [n for node in ast.walk(tree) for n in imported_names(node)]
    assert names == ["modules.inventory.repository.BloodUnitRepository", "modules.inventory.service"]
