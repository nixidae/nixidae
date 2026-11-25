from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from vivarium import logs
from vivarium.events import Event, Kind, Level

if TYPE_CHECKING:
    from pathlib import Path


def event(kind: Kind, text: str, machine: str | None = None, **data: str) -> Event:
    return Event(at=0.0, kind=kind, level=Level.INFO, text=text, machine=machine, data=dict(data))


KUBELET = event(Kind.JOURNAL, "started", "cp", unit="kubelet.service", identifier="kubelet")
SSHD = event(Kind.JOURNAL, "listening", "client", unit="sshd.service", identifier="sshd")
CONSOLE = event(Kind.CONSOLE, "booting", "cp")
NOTE = event(Kind.NOTE, "ready soon")


@pytest.mark.parametrize(
    ("wanted", "matching"),
    [
        (logs.Filter(), [KUBELET, SSHD, CONSOLE, NOTE]),
        (logs.Filter(machines=("cp",)), [KUBELET, CONSOLE]),
        (logs.Filter(units=("kubelet",)), [KUBELET]),
        (logs.Filter(units=("kubelet.service",)), [KUBELET]),
        (logs.Filter(units=("sshd", "kubelet")), [KUBELET, SSHD]),
        (logs.Filter(kinds=("console", "note")), [CONSOLE, NOTE]),
        (logs.Filter(machines=("cp",), kinds=("journal",)), [KUBELET]),
        (logs.Filter(contains="ing"), [SSHD, CONSOLE]),
    ],
)
def test_filter(wanted: logs.Filter, matching: list[Event]) -> None:
    assert [one for one in (KUBELET, SSHD, CONSOLE, NOTE) if wanted.matches(one)] == matching


def test_last_keeps_the_newest() -> None:
    assert logs.last(iter(range(10)), 3) == [7, 8, 9]
    assert logs.last(iter(range(3)), None) == [0, 1, 2]


def write(out: Path, *events: Event) -> None:
    lines = [one.as_json() for one in events]
    (out / logs.EVENTS).write_text("\n".join([lines[0], "not json", *lines[1:]]) + "\n")


@pytest.mark.anyio
async def test_read_renders_the_matches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tmp_path, KUBELET, SSHD, CONSOLE)
    wanted = logs.Filter(machines=("cp",))
    assert await logs.read(tmp_path, wanted, count=None, follow=True, as_json=False) == 0
    assert capsys.readouterr().out == "[cp] kubelet.service: started\n[cp] booting\n"


@pytest.mark.anyio
async def test_read_json_and_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tmp_path, KUBELET, SSHD, CONSOLE)
    assert await logs.read(tmp_path, logs.Filter(), count=1, follow=False, as_json=True) == 0
    [line] = capsys.readouterr().out.splitlines()
    assert json.loads(line)["text"] == "booting"


@pytest.mark.anyio
async def test_no_run_is_exit_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert await logs.read(tmp_path, logs.Filter(), count=None, follow=False, as_json=False) == 3
    assert "no events.jsonl" in capsys.readouterr().err
