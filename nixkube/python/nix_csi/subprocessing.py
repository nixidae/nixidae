# SPDX-License-Identifier: MIT

import asyncio
import logging
import shlex
import time
from typing import NamedTuple

from .errors import CommandTimeoutError, SubprocessError

logger = logging.getLogger("nix-csi")
subproc_logger = logging.getLogger("nix-csi.subprocessing")


class SubprocessResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    combined: str
    elapsed: float


def _format_command_preview(args, max_args=20):
    """Format command for error messages, truncating if too long."""
    cmd_preview = shlex.join([str(arg) for arg in args[:max_args]])
    suffix = f" ... ({len(args)} total args)" if len(args) > max_args else ""
    return f"{cmd_preview}{suffix}"


async def try_captured(*args, timeout: float | None = None):
    result = await run_captured(*args, timeout=timeout)
    if result.returncode != 0:
        raise SubprocessError(
            result.returncode,
            result.stdout,
            result.stderr,
            result.combined,
            list(args),
        )
    return result


async def try_console(
    *args, log_level: int = logging.DEBUG, timeout: float | None = None
):
    result = await run_console(*args, log_level=log_level, timeout=timeout)
    if result.returncode != 0:
        raise SubprocessError(
            result.returncode,
            result.stdout,
            result.stderr,
            result.combined,
            list(args),
        )
    return result


# Run async subprocess, capture output and returncode
async def run_captured(*args, timeout: float | None = None):
    return await run_console(*args, log_level=logging.NOTSET, timeout=timeout)


# Run async subprocess, forward output to console and return returncode
async def run_console(
    *args, log_level: int = logging.DEBUG, timeout: float | None = None
):
    start_time = time.perf_counter()
    log_command(*args, log_level=log_level)
    proc = await asyncio.create_subprocess_exec(
        *[str(arg) for arg in args],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_data = []
    stderr_data = []
    combined_data = []

    async def stream_output(stream, buffer):
        try:
            async for line in stream:
                decoded = line.decode().strip()
                buffer.append(decoded)
                combined_data.append(decoded)
                logger.log(log_level, decoded)
        except Exception as e:
            logger.error(f"Error reading subprocess stream: {e}")
            # Continue - proc.wait() will still complete and we'll get returncode

    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(
                stream_output(proc.stdout, stdout_data),
                stream_output(proc.stderr, stderr_data),
                proc.wait(),
            )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        # Use return code 124 (conventional timeout code) for simplicity
        raise CommandTimeoutError(
            returncode=124,
            stdout="\n".join(stdout_data).strip(),
            stderr="\n".join(stderr_data).strip(),
            combined="\n".join(combined_data).strip(),
            command=list(args),
        )

    elapsed_time = time.perf_counter() - start_time
    cmd_str = shlex.join([str(arg) for arg in args])

    # Log all command timings for profiling
    subproc_logger.debug(
        f"Command completed in {elapsed_time:.2f}s (rc={proc.returncode}): {cmd_str}"
    )

    # Also log slow commands to main logger
    if elapsed_time > 5:
        logger.info(f"Slow command executed in {elapsed_time:.2f}s: {cmd_str}")

    if proc.returncode is None:
        raise RuntimeError("Process returncode is None after wait()")
    return SubprocessResult(
        proc.returncode,
        "\n".join(stdout_data).strip(),
        "\n".join(stderr_data).strip(),
        "\n".join(combined_data).strip(),
        elapsed_time,
    )


def log_command(*args, log_level: int):
    logger.log(
        log_level,
        f"Running command: {shlex.join([str(arg) for arg in args])}",
    )
