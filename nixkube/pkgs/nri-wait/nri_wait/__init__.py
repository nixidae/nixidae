"""NRI wait - OCI hook for waiting on Nix builds."""

import json
import sys
import time

import zmq

__version__ = "0.1.0"


def check_build_status(
    context: zmq.Context,
    query_socket_path: str,
    oci_state: dict,
    timeout: int,
) -> bool:
    """Query REP socket to check if build is already done.

    Returns True if build is complete, False if still pending or socket unavailable.
    """
    req = context.socket(zmq.REQ)

    # Set timeouts so we don't hang
    timeout_ms = timeout * 1000
    req.setsockopt(zmq.RCVTIMEO, timeout_ms)
    req.setsockopt(zmq.SNDTIMEO, timeout_ms)

    socket_path = f"ipc://{query_socket_path}"
    print(f"[nri-wait] Connecting to query socket: {socket_path}", file=sys.stderr)

    try:
        req.connect(socket_path)
    except zmq.error.ZMQError as e:
        print(
            f"[nri-wait] Failed to connect to query socket: {e} (may not be ready yet)",
            file=sys.stderr,
        )
        req.close()
        return False  # Assume not done, will wait on pub socket

    # Send the full OCI state so nix-nri can extract id, pid, and bundle itself.
    query = json.dumps(oci_state)

    try:
        req.send(query.encode())
        response_bytes = req.recv()
    except zmq.error.Again:
        print("[nri-wait] Query socket timeout", file=sys.stderr)
        req.close()
        return False
    except zmq.error.ZMQError as e:
        print(f"[nri-wait] Query error: {e}", file=sys.stderr)
        req.close()
        return False

    req.close()

    # Parse response
    try:
        response = json.loads(response_bytes.decode())
        print(f"[nri-wait] Query response: {response}", file=sys.stderr)

        if response.get("status") == "done":
            return True
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as e:
        print(f"[nri-wait] Failed to parse response: {e}", file=sys.stderr)

    return False


def wait_for_completion(
    context: zmq.Context, pub_socket_path: str, container_id: str, timeout: int
) -> None:
    """Subscribe to PUB socket and wait for build completion message.

    Raises SystemExit(1) on timeout or error.
    """
    sub = context.socket(zmq.SUB)

    # Subscribe to all messages (empty filter = all)
    sub.setsockopt(zmq.SUBSCRIBE, b"")

    # Set timeout
    timeout_ms = timeout * 1000
    sub.setsockopt(zmq.RCVTIMEO, timeout_ms)

    socket_path = f"ipc://{pub_socket_path}"
    print(f"[nri-wait] Connecting to pub socket: {socket_path}", file=sys.stderr)

    try:
        sub.connect(socket_path)
    except zmq.error.ZMQError as e:
        print(f"[nri-wait] Failed to connect to pub socket: {e}", file=sys.stderr)
        sub.close()
        sys.exit(1)

    # Wait for completion message with rolling timeout on progress updates
    absolute_deadline = time.time() + timeout  # Absolute deadline (safety timeout)
    progress_deadline = time.time() + timeout  # Resets on progress messages

    while True:
        # Check absolute safety deadline
        now = time.time()
        if now >= absolute_deadline:
            print(
                f"[nri-wait] Absolute timeout waiting for build completion ({timeout}s)",
                file=sys.stderr,
            )
            sub.close()
            sys.exit(1)

        # Use whichever deadline is sooner
        next_deadline = min(absolute_deadline, progress_deadline)
        remaining = next_deadline - now
        remaining_ms = max(1, int(remaining * 1000))
        sub.setsockopt(zmq.RCVTIMEO, remaining_ms)

        try:
            msg_bytes = sub.recv()

            # Try to parse as JSON
            try:
                msg = json.loads(msg_bytes.decode())
                print(f"[nri-wait] Received message: {msg}", file=sys.stderr)

                if msg.get("container_id") == container_id:
                    # Exit immediately on "done" status
                    if msg.get("status") == "done":
                        print(
                            f"[nri-wait] Build completed for container {container_id}",
                            file=sys.stderr,
                        )
                        sub.close()
                        return

                    # Reset progress deadline on status updates (e.g., "progress", "building")
                    if msg.get("status") in ("progress", "building"):
                        progress_deadline = time.time() + timeout
                        print(
                            f"[nri-wait] Progress update for container {container_id}, "
                            f"progress timeout reset to {timeout}s",
                            file=sys.stderr,
                        )

            except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
                # Ignore unparsable messages
                pass

        except zmq.error.Again:
            # Timeout from recv - check which deadline we hit
            if time.time() >= progress_deadline:
                print(
                    f"[nri-wait] Progress timeout waiting for build completion ({timeout}s)",
                    file=sys.stderr,
                )
                sub.close()
                sys.exit(1)
            # Otherwise loop and check absolute deadline

        except zmq.error.ZMQError as e:
            print(f"[nri-wait] Socket error: {e}", file=sys.stderr)
            sub.close()
            sys.exit(1)
