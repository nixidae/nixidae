# SPDX-License-Identifier: MIT
"""NRI pod annotation parsing for Nix store mounts and RW /nix configuration."""

from pathlib import Path


def _parse_store_mounts_for_name(
    pod_annotations, target_name: str, system: str
) -> dict[Path, Path]:
    """
    Parse store mount annotations matching a specific name (container name or "pod" for wildcard).

    Annotations format: nixkube/{target-name}(-{suffix})?(@{system})?: /path/in/container=/source
    - suffix: optional, allows multiple mounts to same destination (ignored by parser)
    - system: optional, filters annotation to specific system (e.g., x86_64-linux, aarch64-linux)
    - source: auto-detected as store path, flake reference, or nix expression

    For wildcard (target_name="pod"):
      nixkube/pod-1: /etc/ssl/certs=/nix/store/cacert-1.0/etc/ssl/certs
      nixkube/pod-2@x86_64-linux: /etc/passwd=/nix/store/fakeNss-x86/etc/passwd

    For container (target_name="myapp"):
      nixkube/myapp-1@aarch64-linux: /etc/ssl=/nix/store/cacert-aarch64/etc/ssl

    Returns: {Path("/path/in/container"): Path("/nix/store/.../package")}
    Annotations without @system apply to all systems.
    """
    mounts: dict[Path, Path] = {}
    prefix = f"nixkube/{target_name}"

    for key, value in pod_annotations.items():
        # Match annotations starting with prefix, optionally followed by -suffix and/or @system
        if (
            key == prefix
            or key.startswith(prefix + "-")
            or key.startswith(prefix + "@")
        ):
            # Parse system suffix if present
            key_system = None
            if "@" in key:
                _, system_part = key.rsplit("@", 1)
                key_system = system_part

            # Skip if system filter is specified and doesn't match
            if key_system is not None and key_system != system:
                continue

            if "=" in value:
                container_path_str, source_str = value.split("=", 1)
                mounts[Path(container_path_str)] = Path(source_str)

    return mounts


def parse_nix_rw(pod_annotations, container_name: str, system: str) -> bool:
    """Return True if this container should get a read-write /nix overlayfs.

    Container-specific annotation takes precedence over the pod-wide default,
    including an explicit "false" to opt a single container out of pod-wide RW.

    Supports system-specific variants with @{system} suffix.

    Annotations:
      nixkube/pod-rw: "true"                 — all containers get RW /nix
      nixkube/pod-rw@x86_64-linux: "true"   — all containers get RW /nix on x86_64
      nixkube/{container-name}-rw: "true"   — only this container gets RW /nix
      nixkube/{container-name}-rw@aarch64-linux: "false" — this container stays RO on aarch64
    """

    # Helper to check annotation with optional system suffix
    def check_annotation(key: str) -> bool:
        if key in pod_annotations:
            return pod_annotations[key] == "true"
        # Also check system-specific variant
        system_key = f"{key}@{system}"
        if system_key in pod_annotations:
            return pod_annotations[system_key] == "true"
        return False

    # Check container-specific first (takes precedence)
    container_key = f"nixkube/{container_name}-rw"
    if (
        container_key in pod_annotations
        or f"{container_key}@{system}" in pod_annotations
    ):
        return check_annotation(container_key)

    # Fall back to pod-wide setting
    return check_annotation("nixkube/pod-rw")


def parse_store_mounts(
    pod_annotations, container_name: str, system: str
) -> dict[Path, Path]:
    """
    Parse store mount annotations from pod metadata with system filtering.

    Supports annotation patterns:
    1. Pod-wide (apply to all containers):  nixkube/pod: /etc/ssl=/source
    2. Container-specific (overrides pod-wide): nixkube/container-name: /etc/ssl=/source
    3. System-specific variants with @{system} suffix

    Source is auto-detected: store path, flake reference, or nix expression.

    Example annotations:
      nixkube/pod: /etc/ssl/certs=/nix/store/abc-cacert-1.0/etc/ssl/certs
      nixkube/pod@x86_64-linux: /etc/passwd=/nix/store/fakeNss-x86/etc/passwd
      nixkube/myapp-1@aarch64-linux: /etc/ssl=/nix/store/cacert-aarch64

    Returns dict: {Path("/path/in/container"): Path("/source")}
    - Container-specific annotations override pod-wide annotations for the same path
    - System-specific annotations apply only to matching system
    - Annotations without @system apply to all systems
    """
    # Get pod-wide mounts first, filtered by system
    pod_mounts = _parse_store_mounts_for_name(pod_annotations, "pod", system)

    # Get container-specific mounts (these override pod-wide), filtered by system
    container_mounts = _parse_store_mounts_for_name(
        pod_annotations, container_name, system
    )

    # Merge: container-specific overrides pod-wide
    return {**pod_mounts, **container_mounts}
