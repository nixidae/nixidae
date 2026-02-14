#!/usr/bin/env python3
import logging
import os
import sqlite3
import subprocess
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("validpaths-monitor")

DB = "/nix/var/nix/db/db.sqlite"
SCRIPT = "/etc/nix-csi/cache-push.sh"
SEEN_IDS_FILE = "/nix/var/nix-csi/seen-validpath-ids"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))


def load_seen_ids():
    seen = set()
    if Path(SEEN_IDS_FILE).exists():
        try:
            with open(SEEN_IDS_FILE) as f:
                seen = set(int(line.strip()) for line in f if line.strip())
        except Exception as e:
            logger.error(f"Failed to load seen IDs: {e}")
    return seen


def save_seen_ids(seen):
    try:
        Path(SEEN_IDS_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(SEEN_IDS_FILE, "w") as f:
            f.write("\n".join(str(id) for id in sorted(seen)))
    except Exception as e:
        logger.error(f"Failed to save seen IDs: {e}")


def main():
    logger.info("Starting ValidPaths monitor")
    logger.info(f"Database: {DB}")
    logger.info(f"Cache push script: {SCRIPT}")
    logger.info(f"Poll interval: {POLL_INTERVAL}s")

    seen = load_seen_ids()
    logger.info(f"Loaded {len(seen)} seen ValidPath IDs")

    while True:
        try:
            conn = sqlite3.connect(DB)
            max_seen = max(seen) if seen else 0
            cursor = conn.execute(
                "SELECT id, path FROM ValidPaths WHERE id > ? ORDER BY id", (max_seen,)
            )

            new_count = 0
            for row in cursor:
                id, path = row
                if id not in seen:
                    logger.info(f"New path detected: {path} (id={id})")
                    try:
                        # Run cache push script
                        result = subprocess.run(
                            [SCRIPT, path],
                            capture_output=True,
                            text=True,
                            timeout=300,  # 5 minute timeout
                        )
                        if result.returncode == 0:
                            logger.info(f"Successfully pushed {path} to cache")
                            seen.add(id)
                            new_count += 1
                        else:
                            logger.error(
                                f"Cache push failed for {path}: {result.stderr}"
                            )
                    except subprocess.TimeoutExpired:
                        logger.error(f"Cache push timed out for {path}")
                    except Exception as e:
                        logger.error(f"Cache push failed for {path}: {e}")

            conn.close()

            if new_count > 0:
                save_seen_ids(seen)
                logger.info(f"Processed {new_count} new paths")

        except Exception as e:
            logger.error(f"Error querying database: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
