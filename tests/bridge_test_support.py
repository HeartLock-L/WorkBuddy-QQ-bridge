"""Load selected owned code for tests without importing the bot framework."""

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
def _plugin_main() -> Path:
    """Locate the plugin in a deployment tree or a repository checkout."""
    for candidate in (
        ROOT / "AstrBot/data/plugins/workbuddy_bridge/main.py",
        ROOT / "workbuddy_bridge/main.py",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "workbuddy_bridge/main.py not found; deploy the plugin first"
    )


def _scripts_dir() -> Path:
    """Locate the maintenance scripts that ship with the plugin."""
    for base in PLUGIN.parents:
        candidate = base / "scripts"
        if (candidate / "diagnostic_evidence.py").is_file():
            return candidate
    return ROOT / "scripts"


PLUGIN = _plugin_main()
SCRIPTS = _scripts_dir()


def load_module(path, name):
    """Load an independent module under an explicit test-only name.

    Args:
        path: Source file to load without importing its parent package.
        name: Unique module name used by annotations and dataclasses.

    Returns:
        A fresh module; callers retain ownership of its mutable test state.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_definitions(
    namespace, *, functions=(), constants=(), methods=(), undecorated_methods=()
):
    """Compile explicit production definitions against caller-owned test fakes.

    Args:
        namespace: Isolated globals populated in place with selected definitions.
        functions: Module-level functions to include in source order.
        constants: Module-level assignments to include in source order.
        methods: Methods from Main to include in a standalone Bridge class.
        undecorated_methods: Methods whose framework decorators must be removed.

    Returns:
        The supplied namespace, including Bridge when methods were requested.

    Raises:
        AssertionError: A requested definition is missing from the source.
    """
    sources = ("bridge_policy.py", "draw_flow.py", "proactive_chat.py", PLUGIN.name)
    nodes = []
    for name in sources:
        path = PLUGIN.with_name(name)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        nodes.extend(tree.body)
    functions, constants, methods = set(functions), set(constants), set(methods)
    found_functions, found_constants, found_methods = set(), set(), set()
    selected = []
    method_body = []
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in functions:
                selected.append(node)
                found_functions.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if names & constants:
                selected.append(node)
                found_constants.update(names & constants)
        elif (
            isinstance(node, ast.ClassDef)
            and node.name in {"Main", "DrawFlow", "ProactiveChat"}
            and methods
        ):
            for method in node.body:
                if (
                    isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and method.name in methods
                ):
                    if method.name in undecorated_methods:
                        method.decorator_list = []
                    method_body.append(method)
                    found_methods.add(method.name)
    if methods:
        selected.append(
            ast.ClassDef(
                name="Bridge",
                bases=[],
                keywords=[],
                decorator_list=[],
                body=method_body,
            )
        )
    missing = (functions - found_functions) | (constants - found_constants)
    missing |= {"Main." + name for name in methods - found_methods}
    if missing:
        raise AssertionError(f"Missing production definitions: {sorted(missing)}")
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(PLUGIN), "exec"), namespace)  # noqa: S102 - owned AST with explicit offline fakes.
    return namespace
