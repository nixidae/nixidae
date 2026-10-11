"""Nix store state, the Salt way: each state says what changed, honours
`test=True`, and works as a requisite.

    hello:
      havsorm.realised:
        - name: /nix/store/...-hello-2.12.1
        - root: /nix/var/nix/gcroots/salt/hello
"""

import os
from typing import Any

from salt.exceptions import CommandExecutionError


def realised(name: str, root: str | None = None, store: str | None = None) -> dict[str, Any]:
    """The store holds `name`, substituted if it did not, and `root` keeps
    it from the collector."""
    ret: dict[str, Any] = {"name": name, "result": True, "changes": {}, "comment": ""}
    try:
        valid = __salt__["havsorm.is_realised"](name, store=store)
    except CommandExecutionError as e:
        return {**ret, "result": False, "comment": str(e)}
    rooted = root is None or (os.path.islink(root) and os.readlink(root) == name)
    if valid and rooted:
        where = f", rooted at {root}" if root else ""
        return {**ret, "comment": f"{name} is in the store{where}"}

    want: dict[str, Any] = {}
    steps = []
    if not valid:
        want["realised"] = name
        steps.append("substituted")
    if not rooted:
        want["root"] = root
        steps.append(f"rooted at {root}")
    if __opts__["test"]:
        return {**ret, "result": None, "changes": want,
                "comment": f"{name} would be " + " and ".join(steps)}
    try:
        if not valid:
            __salt__["havsorm.realise"](name, store=store)
        if not rooted:
            __salt__["havsorm.add_root"](name, root, store=store)
    except CommandExecutionError as e:
        return {**ret, "result": False, "comment": str(e)}
    return {**ret, "changes": want, "comment": f"{name} was " + " and ".join(steps)}
