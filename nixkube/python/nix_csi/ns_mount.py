# SPDX-License-Identifier: MIT

"""Bind-mount paths inside a running container's mount namespace.

## Why this module exists

NRI `createRuntime` hooks (where nri-wait runs) execute in the *host* mount
namespace and before `pivot_root`.  The OCI runtime injects our bind mounts
declaratively (via CreateContainerResponse), but those are limited to paths
that exist on the host.  For store mount paths that need to live *inside* the
container's own namespace (e.g. /etc/ssl populated from a Nix store path),
we need to mount them ourselves after the namespace is created.

## The execution context

When our `createRuntime` hook fires:
  - The container init process (PID reported via OCI state stdin) is alive
  - Its mount namespace (CLONE_NEWNS) already exists and has the OCI-declared
    mounts applied (bind-mounted in by the OCI runtime)
  - pivot_root has NOT happened yet: the container init's root directory is
    still `/` on the host, not the container's rootfs

## Why multiprocessing.spawn (not threads, not fork)

  - `setns()` affects the *calling thread's* namespace, not the whole process.
    Using threads would pollute the asyncio event loop thread's namespace.
  - `fork` is unsafe from within asyncio: forking a process that holds open
    event-loop fds, locks, etc. can deadlock.  `spawn` starts a fresh Python
    interpreter with no inherited async state.
  - `spawn` passes all state explicitly via picklable function arguments,
    making the worker entirely self-contained.

## The rootfs fd trick (why we can't just chroot after setns naively)

After `setns(CLONE_NEWNS)` we are in the container's mount namespace, but our
process root is still `/` on the host.  To resolve paths like `/etc/ssl`
inside the container's rootfs we need to chroot there.

The rootfs lives at `{bundle}/rootfs` on the host.  We need to open a file
descriptor to it while we're still in the host mount namespace (where
`/host/proc` is accessible), BEFORE calling setns.  Because pre-pivot_root the
container init's root *is* the host root, the kernel path

    /host/proc/{pid}/root/{bundle}/rootfs

traverses through the container init's root (== host `/`) to reach the bundle
rootfs directory, giving us an fd we can later use after the namespace switch.

After setns, we call `fchdir(rootfs_fd)` to change our CWD to the rootfs, then
`chroot(".")` to make it our root, at which point all subsequent paths resolve
inside the container's rootfs.
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

MS_BIND = 4096
MS_RDONLY = 1
CLONE_NEWNS = 0x00020000


def _mount_worker(
    container_pid: int,
    bundle: str,
    mounts: list[tuple[Path, Path]],
    result_queue: "multiprocessing.Queue[Exception | None]",
    host_proc_path: str,
) -> None:
    """Worker that runs in a fresh spawned process to perform the mounts.

    All arguments are passed explicitly (spawn context, no inherited state).
    Sends None into result_queue on success, or the Exception on failure.
    The exception carries its full traceback in __notes__ so it survives
    pickling back to the parent process.

    Sequence:
      1. Open rootfs fd  — while still in host namespace (O_PATH, no exec)
      2. setns           — enter container's mount namespace
      3. fchdir+chroot   — enter container's rootfs using the pre-opened fd
      4. For each mount: resolve src, create mount point, call mount(2)

    All path operations after step 3 resolve inside the container's rootfs.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)

        # Step 1: Open rootfs fd while still in the host mount namespace.
        # Path: /host/proc/{pid}/root resolves the container init's root dir.
        # Pre-pivot_root that equals the host root, so appending {bundle}/rootfs
        # reaches the OCI bundle's rootfs directory on the host filesystem.
        # O_PATH avoids any permission checks on the directory itself and does
        # not require execute permission on intermediate symlinks.
        rootfs_path = f"{host_proc_path}/{container_pid}/root{bundle}/rootfs"
        rootfs_fd = os.open(rootfs_path, os.O_PATH | os.O_DIRECTORY)

        # Step 2: Enter the container's mount namespace.
        # After this, /host/proc is no longer accessible (it's a daemonset
        # mount, not in the container's namespace), but rootfs_fd remains valid
        # because file descriptors are not namespace-scoped.
        with open(f"{host_proc_path}/{container_pid}/ns/mnt", "rb") as f:
            ret = libc.setns(f.fileno(), CLONE_NEWNS)
        if ret != 0:
            errno = ctypes.get_errno()
            os.close(rootfs_fd)
            raise OSError(errno, f"setns({container_pid}) failed: {os.strerror(errno)}")

        # Step 3: Enter the container's rootfs.
        # fchdir changes our CWD to the rootfs using the fd we opened before
        # setns.  chroot(".") makes "." (our current CWD, the rootfs) the new
        # root — all absolute paths now resolve inside the container's rootfs.
        os.fchdir(rootfs_fd)
        os.close(rootfs_fd)
        os.chroot(".")

        # Step 4: Perform each bind mount inside the container's rootfs+namespace.
        for src, dst in mounts:
            # Resolve symlinks in the source path.  After chroot, this resolves
            # through the container's rootfs, following any Nix store symlinks
            # that point into other store paths visible via the /nix bind mount.
            src = src.resolve()

            # Create the mount point with the right type (dir or file).
            # The kernel requires the mount point to exist and match the source type.
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.touch()

            ret = libc.mount(
                str(src).encode(),
                str(dst).encode(),
                None,
                MS_BIND | MS_RDONLY,
                None,
            )
            if ret != 0:
                errno = ctypes.get_errno()
                raise OSError(
                    errno,
                    f"mount({src!r} → {dst!r}) failed: {os.strerror(errno)}",
                )

        result_queue.put(None)
    except Exception as e:
        # Attach the traceback as a string so it survives pickling across the
        # process boundary back to the asyncio event loop.
        e.__notes__ = [traceback.format_exc()]
        result_queue.put(e)


async def mount_in_container(
    container_pid: int,
    bundle: str,
    mounts: list[tuple[Path, Path]],
) -> None:
    """Bind-mount paths inside a container's mount namespace.

    bundle: OCI bundle path (e.g. /run/containerd/.../abc123), used to open
            the container's rootfs fd before entering the mount namespace.
    mounts: list of (src, dst) Path pairs; src is resolved inside the
            container's rootfs after setns+chroot.
    Raises the worker's original exception (with traceback) on failure.
    """
    ctx = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.Queue = ctx.Queue()

    proc = ctx.Process(
        target=_mount_worker,
        args=(container_pid, bundle, mounts, result_queue, HOST_PROC_PATH),
        daemon=True,
    )
    proc.start()
    # Block the event loop thread without blocking the event loop itself.
    await asyncio.to_thread(proc.join)

    result = result_queue.get_nowait()
    if isinstance(result, Exception):
        notes = getattr(result, "__notes__", [])
        if notes:
            logger.error("[NS-MOUNT] Worker traceback:\n%s", notes[0])
        raise result

    logger.info(
        "[NS-MOUNT] Mounted %d path(s) in container pid=%d", len(mounts), container_pid
    )
