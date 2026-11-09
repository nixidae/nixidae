"""What Nix says while `vivarium run` evaluates and builds a test.

Every record goes to `<out>/build.jsonl`, one JSON object a line, as huggorm
hands it over: Nix's activity tree with its typed progress, each build's
log lines included. `vivarium monitor` reads that file while the run has
no control socket yet, so a slow build is not a silent one.

stderr gets what `nix build` prints at its default verbosity: each
message, each activity's `...` line. Not a count of builds: `vivarium run`
realises one string context at a time, each its own `builds` activity, so
a count restarts at `0/1` for every derivation. A build's log lines stay
in the file. When a build fails, Nix's
error carries the end of that build's log, so the reason is still on
stderr.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TextIO

import huggorm

if TYPE_CHECKING:
    from pathlib import Path

    from huggorm import LogRecord

FILE: Final = "build.jsonl"

FAILED: Final = "failed"
"""The `action` of the last line when evaluating or building failed. Not
one of Nix's: the CLI writes it, and its `text` is the error."""

SHOWN: Final = huggorm.Verbosity.INFO
"""What `nix build` prints with no verbosity flag."""


@dataclass(frozen=True, slots=True)
class BuildRecord:
    """One record, in the names of Nix's `internal-json` log format."""

    action: str
    id: int = 0
    parent: int = 0
    level: int = 0
    type: int = 0
    text: str = ""
    fields: tuple[int | str, ...] = ()

    @classmethod
    def of(cls, record: LogRecord) -> BuildRecord:
        info = record.info() if record.action() == "msg" else None
        return cls(
            action=record.action(),
            id=record.id(),
            parent=record.parent(),
            level=record.level(),
            type=record.type(),
            text=record.text() if info is None else info.msg(),
            fields=tuple(item.integer() if item.is_int() else item.text() for item in record.fields()),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "action": self.action,
                "id": self.id,
                "parent": self.parent,
                "level": self.level,
                "type": self.type,
                "text": self.text,
                "fields": list(self.fields),
            }
        )


def line(record: BuildRecord) -> str | None:
    """The line `nix build` prints for `record`, or `None`."""
    if record.action == "msg":
        return record.text
    if record.action == "start" and record.text and record.level <= SHOWN:
        return f"{record.text}..."
    return None


class Recorder:
    """Takes each record: one line to the file, and its stderr line if any."""

    def __init__(self, path: Path | None, stderr: TextIO) -> None:
        self._file = None if path is None else path.open("w", buffering=1, encoding="utf-8")
        self._stderr = stderr

    def take(self, record: LogRecord) -> None:
        # huggorm's own marker that a call raised its last record; not Nix's.
        if record.action() != "finalized":
            self.write(BuildRecord.of(record))

    def write(self, record: BuildRecord) -> None:
        if self._file is not None:
            self._file.write(record.to_json() + "\n")
        shown = line(record)
        if shown is not None:
            self._stderr.write(shown + "\n")
            self._stderr.flush()

    def failed(self, error: str) -> None:
        """The last line: why there is no run."""
        if self._file is not None:
            self._file.write(BuildRecord(FAILED, text=error).to_json() + "\n")

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
