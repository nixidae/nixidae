"""NRI wait - OCI hook for waiting on Nix builds."""

import json
import sys
import time

import zmq


__version__ = "0.1.0"


def check_build_status(context: zmq.Context, query_socket_path: str, container_id: str, timeout: int) -> bool:
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
        print(f"[nri-wait] Failed to connect to query socket: {e} (may not be ready yet)", file=sys.stderr)
        req.close()
        return False  # Assume not done, will wait on pub socket

    # Send query
    query = json.dumps({"container_id": container_id})

    try:
        req.send(query.encode())
        response_bytes = req.recv()
    except zmq.error.Again:
        print(f"[nri-wait] Query socket timeout", file=sys.stderr)
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


def wait_for_completion(context: zmq.Context, pub_socket_path: str, container_id: str, timeout: int) -> None:
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

    # Wait for completion message
    deadline = time.time() + timeout

    while True:
        try:
            msg_bytes = sub.recv()

            # Try to parse as JSON
            try:
                msg = json.loads(msg_bytes.decode())
                print(f"[nri-wait] Received message: {msg}", file=sys.stderr)

                if msg.get("container_id") == container_id and msg.get("status") == "done":
                    print(f"[nri-wait] Build completed for container {container_id}", file=sys.stderr)
                    sub.close()
                    return
            except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
                # Ignore unparseable messages
                pass

        except zmq.error.Again:
            # Timeout occurred
            remaining = deadline - time.time()
            if remaining <= 0:
                print(f"[nri-wait] Timeout waiting for build completion ({timeout}s)", file=sys.stderr)
                sub.close()
                sys.exit(1)

            # Re-set timeout for next iteration with remaining time
            remaining_ms = max(1, int(remaining * 1000))
            sub.setsockopt(zmq.RCVTIMEO, remaining_ms)

        except zmq.error.ZMQError as e:
            print(f"[nri-wait] Socket error: {e}", file=sys.stderr)
            sub.close()
            sys.exit(1)
