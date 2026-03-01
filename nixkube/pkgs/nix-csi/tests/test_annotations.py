# SPDX-License-Identifier: MIT

from pathlib import Path

from nix_csi.nriplugin import (
    _parse_store_mounts_for_name,
    parse_nix_rw,
    parse_store_mounts,
)


class TestParseStoreMountsForName:
    """Test _parse_store_mounts_for_name annotation parsing."""

    def test_basic_store_path_mount(self):
        """Parse a basic store path mount."""
        annotations = {"nix-nri/pod": "/etc/ssl=/nix/store/cacert-1.0/etc/ssl"}
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result == {Path("/etc/ssl"): Path("/nix/store/cacert-1.0/etc/ssl")}

    def test_multiple_suffixes(self):
        """Parse multiple mounts with different suffixes."""
        annotations = {
            "nix-nri/pod-ssl": "/etc/ssl/certs=/nix/store/cacert-1.0/etc/ssl/certs",
            "nix-nri/pod-passwd": "/etc/passwd=/nix/store/fakeNss/etc/passwd",
        }
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert len(result) == 2
        assert Path("/etc/ssl/certs") in result
        assert Path("/etc/passwd") in result

    def test_system_specific_mount(self):
        """Parse system-specific annotations."""
        annotations = {
            "nix-nri/pod@x86_64-linux": "/etc/myapp=/nix/store/x86-hash",
            "nix-nri/pod@aarch64-linux": "/etc/myapp=/nix/store/aarch64-hash",
        }
        # Should match x86_64-linux
        result_x86 = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result_x86 == {Path("/etc/myapp"): Path("/nix/store/x86-hash")}

        # Should match aarch64-linux
        result_arm = _parse_store_mounts_for_name(annotations, "pod", "aarch64-linux")
        assert result_arm == {Path("/etc/myapp"): Path("/nix/store/aarch64-hash")}

    def test_system_mismatch_filtered(self):
        """System-specific annotations are filtered out if system doesn't match."""
        annotations = {
            "nix-nri/pod@aarch64-linux": "/etc/myapp=/nix/store/aarch64-hash",
        }
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result == {}

    def test_all_systems_backward_compat(self):
        """Annotations without @system apply to all systems."""
        annotations = {"nix-nri/pod": "/etc/ssl=/nix/store/cacert/etc/ssl"}
        result_x86 = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        result_arm = _parse_store_mounts_for_name(annotations, "pod", "aarch64-linux")

        assert result_x86 == {Path("/etc/ssl"): Path("/nix/store/cacert/etc/ssl")}
        assert result_arm == {Path("/etc/ssl"): Path("/nix/store/cacert/etc/ssl")}

    def test_system_specific_with_suffix(self):
        """Parse system-specific annotations with multiple mount suffix."""
        annotations = {
            "nix-nri/pod-ssl-1@x86_64-linux": "/etc/ssl=/nix/store/x86-cacert",
            "nix-nri/pod-ssl-1@aarch64-linux": "/etc/ssl=/nix/store/arm-cacert",
        }
        result_x86 = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result_x86 == {Path("/etc/ssl"): Path("/nix/store/x86-cacert")}

    def test_container_specific_prefix(self):
        """Parse container-specific annotations."""
        annotations = {
            "nix-nri/myapp": "/etc/myapp=/nix/store/myapp-1.0",
            "nix-nri/myapp-ssl": "/etc/ssl=/nix/store/cacert",
        }
        result = _parse_store_mounts_for_name(annotations, "myapp", "x86_64-linux")
        assert len(result) == 2
        assert result[Path("/etc/myapp")] == Path("/nix/store/myapp-1.0")
        assert result[Path("/etc/ssl")] == Path("/nix/store/cacert")

    def test_container_specific_with_system(self):
        """Parse container-specific system-filtered annotations."""
        annotations = {
            "nix-nri/myapp@x86_64-linux": "/etc/myapp=/nix/store/x86-app",
            "nix-nri/myapp@aarch64-linux": "/etc/myapp=/nix/store/arm-app",
        }
        result_x86 = _parse_store_mounts_for_name(annotations, "myapp", "x86_64-linux")
        assert result_x86 == {Path("/etc/myapp"): Path("/nix/store/x86-app")}

    def test_invalid_annotation_skipped(self):
        """Annotations without '=' are skipped."""
        annotations = {
            "nix-nri/pod": "invalid",
            "nix-nri/pod-valid": "/etc/ssl=/nix/store/cacert",
        }
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert len(result) == 1
        assert result[Path("/etc/ssl")] == Path("/nix/store/cacert")

    def test_flake_reference_detection(self):
        """Flake references are preserved as source paths."""
        annotations = {
            "nix-nri/pod": "/etc/myapp=github:nixos/nixpkgs#legacyPackages.x86_64-linux.hello"
        }
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result == {
            Path("/etc/myapp"): Path(
                "github:nixos/nixpkgs#legacyPackages.x86_64-linux.hello"
            )
        }

    def test_nix_expression_detection(self):
        """Nix expressions are preserved as source paths."""
        annotations = {"nix-nri/pod": '/etc/result=builtins.toFile "test" "value"'}
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result == {Path("/etc/result"): Path('builtins.toFile "test" "value"')}


