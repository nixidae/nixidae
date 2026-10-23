"""Declared types -> C++ spellings.

How a declared type is spelled in a nanobind signature, by value and
as a parameter, and the caster each spelling needs. One answer for
every stage that writes C++.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from huggorm_dsl.declare import STRING, Crossing, Cxx, Decl, DeclKind
from huggorm_dsl.read import Class, Param

if TYPE_CHECKING:
    from huggorm_gen.ir import TypeRef


# Where an emitted C++ type goes. The same namespace `errors.hpp`
# already opens, so a translation unit that includes it reopens one
# namespace rather than gaining a second.
NAMESPACE = "huggorm"


class Declared(Protocol):
    """A declared class, read or resolved: both carry these two."""

    @property
    def name(self) -> str: ...

    @property
    def decl(self) -> Decl: ...


def held(cls: Declared) -> str:
    """The C++ type this class binds.

    Two sources, and which one applies is the difference between a
    HANDLE and a RECORD. `@binding(cxx=...)` names a type libstore
    already has, and the binding holds one of those. A produced value
    names none, because libstore has no type shaped like the answer -
    `queryPathInfo` hands back a ValidPathInfo whose fields a caller
    reads one at a time. So the emitter declares the struct, and the
    name is the class's own."""
    return cls.decl.cxx or f"{NAMESPACE}::{cls.name}"


def bare(cls: Class, known: Mapping[str, Class]) -> str:
    """The C++ type behind a declared class, or a refusal."""
    if cls.is_union:
        # A SUM, and std::variant is what C++ already calls one.
        # nanobind casts it natively (<nanobind/stl/variant.h>), so a
        # parameter of a union type needs no dispatch written by hand:
        # the caster tries each arm and the body receives the one that
        # matched.
        #
        # The ARMS, not the C++ union type upstream declares.
        # nix::DerivedPath IS a std::variant, but over
        # DerivedPathOpaque rather than StorePath - and nanobind's
        # caster is specialised on std::variant exactly, not on
        # something deriving from one. So the binding takes the arms
        # the PYTHON side has and a body converts, which is a decision
        # and belongs in a body.
        if cls.decl.variant is not None:
            # THE UNION'S OWN TYPE. A generated type_caster casts it
            # to the arms Python has, so a signature names what
            # libstore names and no body converts.
            return cls.decl.variant.cxx
        return arms_type(cls, known)
    if cls.is_words:
        # A vocabulary. The member IS the string a Nix parser takes,
        # so it crosses as one - a fact about the words rather than
        # about either binding.
        return STRING.spelling
    if not cls.decl.cxx and not cls.decl.produced:
        raise TypeError(
            f"'{cls.name}' has no C++ type behind it. Only a class with "
            f"@binding(cxx=...) or @produced can cross as one.")
    return held(cls)


def arms_type(cls: Class, known: Mapping[str, Class]) -> str:
    """A union as the std::variant of the arms PYTHON has.

    Not what a signature says any more - that is the union's own C++
    type - but what the caster casts through, and what `from_arms`
    takes."""
    inner = ", ".join(arm(cls, a, known) for a in cls.decl.arms)
    return f"std::variant<{inner}>"


def arm(cls: Class, name: str, known: Mapping[str, Class]) -> str:
    """One arm as Python has it, in C++: a builtin from the alias the
    declaration wrote, and a class through its own declaration."""
    if (scalar := cls.decl.scalars.get(name)) is not None:
        return fact(scalar.python, scalar.cxx).spelling
    other = known[name]
    if other.decl.holder:
        # Its share, not a copy: `fetchers::LazyAttr` is a `ref`, and
        # two lazy attributes are equal only as the same computation.
        return f"std::{other.decl.holder}<{bare(other, known)}>"
    return bare(other, known)


def fact(python: str, said: Cxx | None) -> Cxx:
    """The C++ an alias carries, or a refusal."""
    if said is None:
        raise TypeError(
            f"'{python}' has no C++ spelling. A bound class names types "
            f"through an Annotated alias in declare.py.")
    return said


