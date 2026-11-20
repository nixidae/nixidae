"""The app, driven through Textual's pilot against the toy configuration."""

from __future__ import annotations

import io
from pathlib import Path

import anyio
from rich.console import Console
from textual.pilot import Pilot
from textual.widgets import Input, ListView, Static, Tree
from toy import SOURCE

from modiff.app import DrvRow, EditScreen, ModiffApp, Target, TreeData, find_options, target
from modiff.engine import EngineThread
from modiff.model import Edit

SETTLE = 30


def app_for(tmp_path: Path, edits: list[Edit]) -> ModiffApp:
    return ModiffApp(EngineThread(SOURCE, store=f"local?root={tmp_path}"), edits)


async def settled(app: ModiffApp, pilot: Pilot[None]) -> None:
    """Wait for the cascade that matches the edits. The pause first lets
    a key's handler start the recompute that replaces `settled`."""
    await pilot.pause()
    with anyio.fail_after(SETTLE):
        await app.settled.wait()
    await pilot.pause()


async def drained(app: ModiffApp, pilot: Pilot[None]) -> None:
    """Wait for every job queued so far. The engine runs one job at a time,
    so a job queued last finishes last."""
    await pilot.pause()
    future = app.engine.submit(lambda _: None)
    with anyio.fail_after(SETTLE):
        await anyio.to_thread.run_sync(lambda: future.result())
    await pilot.pause()


def detail_text(app: ModiffApp) -> str:
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(app.query_one("#detail", Static).content)
    return console.export_text()


def options(app: ModiffApp) -> list[str]:
    tree: Tree[TreeData] = app.query_one("#cascade", Tree)
    return [".".join(node.data[0].option) for node in tree.root.children if isinstance(node.data, tuple)]


def drv_rows(app: ModiffApp) -> list[str]:
    tree: Tree[TreeData] = app.query_one("#cascade", Tree)
    return [str(leaf.label) for node in tree.root.children if isinstance(node.data, DrvRow) for leaf in node.children]


async def test_start_edit_shows_its_cascade_first(tmp_path: Path) -> None:
    app = app_for(tmp_path, [Edit(("a",), "2")])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        assert options(app) == ["a", "b", "drv", "top"]
        assert drv_rows(app) == ["~ dep-10 → dep-20", "~ top"]


async def test_a_rebuilt_derivation_names_its_changed_input(tmp_path: Path) -> None:
    app = app_for(tmp_path, [Edit(("a",), "2")])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        tree: Tree[TreeData] = app.query_one("#cascade", Tree)
        drvs = next(n for n in tree.root.children if isinstance(n.data, DrvRow))
        drvs.expand()
        await pilot.pause()
        top = next(
            n for n in drvs.children if isinstance(n.data, DrvRow) and n.data.change and n.data.change.name == "top"
        )
        tree.move_cursor(top)
        await drained(app, pilot)
        text = detail_text(app)
        assert "Inputs that changed" in text
        assert "dep-10 → dep-20" in text


async def test_enter_on_a_changed_input_goes_to_its_row(tmp_path: Path) -> None:
    app = app_for(tmp_path, [Edit(("a",), "2")])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        tree: Tree[TreeData] = app.query_one("#cascade", Tree)
        drvs = next(n for n in tree.root.children if isinstance(n.data, DrvRow))
        drvs.expand()
        await pilot.pause()
        top = next(
            n for n in drvs.children if isinstance(n.data, DrvRow) and n.data.change and n.data.change.name == "top"
        )
        tree.move_cursor(top)
        await drained(app, pilot)
        top.expand()
        await pilot.pause()
        assert [str(n.label) for n in top.children] == ["~ dep-10 → dep-20  ⏎ its row", "~ hidden-10 → hidden-20"]
        hidden, dep = top.children[1], top.children[0]
        tree.move_cursor(hidden)
        await drained(app, pilot)
        assert "no row of its own" in detail_text(app)
        assert "env.name" in detail_text(app)
        tree.move_cursor(dep)
        await pilot.press("enter")
        await pilot.pause()
        node = tree.cursor_node
        assert node is not None and node.parent is drvs
        assert isinstance(node.data, DrvRow) and node.data.change is not None
        assert node.data.change.name == "dep-20"


