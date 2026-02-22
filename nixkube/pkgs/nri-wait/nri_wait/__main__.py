"""Entry point for nri-wait OCI hook."""

import os
import sys

import zmq

from . import check_build_status, wait_for_completion


def main() -> None:
    """Main entry point - orchestrates query and wait phases."""
    # Read from environment variables (set by OCI hook)
    try:
        container_id = os.environ["NRI_CONTAINER_ID"]
        query_socket = os.environ["NRI_QUERY_SOCKET"]
        pub_socket = os.environ["NRI_PUB_SOCKET"]
        timeout = int(os.environ.get("NRI_TIMEOUT", "30"))
    except KeyError as e:
        print(f"Error: Required environment variable not set: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError:
        print("Error: NRI_TIMEOUT must be a valid integer", file=sys.stderr)
        sys.exit(1)

    # Create ZeroMQ context
    context = zmq.Context()

    try:
        # Phase 1: Query if build is already done
        if check_build_status(context, query_socket, container_id, timeout):
            print(
                f"[nri-wait] Build already completed for {container_id}",
                file=sys.stderr,
            )
            return

        print(
            f"[nri-wait] Build pending for {container_id}, subscribing to updates...",
            file=sys.stderr,
        )

        # Phase 2: Subscribe to PUB socket and wait for completion
        wait_for_completion(context, pub_socket, container_id, timeout)
    finally:
        context.term()


if __name__ == "__main__":
    main()
