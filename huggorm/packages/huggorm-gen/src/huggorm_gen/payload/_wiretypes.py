"""
The wire's own facts about a type, shared by the build and the codec.

Which builtins go in a field as themselves, which declared type goes
in one as a builtin, how a union arm and a map entry are named, and
how deep a union may nest. The build resolves every type into a
`Wire` descriptor; these are the few facts both sides still state.

It is copied into the generated package as `_wiretypes.py`, the same
way `_runtime.py` is, so the two sides read one definition instead of
two that drift.
"""

import datetime
import os
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ._shapes import Scalar

# A declared type that is not a builtin and still goes in a field as
# one, with the builtin it goes in as.
#
# `datetime.timedelta` is what a DURATION is
# above every boundary: nanobind's own chrono caster hands a
# `std::chrono::microseconds` over as one, so the in-process surface
# needs nothing of ours (nixidae/nixidae#608).
#
# It crosses as an int of MICROSECONDS. Carl's decision, and the two
# reasons agree: a timedelta's own finest unit IS the microsecond, so
# nothing is rounded, and upstream holds the same resolution - a
# coarser wire would lose a build's CPU time on the way through and
# the round-trip gate would say so.
#
# Not seconds plus nanos: that is a second representation to convert
# through, and a precision neither end has.
_MICROSECOND = datetime.timedelta(microseconds=1)


def _duration_out(value: datetime.timedelta) -> int:
    """A duration as the whole microseconds a field carries.

    Floor division by one microsecond, which is exact: a timedelta
    holds days, seconds and microseconds as integers, so there is no
    remainder to lose."""
    return value // _MICROSECOND


def _duration_in(raw: int) -> datetime.timedelta:
    """The microseconds a field carried, as a duration again."""
    return datetime.timedelta(microseconds=raw)


@dataclass(frozen=True, slots=True)
class Spelled:
    """How one declared type goes in a builtin field.

    `field` is the builtin, which the schema reads. `out` and `back`
    convert at each end, which the codec reads. Two converters, unlike
    a vocabulary: a StrEnum member IS a str, so its one constructor
    serves both directions, and a timedelta is not an int."""

    field: Scalar
    out: Callable[[Any], Any]
    back: Callable[[Any], Any]


# A filesystem path crosses as the str it is. It names a file on the
# SERVER's machine, which #142 makes the client's: a same-uid Unix
# socket. A socket forwarded over ssh or into a container breaks that,
# and the path then names a file the client cannot open (nixidae/nixidae#722).
SPELLED = {
    "datetime.timedelta": Spelled(Scalar.INT, _duration_out, _duration_in),
    "pathlib.Path": Spelled(Scalar.STR, os.fspath, pathlib.Path),
}


def python_spelling(type_str: str) -> str:
    """The Python type a wire scalar is above the boundary. Every
    other name answers itself, a class's included."""
    return Scalar(type_str).python if type_str in Scalar else type_str


# How deep a UNION may nest before the codec refuses.
#
# A union arm may hold the union again - that is what lets a
# SingleDerivedPath name the output of a derivation that is itself an
# output - so a peer can send a chain of any length. The wire is a
# trust boundary, and without a cap the answer is a RecursionError
# that reaches a caller as an anonymous InternalError.
#
# Generous on purpose: anything real is one or two deep, so only a bug
# or an attack sees this.
MAX_UNION_DEPTH = 32
