"""CSI driver exception hierarchy with Kubernetes event mapping."""

from grpclib import GRPCError
from grpclib.const import Status


class CSIError(GRPCError):
    """Base CSI error with Kubernetes event mapping capability.

    Inherits from GRPCError so unhandled exceptions are properly
    reported through gRPC. Each subclass has a 'reason' field that
    maps to Kubernetes event reason codes.
    """

    reason: str = "InternalError"

    def __init__(
        self,
        status: Status,
        message: str,
        logs: dict[str, str] | None = None,
        details: str | None = None,
    ) -> None:
        """Initialize CSI error.

        Args:
            status: gRPC status code
            message: Human-readable error message for events
            logs: Dict with 'stdout', 'stderr', 'combined' from subprocess
            details: Additional details for gRPC response
        """
        self.message = message
        self.logs = logs or {}
        super().__init__(status, message, details)


# Store path closure errors
class StorePathClosureError(CSIError):
    """Error retrieving store path closure with 'nix path-info --recursive'."""

    reason = "StorePathClosureFailed"


class VerifyStorePathsError(CSIError):
    """Error verifying store path integrity with 'nix store verify --recursive'."""

    reason = "StorePathVerificationFailed"


class HardlinkClosureError(CSIError):
    """Error hardlinking store paths to volume root."""

    reason = "HardlinkFailed"


class InitDatabaseError(CSIError):
    """Error initializing Nix database in volume."""

    reason = "InitDatabaseFailed"


class InstallGCRootError(CSIError):
    """Error installing garbage collection root."""

    reason = "GCRootInstallationFailed"


class InstallResultLinkError(CSIError):
    """Error installing /nix/var/result symlink in volume."""

    reason = "ResultLinkInstallationFailed"


class VolumePreparationError(CSIError):
    """Error during overall volume preparation."""

    reason = "VolumePreparationFailed"


class MountError(CSIError):
    """Error mounting volume to target path."""

    reason = "VolumeMountFailed"


# Build operation errors
class StoreBuildError(CSIError):
    """Error building a store path directly."""

    reason = "StoreBuildFailed"


class FlakeBuildError(CSIError):
    """Error building from a flake reference."""

    reason = "FlakeBuildFailed"


class ExprBuildError(CSIError):
    """Error evaluating a Nix expression."""

    reason = "ExprBuildFailed"


class SystemDetectionError(CSIError):
    """Error detecting system type."""

    reason = "SystemDetectionFailed"
