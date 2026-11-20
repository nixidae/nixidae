"""The terminal UI. Everything here runs on Textual's thread; evaluation
runs on the engine's, and comes back as plain data through
`call_from_thread`."""

from __future__ import annotations

import concurrent.futures
import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import ClassVar, Final

import anyio
from rich.console import Group as RichGroup, RenderableType
from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Input, Label, ListItem, ListView, OptionList, Static, Tree
from textual.widgets.tree import TreeNode

from .engine import Crash, Engine, EngineThread
from .model import (
    EDIT_FILE,
    Cascade,
    Change,
    Edit,
    Group,
    Kind,
    Loaded,
    OptionPath,
    OptionView,
    Quiet,
    format_path,
    parse_option_path,
    priority_text,
)

SHORT: Final = 60
MARK: Final = {Kind.CHANGED: "~", Kind.ADDED: "+", Kind.REMOVED: "-", Kind.FAILED: "!"}
STYLE: Final = {Kind.CHANGED: "yellow", Kind.ADDED: "green", Kind.REMOVED: "red", Kind.FAILED: "bold red"}


def short(value: str, text: bool) -> str:
    shown = repr(value) if text else value
    return shown if len(shown) <= SHORT else shown[: SHORT - 1] + "…"


STORE_SOURCE: Final = re.compile(r"/nix/store/[a-z0-9]{32}-[^/\s]+/")


def short_file(text: str) -> str:
    """A path inside a store source, from that source's root: the hash is
    noise to a reader."""
    return STORE_SOURCE.sub("", text)


def change_value(change: Change) -> str:
    match change.kind:
        case Kind.CHANGED:
            return f"{short(change.old, change.text)} → {short(change.new, change.text)}"
        case Kind.ADDED:
            return short(change.new, change.text) if change.new else "added"
        case Kind.REMOVED:
            return short(change.old, change.text) if change.old else "removed"
        case Kind.FAILED:
            return short_file(change.new.splitlines()[0]) if change.new else ""


def change_line(change: Change, option: OptionPath) -> Text:
    rest = change.path[len(option) :]
    line = Text(f"{MARK[change.kind]} ", style=STYLE[change.kind])
    if rest:
        line.append(format_path(rest), style="bold")
        line.append("  ")
    line.append(change_value(change), style="red" if change.kind is Kind.FAILED else "")
    return line


def is_root_change(group: Group) -> bool:
    return len(group.changes) == 1 and len(group.changes[0].path) == len(group.option)


QUIET_LABEL: Final = {
    Quiet.DROPPED: "dropped",
    Quiet.ELSEWHERE: "no change here",
    Quiet.UNREAD: "nothing reads it",
    Quiet.SAME: "same value",
    Quiet.PARTIAL: "evaluated parts same",
}


def quiet_note(group: Group) -> Text:
    path = format_path(group.option)
    match group.quiet:
        case Quiet.DROPPED:
            body = (
                f"The edit changes nothing: it is not among the definitions of {path}. The module "
                "system drops a definition that a higher priority outranks, or whose lib.mkIf "
                "condition is false. A lower number wins: lib.mkForce is 50, a plain definition "
                "100, lib.mkDefault 1000."
            )
        case Quiet.ELSEWHERE:
            body = (
                f"Nothing changed at {path} itself. An edit can act through another path, as a "
                "renamed option does. Turn edits off one at a time to see what each one changes."
            )
        case Quiet.PARTIAL:
            body = (
                f"Every part of {path} that the configuration evaluated has the same value. The "
                "other parts were never read, so their change reaches nothing."
            )
        case Quiet.UNREAD:
            body = (
                f"The edit changes nothing that was evaluated: nothing the configuration evaluated "
                f"reads {path}. Nix is lazy, so a value nobody needs is never computed. Something "
                "must read the option first, such as a module that turns it into a file or a unit."
            )
        case Quiet.SAME | None:
            body = f"The edit changes nothing: {path} already had this value."
    return Text(body, style="magenta")


