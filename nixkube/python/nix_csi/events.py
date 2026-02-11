import hashlib
import logging
from datetime import datetime, timezone

from kr8s.asyncio.objects import new_class

from .constants import CSI_POD_NAME

logger = logging.getLogger("nix-csi")

# Create ModernEvent class for events.k8s.io/v1 API
ModernEvent = new_class(
    kind="Event",
    version="events.k8s.io/v1",
    namespaced=True,
)


async def report_event(
    pod_name: str,
    pod_namespace: str,
    pod_uid: str,
    reason: str,
    note: str,
    event_type: str = "Normal",
    action: str = "Nix",
) -> None:
    """
    Report a Kubernetes event for a pod, incrementing count if it already exists.

    Events are best-effort - failures are logged but don't propagate to avoid
    blocking main operations.

    Args:
        pod_name: Name of the pod
        pod_namespace: Namespace of the pod
        pod_uid: UID of the pod
        reason: Short, UpperCamelCase reason code (max 128 chars), e.g., "BuildFailed"
        note: Human-readable description (max 1kB), e.g., error message or status
        event_type: "Normal" or "Warning" (default: "Normal")
        action: Machine-readable action taken (default: "Nix")
    """
    logger.debug(f"report_event: {reason} for pod {pod_name}")
    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # Search for existing event with same pod and reason
        try:
            events = []
            async for event in ModernEvent.list(
                namespace=pod_namespace,
                field_selector={
                    "regarding.uid": pod_uid,
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
                    f"Incremented event {reason} for pod {pod_name}",
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
                    f"{pod_uid}{reason}".encode()
                ).hexdigest()[:8]
                event_name = f"{pod_name}.{event_hash}"

                event_spec = {
                    "metadata": {
                        "name": event_name,
                        "namespace": pod_namespace,
                    },
                    "type": event_type,
                    "reason": reason,
                    "action": action,
                    "regarding": {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "name": pod_name,
                        "namespace": pod_namespace,
                        "uid": pod_uid,
                    },
                    "reportingController": "nix-csi",
                    "reportingInstance": CSI_POD_NAME,
                    "note": note,
                    "eventTime": now_iso,
                }
                event = await ModernEvent(event_spec, namespace=pod_namespace)
                await event.create()
                logger.debug(
                    f"Created event {reason} for pod {pod_name}",
                )
            except Exception as e:
                logger.warning(f"Failed to create event: {e}")

    except Exception as e:
        # Catch-all to ensure events never block operations
        logger.warning(f"Unexpected error reporting event: {e}")
