"""Data models for the CSI driver."""

from dataclasses import dataclass


@dataclass
class PodInfo:
    """Container for pod identification information for events."""

    name: str
    namespace: str
    uid: str
