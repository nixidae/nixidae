"""The evaluator side: one `EvalState`, the base configuration, the edits.

An `Engine` is bound to the thread that made it, because its `EvalState`
is. `EngineThread` gives it one, with the stack the evaluator needs.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Final

from huggorm import Derivation, EvalState, Store, Value
from huggorm.errors import NixError

from .model import (
    Cascade,
    Change,
    Definition,
    DrvDiff,
    Edit,
    Group,
    Kind,
    Loaded,
    OptionPath,
    OptionView,
    Path,
    Quiet,
    blamed_path,
    changed_attrs,
    changed_paths,
    drv_file,
    edit_file,
    edited_path,
    format_path,
    is_drv_file,
    nix_string,
    overlaps,
    pair_drvs,
)
from .walk import (
    SKIP,
    UNEVALUATED,
    Walker,
    count_forced,
    drv_string,
    error_text,
    fully_forced,
    instantiated,
    show_forced,
    show_lazy,
)

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
    file = nix_string(edit_file(edit.path))
    return (
        "{ lib, config, ... }: with config._module.args; "
        f"{{ _file = {file}; config = lib.setAttrByPath [ {path} ] ({edit.expr}); }}"
    )


class Engine:
    def __init__(self, source: Source, store: str = "auto") -> None:
        self.store = Store(store)
        self.state = EvalState(self.store)
        self.source = source
        self._extend = self.state.eval_expr(EXTEND, "/")
        self.base: Value | None = None
        self.ext: Value | None = None
        self.base_drvs: frozenset[str] = frozenset()
        # Output path -> `.drv` path, for each side.
        self.base_derivers: dict[str, str] = {}
        self.ext_derivers: dict[str, str] = {}
        self.base_recorded_drvs: frozenset[str] = frozenset()
        self._options: dict[OptionPath, bool] = {}
        # The last walk met a failure, so it left parts of the new side
        # unforced that the base forced.
        self._walk_failed = False
        # Options `explain` forced as deep as the base since the last apply.
        self._forced_like: set[OptionPath] = set()

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
        forcing: Path = ()
        try:
            base = self.state.eval_expr(self.source.expr, self.source.base_dir)
            self._sel(base, ("config",))
            for forcing in self.source.force:
                self._sel(base, forcing)
        except NixError as err:
            where = f"{format_path(forcing)}: " if forcing else ""
            return Loaded(time.monotonic() - start, 0, where + error_text(err))
        finally:
            self.state.set_record_derivations(False)
        self.base_drvs = frozenset(self.state.recorded_out_paths())
        self.base_derivers = self._derivers(self.base_drvs)
        self.base_recorded_drvs = frozenset(self.base_derivers.values())
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
        self._forced_like.clear()
        if not enabled:
            return Cascade()
        modules = self.state.make_list()
        for edit in enabled:
            try:
                # Alone, so a syntax error points into the edit and not into
                # the module around it. `with` defers each free name.
                self.state.parse_expr(f"with {{ }};\n{edit.expr}", "/")
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
        drvs = set(self.state.recorded_out_paths())
        self.ext_derivers = self._derivers(drvs)
        self.state.clear_recorded_derivations()
        base_drvs = set(self.base_drvs)
        # A `.drv` whose output a coercion recorded is listed by that output.
        listed_old = self.base_recorded_drvs
        listed_new = set(self.ext_derivers.values())
        for old, new in walker.rebuilt:
            # Keyed as the walk keyed it: the base forced only `drvPath`
            # of a top level, and reading its `outPath` would change the
            # key of every later cascade.
            old_key = instantiated(old)
            new_key = drv_string(self.state, new, "outPath" if old_key and not is_drv_file(old_key) else "drvPath")
            if not old_key or not new_key or old_key in listed_old or new_key in listed_new:
                continue
            base_drvs.add(old_key)
            drvs.add(new_key)
            old_drv = old_key if is_drv_file(old_key) else drv_string(self.state, old, "drvPath")
            new_drv = drv_string(self.state, new, "drvPath")
            if old_drv and new_drv:
                self.base_derivers.setdefault(old_key, old_drv)
                self.ext_derivers[new_key] = new_drv
        edited = [e.path for e in enabled]
        changes = [self._blame(c, walker.failures.get(c.new, []), edited) for c in walker.result()]
        clean = not walker.failures
        self._walk_failed = not clean
        quiet = self._quiet(enabled, changes) if clean else ()
        added = tuple(sorted(drvs - base_drvs)) if clean else ()
        removed = tuple(sorted(self._unreached(base_drvs - drvs, walker.kept))) if clean else ()
        return Cascade(
            groups=self._group(changes) + quiet,
            seconds=time.monotonic() - start,
            visited=walker.visited,
            drvs_added=added,
            drvs_removed=removed,
            drvs=pair_drvs(added, removed, self._likeness),
            derivers=self.base_derivers | self.ext_derivers,
        )

    def _unreached(self, gone: set[str], kept: list[str]) -> set[str]:
        """The base derivations the new side no longer reaches. Nix records
        a derivation when it instantiates one, and a value the new side
        shares with the base (`pkgs` through `specialArgs`) is instantiated
        once: its inputs are never recorded again. So the new side reaches
        whatever the closure of the derivations it rebuilt or kept holds."""
        if not gone:
            return gone
        roots = set(self.ext_derivers.values())
        roots |= {drv for key in kept if (drv := drv_file(key, self.base_derivers))}
        if not roots:
            return gone
        closure = self.store.compute_fs_closure([self.store.parse_store_path(r) for r in roots])
        reached = {self.store.print_store_path(p) for p in closure}
        return {key for key in gone if drv_file(key, self.base_derivers) not in reached}

    def _likeness(self, old_key: str, new_key: str) -> int:
        """How many attributes and inputs two derivations share."""
        old_path = drv_file(old_key, self.base_derivers)
        new_path = drv_file(new_key, self.ext_derivers)
        if old_path is None or new_path is None:
            return 0
        try:
            old = self.store.read_derivation(self.store.parse_store_path(old_path))
            new = self.store.read_derivation(self.store.parse_store_path(new_path))
        except NixError:
            return 0
        shared_attrs = self._drv_attrs(old).items() & self._drv_attrs(new).items()
        return len(shared_attrs) + len(set(old.input_drvs()) & set(new.input_drvs()))

    def _derivers(self, outs: Iterable[str]) -> dict[str, str]:
        """The `.drv` of each recorded output path. Recording coerced each
        one, which instantiated it: its `drvPath` is already known."""
        found: dict[str, str] = {}
        for out in outs:
            drv = self.state.recorded_derivation(out)
            if drv is not None and (path := drv_string(self.state, drv, "drvPath")):
                found[out] = path
        return found

    def _full(self, names: Iterable[str]) -> set[str]:
        """`input_drvs` names each `.drv` without the store directory."""
        return {f"{self.store.store_dir()}/{name}" for name in names}

    def _drv_attrs(self, drv: Derivation) -> dict[str, str]:
        attrs = {"builder": drv.builder(), "args": " ".join(drv.args()), "system": drv.system()}
        # An output's variable holds its path, which differs on every
        # rebuild: a result of the change, not a cause.
        outputs = drv.outputs().keys()
        attrs |= {f"env.{k}": v for k, v in drv.env().items() if k not in outputs}
        if (structured := drv.structured_attrs()) is not None:
            # Values of any JSON shape: shown as text, compared as text.
            for key, value in json.loads(structured).items():
                attrs[f"attrs.{key}"] = value if isinstance(value, str) else json.dumps(value, indent=2)
        return attrs

    def drv_diff(self, old_key: str, new_key: str) -> DrvDiff:
        """Each side is a listed output path, or a `.drv` path."""
        old_path = drv_file(old_key, self.base_derivers)
        new_path = drv_file(new_key, self.ext_derivers)
        if old_path is None or new_path is None:
            return DrvDiff(error="modiff did not see this derivation instantiated on both sides")
        try:
            old = self.store.read_derivation(self.store.parse_store_path(old_path))
            new = self.store.read_derivation(self.store.parse_store_path(new_path))
        except NixError as err:
            return DrvDiff(old_path, new_path, error=error_text(err))
        return DrvDiff(
            old_path,
            new_path,
            inputs=changed_paths(self._full(old.input_drvs()), self._full(new.input_drvs()), self._likeness),
            sources=changed_paths(
                {str(p.to_string()) for p in old.input_srcs()}, {str(p.to_string()) for p in new.input_srcs()}
            ),
            attrs=changed_attrs(self._drv_attrs(old), self._drv_attrs(new)),
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

    def _blame(self, change: Change, failed: Iterable[Path], edited: Iterable[Path]) -> Change:
        """A failure under the option its message names, else under the
        edit it failed in. The walk meets a conflict on
        `system.stateVersion` first in `assertions`, which sorts first and
        reads nearly every option."""
        if change.kind is not Kind.FAILED:
            return change
        blamed = blamed_path(change.new)
        # Inside one option a message names merge positions, such as
        # `checks."[definition 3-entry 1]"`, which are no path.
        if blamed and self.option_of(blamed) not in {(), self.option_of(change.path)}:
            return replace(change, path=blamed, via=change.path)
        own = edited_path(failed, edited)
        if own is None or own == change.path:
            return change
        return replace(change, path=own, via=change.path)

    def _group(self, changes: list[Change]) -> tuple[Group, ...]:
        by_option: dict[OptionPath, list[Change]] = {}
        for change in changes:
            by_option.setdefault(self.option_of(change.path), []).append(change)
        return tuple(Group(option, tuple(group)) for option, group in by_option.items())

    def _quiet(self, edits: list[Edit], changes: list[Change]) -> tuple[Group, ...]:
        """A group for each edit that changed nothing, with the reason."""
        if self.base is None:
            return ()
        config = self._sel(self.base, ("config",))
        out: list[Group] = []
        for edit in edits:
            if any(overlaps(edit.path, change.path) for change in changes):
                continue
            option = self.option_of(edit.path)
            v = self._peek(config, edit.path)
            if option and not self._defines(edit, option):
                reason = Quiet.DROPPED
            elif changes:
                reason = Quiet.ELSEWHERE
            elif v is None or v.type_name() in UNEVALUATED:
                reason = Quiet.UNREAD
            else:
                reason = Quiet.SAME if fully_forced(v) else Quiet.PARTIAL
            out.append(Group(option or edit.path, (), reason))
        return tuple(out)

    def _defines(self, edit: Edit, option: OptionPath) -> bool:
        """Whether the edit survives among the option's definitions. The
        module system drops a definition that a higher priority outranks."""
        if self.ext is None:
            return False
        file = edit_file(edit.path)
        try:
            defs = self._sel(self.ext, ("options", *option, "definitionsWithLocations"))
            return any(str(self._sel(defs, (i, "file")).string_value()) == file for i in range(defs.length()))
        except NixError:
            return True

    def freeform(self) -> bool:
        """The top level takes paths no option declares. The walk skips
        `_module`, so reading it here widens no cascade."""
        if self.base is None:
            return False
        try:
            # `evalModules` returns `_module` beside `config`, not in it.
            kind = self._sel(self.base, ("_module", "freeformType")).type_name()
        except NixError:
            return False
        return kind != "null"

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

    def _definitions(self, root: Value | None, option: OptionPath) -> list[tuple[str, Value]]:
        """Each definition's file and value, the value as far as it is
        evaluated."""
        if root is None:
            return []
        defs = self._sel(root, ("options", *option, "definitionsWithLocations"))
        out: list[tuple[str, Value]] = []
        for i in range(defs.length()):
            item = self._sel(defs, (i,))
            file = self._sel(item, ("file",))
            out.append((str(file.string_value()), item.get("value")))
        return out

    def _shown_new(self, v: Value) -> str:
        """A new value that the base evaluated: a thunk left here failed,
        and forcing it again raises its error."""
        if v.type_name() in UNEVALUATED:
            self.state.force(v)
        return show_lazy(v)

    def _force_like_base(self, olds: list[tuple[str, Value]], news: list[tuple[str, Value]]) -> None:
        """Force each new definition as far as the base forced the old one
        from the same file."""
        walker = Walker(self.state)
        left = list(olds)
        for file, new in news:
            match = next((pair for pair in left if pair[0] == file), None)
            if match is not None:
                left.remove(match)
                walker.force_like(match[1], new)

    def _priority(self, root: Value | None, option: OptionPath) -> int | None:
        if root is None:
            return None
        node = self._sel(root, ("options", *option))
        if not node.has("highestPrio"):
            return None
        prio = self._sel(node, ("highestPrio",))
        return prio.integer() if prio.type_name() == "int" else None

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
            # The default is a definition, so forcing it forces config. That
            # is safe only in the extended evaluation, which the next apply
            # replaces; the base must stay as its evaluation left it.
            default = show_lazy(node.get("default"))
            if self.ext is not None:
                try:
                    default = show_forced(self._sel(node, ("default",)))
                except NixError as err:
                    errors.append(error_text(err))

        def current(root: Value | None) -> str:
            if root is None:
                return ""
            v = self._peek(self._sel(root, ("config",)), option)
            return "«thunk»" if v is None else show_lazy(v)

        # Listing definitions forces each one and its mkIf condition
        # (`dischargeProperties`). Merging the value did that already, so
        # the base is safe to read only where its value was evaluated.
        base_value = None if self.base is None else self._peek(self._sel(self.base, ("config",)), option)
        base_read = base_value is not None and base_value.type_name() not in UNEVALUATED
        olds: list[tuple[str, Value]] = []
        news: list[tuple[str, Value]] = []
        prio_old = prio_new = None
        new = current(self.ext)
        try:
            if base_read:
                olds = self._definitions(self.base, option)
                prio_old = self._priority(self.base, option)
            news = self._definitions(self.ext, option)
            prio_new = self._priority(self.ext, option)
        except NixError as err:
            errors.append(error_text(err))
        if base_read and base_value is not None and self.ext is not None and self._walk_failed:
            # 0.8 s for the 954 definitions of NixOS `assertions`: only
            # where a failure stopped the walk, once per apply.
            new_value = self._peek(self._sel(self.ext, ("config",)), option)
            if option not in self._forced_like:
                self._forced_like.add(option)
                self._force_like_base(olds, news)
                if new_value is not None:
                    Walker(self.state).force_like(base_value, new_value)
            try:
                new = current(self.ext) if new_value is None else self._shown_new(new_value)
            except NixError as err:
                new = f"«error: {error_text(err).splitlines()[0]}»"
        return OptionView(
            path=option,
            type_description=text(("type", "description")),
            description=text(("description",)) if node.has("description") else "",
            declarations=tuple(declarations),
            default=default,
            old=current(self.base),
            new=new,
            defs_old=tuple(Definition(file, show_lazy(v)) for file, v in olds),
            defs_new=tuple(Definition(file, show_lazy(v)) for file, v in news),
            base_read=base_read,
            prio_old=prio_old,
            prio_new=prio_new,
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
