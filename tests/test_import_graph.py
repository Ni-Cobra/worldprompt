"""The package's module import graph must stay acyclic, lazy imports included."""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "worldprompt_core"
MODULES = sorted(path.stem for path in PACKAGE.glob("*.py") if path.stem != "__init__")


def imports_of(module):
    """Sibling modules imported anywhere in the file (function-local imports count)."""
    found = set()
    for node in ast.walk(ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.split(".")[0])
            else:  # from . import a, b
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name.split(".")[1] for alias in node.names if alias.name.startswith("worldprompt_core."))
    return {name for name in found if name in MODULES and name != module}


def find_cycle(graph):
    state, stack = {}, []

    def visit(node):
        state[node] = "open"
        stack.append(node)
        for target in sorted(graph[node]):
            if state.get(target) == "open":
                return stack[stack.index(target):] + [target]
            if target not in state and (cycle := visit(target)):
                return cycle
        stack.pop()
        state[node] = "done"
        return None

    for node in sorted(graph):
        if node not in state and (cycle := visit(node)):
            return cycle
    return None


def test_module_import_graph_is_acyclic():
    graph = {module: imports_of(module) for module in MODULES}
    assert find_cycle(graph) is None, " -> ".join(find_cycle(graph))


def test_leaf_modules_import_nothing_above_them():
    allowed = {"block_state": {"limits"}, "limits": set(), "agent_types": {"limits"},
               "block_registry": {"block_state", "limits"}, "wsl": {"agent_types"}}
    for module, expected in allowed.items():
        assert imports_of(module) <= expected, module


@pytest.mark.parametrize("module", MODULES)
def test_each_module_imports_on_its_own(module):
    # A fresh interpreter catches import-order dependencies that a shared test process hides.
    done = subprocess.run([sys.executable, "-c", f"import worldprompt_core.{module}"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
