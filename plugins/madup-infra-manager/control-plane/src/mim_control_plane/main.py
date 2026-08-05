"""Application entrypoint for the fixed MIM runtime image."""

from __future__ import annotations

from mim_control_plane.runtime import build_runtime_app_from_environment

app = build_runtime_app_from_environment()