async def test_edit_through_the_modal(tmp_path: Path) -> None:
    app = app_for(tmp_path, [])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        assert options(app) == []
        await pilot.press("a", *"a", "enter", *"3", "enter")
        await settled(app, pilot)
        assert options(app) == ["a", "b", "drv", "top"]


def test_target_places_a_path_among_the_options() -> None:
    paths = frozenset({("users", "users"), ("services", "openssh", "enable")})
    assert target(paths, ("services", "openssh", "enable")) == (Target.OPTION, ("services", "openssh", "enable"))
    assert target(paths, ("users", "users", "alice", "uid")) == (Target.INSIDE, ("users", "users"))
    assert target(paths, ("services", "openssh")) == (Target.ABOVE, None)
    assert target(paths, ("services", "opnessh", "enable")) == (Target.UNKNOWN, None)
    assert target(paths, ("_module", "args", "foo")) == (Target.MODULE, None)


def about(app: ModiffApp) -> str:
    return str(app.screen.query_one("#about", Static).render())


async def test_edit_modal_describes_the_option(tmp_path: Path) -> None:
    app = app_for(tmp_path, [])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        await pilot.press("a", *"a")
        await drained(app, pilot)
        assert "type: signed integer" in about(app)
        assert "The input." in about(app)
        assert "now: 1" in about(app)
        await pilot.press(*".x")
        await drained(app, pilot)
        assert "inside the option a" in about(app)


async def test_edit_modal_refuses_an_unknown_path(tmp_path: Path) -> None:
    app = app_for(tmp_path, [])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        await pilot.press("a", *"nope", "enter", *"3", "enter")
        await pilot.pause()
        assert isinstance(app.screen, EditScreen)
        assert "no option has this path" in about(app)
        assert app.edits == []
        assert app.screen.query_one("#path", Input).has_focus


def test_find_options_ranks_the_last_segment_first() -> None:
    options = ["services.openssh.ports", "services.openssh.enable", "networking.firewall.allowedTCPPorts", "ports"]
    assert find_options(options, "ports") == [
        "ports",
        "services.openssh.ports",
        "networking.firewall.allowedTCPPorts",
    ]
    assert find_options(options, "openssh port") == ["services.openssh.ports"]
    assert find_options(options, "", limit=2) == options[:2]


async def test_browse_shows_an_unchanged_option_and_edits_it(tmp_path: Path) -> None:
    app = app_for(tmp_path, [Edit(("a",), "2")])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        await pilot.press("slash", *"ports", "enter")
        await pilot.pause()
        assert options(app) == ["ports", "a", "b", "drv", "top"]
        # The cursor was on line 0 already, so no highlight event shows the
        # new row there.
        assert isinstance(app.showing, tuple) and app.showing[0].option == ("ports",)
        await pilot.press("e", *"[ 2222 ]", "enter")
        await settled(app, pilot)
        assert app.edits == [Edit(("a",), "2"), Edit(("ports",), "[ 2222 ]")]
        # The toy never reads `ports`: the browsed row is the quiet one.
        assert options(app) == ["ports", "a", "b", "drv", "top"]


async def test_toggling_an_edit_off_clears_its_cascade(tmp_path: Path) -> None:
    app = app_for(tmp_path, [Edit(("a",), "2"), Edit(("c",), '"x"')])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        # `c` changes nothing evaluated: it stays listed, with the reason.
        assert sorted(options(app)) == ["a", "b", "c", "drv", "top"]
        app.query_one("#edits", ListView).focus()
        await pilot.press("space")
        await settled(app, pilot)
        assert options(app) == ["c"]
        assert not app.edits[0].enabled
