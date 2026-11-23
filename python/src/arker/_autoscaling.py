"""Autoscaling updates distinguish an omitted rule from one explicitly cleared."""

from __future__ import annotations

from dataclasses import dataclass

from ._unset import UNSET, Unset
from .generated import api_models


@dataclass(frozen=True)
class ResourcesAutoscalingUpdate:
    vcpu: api_models.AutoscalingRule | Unset | None = UNSET
    memory_mib: api_models.AutoscalingRule | Unset | None = UNSET
    disk_mib: api_models.DiskAutoscalingRule | Unset | None = UNSET
