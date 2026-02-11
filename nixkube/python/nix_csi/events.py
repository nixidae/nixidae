import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from kr8s.asyncio.objects import new_class

from .constants import KUBE_POD_NAME

logger = logging.getLogger("nix-csi")


@dataclass
class PodInfo:
    """Container for pod identification information for events."""

    name: str
    namespace: str
    uid: str

# Create ModernEvent class for events.k8s.io/v1 API
ModernEvent = new_class(
    kind="Event",
    version="events.k8s.io/v1",
    namespaced=True,
)

# Kubernetes event note field max size is 1kB (1000 bytes per Kubernetes docs)
MAX_EVENT_NOTE_SIZE = 1000


def _format_event_note(message: str, logs: str | None = None) -> str:
    """Format and truncate event note, preserving message and truncating logs if needed.

    Args:
        message: Human-readable message (always preserved)
        logs: Optional build/subprocess logs to append

    Returns:
        Formatted and truncated note (max 1000 bytes)
    """
    message_bytes = message.encode()

    if not logs:
        # Just truncate message if needed
        if len(message_bytes) <= MAX_EVENT_NOTE_SIZE:
            return message
        return message_bytes[:MAX_EVENT_NOTE_SIZE].decode(errors="ignore")

    # Both message and logs
    combined = f"{message}\n{logs}"
    combined_bytes = combined.encode()

    if len(combined_bytes) <= MAX_EVENT_NOTE_SIZE:
        return combined

    # Message fits, truncate logs to make room
    available_for_logs = MAX_EVENT_NOTE_SIZE - len(message_bytes) - 1  # -1 for newline
    if available_for_logs > 0:
        logs_bytes = logs.encode()
        # Keep the last part of logs (most relevant)
        truncated_logs = logs_bytes[-available_for_logs:].decode(errors="ignore")
        return f"{message}\n{truncated_logs}"

    # Message alone takes most space, just return truncated message
    return message_bytes[:MAX_EVENT_NOTE_SIZE].decode(errors="ignore")


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
    pod: PodInfo,
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
    logger.debug(f"report_event: {reason} for pod {pod.name}")
    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # Format the final note with message and optional logs
        final_note = _format_event_note(full_note, logs)

        # Search for existing event with same pod and reason
        try:
            events = []
            async for event in ModernEvent.list(
                namespace=pod.namespace,
                field_selector={
                    "regarding.uid": pod.uid,
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
                    f"Incremented event {reason} for pod {pod.name}",
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
                    f"{pod.uid}{reason}".encode()
                ).hexdigest()[:8]
                event_name = f"{pod.name}.{event_hash}"

                event_spec = {
                    "metadata": {
                        "name": event_name,
                        "namespace": pod.namespace,
                    },
                    "type": event_type,
                    "reason": reason,
                    "action": action,
                    "regarding": {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "name": pod.name,
                        "namespace": pod.namespace,
                        "uid": pod.uid,
                    },
                    "reportingController": "nix-csi",
                    "reportingInstance": KUBE_POD_NAME,
                    "note": final_note,
                    "eventTime": now_iso,
                }
                event = await ModernEvent(event_spec, namespace=pod.namespace)
                await event.create()
                logger.debug(
                    f"Created event {reason} for pod {pod.name}",
                )
            except Exception as e:
                logger.warning(f"Failed to create event: {e}")

    except Exception as e:
        # Catch-all to ensure events never block operations
        logger.warning(f"Unexpected error reporting event: {e}")