def group_label(group: Group, edited: bool, browsed: bool = False) -> Text:
    name = format_path(group.option) if group.option else "(no option)"
    # Text(..., style=) styles everything appended later too.
    label = Text()
    if browsed:
        label.append("» ", style="bold cyan")
    if edited:
        label.append("✎ ", style="bold magenta")
    label.append(name, style="bold")
    if not group.changes and group.quiet is None:
        # Not in the cascade, which says nothing of a value the base never read.
        label.append("  browsing", style="dim cyan")
        return label
    if group.quiet is not None:
        label.append(f"  {QUIET_LABEL[group.quiet]}", style="dim magenta")
        return label
    if is_root_change(group):
        only = group.changes[0]
        label.stylize(STYLE[only.kind], 0, len(label))
        label.append(f"  {change_value(only)}")
        return label
    counts = {kind: sum(1 for c in group.changes if c.kind is kind) for kind in Kind}
    for kind, count in counts.items():
        if count:
            label.append(f"  {MARK[kind]}{count}", style=STYLE[kind])
    return label


def diff_text(old: str, new: str) -> RenderableType:
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", lineterm="")
    return Syntax("\n".join(lines), "diff", theme="ansi_dark", word_wrap=True)


def section(title: str) -> Text:
    return Text(f"\n{title}", style="bold underline")


def definition(mark: str, file: str, value: str, style: str) -> list[RenderableType]:
    return [Text(f"{mark} {short_file(file)}", style=style), Text(_indent(value), style=style or "dim")]


def definitions(view: OptionView) -> list[RenderableType]:
    """Each definition, marked `-` where the edits removed it and `+`
    where they added it."""
    if not view.base_read:
        out: list[RenderableType] = [
            Text(
                "The configuration never evaluated this option. These are the definitions with "
                "the edits: listing the ones before would evaluate them, and change what the "
                "next cascade compares.",
                style="dim italic",
            )
        ]
        for d in view.defs_new:
            edit = d.file.startswith(EDIT_FILE)
            out += definition("+" if edit else " ", d.file, d.value, "green" if edit else "")
        return out
    out = []
    old = {(d.file, d.value) for d in view.defs_old}
    new = {(d.file, d.value) for d in view.defs_new}
    for d in view.defs_old:
        gone = (d.file, d.value) not in new and bool(view.defs_new or view.new)
        out += definition("-" if gone else " ", d.file, d.value, "red" if gone else "")
    for d in view.defs_new:
        if (d.file, d.value) not in old:
            out += definition("+", d.file, d.value, "green")
    return out


def option_detail(view: OptionView, changes: tuple[Change, ...], note: Text | None = None) -> RenderableType:
    parts: list[RenderableType] = [Text(format_path(view.path), style="bold reverse")]
    if note is not None:
        parts.append(note)
    if view.type_description:
        parts.append(Text.assemble(("type: ", "dim"), view.type_description))
    if view.description:
        parts.append(Text(view.description.strip()))
    for file in view.declarations:
        parts.append(Text.assemble(("declared in ", "dim"), short_file(file)))
    if view.default:
        parts.append(Text.assemble(("default: ", "dim"), view.default))

    parts.append(section("Definitions"))
    parts.append(
        Text(
            "The definitions that win. Each has a priority, and the lowest number wins; the module "
            "system drops the rest and merges the winners.",
            style="dim italic",
        )
    )
    if view.prio_old is not None or view.prio_new is not None:
        old = None if view.prio_old is None else priority_text(view.prio_old)
        new = None if view.prio_new is None else priority_text(view.prio_new)
        shown = f"{old} → {new}" if old and new and old != new else (new or old or "")
        parts.append(Text.assemble(("winning priority: ", "dim"), shown))
    parts.extend(definitions(view))

    parts.append(section("Value"))
    if "«thunk»" in view.old or "«thunk»" in view.new:
        parts.append(Text("«thunk» is a part nothing evaluated yet: Nix is lazy.", style="dim italic"))
    if view.new and view.new != view.old:
        parts.append(diff_text(view.old, view.new))
    else:
        parts.append(Text(view.old))

    if changes:
        parts.append(section("What changed under it"))
        for change in changes:
            parts.append(change_line(change, view.path))
            if change.kind is Kind.FAILED:
                parts.append(Text(_indent(short_file(change.new)), style="red"))
                if change.old:
                    parts.append(Text(f"  (and {change.old} more)", style="dim"))
            elif change.text and ("\n" in change.old or "\n" in change.new):
                parts.append(diff_text(change.old, change.new))
    for error in view.errors:
        parts.append(Text(error, style="red"))
    return RichGroup(*parts)


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())


