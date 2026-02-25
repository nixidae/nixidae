# SPDX-License-Identifier: MIT

"""Mount /nix and FHS store paths into a running container's mount namespace.

## Approach

For /nix we use the newer open_tree(2)/move_mount(2) API (Linux 5.2+):
  - open_tree captures a detached clone of the source mount tree while still in
    the daemonset's namespace, giving us an fd that survives setns(2) — the
    source path no longer needs to be visible inside the container's namespace.
  - move_mount attaches that fd at /nix inside the container's namespace.
  - A subsequent mount(2) remount makes /nix read-only.

For FHS store mounts we use the traditional mount(2) MS_BIND approach, executed
inside the container namespace after /nix has been attached (so /nix/store/...
paths are reachable as bind-mount sources). These mounts are read-write to
allow a writable overlayfs layer over /nix in the future.

## Execution context

When our createRuntime hook fires the container init process is alive with its
mount namespace established. pivot_root has NOT happened yet — the container's
root is still the host root. We use the fchdir+chroot trick to enter the
container's rootfs: open an O_PATH fd to the bundle rootfs before setns (while
the host path is accessible), then fchdir+chroot after setns.

## Why multiprocessing.spawn

setns(2) affects the calling thread's namespace, which would contaminate the
asyncio event-loop thread. fork is unsafe inside asyncio. spawn starts a fresh
Python interpreter with no inherited async state and all state passed explicitly
via picklable arguments.

## Worker sequence

  1. open_tree     — clone /nix mount tree fd while in daemonset namespace
  2. rootfs fd     — O_PATH to bundle/rootfs while in daemonset namespace
  3. setns         — enter container mount namespace
  4. fchdir+chroot — pivot into container rootfs
  5. move_mount    — attach /nix fd; then remount read-only
  6. MS_BIND       — bind each FHS store path (read-write) inside container;
                     sources resolved after step 5 so /nix/store/... is visible
"""

import asyncio
import ctypes
import logging
import multiprocessing
import os
import traceback
from pathlib import Path

from .constants import HOST_PROC_PATH

logger = logging.getLogger("nix-nri")

# mount(2) flags
MS_RDONLY = 1
MS_REMOUNT = 32
MS_BIND = 4096

# setns(2) namespace flag
CLONE_NEWNS = 0x00020000

# openat/open_tree dirfd sentinel
AT_FDCWD = -100

# open_tree(2) flags (Linux 5.2, include/uapi/linux/mount.h)
OPEN_TREE_CLONE = 1  # clone the subtree into a detached mount namespace
OPEN_TREE_CLOEXEC = os.O_CLOEXEC
AT_RECURSIVE = 0x8000  # recurse into sub-mounts within the subtree

# move_mount(2) flags (Linux 5.2, include/uapi/linux/mount.h)
MOVE_MOUNT_F_EMPTY_PATH = 0x4  # from_dirfd is the mount fd; from_pathname ignored

# Linux syscall numbers for open_tree and move_mount.
# Both were introduced in Linux 5.2 via the common syscall table
# (include/uapi/asm-generic/unistd.h), so x86_64 and aarch64 share them.
_NR_OPEN_TREE = 428
_NR_MOVE_MOUNT = 429


def _open_tree(libc: ctypes.CDLL, path: Path) -> int:
    """Clone the mount subtree at path; return an fd to the detached clone."""
    flags = OPEN_TREE_CLONE | AT_RECURSIVE | OPEN_TREE_CLOEXEC
    fd = libc.syscall(
        ctypes.c_long(_NR_OPEN_TREE),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_uint(flags),
    )
    if fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"open_tree({path!r}): {os.strerror(errno)}")
    return int(fd)


def _move_mount(libc: ctypes.CDLL, from_fd: int, to_path: Path) -> None:
    """Attach the detached mount fd at to_path (resolved in current root)."""
    ret = libc.syscall(
        ctypes.c_long(_NR_MOVE_MOUNT),
        ctypes.c_int(from_fd),
        ctypes.c_char_p(b""),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(to_path)),
        ctypes.c_uint(MOVE_MOUNT_F_EMPTY_PATH),
    )
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(
            errno, f"move_mount(fd={from_fd} → {to_path!r}): {os.strerror(errno)}"
        )


