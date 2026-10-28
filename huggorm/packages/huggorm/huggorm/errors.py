"""
The exception hierarchy, re-exported at the front door.

`huggorm_bindings.errors` is where these are declared - the bindings
own them, because libstore is what raises them and the class names
cross the wire against that declared set (nixidae/nixidae#573). This module
exists so a caller writes `huggorm.errors.InvalidPath` and never has
to learn which of the three build packages holds it.

Re-exported by name rather than star-imported, so a typechecker sees
the same set a reader does, and so adding one upstream is a visible
edit here rather than a silent widening.
"""

from __future__ import annotations

from huggorm_bindings.errors import (
    Abort as Abort,
    BadStorePath as BadStorePath,
    BadStorePathName as BadStorePathName,
    BuildError as BuildError,
    EvalBaseError as EvalBaseError,
    EvalError as EvalError,
    InvalidPath as InvalidPath,
    ListIndex as ListIndex,
    MissingAttribute as MissingAttribute,
    NixAssertionError as NixAssertionError,
    NixError as NixError,
    NixTypeError as NixTypeError,
    ParseError as ParseError,
    SysError as SysError,
    ThrownError as ThrownError,
    UndefinedVarError as UndefinedVarError,
    UnimplementedError as UnimplementedError,
    Unsupported as Unsupported,
    UsageError as UsageError,
)

__all__ = [
    "Abort",
    "BadStorePath",
    "BadStorePathName",
    "BuildError",
    "EvalBaseError",
    "EvalError",
    "InvalidPath",
    "ListIndex",
    "MissingAttribute",
    "NixAssertionError",
    "NixError",
    "NixTypeError",
    "ParseError",
    "SysError",
    "ThrownError",
    "UndefinedVarError",
    "UnimplementedError",
    "Unsupported",
    "UsageError",
]