HELP: Final = Text.assemble(
    ("modiff", "bold"),
    " shows what one change to a module system configuration changes everywhere else.\n\n",
    "Press ",
    ("a", "bold"),
    " to add an edit: an option path and a Nix expression, as in a module.\n",
    "  services.openssh.enable = true\n",
    "  networking.firewall.allowedTCPPorts = [ 8080 ]\n",
    "  boot.loader.grub.enable = lib.mkForce true\n\n",
    "The cascade on the left lists every option whose value changed, as far as the "
    "configuration was evaluated. Move onto one to see its type, every module that "
    "defines it, and its value before and after.\n\n",
    "Inside an edit, ",
    ("lib", "bold"),
    ", ",
    ("config", "bold"),
    " and the module arguments (",
    ("pkgs", "bold"),
    " on NixOS) are in scope.",
)


@dataclass(frozen=True, slots=True)
class EditRequest:
    path: str
    expr: str


class Target(StrEnum):
    """Where an edit's path lies among the declared options."""

    OPTION = "option"
    # Under an option, as `users.users.alice.uid` lies under `users.users`.
    INSIDE = "inside"
    # A module may set a group: `services.openssh = { enable = true; }`.
    ABOVE = "above"
    UNKNOWN = "unknown"


def target(options: frozenset[OptionPath], path: OptionPath) -> tuple[Target, OptionPath | None]:
    """The option an edit of `path` defines, or the reason it defines none."""
    if path in options:
        return Target.OPTION, path
    for n in range(len(path) - 1, 0, -1):
        if path[:n] in options:
            return Target.INSIDE, path[:n]
    if any(o[: len(path)] == path for o in options):
        return Target.ABOVE, None
    return Target.UNKNOWN, None


type Explain = Callable[[OptionPath, Callable[[OptionView], None]], None]

SUMMARY_LINES: Final = 3


def edit_summary(view: OptionView, inside: bool) -> Text:
    out = Text()
    if inside:
        out.append(f"inside the option {format_path(view.path)}\n", style="dim")
    if view.type_description:
        out.append("type: ", style="dim").append(view.type_description + "\n")
    lines = view.description.strip().splitlines()
    if lines:
        more = " …" if len(lines) > SUMMARY_LINES else ""
        out.append("\n".join(lines[:SUMMARY_LINES]) + more + "\n")
    now = view.new or view.old
    out.append("now: ", style="dim").append(short(now, False) if now else "«no value»")
    if now == "«thunk»":
        out.append("  nothing evaluated it yet", style="dim italic")
    return out


