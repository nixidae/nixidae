# SPDX-License-Identifier: MIT

import hashlib
import logging
from datetime import datetime, timezone

from kr8s.asyncio.objects import Pod, new_class

from .constants import KUBE_POD_NAME
from .errors import CSIError

logger = logging.getLogger("nixkube.events")


async def emit_event_for_exception(
    pod: Pod,
    exception: CSIError,
    event_type: str = "Warning",
) -> None:
    """Emit a Kubernetes event for a CSIError exception.

    Args:
        pod: Kubernetes Pod object for event targeting
        exception: CSIError with reason and logs
        event_type: "Normal" or "Warning" (default: "Warning")
    """
    await report_event(
        pod,
        reason=exception.reason,
        note=exception.message,
        logs=exception.logs,
        event_type=event_type,
    )


# Create ModernEvent class for events.k8s.io/v1 API
ModernEvent = new_class(
    kind="Event",
    version="events.k8s.io/v1",
    namespaced=True,
)

# Kubernetes event note field max size is 1kB (1000 bytes per Kubernetes docs)
MAX_EVENT_NOTE_SIZE = 1000


def _format_event_note(message: str, logs: str | None = None) -> str:
    """Format event note with message and logs, truncated to 1000 bytes.

    Preserves the full message and truncates logs if needed.
    Naturally handles UTF-8 multi-byte characters.

    Args:
        message: Human-readable message (must be < 1000 bytes)
        logs: Optional build/subprocess logs to append

    Returns:
        Formatted and truncated note (max 1000 bytes)
    """
    message_bytes = message.encode()

    # Message must fit within limit (defensive assertion)
    assert len(message_bytes) <= MAX_EVENT_NOTE_SIZE, (
        f"Message alone exceeds {MAX_EVENT_NOTE_SIZE} bytes: {len(message_bytes)}"
    )

    if not logs:
        return message

    # Calculate space available for logs (reserve space for newline separator)
    available_for_logs = MAX_EVENT_NOTE_SIZE - len(message_bytes) - 1

    # Take last 1000 characters of logs (most recent/relevant)
    if len(logs) > 1000:
        logs = logs[-1000:]

    # Trim logs from start until they fit in available space
    logs_bytes = logs.encode()
    while len(logs_bytes) > available_for_logs and len(logs) > 0:
        excess_bytes = len(logs_bytes) - available_for_logs
        logs = logs[excess_bytes:]  # Remove first excess_bytes chars
        logs_bytes = logs.encode()

    # Combine and verify final size (defensive assertion)
    result = f"{message}\n{logs}"
    result_bytes = result.encode()

    assert len(result_bytes) <= MAX_EVENT_NOTE_SIZE, (
        f"Failed to truncate event note to {MAX_EVENT_NOTE_SIZE} bytes: {len(result_bytes)}"
    )

    return result


def _extract_build_logs(exception: Exception) -> str:
    """Extract build logs from an exception, preferring stderr/combined output."""
    # GRPCError stores combined output in args[2] (the details)
    if hasattr(exception, "args") and len(exception.args) > 2:
        details = exception.args[2]
        if isinstance(details, str) and details:
            return details
    # Fallback to exception message
    return str(exception)


async def report_event(
    pod: Pod,
    reason: str,
    note: str | None = None,
    event_type: str = "Normal",
    action: str = "Nix",
    logs: str | Exception | None = None,
) -> None:
    """
    Report a Kubernetes event for a pod, incrementing count if it already exists.

    Events are best-effort - failures are logged but don't propagate to avoid
    blocking main operations.

    Args:
        pod: PodInfo object with name, namespace, and uid
        reason: Short, UpperCamelCase reason code without "Nix" prefix (will be added)
                e.g., "PodStoreBuildFailed" → "NixPodStoreBuildFailed"
        note: Human-readable description (will be combined with logs)
        event_type: "Normal" or "Warning" (default: "Normal")
        action: Machine-readable action taken (default: "Nix")
        logs: Optional build/subprocess logs (string or exception) to append to note (will be truncated)
    """
    # Extract logs from exception if needed
    if isinstance(logs, Exception):
        logs = _extract_build_logs(logs)

    # Combine note and logs, preserving the message part
    full_note = note or ""
    if logs:
        full_note = f"{full_note}\n{logs}" if full_note else logs

    # Ensure reason is prefixed with "Nix" if not already
    if not reason.startswith("Nix"):
        reason = f"Nix{reason}"
    logger.debug(f"report_event: {reason} for pod {pod.metadata.name}")
    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # Format the final note with message and optional logs
        final_note = _format_event_note(full_note, logs)

        # Search for existing event with same pod and reason
        try:
            events = []
            async for event in ModernEvent.list(
                namespace=pod.metadata.namespace,
                field_selector={
                    "regarding.uid": pod.metadata.uid,
                    "reason": reason,
                },
            ):
                events.append(event)
        except Exception as e:
            logger.warning(f"Failed to list events: {e}")
            events = []

        if events:
            # Event already exists - patch to increment count
            event = events[0]
            try:
                # Initialize or increment series count
                if "series" not in event:
                    event["series"] = {"count": 2, "lastObservedTime": now_iso}
                else:
                    # Increment existing count
                    current_count = event["series"].get("count", 1)
                    event["series"]["count"] = current_count + 1
                    event["series"]["lastObservedTime"] = now_iso

                await event.patch()
                logger.debug(
                    f"Incremented event {reason} for pod {pod.metadata.name}",
                    extra={"count": event["series"]["count"]},
                )
            except Exception as e:
                logger.warning(f"Failed to patch existing event: {e}")
        else:
            # Create new event
            try:
                # Generate event name using hash of pod uid and reason
                # This ensures the same event (same pod + reason) gets the same name
                # so we can find and patch it to update series when it recurs
                event_hash = hashlib.md5(
                    f"{pod.metadata.uid}{reason}".encode()
                ).hexdigest()[:8]
                event_name = f"{pod.metadata.name}.{event_hash}"

                event_spec = {
                    "metadata": {
                        "name": event_name,
                        "namespace": pod.metadata.namespace,
                    },
                    "type": event_type,
                    "reason": reason,
                    "action": action,
                    "regarding": {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "name": pod.metadata.name,
                        "namespace": pod.metadata.namespace,
                        "uid": pod.metadata.uid,
                    },
                    "reportingController": "nixkube",
                    "reportingInstance": KUBE_POD_NAME,
                    "note": final_note,
                    "eventTime": now_iso,
                }
                event = await ModernEvent(event_spec, namespace=pod.metadata.namespace)
                await event.create()
                logger.debug(
                    f"Created event {reason} for pod {pod.metadata.name}",
                )
            except Exception as e:
                logger.warning(f"Failed to create event: {e}")

    except Exception as e:
        # Catch-all to ensure events never block operations
        logger.warning(f"Unexpected error reporting event: {e}")
