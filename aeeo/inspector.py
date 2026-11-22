"""Static code inspection: index the repo and rank components relevant to a requirement.

Deterministic and model-free, so the architect agent reasons over real
evidence (symbols, imports, reverse dependencies) rather than guessing.
"""

from __future__ import annotations

import ast
import re
from collections import Counter

STOP = set("""a an the and or to of in on for with at by is are be as it this that from not no can
must should never only so i we want they their order orders new any""".split())


def _words(text: str) -> list[str]:
    parts = re.findall(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+", text)
    return [w.lower() for w in parts if len(w) > 2 and w.lower() not in STOP]


def index_repo(files: dict[str, str]) -> list[dict]:
    index = []
    for path, src in files.items():
        if not path.endswith(".py") or path.endswith("__init__.py"):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        symbols, imports = [], []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                symbols.append(node.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
            elif isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
        index.append({
            "path": path,
            "kind": "test" if path.startswith("tests/") else "source",
            "service": path.split("/")[1] if path.startswith("services/") else None,
            "docstring": (ast.get_docstring(tree) or "").split("\n")[0],
            "symbols": symbols,
            "imports": imports,
            "loc": src.count("\n") + 1,
        })
    by_module = {e["path"][:-3].replace("/", "."): e["path"] for e in index}
    for e in index:
        e["imported_by"] = [o["path"] for o in index if any(by_module.get(m) == e["path"] for m in o["imports"])]
    return index


def rank_components(requirement: str, index: list[dict], top: int = 6) -> list[dict]:
    req = Counter(_words(requirement))
    ranked = []
    for e in index:
        if e["kind"] == "test":
            continue
        bag = Counter(_words(" ".join([e["path"], e["docstring"], *e["symbols"]])))
        score = sum(min(req[w], bag[w]) for w in bag)
        # Boost files that callers depend on (central logic) when they already match.
        score += 0.5 * len(e["imported_by"]) if score else 0
        ranked.append({**e, "score": round(score, 2)})
    ranked.sort(key=lambda e: (-e["score"], e["path"]))
    return ranked[:top]
