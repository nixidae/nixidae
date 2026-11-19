"""The evaluator side: one `EvalState`, the base configuration, the edits.

An `Engine` is bound to the thread that made it, because its `EvalState`
is. `EngineThread` gives it one, with the stack the evaluator needs.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from huggorm import EvalState, Store, Value
from huggorm.errors import NixError

from .model import (
    Cascade,
    Change,
    Definition,
    Edit,
    Group,
    Loaded,
    OptionPath,
    OptionView,
    Path,
    format_path,
    nix_string,
)
from .walk import SKIP, Walker, count_forced, error_text, show_forced, show_lazy

# Nix's CLI sets this stack in `main()`; an embedder's thread keeps 8 MiB, and
# the default max-call-depth of 10000 needs about 27 MB (huggorm's runtime,
# `EVAL_STACK`).
EVAL_STACK: Final = 60 * 1024 * 1024

# `apply_auto` needs formals. The `extendModules` that NixOS's
# eval-config.nix hands back declares none, so the call goes through this.
EXTEND: Final = "{ base, modules }: base.extendModules { inherit modules; }"

DEMO_MODULE: Final = """{
  boot.loader.grub.enable = false;
  fileSystems."/" = { device = "/dev/null"; fsType = "ext4"; };
  system.stateVersion = "25.11";
}"""


@dataclass(frozen=True, slots=True)
class Source:
    """An expression that evaluates to an `evalModules` result, and the
    paths under it to force: what "evaluated" means for this configuration."""

    expr: str
    force: tuple[Path, ...]
    base_dir: str = "/"


def nixos_source(configuration: str | None, nixpkgs: str) -> Source:
    module = configuration if configuration is not None else DEMO_MODULE
    # `<nixpkgs>/nixos` is not a path: a lookup path takes the rest inside.
    eval_config = (
        f"{nixpkgs[:-1]}/nixos/lib/eval-config.nix>"
        if nixpkgs.startswith("<") and nixpkgs.endswith(">")
        else f"{nixpkgs}/nixos/lib/eval-config.nix"
    )
    expr = f"import {eval_config} {{ modules = [ ({module}) ]; }}"
    return Source(expr=expr, force=(("config", "system", "build", "toplevel", "drvPath"),))


def edit_module(edit: Edit) -> str:
    """The edit as a module. `with config._module.args` puts `pkgs` and the
    other module arguments in scope; `with` never shadows `lib`. `_file`
    names the edit in `definitionsWithLocations`."""
    path = " ".join(nix_string(p) for p in edit.path)
    file = nix_string(f"modiff edit: {format_path(edit.path)}")
    return (
        "{ lib, config, ... }: with config._module.args; "
        f"{{ _file = {file}; config = lib.setAttrByPath [ {path} ] ({edit.expr}); }}"
    )


class Engine:
    def __init__(self, source: Source, store: str = "auto") -> None:
        self.state = EvalState(Store(store))
        self.source = source
        self._extend = self.state.eval_expr(EXTEND, "/")
        self.base: Value | None = None
        self.ext: Value | None = None
        self.base_drvs: frozenset[str] = frozenset()
        self._options: dict[OptionPath, bool] = {}

    def _sel(self, v: Value, path: Path) -> Value:
        self.state.force(v)
        for segment in path:
            v = v.at(segment) if isinstance(segment, int) else v.get(segment)
            self.state.force(v)
        return v

    def _peek(self, v: Value, path: Path) -> Value | None:
        """The value at `path` without forcing anything: None where the
        path reaches an unforced value."""
        for segment in path:
            if v.type_name() == "attrs" and isinstance(segment, str) and v.has(segment):
                v = v.get(segment)
            elif v.type_name() == "list" and isinstance(segment, int) and segment < v.length():
                v = v.at(segment)
            else:
                return None
        return v

    def load(self) -> Loaded:
        start = time.monotonic()
        self.state.set_record_derivations(True)
        try:
            base = self.state.eval_expr(self.source.expr, self.source.base_dir)
            self._sel(base, ("config",))
            for path in self.source.force:
                self._sel(base, path)
        except NixError as err:
            return Loaded(time.monotonic() - start, 0, error_text(err))
        finally:
            self.state.set_record_derivations(False)
        self.base_drvs = frozenset(self.state.recorded_out_paths())
        self.state.clear_recorded_derivations()
        self.base = base
        return Loaded(time.monotonic() - start, count_forced(self._sel(base, ("config",))))

    def apply(self, edits: list[Edit]) -> Cascade:
        if self.base is None:
            return Cascade(error="the base configuration did not evaluate")
        start = time.monotonic()
        enabled = [e for e in edits if e.enabled]
        self.ext = None
        self._options.clear()
        if not enabled:
            return Cascade()
        modules = self.state.make_list()
        for edit in enabled:
            try:
                self.state.list_append(modules, self.state.eval_expr(edit_module(edit), "/"))
            except NixError as err:
                return Cascade(error=f"{format_path(edit.path)}: {error_text(err)}")
        args = self.state.make_attrs()
        self.state.attrs_set(args, "base", self.base)
        self.state.attrs_set(args, "modules", modules)
        try:
            ext = self._extend.apply_auto(args)
            econfig = self._sel(ext, ("config",))
        except NixError as err:
            return Cascade(seconds=time.monotonic() - start, error=error_text(err))
        self.ext = ext
        walker = Walker(self.state)
        self.state.set_record_derivations(True)
        try:
            walker.compare(self._sel(self.base, ("config",)), econfig, (), set())
        finally:
            self.state.set_record_derivations(False)
        drvs = frozenset(self.state.recorded_out_paths())
        self.state.clear_recorded_derivations()
        changes = walker.result()
        clean = not walker.failures
        return Cascade(
            groups=self._group(changes),
            seconds=time.monotonic() - start,
            visited=walker.visited,
            drvs_added=tuple(sorted(drvs - self.base_drvs)) if clean else (),
            drvs_removed=tuple(sorted(self.base_drvs - drvs)) if clean else (),
        )

    def _options_root(self) -> Value:
        root = self.ext if self.ext is not None else self.base
        if root is None:
            raise RuntimeError("no configuration loaded")
        return self._sel(root, ("options",))

    def _is_option(self, v: Value) -> bool:
        if v.type_name() != "attrs" or not v.has("_type"):
            return False
        try:
            kind = self._sel(v, ("_type",))
        except NixError:
            return False
        return kind.type_name() == "string" and kind.string_value() == "option"

    def option_of(self, path: Path) -> OptionPath:
        """The longest prefix of `path` that an option declares."""
        node = self._options_root()
        for i, segment in enumerate(path):
            if not isinstance(segment, str):
                return ()
            try:
                self.state.force(node)
            except NixError:
                return ()
            if node.type_name() != "attrs" or not node.has(segment):
                return ()
            node = node.get(segment)
            prefix = tuple(str(s) for s in path[: i + 1])
            if self._is_option(node):
                return prefix
        return ()

    def _group(self, changes: list[Change]) -> tuple[Group, ...]:
        by_option: dict[OptionPath, list[Change]] = {}
        for change in changes:
            by_option.setdefault(self.option_of(change.path), []).append(change)
        return tuple(Group(option, tuple(group)) for option, group in by_option.items())

    def option_paths(self) -> list[OptionPath]:
        found: list[OptionPath] = []

        def walk(v: Value, path: OptionPath) -> None:
            try:
                self.state.force(v)
            except NixError:
                return
            if v.type_name() != "attrs":
                return
            if self._is_option(v):
                found.append(path)
                return
            for i in range(v.size()):
                name = v.name_at(i)
                if name not in SKIP:
                    walk(v.value_at(i), (*path, name))

        walk(self._options_root(), ())
        return found

    def _definitions(self, root: Value | None, option: OptionPath) -> tuple[Definition, ...]:
        if root is None:
            return ()
        defs = self._sel(root, ("options", *option, "definitionsWithLocations"))
        out: list[Definition] = []
        for i in range(defs.length()):
            item = self._sel(defs, (i,))
            file = self._sel(item, ("file",))
            value = show_forced(item.get("value"))
            out.append(Definition(str(file.string_value()), value))
        return tuple(out)

    def explain(self, option: OptionPath) -> OptionView:
        errors: list[str] = []
        node = self._sel(self._options_root(), option)

        def text(path: Path) -> str:
            try:
                v = self._sel(node, path)
            except NixError as err:
                errors.append(error_text(err))
                return ""
            if v.type_name() == "attrs" and v.has("text"):
                v = self._sel(v, ("text",))
            return str(v.string_value()) if v.type_name() == "string" else show_forced(v)

        declarations: list[str] = []
        if node.has("declarationPositions"):
            positions = self._sel(node, ("declarationPositions",))
            for i in range(positions.length()):
                pos = self._sel(positions, (i,))
                file = str(self._sel(pos, ("file",)).string_value())
                line = self._sel(pos, ("line",))
                declarations.append(f"{file}:{line.integer()}" if line.type_name() == "int" else file)
        elif node.has("declarations"):
            decls = self._sel(node, ("declarations",))
            declarations = [str(self._sel(decls, (i,)).string_value()) for i in range(decls.length())]

        default = ""
        if node.has("defaultText"):
            default = text(("defaultText",))
        elif node.has("default"):
            default = text(("default",))

        def current(root: Value | None) -> str:
            if root is None:
                return ""
            v = self._peek(self._sel(root, ("config",)), option)
            return "«thunk»" if v is None else show_lazy(v)

        defs_old: tuple[Definition, ...] = ()
        defs_new: tuple[Definition, ...] = ()
        try:
            defs_old = self._definitions(self.base, option)
            defs_new = self._definitions(self.ext, option)
        except NixError as err:
            errors.append(error_text(err))
        return OptionView(
            path=option,
            type_description=text(("type", "description")),
            description=text(("description",)) if node.has("description") else "",
            declarations=tuple(declarations),
            default=default,
            old=current(self.base),
            new=current(self.ext),
            defs_old=defs_old,
            defs_new=defs_new,
            errors=tuple(errors),
        )


@dataclass(frozen=True, slots=True)
class Crash:
    """A Python error on the engine thread, as text: an exception object
    may hold values that belong to that thread."""

    text: str


class EngineThread:
    """One thread, with the evaluator's stack, that owns an `Engine`."""

    def __init__(self, source: Source, store: str = "auto") -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="modiff-engine")
        with _STACK_LOCK:
            previous = threading.stack_size(EVAL_STACK)
            try:
                self._pool.submit(lambda: None).result()
            finally:
                threading.stack_size(previous)
        self._engine = self._pool.submit(Engine, source, store).result()

    def submit[T](self, job: Callable[[Engine], T]) -> concurrent.futures.Future[T | Crash]:
        def run() -> T | Crash:
            try:
                return job(self._engine)
            except Exception:  # noqa: BLE001 -- crosses the thread as text; the UI shows it
                return Crash(traceback.format_exc())

        return self._pool.submit(run)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# `threading.stack_size` is process-global; hold it only across one spawn.
_STACK_LOCK: Final = threading.Lock()