class EditScreen(ModalScreen[Edit | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    EditScreen { align: center middle; }
    EditScreen > Vertical { width: 90%; height: auto; border: thick $accent; padding: 1 2; background: $surface; }
    EditScreen Input { margin-bottom: 1; }
    EditScreen #about { margin-bottom: 1; }
    """

    def __init__(
        self, request: EditRequest, options: list[str], paths: frozenset[OptionPath], explain: Explain
    ) -> None:
        super().__init__()
        self.request = request
        self.options = options
        # Empty until the engine has listed the options: then nothing is refused.
        self.paths = paths
        self.explain = explain
        self.about_path: OptionPath | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Option path (tab completes)")
            yield Input(
                self.request.path,
                placeholder="services.openssh.enable",
                suggester=SuggestFromList(self.options, case_sensitive=True),
                id="path",
            )
            yield Static("", id="about")
            yield Label("Value, a Nix expression (lib, config, pkgs in scope)")
            yield Input(self.request.expr, placeholder="true", id="expr")
            yield Label("enter: apply   escape: cancel", classes="dim")

    def on_mount(self) -> None:
        self.query_one("#expr" if self.request.path else "#path", Input).focus()
        self.describe(self.request.path)

    @on(Input.Changed, "#path")
    def _path_changed(self, event: Input.Changed) -> None:
        self.describe(event.value)

    def describe(self, text: str) -> None:
        about = self.query_one("#about", Static)
        path = parse_option_path(text.strip())
        self.about_path = path
        if not path or not self.paths:
            about.update("")
            return
        kind, option = target(self.paths, path)
        if option is None:
            about.update(
                Text("sets the options under it", style="dim")
                if kind is Target.ABOVE
                else Text("no option has this path", style="red")
            )
            return
        about.update(Text(f"reading {format_path(option)}…", style="dim"))

        def shown(view: OptionView) -> None:
            if self.about_path == path and self.is_attached:
                about.update(edit_summary(view, kind is Target.INSIDE))

        self.explain(option, shown)

    @on(Input.Submitted, "#path")
    def _next(self) -> None:
        self.query_one("#expr", Input).focus()

    @on(Input.Submitted, "#expr")
    def _done(self) -> None:
        path_input = self.query_one("#path", Input)
        path = parse_option_path(path_input.value.strip())
        expr = self.query_one("#expr", Input).value.strip()
        if path and self.paths and target(self.paths, path)[0] is Target.UNKNOWN:
            # Evaluating would only say "option does not exist", seconds later.
            path_input.focus()
            return
        self.dismiss(Edit(path, expr) if path and expr else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


BROWSE_LIMIT: Final = 200


def find_options(options: list[str], query: str, limit: int = BROWSE_LIMIT) -> list[str]:
    """The option paths holding every word of `query`. A path whose last
    segment holds the first word ranks first, then shorter paths."""
    words = query.lower().split()
    if not words:
        return options[:limit]
    found = [o for o in options if all(w in o.lower() for w in words)]
    found.sort(key=lambda o: (words[0] not in o.lower().rsplit(".", 1)[-1], len(o), o))
    return found[:limit]


class BrowseScreen(ModalScreen[OptionPath | None]):
    AUTO_FOCUS = "#query"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("down", "move(1)", "Next", show=False),
        Binding("up", "move(-1)", "Previous", show=False),
    ]
    DEFAULT_CSS = """
    BrowseScreen { align: center middle; }
    BrowseScreen > Vertical { width: 90%; height: 80%; border: thick $accent; padding: 1 2; background: $surface; }
    BrowseScreen OptionList { height: 1fr; }
    """

    def __init__(self, options: list[str]) -> None:
        super().__init__()
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Find an option: type words of its path")
            yield Input(placeholder="openssh port", id="query")
            yield OptionList(*find_options(self.options, ""), id="matches", markup=False)
            yield Label("enter: show it   up/down: move   escape: cancel", classes="dim")

    def on_mount(self) -> None:
        self.query_one("#matches", OptionList).highlighted = 0

    @on(Input.Changed, "#query")
    def _filter(self, event: Input.Changed) -> None:
        matches = self.query_one("#matches", OptionList)
        matches.set_options(find_options(self.options, event.value))
        matches.highlighted = 0 if matches.option_count else None

    def action_move(self, step: int) -> None:
        matches = self.query_one("#matches", OptionList)
        if matches.option_count:
            current = matches.highlighted or 0
            matches.highlighted = max(0, min(matches.option_count - 1, current + step))

    @on(Input.Submitted, "#query")
    def _submit(self) -> None:
        matches = self.query_one("#matches", OptionList)
        if matches.highlighted is None:
            self.dismiss(None)
            return
        self._choose(str(matches.get_option_at_index(matches.highlighted).prompt))

    @on(OptionList.OptionSelected, "#matches")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self._choose(str(event.option.prompt))

    def _choose(self, path: str) -> None:
        self.dismiss(parse_option_path(path))

    def action_cancel(self) -> None:
        self.dismiss(None)


type TreeData = tuple[Group, Change | None]


class ModiffApp(App[None]):
    TITLE = "modiff"
    CSS = """
    #left { width: 45%; }
    #edits { height: auto; max-height: 10; border: round $primary; }
    #cascade { border: round $primary; }
    #detail-scroll { border: round $secondary; }
    #status { height: 1; background: $boost; padding: 0 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("slash", "browse", "Find option"),
        Binding("a", "add_edit", "Add edit"),
        Binding("e", "edit_here", "Edit this option"),
        Binding("space", "toggle_edit", "Toggle edit"),
        Binding("x", "delete_edit", "Delete edit"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, engine: EngineThread, edits: list[Edit]) -> None:
        super().__init__()
        self.engine = engine
        self.edits = edits
        self.options: list[str] = []
        self.paths: frozenset[OptionPath] = frozenset()
        self.loaded: Loaded | None = None
        self.cascade = Cascade()
        self.busy = False
        self.pending = False
        self.generation = 0
        self.views: dict[OptionPath, OptionView] = {}
        self.showing: TreeData | None = None
        # An option found with `/`: listed first, changed or not.
        self.browsed: OptionPath | None = None
        # Set when the cascade on screen matches the edits; tests wait on it.
        self.settled = anyio.Event()

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="left"):
                edits = ListView(id="edits")
                edits.border_title = "Edits"
                yield edits
                tree: Tree[TreeData] = Tree("cascade", id="cascade")
                tree.show_root = False
                tree.border_title = "Cascade"
                yield tree
            scroll = VerticalScroll(id="detail-scroll")
            scroll.border_title = "Detail"
            with scroll:
                yield Static(HELP, id="detail")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.status("evaluating the base configuration…")
        self.run_job(Engine.load, self.on_loaded)

    def on_unmount(self) -> None:
        self.engine.shutdown()

    def run_job[T](self, job: Callable[[Engine], T], done: Callable[[T], None]) -> None:
        def finish(future: concurrent.futures.Future[T | Crash]) -> None:
            # Quitting cancels queued jobs, and a running one can finish after
            # the app stopped: nothing waits for either answer.
            if future.cancelled():
                return
            try:
                self.call_from_thread(self._deliver, future.result(), done)
            except RuntimeError:
                if self.is_running:
                    raise

        self.engine.submit(job).add_done_callback(finish)

    def _deliver[T](self, result: T | Crash, done: Callable[[T], None]) -> None:
        if isinstance(result, Crash):
            self.busy = False
            self.detail(Text(result.text, style="red"))
            self.status("internal error; see the detail pane")
            return
        done(result)

    def status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def detail(self, renderable: RenderableType) -> None:
        self.query_one("#detail", Static).update(renderable)

    def on_loaded(self, loaded: Loaded) -> None:
        self.loaded = loaded
        if loaded.error is not None:
            self.detail(Text(f"The base configuration does not evaluate:\n\n{loaded.error}", style="red"))
            self.status("base failed")
            self.settled.set()
            return
        self.run_job(Engine.option_paths, self.on_options)
        self.refresh_edits()
        self.recompute()

    def on_options(self, paths: list[OptionPath]) -> None:
        self.options = sorted(format_path(p) for p in paths)
        self.paths = frozenset(paths)

    def recompute(self) -> None:
        if self.busy:
            self.pending = True
            return
        self.busy = True
        if self.settled.is_set():
            self.settled = anyio.Event()
        self.status(self.base_status() + " · evaluating the edits…")
        edits = list(self.edits)
        self.run_job(lambda engine: engine.apply(edits), self.on_cascade)

    def base_status(self) -> str:
        if self.loaded is None:
            return ""
        return f"base {self.loaded.seconds:.1f}s, {self.loaded.forced} values evaluated"

    def on_cascade(self, cascade: Cascade) -> None:
        self.busy = False
        if self.pending:
            self.pending = False
            self.recompute()
            return
        try:
            self.show_cascade(cascade)
        finally:
            self.settled.set()

    def show_cascade(self, cascade: Cascade) -> None:
        self.cascade = cascade
        self.generation += 1
        self.views.clear()
        tree = self.query_one("#cascade", Tree)
        tree.clear()
        if cascade.error is not None:
            self.detail(Text(f"The edits do not evaluate:\n\n{short_file(cascade.error)}", style="red"))
            self.status(self.base_status() + " · edits failed")
            return
        edited = {e.path for e in self.edits if e.enabled}

        def is_edited(group: Group) -> bool:
            return any(group.option == e[: len(group.option)] for e in edited) and bool(group.option)

        first: TreeNode[TreeData] | None = None
        for group in sorted(cascade.groups, key=lambda g: (g.option != self.browsed, not is_edited(g))):
            node = tree.root.add(
                group_label(group, is_edited(group), browsed=group.option == self.browsed), data=(group, None)
            )
            if first is None:
                first = node
            if not is_root_change(group):
                for change in group.changes:
                    node.add_leaf(change_line(change, group.option), data=(group, change))
        if self.browsed is not None and (first is None or first.data is None or first.data[0].option != self.browsed):
            unchanged = Group(self.browsed, ())
            tree.root.add(group_label(unchanged, False, browsed=True), data=(unchanged, None), before=0)
        total = sum(len(g.changes) for g in cascade.groups)
        drvs = (
            f", derivations +{len(cascade.drvs_added)} -{len(cascade.drvs_removed)}"
            if cascade.drvs_added or cascade.drvs_removed
            else ""
        )
        changed = sum(1 for g in cascade.groups if g.quiet is None)
        tree.border_title = f"Cascade: {changed} options, {total} changes"
        self.status(self.base_status() + f" · edits {cascade.seconds:.1f}s{drvs}")
        if not tree.root.children:
            self.detail(HELP)
        else:
            # A cursor already on line 0 does not move, so no highlight event
            # shows the new row: show it here.
            def first_row() -> None:
                tree.move_cursor_to_line(0)
                self.show_node(tree.cursor_node)

            self.call_after_refresh(first_row)
        if self.generation == 1 or self.focused is not self.query_one("#edits", ListView):
            tree.focus()

    @on(Tree.NodeHighlighted)
    def _highlighted(self, event: Tree.NodeHighlighted[TreeData]) -> None:
        self.show_node(event.node)

    def show_node(self, node: TreeNode[TreeData] | None) -> None:
        data = None if node is None else node.data
        if data is None:
            return
        self.showing = data
        group, change = data
        if not group.option:
            self.detail(RichGroup(*(change_line(c, ()) for c in group.changes)))
            return
        changes = group.changes if change is None else (change,)
        note = None if group.quiet is None else quiet_note(group)
        generation = self.generation
        self.detail(Text(f"reading {format_path(group.option)}…", style="dim"))

        def shown(view: OptionView) -> None:
            # Jobs finish in any order; only the row still highlighted shows.
            if generation == self.generation and self.showing is data:
                self.detail(option_detail(view, changes, note))

        self.explain(group.option, shown)

    def refresh_edits(self) -> None:
        listing = self.query_one("#edits", ListView)
        index = listing.index
        listing.clear()
        for edit in self.edits:
            mark = "x" if edit.enabled else " "
            # A Label parses markup, and `[x]` reads as a tag.
            listing.append(ListItem(Label(Text(f"[{mark}] {format_path(edit.path)} = {edit.expr}"))))
        listing.border_title = f"Edits ({len(self.edits)})"
        if self.edits:
            listing.index = min(index or 0, len(self.edits) - 1)

    def selected_edit(self) -> int | None:
        index = self.query_one("#edits", ListView).index
        return index if index is not None and index < len(self.edits) else None

    def open_editor(self, request: EditRequest) -> None:
        def saved(edit: Edit | None) -> None:
            if edit is None:
                return
            self.edits = [e for e in self.edits if e.path != edit.path] + [edit]
            self.refresh_edits()
            self.recompute()

        self.push_screen(EditScreen(request, self.options, self.paths, self.explain), saved)

    def explain(self, option: OptionPath, done: Callable[[OptionView], None]) -> None:
        if (view := self.views.get(option)) is not None:
            done(view)
            return
        generation = self.generation

        def shown(view: OptionView) -> None:
            if generation == self.generation:
                self.views[option] = view
            done(view)

        self.run_job(lambda engine: engine.explain(option), shown)

    def action_browse(self) -> None:
        def found(option: OptionPath | None) -> None:
            if option is None:
                return
            self.browsed = option
            # Rebuilds the tree from the cascade on screen; nothing evaluates.
            self.show_cascade(self.cascade)
            self.query_one("#cascade", Tree).focus()

        self.push_screen(BrowseScreen(self.options), found)

    def action_add_edit(self) -> None:
        self.open_editor(EditRequest("", ""))

    def action_edit_here(self) -> None:
        if self.focused is self.query_one("#edits", ListView):
            index = self.selected_edit()
            if index is not None:
                edit = self.edits[index]
                self.open_editor(EditRequest(format_path(edit.path), edit.expr))
            return
        node = self.query_one("#cascade", Tree).cursor_node
        if node is None or node.data is None or not node.data[0].option:
            self.action_add_edit()
            return
        option = node.data[0].option
        existing = next((e for e in self.edits if e.path == option), None)
        view = self.views.get(option)
        expr = existing.expr if existing else (view.new or view.old if view else "")
        self.open_editor(EditRequest(format_path(option), expr if "\n" not in expr and "«" not in expr else ""))

    def action_toggle_edit(self) -> None:
        index = self.selected_edit()
        if index is None:
            return
        edit = self.edits[index]
        self.edits[index] = replace(edit, enabled=not edit.enabled)
        self.refresh_edits()
        self.recompute()

    def action_delete_edit(self) -> None:
        index = self.selected_edit()
        if index is None:
            return
        del self.edits[index]
        self.refresh_edits()
        self.recompute()
