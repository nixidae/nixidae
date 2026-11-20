"""The app, driven through Textual's pilot against the toy configuration."""

from __future__ import annotations

from pathlib import Path

import anyio
from textual.pilot import Pilot
from textual.widgets import ListView, Tree
from toy import SOURCE

from modiff.app import ModiffApp, TreeData
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


def options(app: ModiffApp) -> list[str]:
    tree: Tree[TreeData] = app.query_one("#cascade", Tree)
    return [".".join(node.data[0].option) for node in tree.root.children if node.data is not None]


async def test_start_edit_shows_its_cascade_first(tmp_path: Path) -> None:
    app = app_for(tmp_path, [Edit(("a",), "2")])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        assert options(app) == ["a", "b", "drv"]


async def test_edit_through_the_modal(tmp_path: Path) -> None:
    app = app_for(tmp_path, [])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        assert options(app) == []
        await pilot.press("a", *"a", "enter", *"3", "enter")
        await settled(app, pilot)
        assert options(app) == ["a", "b", "drv"]


async def test_toggling_an_edit_off_clears_its_cascade(tmp_path: Path) -> None:
    app = app_for(tmp_path, [Edit(("a",), "2"), Edit(("c",), '"x"')])
    async with app.run_test(size=(160, 50)) as pilot:
        await settled(app, pilot)
        # `c` changes nothing evaluated: it stays listed, with the reason.
        assert sorted(options(app)) == ["a", "b", "c", "drv"]
        app.query_one("#edits", ListView).focus()
        await pilot.press("space")
        await settled(app, pilot)
        assert options(app) == ["c"]
        assert not app.edits[0].enabled