class TestParseStoreMounts:
    """Test parse_store_mounts with wildcard and container-specific merging."""

    def test_wildcard_and_container_merge(self):
        """Container-specific mounts override wildcard mounts."""
        annotations = {
            "nix-nri/pod": "/etc/ssl=/nix/store/cacert-default",
            "nix-nri/myapp": "/etc/ssl=/nix/store/cacert-myapp",
        }
        result = parse_store_mounts(annotations, "myapp", "x86_64-linux")
        # Container-specific should override wildcard
        assert result[Path("/etc/ssl")] == Path("/nix/store/cacert-myapp")

    def test_wildcard_plus_container_specific_paths(self):
        """Merge wildcard and container-specific mounts for different paths."""
        annotations = {
            "nix-nri/pod": "/etc/ssl=/nix/store/cacert",
            "nix-nri/myapp": "/opt/app=/nix/store/myapp",
        }
        result = parse_store_mounts(annotations, "myapp", "x86_64-linux")
        assert len(result) == 2
        assert result[Path("/etc/ssl")] == Path("/nix/store/cacert")
        assert result[Path("/opt/app")] == Path("/nix/store/myapp")

    def test_system_specific_wildcard_override(self):
        """System-specific wildcard annotations apply when system matches."""
        annotations = {
            "nix-nri/pod": "/etc/ssl=/nix/store/cacert-default",
            "nix-nri/pod@x86_64-linux": "/etc/ssl=/nix/store/cacert-x86",
        }
        result_x86 = parse_store_mounts(annotations, "myapp", "x86_64-linux")
        result_arm = parse_store_mounts(annotations, "myapp", "aarch64-linux")

        # x86 should get system-specific version
        assert result_x86[Path("/etc/ssl")] == Path("/nix/store/cacert-x86")
        # aarch64 should get default (no system-specific match)
        assert result_arm[Path("/etc/ssl")] == Path("/nix/store/cacert-default")

    def test_system_specific_container_override(self):
        """System-specific container annotations override wildcards."""
        annotations = {
            "nix-nri/pod@x86_64-linux": "/opt/lib=/nix/store/lib-x86",
            "nix-nri/myapp@x86_64-linux": "/opt/lib=/nix/store/lib-myapp-x86",
        }
        result = parse_store_mounts(annotations, "myapp", "x86_64-linux")
        assert result[Path("/opt/lib")] == Path("/nix/store/lib-myapp-x86")


class TestParseNixRW:
    """Test parse_nix_rw RW flag parsing."""

    def test_pod_wide_rw_enabled(self):
        """Pod-wide RW flag enables RW for all containers."""
        annotations = {"nix-nri/pod-rw": "true"}
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is True

    def test_pod_wide_rw_disabled(self):
        """Absent RW flag defaults to false."""
        annotations = {}
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is False

    def test_pod_wide_rw_explicit_false(self):
        """Explicit false disables RW."""
        annotations = {"nix-nri/pod-rw": "false"}
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is False

    def test_container_specific_override(self):
        """Container-specific RW overrides pod-wide setting."""
        annotations = {
            "nix-nri/pod-rw": "true",
            "nix-nri/myapp-rw": "false",
        }
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is False

    def test_system_specific_pod_rw(self):
        """System-specific pod-wide RW flag applies to matching system."""
        annotations = {
            "nix-nri/pod-rw@x86_64-linux": "true",
            "nix-nri/pod-rw@aarch64-linux": "false",
        }
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is True
        assert parse_nix_rw(annotations, "myapp", "aarch64-linux") is False

    def test_system_specific_container_rw(self):
        """System-specific container-specific RW overrides pod-wide."""
        annotations = {
            "nix-nri/pod-rw": "true",
            "nix-nri/myapp-rw@aarch64-linux": "false",
        }
        # x86 gets pod-wide true
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is True
        # aarch64 gets container-specific false
        assert parse_nix_rw(annotations, "myapp", "aarch64-linux") is False

    def test_container_takes_precedence_over_system_pod(self):
        """Container-specific (any system) takes precedence over system-specific pod."""
        annotations = {
            "nix-nri/pod-rw@x86_64-linux": "true",
            "nix-nri/myapp-rw": "false",
        }
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is False

    def test_system_specific_container_without_system_specific_pod(self):
        """System-specific container RW works with default pod RW."""
        annotations = {
            "nix-nri/pod-rw": "false",
            "nix-nri/myapp-rw@x86_64-linux": "true",
        }
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is True
        assert parse_nix_rw(annotations, "myapp", "aarch64-linux") is False
