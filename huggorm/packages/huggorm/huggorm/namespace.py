"""
A process in its own user namespace, over an overlay Nix store.

The overlay's lower layer is a store the host already has, and its
upper layer is a scratch directory. A build in it reads every lower
path without a copy and writes only the upper layer. The mount lives
in a private mount namespace, so the host store does not change.

The namespace is the reason the server sets its own `sandbox` and
`sandbox-paths`: a local overlay store is a store of its own, and the
daemon's sandbox settings do not reach it (nixidae/nixidae#1060).

`unshare(CLONE_NEWUSER)` fails with EINVAL in a process with more than
one thread. So `enter_overlay_namespace` runs in `main()`, before the
event loop and before any Nix thread starts. Measured: the process has
one thread after `import huggorm_bindings` and `huggorm.server`.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import pathlib
import shutil
import sys
import threading
import urllib.parse
from dataclasses import dataclass

# mount(2) flags; Python has no binding for mount.
_MS_REC = 0x4000
_MS_PRIVATE = 1 << 18

#: Where the overlay mounts. Anywhere else makes a relocated store,
#: whose paths do not run where they are.
STORE_DIR = "/nix/store"

#: Nix refuses a `local-overlay://` URI without this feature.
EXPERIMENTAL_FEATURE = "local-overlay-store"

#: As the only mapped user the process is root in the namespace, and
#: the build-users group has no mapping there: Nix gets EINVAL when it
#: gives the store to that group. Nix's own tests of this store type set
#: the first two values. The rest each need the host's real root, and
#: give "Permission denied" with the host's nix.conf (`--load-config`,
#: measured): `.links` and `userpool2` belong to root, and the session's
#: cgroup is not delegated to the namespace.
REQUIRED_SETTINGS: dict[str, str] = {
    "build-users-group": "",
    "require-drop-supplementary-groups": "false",
    "auto-optimise-store": "false",
    "auto-allocate-uids": "false",
    "use-cgroups": "false",
}


@dataclass(frozen=True, slots=True)
class OverlayNamespace:
    """Where the layers of one overlay store live.

    `work_dir` must be on the filesystem of `upper_dir` and not inside
    it; OverlayFS requires both."""

    upper_dir: str
    work_dir: str
    state_dir: str
    log_dir: str
    #: The store that supplies the metadata of the paths `lower_dir`
    #: supplies as bytes.
    lower_store: str = "daemon"
    #: Nix compares this with the `realStoreDir` of `lower_store`
    #: (`local-overlay-store.cc` reads `/proc/self/mounts`), so the two
    #: move together. A pair that disagrees gives "overlay filesystem
    #: /nix/store mounted incorrectly".
    lower_dir: str = STORE_DIR

    @classmethod
    def under(cls, root: str | os.PathLike[str], *,
              lower_store: str = "daemon",
              lower_dir: str = STORE_DIR) -> OverlayNamespace:
        """The four directories under one `root`."""
        base = pathlib.Path(root)
        return cls(upper_dir=str(base / "upper"),
                   work_dir=str(base / "work"),
                   state_dir=str(base / "state"),
                   log_dir=str(base / "log"),
                   lower_store=lower_store,
                   lower_dir=lower_dir)

    def store_uri(self) -> str:
        """The `local-overlay://` URI of this store. The parameter names
        are those of `local-overlay-store.hh` and the local-fs store;
        Nix parses and validates the URI when it opens it. `work_dir`
        is absent: the mount needs it, the store does not."""
        query = urllib.parse.urlencode({
            "lower-store": self.lower_store,
            "upper-layer": self.upper_dir,
            "state": self.state_dir,
            "log": self.log_dir,
        })
        return f"local-overlay://?{query}"


def remove_layers(root: str | os.PathLike[str]) -> None:
    """Remove `root` and the layers under it. OverlayFS leaves
    `work/work` at mode 000 and a store leaves its paths read-only, and
    this user owns both, so it puts the bits back first."""
    for dirpath, dirnames, _ in os.walk(root):
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), 0o700)
    shutil.rmtree(root)


def _mount(source: str, target: str, fstype: str | None, flags: int,
           data: str | None) -> None:
    name = ctypes.util.find_library("c")
    if name is None:
        raise RuntimeError("cannot find libc, so no overlay mount")
    libc = ctypes.CDLL(name, use_errno=True)

    def encode(value: str | None) -> bytes | None:
        return None if value is None else value.encode()

    if libc.mount(encode(source), encode(target), encode(fstype),
                  ctypes.c_ulong(flags), encode(data)) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"mount({source!r}, {target!r}, {fstype!r}): "
                             f"{os.strerror(errno)}")


def enter_overlay_namespace(spec: OverlayNamespace, *,
                            mount_at: str = STORE_DIR) -> None:
    """Enter a private user and mount namespace, and mount the overlay
    at `mount_at`. Call it on the only thread, before Nix opens a store.
    The mount is visible to this process and its children alone."""
    if sys.platform != "linux":
        raise RuntimeError(
            f"an overlay store namespace needs Linux, not {sys.platform}")
    threads = threading.active_count()
    if threads != 1:
        raise RuntimeError(
            f"enter_overlay_namespace needs a single-threaded process, "
            f"and this one has {threads} threads: unshare(CLONE_NEWUSER) "
            f"fails with EINVAL once a second thread exists")

    uid, gid = os.getuid(), os.getgid()
    try:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
    except OSError as exc:
        raise RuntimeError(
            f"cannot create a user namespace ({exc.strerror}): this host "
            f"does not let an unprivileged user create one") from exc

    # Root inside, and nothing else mapped: root is what lets Nix build
    # without a build-users group. The kernel refuses an unprivileged
    # gid_map until setgroups is denied.
    pathlib.Path("/proc/self/setgroups").write_text("deny")
    pathlib.Path("/proc/self/uid_map").write_text(f"0 {uid} 1")
    pathlib.Path("/proc/self/gid_map").write_text(f"0 {gid} 1")

    # Without this the overlay propagates back out on a host where / is
    # shared (the systemd default), and every process sees it.
    _mount("none", "/", None, _MS_REC | _MS_PRIVATE, None)

    for directory in (spec.upper_dir, spec.work_dir, spec.state_dir,
                      spec.log_dir, mount_at):
        pathlib.Path(directory).mkdir(parents=True, exist_ok=True)

    # `userxattr` is required: an unprivileged mount cannot write the
    # `trusted.overlay.*` attributes, so a whiteout over a lower path
    # would fail. OverlayFS resolves `lowerdir` before the new mount
    # hides it, so a lower layer at `mount_at` stays readable.
    options = (f"lowerdir={spec.lower_dir},upperdir={spec.upper_dir},"
               f"workdir={spec.work_dir},userxattr")
    try:
        _mount("overlay", mount_at, "overlay", 0, options)
    except OSError as exc:
        raise RuntimeError(
            f"cannot mount the overlay store at {mount_at} "
            f"({exc.strerror}). {spec.work_dir} must be on the filesystem "
            f"of {spec.upper_dir}, and that filesystem must support user "
            f"extended attributes") from exc
