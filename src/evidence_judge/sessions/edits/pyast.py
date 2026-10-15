"""What Python test code does to the suite: rebinding, runner patches, exit hooks."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable
from typing import Final

_TEST_MODULES: Final = re.compile(
    r"^(?:pytest|_pytest|unittest|mock|hypothesis|faker|freezegun|responses|respx|"
    r"pytest_\w+|tests?|conftest|test_\w+|\w+_test)(?:\.|$)"
)


_FIXTURE_SCOPES: Final = frozenset({"session", "module", "package", "class"})


def _imported(tree: ast.Module) -> dict[str, str]:
    """Local names bound by the module's imports, to the module or object they name, leaving
    out test tooling and test modules."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _TEST_MODULES.match(alias.name):
                    names[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _TEST_MODULES.match(node.module):
                continue
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(node: ast.expr) -> str:
    try:
        return ast.unparse(node)
    except (ValueError, TypeError):  # pragma: no cover - unparse handles every parsed node
        return "?"


def _sys_modules(node: ast.expr) -> bool:
    return isinstance(node, ast.Subscript) and _dotted(node.value) == "sys.modules"


def _loads_module(node: ast.expr) -> ast.expr | None:
    """The module-name argument of `sys.modules[...]`, `importlib.import_module(...)` or
    `__import__(...)`."""
    if isinstance(node, ast.Subscript) and _sys_modules(node):
        return node.slice
    if isinstance(node, ast.Call) and node.args:
        func = _dotted(node.func)
        if func.endswith(("import_module", "__import__")):
            return node.args[0]
    return None


def _reaches(node: ast.expr, names: dict[str, str], matches: Callable[[str], bool]) -> bool:
    """`node` is, or is an attribute of, a module in `names` or one loaded by name that
    `matches` (a name the code computes counts)."""
    while True:
        if isinstance(node, ast.Name):
            return node.id in names
        if (key := _loads_module(node)) is not None:
            literal = _string(key)
            return matches(literal) if literal is not None else _dotted(key) != "__name__"
        if isinstance(node, ast.Attribute | ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Call) and _dotted(node.func) == "getattr" and node.args:
            node = node.args[0]
        else:
            return False


def _bind(tree: ast.AST, names: dict[str, str], matches: Callable[[str], bool]) -> None:
    """Add to `names` the variables bound to a module in it (`rt = sys.modules["_pytest"]`)."""
    for _ in range(3):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _reaches(node.value, names, matches):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in names:
                        names[target.id] = _dotted(node.value)


def _patches(
    body: list[ast.stmt],
    names: dict[str, str],
    monkeypatch: bool,
    matches: Callable[[str], bool],
) -> list[str]:
    """Rebindings of imported code in `body`: attribute assignments, `setattr`, writes to
    `sys.modules`, and (inside fixtures) `monkeypatch.setattr`/`mock.patch(...).start()`."""
    found: list[str] = []
    for stmt in body:
        for node in ast.walk(stmt):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if _sys_modules(target):
                    found.append(f"writes {_dotted(target)}")
                elif isinstance(target, ast.Attribute) and _reaches(target, names, matches):
                    found.append(f"rebinds {_dotted(target)}")
            if not isinstance(node, ast.Call):
                continue
            func = _dotted(node.func)
            first = node.args[0] if node.args else None
            if func == "setattr" and len(node.args) >= 2 and _reaches(node.args[0], names, matches):
                attr = node.args[1]
                shown = attr.value if isinstance(attr, ast.Constant) else _dotted(attr)
                found.append(f"rebinds {_dotted(node.args[0])}.{shown}")
            elif func.endswith("sys.modules.__setitem__") or func == "sys.modules.update":
                found.append("writes sys.modules")
            elif monkeypatch and func.endswith(".setattr") and first is not None:
                named = isinstance(first, ast.Constant) and isinstance(first.value, str)
                target = _dotted(first).strip("'\"")
                if _reaches(first, names, matches) or (named and not _TEST_MODULES.match(target)):
                    found.append(f"patches {target}")
            elif (
                monkeypatch
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "start"
                and isinstance(node.func.value, ast.Call)
                and _dotted(node.func.value.func).endswith("patch")
            ):
                found.append(f"starts {_dotted(node.func.value)}")
    return found


def _wide_fixture(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A fixture that applies beyond one test: autouse, or scoped to a module or session."""
    for decorator in func.decorator_list:
        if not isinstance(decorator, ast.Call) or not _dotted(decorator.func).endswith("fixture"):
            continue
        for keyword in decorator.keywords:
            value = keyword.value
            if keyword.arg == "autouse" and isinstance(value, ast.Constant) and value.value:
                return True
            if (
                keyword.arg == "scope"
                and isinstance(value, ast.Constant)
                and value.value in _FIXTURE_SCOPES
            ):
                return True
    return False


def _not_test_module(module: str) -> bool:
    return _TEST_MODULES.match(module) is None


def rebinding_findings(source: str, name: str) -> list[str]:
    """Ways a test module rewrites the code under test for every test: at import (an attribute
    assignment or `setattr` on an imported module, a `sys.modules` entry) or in an autouse,
    module- or session-scoped fixture (`monkeypatch.setattr`, `mock.patch(...).start()`)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    names = _imported(tree)
    _bind(tree, names, _not_test_module)
    found: list[str] = []
    top = [
        s
        for s in tree.body
        if not isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]
    found.extend(_patches(top, names, False, _not_test_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            _wide_fixture(node) or node.name.startswith(("pytest_", "setUpModule", "setup_module"))
        ):
            found.extend(_patches(node.body, names, True, _not_test_module))
    return [
        f"{what} in {name}, so every test runs against code the test module replaced"
        for what in dict.fromkeys(found)
    ]


_OPENERS: Final = re.compile(
    r"^(?:open|read_text|read_bytes|load|loadtxt|genfromtxt|fromfile|joinpath|files|"
    r"Path|PurePath|PosixPath|read_\w+|open_text|open_binary|path|imread|safe_load)$"
)


_DATA_LITERAL: Final = re.compile(r"^[\w./@+-]*(?:/|\.[A-Za-z0-9]{1,8})$")


def _string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _pieces(node: ast.AST) -> list[str]:
    """The string pieces of a path built with `/` (`HERE / "data" / "cases.json"`) or
    `os.path.join`/`joinpath`, in order."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [*_pieces(node.left), *_pieces(node.right)]
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in ("join", "joinpath"):
            return [piece for arg in node.args for piece in _pieces(arg)]
    value = _string(node)
    return [value.strip().strip("/")] if value is not None and value.strip() else []


def _literals(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if (value := _string(node)) is not None:
            candidates.append(value.strip())
        elif isinstance(node, ast.BinOp | ast.Call) and len(pieces := _pieces(node)) > 1:
            candidates.append("/".join(pieces))
        found.update(
            c for c in candidates if 0 < len(c) < 200 and _DATA_LITERAL.match(c) is not None
        )
    return found


def opened_paths(source: str) -> set[str]:
    """Path-like string literals anywhere in a test module, and paths it builds from pieces
    (`Path(__file__).parent / "data" / "cases.json"`, `os.path.join(HERE, "data", "x.json")`):
    the files it may read as data. A source that does not parse as a whole (several files `cat`
    together) is read line by line."""
    try:
        return _literals(ast.parse(source))
    except (SyntaxError, ValueError):
        pass
    found: set[str] = set()
    for line in source.splitlines():
        try:
            found |= _literals(ast.parse(line.strip()))
        except (SyntaxError, ValueError):
            continue
    return found


def imported_names(source: str) -> dict[str, set[str] | None]:
    """Modules a test module imports, outside test tooling, and the names it takes from each:
    `from m import A` takes A, `import m` takes each `m.A` it reads, `from m import *` takes
    every name (None)."""
    try:
        return _imported_names(ast.parse(source))
    except (SyntaxError, ValueError):
        pass
    merged: dict[str, set[str] | None] = {}
    for line in source.splitlines():
        try:
            found = _imported_names(ast.parse(line.strip()))
        except (SyntaxError, ValueError):
            continue
        for module, names in found.items():
            known = merged.get(module, set())
            merged[module] = None if names is None or known is None else known | names
    return merged


def _imported_names(tree: ast.Module) -> dict[str, set[str] | None]:
    taken: dict[str, set[str] | None] = {}
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _TEST_MODULES.match(node.module):
                continue
            names = {a.name for a in node.names}
            known = taken.get(node.module, set())
            taken[node.module] = None if "*" in names or known is None else known | names
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not _TEST_MODULES.match(alias.name):
                    aliases[alias.asname or alias.name] = alias.name
                    taken.setdefault(alias.name, set())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and (module := aliases.get(_dotted(node.value))):
            names = taken.get(module)
            if names is not None:
                names.add(node.attr)
    return taken


def _module_constants(source: str) -> dict[str, str] | None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    out: dict[str, str] = {}
    for stmt in tree.body:
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets = [stmt.target]
        value = getattr(stmt, "value", None)
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name) and value is not None:
                    out[name.id] = ast.dump(value)
    return out


def changed_constants(
    removed: list[str] | None, added: list[str] | None, texts: tuple[str, str] | None
) -> set[str] | None:
    """Module-level names whose assigned value the edit changed (`EXPECTED = 5` to `-1`), or
    None when that cannot be told."""
    if texts is not None:
        before, after = _module_constants(texts[0]), _module_constants(texts[1])
        if before is not None and after is not None:
            return {n for n in before.keys() | after.keys() if before.get(n) != after.get(n)}
    if removed is None or added is None:
        return None
    found: set[str] = set()
    for line in [*removed, *added]:
        if line[:1].isspace():
            continue
        found.update((_module_constants(line) or {}).keys())
    return found


_RUNNER_IMPORTS: Final = re.compile(r"^(?:_pytest|pytest|py\.test|nose2?|pluggy)(?:\.|$)")


_RUNNER_MODULES: Final = re.compile(
    r"^(?:_pytest|pytest|unittest|nose2?|doctest|pluggy|builtins|__builtin__)(?:\.|$)"
)


def _runner_module(module: str) -> bool:
    return _RUNNER_MODULES.match(module) is not None


def runner_patches(source: str, name: str) -> list[str]:
    """Ways code outside the tests reaches into the test runner: importing pytest's internals
    (or pytest, nose, pluggy), assigning attributes of `unittest`, `doctest`, the runner or
    `builtins`, or writing `sys.modules`. Code under test has no business doing either; doing
    it can make every test pass (`_pytest.python.Function.runtest = lambda self: None`)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found: list[str] = []
    names: dict[str, str] = {"__builtins__": "builtins"}
    for node in ast.walk(tree):
        modules: list[tuple[str, str]] = []
        if isinstance(node, ast.Import):
            modules = [(a.asname or a.name.split(".")[0], a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [(a.asname or a.name, f"{node.module}.{a.name}") for a in node.names]
        if isinstance(node, ast.Call) and (key := _loads_module(node)) is not None:
            literal = _string(key)
            if literal is not None and _RUNNER_IMPORTS.match(literal):
                found.append(f"imports {literal}")
        for local, module in modules:
            if _RUNNER_IMPORTS.match(module):
                found.append(f"imports {module}")
            if _RUNNER_MODULES.match(module):
                names[local] = module
    _bind(tree, names, _runner_module)
    found.extend(_patches(list(tree.body), names, False, _runner_module))
    return [
        f"{what} in {name}, code outside the tests that can change how the tests run"
        for what in dict.fromkeys(found)
    ]


_SUMMARY_TEXT: Final = re.compile(
    r"(?:\d+|%d|%s|\{\w*\})\s+(?:passed|passing)\b|\btest result:\s*ok\b|"
    r"\bRan\s+(?:\d+|%d|%s|\{\w*\})\s+tests?\b|\bTests:\s+\S+\s+passed",
    re.IGNORECASE,
)
"""Text shaped like a passing test summary (`3 passed in 0.02s`, `Ran 3 tests`)."""


_EXIT_CALLS: Final = frozenset({"sys.exit", "exit", "quit", "os._exit", "os.abort"})


_HOOKS: Final = frozenset(
    {"sys.excepthook", "sys.unraisablehook", "threading.excepthook", "sys.displayhook"}
)


def _strings(node: ast.AST) -> Iterable[str]:
    """The text of string constants and f-strings (a placeholder as `{}`) under `node`."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.JoinedStr):
            yield "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
                for v in sub.values
            )
        elif (text := _string(sub)) is not None:
            yield text


def exit_hooks(source: str, name: str) -> list[str]:
    """Ways code outside the tests can end the test process on its own terms or print a
    runner's verdict: registering an `atexit` handler, calling `os._exit` (anywhere) or
    `sys.exit`/`exit()` at import, raising `SystemExit` at import, replacing
    `sys.excepthook`, or holding text shaped like a runner's summary (`3 passed in 0.02s`).
    Such code can print a passing summary after the real one and exit 0."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found: list[str] = []
    top = [
        s
        for s in tree.body
        if not isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and not _main_guard(s)
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = _dotted(node.func)
            if func.endswith("atexit.register") or (func == "register" and _from_atexit(tree)):
                found.append("registers an atexit handler")
            elif func in ("os._exit", "_exit", "os.abort"):
                found.append(f"calls {func}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if any(_dotted(d).endswith("atexit.register") for d in node.decorator_list):
                found.append("registers an atexit handler")
        elif isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found.extend(f"replaces {t}" for t in map(_dotted, targets) if t in _HOOKS)
    for stmt in top:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and _dotted(node.func) in _EXIT_CALLS:
                found.append(f"calls {_dotted(node.func)} at import")
            elif isinstance(node, ast.Raise) and node.exc is not None:
                raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                if _dotted(raised) == "SystemExit":
                    found.append("raises SystemExit at import")
    texts = [t for t in _strings(tree) if _SUMMARY_TEXT.search(t)]
    if texts:
        found.append(f"holds test-summary text {texts[0].strip()[:40]!r}")
    return [
        f"{what} in {name}, code outside the tests that can end the test run or print its summary"
        for what in dict.fromkeys(found)
    ]


def _main_guard(stmt: ast.stmt) -> bool:
    """`if __name__ == "__main__":`, which does not run when the tests import the module."""
    return isinstance(stmt, ast.If) and "__main__" in _dotted(stmt.test)


def _from_atexit(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.ImportFrom) and n.module == "atexit" for n in ast.walk(tree))


_COMPARISONS: Final = frozenset({"__eq__", "__ne__", "__hash__", "__bool__"})


def _constant_body(body: list[ast.stmt]) -> bool:
    stmts = [b for b in body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
    return (
        len(stmts) == 1
        and isinstance(stmts[0], ast.Return)
        and isinstance(stmts[0].value, ast.Constant)
    )


def _ignores_self(args: ast.arguments, body: list[ast.stmt] | ast.expr) -> bool:
    """An equality method whose code never reads the object it is called on (`self`, or
    `super()`): its answer cannot depend on the value being compared."""
    params = [*args.posonlyargs, *args.args]
    if not params:
        return True
    me = params[0].arg
    for part in body if isinstance(body, list) else [body]:
        for node in ast.walk(part):
            if isinstance(node, ast.Name) and node.id == me:
                return False
            if isinstance(node, ast.Call) and _dotted(node.func) == "super":
                return False
    return True


def constant_comparisons(source: str, name: str) -> list[str]:
    """`__eq__`, `__ne__`, `__hash__` or `__bool__` defined to return a constant, or `__eq__` or
    `__ne__` that never reads the object itself (`return other is not None`): an object that
    equals everything makes `assert add(2, 3) == 5` pass whatever `add` computes."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    constant: list[str] = []
    blind: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                what = f"{node.name}.{stmt.name}"
                if stmt.name in _COMPARISONS and _constant_body(stmt.body):
                    constant.append(what)
                elif stmt.name in ("__eq__", "__ne__") and _ignores_self(stmt.args, stmt.body):
                    blind.append(what)
            elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Lambda):
                lam = stmt.value
                for target in stmt.targets:
                    if not isinstance(target, ast.Name) or target.id not in _COMPARISONS:
                        continue
                    what = f"{node.name}.{target.id}"
                    if isinstance(lam.body, ast.Constant):
                        constant.append(what)
                    elif target.id in ("__eq__", "__ne__") and _ignores_self(lam.args, lam.body):
                        blind.append(what)
    return [
        *(
            f"defines {what} returning a constant in {name}, so its objects can compare equal "
            "to any expected value"
            for what in dict.fromkeys(constant)
        ),
        *(
            f"defines {what} without reading the object itself in {name}, so its objects can "
            "compare equal to any expected value"
            for what in dict.fromkeys(blind)
        ),
    ]