def value(t: TypeRef, known: Mapping[str, Class]) -> tuple[str, str | None]:
    """A declared type as C++ carries it BY VALUE, and its caster.

    The value form, not the parameter form. A return is a value, a
    vector's element is a value, and an optional's payload is a
    value - so this is the shape everything else is built from and
    `param` adds the reference where a parameter wants one."""
    other = t.required.declared
    if other is not None and other.decl.kind is DeclKind.ERROR:
        # A live Python EXCEPTION, handed over rather than raised. A
        # BuildResult's failure arm is a nix::BuildError, and reading
        # a failed result is not an exception (nixidae/nixidae#608) - so what
        # crosses is the object.
        #
        # `nb::object` whether or not the declaration wrote `| None`,
        # and the optional is dropped on purpose: `nb::none()` IS the
        # absent value here, and `std::optional<nb::object>` would
        # give a Python caller one spelling for absent and the emitter
        # two.
        return "nb::object", None
    if t.origin is not None:
        # `Vec`, `Map` or `Opt`: the alias's template, holding the C++
        # of what it holds. Not an `nb::dict` built by hand in a body:
        # that IS the mapping this exists to derive.
        container = fact(t.spelling, t.spelled)
        held, _ = value(t.inner, known)
        return container.of(held), container.caster
    other = t.declared
    if other is not None:
        spelled = bare(other, known)
        if other.decl.holder:
            # Held through something. `@binding(holder="shared_ptr")`
            # is the declaration saying the factory hands back a
            # reference-counted handle, so Python has to keep a share
            # or the object closes under the name for it.
            return (f"std::{other.decl.holder}<{spelled}>",
                    other.decl.holder)
        if other.is_union:
            # <nanobind/stl/variant.h>, and the ARMS' casters too: a
            # variant of bound classes needs none of its own, but one
            # holding a string or a vector does.
            return spelled, "variant"
        return spelled, STRING.caster if other.is_words else None
    entry = fact(t.name, t.spelled)
    # The value, never the reference: a `const std::string &` member
    # of a struct is a dangling reference waiting to happen, and an
    # optional cannot hold one at all.
    return entry.spelling, entry.caster


def param(t: TypeRef, known: Mapping[str, Class]) -> tuple[str, str | None]:
    """The C++ spelling of a declared type, and the caster it needs.

    A BOUND type - one naming another declared class - is spelled
    through that class's declaration. So `is_valid_path(path:
    StorePath)` becomes `const nix::StorePath &`, and neither
    declaration repeats the other's C++ name."""
    if t.optional or t.origin:
        spelled, caster = value(t, known)
        # By const reference, because these are the types worth not
        # copying - and, for one holding `nb::bytes`, because a copy
        # would be WRONG. A method that releases the GIL runs its whole body
        # with the guard held, so a by-value Python handle changes a
        # reference count without the GIL and nanobind aborts the
        # process: "attempted to change the reference count of a
        # Python object while the GIL was not held". A reference
        # binds to the caster's own object, which nanobind destroys
        # after the guard.
        return f"const {spelled} &", caster
    if (other := t.declared) is not None:
        if other.is_words:
            # A vocabulary. The member IS the string a Nix parser
            # takes, so it crosses as one. That is a fact about the
            # words rather than about the binding, which is why the
            # emitted module is plain Python with no C++ at all.
            return STRING.param
        if other.decl.holder:
            # Its share, as `self` is: a Python-made object has
            # `shared_from_this()` only while a C++ share of it lives,
            # and a body that keeps the object needs one.
            return (f"const std::{other.decl.holder}<{bare(other, known)}> &",
                    other.decl.holder)
        # A wire value is a copy the call reads, so const. A proxy is an
        # object the call may act on: `nix::copyClosure` writes into the
        # destination `Store &`, and a const reference cannot reach it.
        if other.decl.wire is Crossing.VALUE:
            return f"const {bare(other, known)} &", None
        return f"{bare(other, known)} &", None
    return fact(t.spelling, t.spelled).param


def absent(pr: Param) -> bool:
    """Whether this parameter's absence is spelled `None`.

    A CONTAINER whose declared default is None. The declaration's own
    docstring says what that means - a repeated field has no presence
    and needs none, so an absent container IS an empty one - and a
    caller passing None explicitly means the same thing.

    It matters because nanobind's vector caster refuses None: it asks
    for a sequence, and None is not one. So a parameter that reads
    None has to say so in its own type."""
    if not pr.has_default or pr.default is not None:
        return False
    return pr.type.required.container


def default(pr: Param) -> str:
    """A Python default, as C++ spells the same value.

    Two cases the table cannot hold, because both need the
    declaration to resolve them.

    A VOCABULARY member is a name in Python and a string in C++:
    `HashAlgorithm.SHA256` is `"sha256"`, and only the vocabulary
    knows which. Emitting the Python spelling put an undeclared
    identifier in the C++.

    `None` on a CONTAINER is an empty one. A repeated field has no
    presence and needs none - an absent container IS an empty one,
    which is what the declaration's own docstring says - so `nullptr`
    would be a null reference where a value belongs."""
    if not pr.has_default:
        return ""
    value = pr.default
    if pr.member:
        # A vocabulary member IS the string a Nix parser takes.
        return json.dumps(value)
    if value is None and pr.type.optional:
        # An optional parameter, absent. `nullptr`, what a bare None
        # becomes below, is a null POINTER, which a std::optional
        # parameter cannot take.
        return "nb::none()"
    if absent(pr):
        # None, and the signature says so. The parameter arrives as a
        # std::optional and an emitted line turns it into an empty
        # container - so a caller who passes nothing and a caller who
        # passes None get the same answer.
        return "nb::none()"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "nullptr"
    if isinstance(value, str):
        # Double quotes: `'auto'` in C++ is a character literal.
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    raise TypeError(f"{pr.name}: no C++ spelling for the default {value!r}")
