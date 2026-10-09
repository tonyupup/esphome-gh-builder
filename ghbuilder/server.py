"""Run the stock ``esphome-device-builder --remote-build-only`` with compiles sent to GitHub Actions.

The only change to upstream behaviour: the esphome command the receiver spawns for a job is
replaced by :mod:`ghbuilder.shim`, pinned to the esphome version the offloader asked for
(so the receiver never provisions a local venv).
"""

from __future__ import annotations

import sys

from esphome_device_builder.__main__ import main as device_builder_main
from esphome_device_builder.controllers.firmware.controller import FirmwareController


async def _resolve_esphome_cmd(self, job):  # noqa: ANN001
    version = job.target_esphome_version or _installed_version()
    return [sys.executable, "-m", "ghbuilder.shim", "--esphome-version", version]


def _installed_version() -> str:
    from esphome.const import __version__

    return __version__


def main() -> None:
    FirmwareController._resolve_esphome_cmd = _resolve_esphome_cmd  # type: ignore[method-assign]
    device_builder_main()


if __name__ == "__main__":
    main()
