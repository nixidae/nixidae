# SPDX-License-Identifier: MIT

import asyncio
import logging
import shlex
import time
from typing import NamedTuple

from shellous import sh

from .errors import CommandTimeoutError, SubprocessError

logger = logging.getLogger("nixkube.subprocessing")


class SubprocessResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    combined: str
    elapsed: float


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

    stdout_data: list[str] = []
    stderr_data: list[str] = []
    combined_data: list[str] = []

    try:
        async with asyncio.timeout(timeout):
            # Use shellous's byte-by-byte (low level) API for direct stream access
            cmd = sh(*[str(arg) for arg in args]).stdout(sh.CAPTURE).stderr(sh.CAPTURE)
            async with cmd as run:
                # Multiplex streams while maintaining interleaved order for combined output
                # We explicitly called .stdout(sh.CAPTURE) and .stderr(sh.CAPTURE),
                # so stdout and stderr should not be None
                assert run.stdout is not None and run.stderr is not None
                tasks = [
                    _read_stream(run.stdout, stdout_data, combined_data, log_level),
                    _read_stream(run.stderr, stderr_data, combined_data, log_level),
                ]
                await asyncio.gather(*tasks)
            # Use check=False to get exit code without raising on non-zero status
            # (error checking is done by try_captured/try_console)
            result = run.result(check=False)
            returncode = result.exit_code

            # Check if the command was cancelled due to timeout (shellous suppresses
            # the TimeoutError but sets cancelled=True in the Result)
            if result.cancelled:
                raise CommandTimeoutError(
                    returncode=124,
                    stdout="\n".join(stdout_data).strip(),
                    stderr="\n".join(stderr_data).strip(),
                    combined="\n".join(combined_data).strip(),
                    command=list(args),
                )
    except (asyncio.TimeoutError, TimeoutError):
        # asyncio.timeout raises TimeoutError when the deadline is reached.
        # Use return code 124 (conventional timeout code).
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
    logger.log(
        log_level,
        f"Command completed in {elapsed_time:.2f}s (rc={returncode}): {cmd_str}",
    )

    # Also log slow commands to main logger
    if elapsed_time > 5:
        logger.info(f"Slow command executed in {elapsed_time:.2f}s: {cmd_str}")

    return SubprocessResult(
        returncode,
        "\n".join(stdout_data).strip(),
        "\n".join(stderr_data).strip(),
        "\n".join(combined_data).strip(),
        elapsed_time,
    )


async def _read_stream(
    stream: asyncio.StreamReader,
    stream_buffer: list[str],
    combined_buffer: list[str],
    log_level: int,
) -> None:
    """Read lines from a stream and append to both stream-specific and combined buffers."""
    try:
        async for line in stream:
            decoded = line.decode().strip()
            stream_buffer.append(decoded)
            combined_buffer.append(decoded)
            logger.log(log_level, decoded)
    except Exception as e:
        logger.error(f"Error reading subprocess stream: {e}")


def log_command(*args, log_level: int):
    logger.log(
        log_level,
        f"Running command: {shlex.join([str(arg) for arg in args])}",
    )