def _mount_worker(
    container_pid: int,
    bundle: str,
    nix_tree_path: Path,
    store_mounts: list[tuple[Path, Path]],
    result_queue: "multiprocessing.Queue[Exception | None]",
    host_proc_path: str,
) -> None:
    """Worker that runs in a spawned process to mount /nix and store paths.

    Sends None on success or the Exception (with traceback in __notes__) on failure.
    All arguments are passed explicitly (spawn context, no inherited state).
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.syscall.restype = ctypes.c_long

        # Step 1: Clone the /nix mount tree while the source path is still visible.
        # The fd references the clone by kernel object — it survives setns.
        nix_fd = _open_tree(libc, nix_tree_path)

        # Step 2: Open the container rootfs while still in the daemonset namespace.
        # /host/proc/{pid}/root reaches the container init's root; pre-pivot_root
        # that equals the host root, so appending {bundle}/rootfs gives the OCI rootfs.
        rootfs_path = f"{host_proc_path}/{container_pid}/root{bundle}/rootfs"
        rootfs_fd = os.open(rootfs_path, os.O_PATH | os.O_DIRECTORY)

        # Step 3: Enter the container's mount namespace.
        with open(f"{host_proc_path}/{container_pid}/ns/mnt", "rb") as f:
            ret = libc.setns(f.fileno(), CLONE_NEWNS)
        if ret != 0:
            errno = ctypes.get_errno()
            os.close(rootfs_fd)
            raise OSError(errno, f"setns({container_pid}): {os.strerror(errno)}")

        # Step 4: Enter the container's rootfs using the fd opened before setns.
        os.fchdir(rootfs_fd)
        os.close(rootfs_fd)
        os.chroot(".")

        # Step 5: Attach /nix (read-write clone) then remount it read-only.
        # MS_BIND | MS_REMOUNT | MS_RDONLY is the documented way to flip the RO bit
        # on an existing bind-like mount without creating a new mount.
        nix_dest = Path("/nix")
        nix_dest.mkdir(exist_ok=True)
        _move_mount(libc, nix_fd, nix_dest)
        os.close(nix_fd)
        ret = libc.mount(None, b"/nix", None, MS_BIND | MS_REMOUNT | MS_RDONLY, None)
        if ret != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"remount /nix RO: {os.strerror(errno)}")

        # Step 6: Bind-mount each FHS store path (read-write) into the container.
        # /nix is mounted now, so /nix/store/... source paths are reachable.
        # Resolve symlinks after chroot so they follow the container's view.
        for src, dst in store_mounts:
            src = src.resolve()
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.touch()
            ret = libc.mount(
                os.fsencode(src),
                os.fsencode(dst),
                None,
                MS_BIND,
                None,
            )
            if ret != 0:
                errno = ctypes.get_errno()
                raise OSError(
                    errno,
                    f"mount(bind {src!r} → {dst!r}): {os.strerror(errno)}",
                )

        result_queue.put(None)
    except Exception as e:
        e.__notes__ = [traceback.format_exc()]
        result_queue.put(e)


async def mount_in_container(
    container_pid: int,
    bundle: str,
    nix_tree_path: Path,
    store_mounts: list[tuple[Path, Path]],
) -> None:
    """Mount /nix and FHS store paths inside a container's mount namespace.

    nix_tree_path: prepared /nix tree to mount at /nix (read-only)
    store_mounts:  additional (src, dst) pairs for bind mounts (read-write)
    Raises the worker's original exception (with traceback) on failure.
    """
    ctx = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.Queue = ctx.Queue()

    proc = ctx.Process(
        target=_mount_worker,
        args=(
            container_pid,
            bundle,
            nix_tree_path,
            store_mounts,
            result_queue,
            HOST_PROC_PATH,
        ),
        daemon=True,
    )
    proc.start()
    await asyncio.to_thread(proc.join)

    result = result_queue.get_nowait()
    if isinstance(result, Exception):
        notes = getattr(result, "__notes__", [])
        if notes:
            logger.error("[NS-MOUNT] Worker traceback:\n%s", notes[0])
        raise result

    logger.info(
        "[NS-MOUNT] Mounted /nix + %d store mount(s) in container pid=%d",
        len(store_mounts),
        container_pid,
    )
