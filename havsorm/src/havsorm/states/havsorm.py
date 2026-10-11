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


def profile(name: str, path: str, store: str | None = None) -> dict[str, Any]:
    """The profile `name` points at `path`, substituted if the store did
    not hold it. A change makes a new generation, as `nix ... --profile`
    does, so the old one stays for a rollback until it is collected.

        salt-env:
          havsorm.profile:
            - name: /nix/var/nix/profiles/salt
            - path: /nix/store/...-salt-env
    """
    ret: dict[str, Any] = {"name": name, "result": True, "changes": {}, "comment": ""}
    current = __salt__["havsorm.profile_target"](name)
    if current == path:
        return {**ret, "comment": f"{name} points at {path}"}
    change = {"old": current, "new": path}
    if __opts__["test"]:
        return {**ret, "result": None, "changes": change,
                "comment": f"{name} would point at {path}"}
    try:
        if not __salt__["havsorm.is_realised"](path, store=store):
            __salt__["havsorm.realise"](path, store=store)
        change["generation"] = __salt__["havsorm.profile_set"](name, path, store=store)
    except CommandExecutionError as e:
        return {**ret, "result": False, "comment": str(e)}
    return {**ret, "changes": change, "comment": f"{name} points at {path}"}


def generations_deleted(name: str, older_than: str | None = None) -> dict[str, Any]:
    """The profile `name` keeps no generation but the current one, or none
    older than `older_than`, such as `30d`. The store paths stay until
    the collector runs.

        salt-env-history:
          havsorm.generations_deleted:
            - name: /nix/var/nix/profiles/salt
            - older_than: 30d
    """
    ret: dict[str, Any] = {"name": name, "result": True, "changes": {}, "comment": ""}
    try:
        doomed = __salt__["havsorm.profile_delete_generations"](
            name, older_than=older_than, test=True)
        if not doomed:
            return {**ret, "comment": f"{name} has no generation to delete"}
        if __opts__["test"]:
            return {**ret, "result": None, "changes": {"deleted": doomed},
                    "comment": f"{name} would lose generations {doomed}"}
        deleted = __salt__["havsorm.profile_delete_generations"](name, older_than=older_than)
    except CommandExecutionError as e:
        return {**ret, "result": False, "comment": str(e)}
    return {**ret, "changes": {"deleted": deleted},
            "comment": f"{name} lost generations {deleted}"}


def unit(name: str, path: str, root: str | None = None,
         unit_dir: str = "/etc/systemd/system", store: str | None = None) -> dict[str, Any]:
    """The systemd unit `name` is the file `path` in the Nix store.

    The store path that holds the file is substituted if needed, with
    everything it references, so the binaries the unit names are there.
    It is rooted at `root`, by default `/nix/var/nix/gcroots/salt/<name>`:
    the link in `unit_dir` is no root, and the collector would delete
    the unit's closure under a running service. A changed link reloads
    systemd. Restart the service the way Salt does for any unit file:

        nginx-unit:
          havsorm.unit:
            - name: nginx.service
            - path: /nix/store/...-unit-nginx.service/nginx.service
        nginx:
          service.running:
            - watch:
              - havsorm: nginx-unit
    """
    ret: dict[str, Any] = {"name": name, "result": True, "changes": {}, "comment": ""}
    link = os.path.join(unit_dir, name)
    root = root or os.path.join("/nix/var/nix/gcroots/salt", name)
    try:
        holder = __salt__["havsorm.store_path_of"](path, store=store)
    except CommandExecutionError as e:
        return {**ret, "result": False, "comment": str(e)}
    if os.path.lexists(link) and not os.path.islink(link):
        return {**ret, "result": False,
                "comment": f"{link} is a file, not a link: havsorm replaces only its own"}
    current = os.readlink(link) if os.path.islink(link) else None
    rooted = os.path.islink(root) and os.readlink(root) == holder
    if current == path and rooted:
        return {**ret, "comment": f"{link} is {path}"}
    change: dict[str, Any] = {}
    if current != path:
        change["unit"] = {"old": current, "new": path}
    if not rooted:
        change["root"] = root
    if __opts__["test"]:
        return {**ret, "result": None, "changes": change,
                "comment": f"{link} would be {path}"}
    try:
        if not __salt__["havsorm.is_realised"](holder, store=store):
            __salt__["havsorm.realise"](holder, store=store)
        if not rooted:
            os.makedirs(os.path.dirname(root), exist_ok=True)
            __salt__["havsorm.add_root"](holder, root, store=store)
    except CommandExecutionError as e:
        return {**ret, "result": False, "comment": str(e)}
    if current != path:
        os.makedirs(unit_dir, exist_ok=True)
        staged = f"{link}.havsorm"
        os.symlink(path, staged)
        os.replace(staged, link)
    comment = f"{link} is {path}"
    if "unit" in change:
        if "service.systemctl_reload" in __salt__:
            __salt__["service.systemctl_reload"]()
            comment += "; systemd reloaded"
        else:
            comment += "; systemd is not running here, so nothing was reloaded"
    return {**ret, "changes": change, "comment": comment}


_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def _bytes(size: int | str) -> int:
    """`10G` and friends, binary multiples, as Nix's min-free reads them."""
    if isinstance(size, int):
        return size
    text = str(size).strip().upper()
    unit = text[-1] if text and text[-1] in _UNITS else ""
    return int(text[: len(text) - len(unit)]) * _UNITS[unit]


def garbage_collected(name: str, keep_free: int | str | None = None,
                      max_freed: int | str | None = None,
                      store: str | None = None) -> dict[str, Any]:
    """The store holds no path that no root reaches, or, with
    `keep_free`, its file system has at least that much free.

    A collection deletes only what no root reaches, so profiles, units
    and realised paths survive it.

        nix-store:
          havsorm.garbage_collected:
            - keep_free: 10G
    """
    ret: dict[str, Any] = {"name": name, "result": True, "changes": {}, "comment": ""}
    try:
        if keep_free is not None:
            free = __salt__["havsorm.free_bytes"](store=store)
            if free >= _bytes(keep_free):
                return {**ret, "comment": f"{free} bytes free, {keep_free} wanted"}
        dead = __salt__["havsorm.collect_garbage"](test=True, store=store)["paths"]
        if not dead:
            return {**ret, "comment": "no path is dead"}
        if __opts__["test"]:
            return {**ret, "result": None, "changes": {"deleted": dead},
                    "comment": f"{len(dead)} dead paths would be deleted"}
        done = __salt__["havsorm.collect_garbage"](
            max_freed=None if max_freed is None else _bytes(max_freed), store=store)
    except CommandExecutionError as e:
        return {**ret, "result": False, "comment": str(e)}
    return {**ret, "changes": {"deleted": done["paths"], "bytes_freed": done["bytes_freed"]},
            "comment": f"{len(done['paths'])} paths deleted, {done['bytes_freed']} bytes freed"}
