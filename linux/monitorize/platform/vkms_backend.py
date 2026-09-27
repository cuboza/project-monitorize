"""VKMS lifecycle: existing stock DRM outputs or standalone custom EDID CLI."""

from __future__ import annotations

from enum import Enum
import json
import logging
import os
from pathlib import Path
import re
import select
import signal
import sys
import webbrowser

from monitorize.platform.monitorize_vkms_cli import (
    MonitorizeVkmsClient,
    MonitorizeVkmsError,
    VkmsCommandError,
)

log = logging.getLogger(__name__)

VKMS_SLOTS = ("primary",)
MONITORIZE_VKMS_INSTALL_URL = "https://github.com/vinnavannewton/monitorize-vkms"

_DRM_CONNECTOR_NAME = re.compile(r"^card\d+-.+")
_DRM_MODE_NAME = re.compile(r"^(\d+)x(\d+)$")


class VkmsError(MonitorizeVkmsError):
    """Base error for VKMS operations in Monitorize."""


class VkmsCustomEdidUnsupported(VkmsError):
    """The installed VKMS driver does not support custom EDID generation."""


class CustomEdidCapability(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CHECK_FAILED = "check_failed"


def custom_edid_capability_from_response(response: dict) -> CustomEdidCapability:
    """Validate a capability response dictionary."""
    value = str(response.get("capability") or "").lower()
    if value == CustomEdidCapability.SUPPORTED.value:
        return CustomEdidCapability.SUPPORTED
    if value == CustomEdidCapability.UNSUPPORTED.value:
        return CustomEdidCapability.UNSUPPORTED
    raise VkmsError("VKMS capability helper returned an invalid capability result.")


def check_custom_edid_support(client: MonitorizeVkmsClient | None = None) -> CustomEdidCapability:
    """Check whether the standalone monitorize-vkms backend is available and supported."""
    c = client or MonitorizeVkmsClient()
    cap = c.check_capability()
    if cap == "supported":
        return CustomEdidCapability.SUPPORTED
    if cap == "unsupported":
        return CustomEdidCapability.UNSUPPORTED
    return CustomEdidCapability.CHECK_FAILED


def open_monitorize_vkms_install_page() -> bool:
    """Open the monitorize-vkms installation page URL."""
    try:
        return webbrowser.open(MONITORIZE_VKMS_INSTALL_URL)
    except Exception:
        return False


def stock_vkms_connectors(drm_root: Path | None = None) -> list[dict]:
    """Find DRM connectors belonging to the in-tree VKMS driver."""
    found = []
    root = drm_root or Path("/sys/class/drm")
    try:
        connectors = sorted(root.iterdir())
    except OSError:
        return found

    for connector in connectors:
        if not _DRM_CONNECTOR_NAME.fullmatch(connector.name):
            continue
        card_name = connector.name.split("-", 1)[0]
        try:
            driver = (root / card_name / "device" / "driver").resolve(strict=True)
            if driver.name != "vkms":
                continue
            if (connector / "status").read_text(encoding="utf-8").strip() != "connected":
                continue
            raw_modes = (connector / "modes").read_text(encoding="utf-8").splitlines()
            connector_number = int((connector / "connector_id").read_text().strip())
        except (OSError, ValueError):
            continue
        modes = set()
        for raw_mode in raw_modes:
            match = _DRM_MODE_NAME.fullmatch(raw_mode.strip())
            if match:
                modes.add((int(match.group(1)), int(match.group(2))))
        found.append({
            "id": connector.name,
            "name": connector.name.split("-", 1)[1],
            "connector_id": connector_number,
            "modes": [f"{width}x{height}" for width, height in sorted(
                modes, key=lambda mode: (mode[0] * mode[1], mode), reverse=True
            )],
        })
    return found


def resolution_options(drm_root: Path | None = None, connector_id: str = "") -> list[str]:
    """Return only modes advertised by the selected stock VKMS connector."""
    connectors = stock_vkms_connectors(drm_root)
    if connector_id:
        connectors = [entry for entry in connectors if entry["id"] == connector_id]
    else:
        connectors = []
    modes: set[tuple[int, int]] = set()
    for connector in connectors:
        for raw_mode in connector["modes"]:
            match = _DRM_MODE_NAME.fullmatch(raw_mode)
            if match:
                modes.add((int(match.group(1)), int(match.group(2))))

    ordered = sorted(modes, key=lambda mode: (mode[0] * mode[1], mode), reverse=True)
    return [*(f"{width}x{height}" for width, height in ordered), "Custom..."]


def run_stock_vkms_headless(
    connector_id: str, width: int, height: int, fps: int | float, desktop: str
) -> int:
    """Temporarily configure one existing stock VKMS output for a session."""
    from monitorize.platform.stock_vkms_output import StockVkmsError, StockVkmsOutput

    candidates = [entry for entry in stock_vkms_connectors()
                  if entry["id"] == connector_id]
    if len(candidates) != 1:
        print("[ERROR] Select a connected stock VKMS connector before starting.", flush=True)
        return 1
    candidate = candidates[0]
    if f"{width}x{height}" not in candidate["modes"]:
        print(f"[ERROR] {connector_id} no longer advertises {width}x{height}.", flush=True)
        return 1

    controller = StockVkmsOutput(connector_id, desktop, candidate["connector_id"])
    capture = None
    changed = False
    stopping = False
    restore_ok = True

    def cleanup(*_args):
        nonlocal stopping, restore_ok
        if stopping:
            return restore_ok
        stopping = True
        if capture is not None:
            try:
                capture.close()
            except Exception as exc:
                print(f"[ERROR] Could not stop GNOME capture: {exc}", flush=True)
        if changed:
            try:
                controller.restore()
                print(f"[VKMS] Restored {connector_id} desktop state", flush=True)
            except Exception as exc:
                restore_ok = False
                print(f"[ERROR] Could not restore {connector_id}: {exc}", flush=True)
        return restore_ok

    def stop_from_signal(*_args):
        raise SystemExit(0 if cleanup() else 1)

    signal.signal(signal.SIGINT, stop_from_signal)
    signal.signal(signal.SIGTERM, stop_from_signal)
    try:
        controller.snapshot()
        changed = True
        actual = controller.apply(width, height, float(fps))
        if not any(entry["id"] == connector_id
                   and entry["connector_id"] == candidate["connector_id"]
                   for entry in stock_vkms_connectors()):
            raise StockVkmsError("The selected stock VKMS connector disappeared")
        output_name = actual["name"]
        event = {
            "type": "headless_ready", "name": output_name,
            "width": actual["width"], "height": actual["height"],
            "fps": actual["refresh_rate"], "backend": "Sunshine", "vkms": True,
        }
        if "gnome" in desktop.lower():
            from monitorize.platform.gnome_monitor_capture import GnomeMonitorCapture
            capture = GnomeMonitorCapture()
            event.update(capture.start(output_name))
        print(f"MONITORIZE_EVENT {json.dumps(event, separators=(',', ':'))}", flush=True)
        while True:
            if capture is not None:
                capture.dispatch()
            if not any(entry["id"] == connector_id
                       and entry["connector_id"] == candidate["connector_id"]
                       for entry in stock_vkms_connectors()):
                raise StockVkmsError("The selected stock VKMS connector disappeared")
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
            if ready:
                line = sys.stdin.readline()
                if not line or line.strip() == "quit":
                    break
        return 0 if cleanup() else 1
    except Exception as exc:
        print(f"[ERROR] Stock VKMS session failed: {exc}", flush=True)
        return 1
    finally:
        cleanup()


def run_vkms_headless(
    slot: str,
    width: int,
    height: int,
    fps: int | float,
    desktop: str = "",
    *,
    custom_mode: bool = False,
    connector_id: str = "",
    client: MonitorizeVkmsClient | None = None,
) -> int:
    """Hold a VKMS display for one Monitorize session.

    Stock modes temporarily configure the selected existing DRM connector.
    Custom modes use the standalone CLI. The custom path owns this lifetime:
    1. Creates the display via `monitorize-vkms create`
    2. Emits MONITORIZE_EVENT headless_ready
    3. Remains alive waiting for stdin EOF or termination signals
    4. Automatically removes the display via `monitorize-vkms remove` on exit.
    """
    if slot not in VKMS_SLOTS:
        print(f"[ERROR] Unsupported VKMS display slot: {slot}", flush=True)
        return 1

    if not custom_mode:
        return run_stock_vkms_headless(connector_id, width, height, fps, desktop)

    vkms_client = client or MonitorizeVkmsClient()
    if not vkms_client.is_available():
        print(
            "[ERROR] VKMS Experimental requires the standalone monitorize-vkms package. "
            "Install it from https://github.com/vinnavannewton/monitorize-vkms.",
            flush=True,
        )
        return 1

    capture = None
    stopping = [False]
    created = [False]
    created_connector = [None]

    def cleanup(*_args):
        if stopping[0]:
            return
        stopping[0] = True
        if capture is not None:
            capture.close()
        if created[0]:
            conn_str = f" {created_connector[0]}" if created_connector[0] else ""
            print(f"[VKMS] Removing{conn_str} through monitorize-vkms", flush=True)
            res = vkms_client.remove_display(created_connector[0])
            if not res.get("success", True):
                print(
                    f"[ERROR] VKMS cleanup failed: {res.get('message', 'unknown error')}",
                    flush=True,
                )
            else:
                print(f"[VKMS] Virtual display{conn_str} removed", flush=True)

    def stop_from_signal(*_args):
        cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop_from_signal)
    signal.signal(signal.SIGTERM, stop_from_signal)

    try:
        print(f"[VKMS] Using standalone monitorize-vkms backend", flush=True)
        print(f"[VKMS] Requesting display: {width}x{height}@{fps}", flush=True)

        result = vkms_client.create_display(width, height, fps)
        output_name = result["name"]
        created_connector[0] = output_name
        created[0] = True
        actual_width = result["width"]
        actual_height = result["height"]
        actual_fps = result["fps"]

        event = {
            "type": "headless_ready",
            "name": output_name,
            "width": actual_width,
            "height": actual_height,
            "fps": actual_fps,
            "backend": "Sunshine",
            "vkms": True,
        }
        if desktop.lower() == "gnome":
            from monitorize.platform.gnome_monitor_capture import GnomeMonitorCapture
            capture = GnomeMonitorCapture()
            event.update(capture.start(output_name))
        print(f"MONITORIZE_EVENT {json.dumps(event, separators=(',', ':'))}", flush=True)
        print(
            f"[VKMS] {output_name} is active at {actual_width}x{actual_height}@{actual_fps:g}Hz.",
            flush=True,
        )

        while not stopping[0]:
            if capture is not None:
                capture.dispatch()
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
            if ready:
                line = sys.stdin.readline()
                if not line or line.strip() == "quit":
                    break
        return 0
    except VkmsCommandError as exc:
        if exc.error_type == "edid_error" or "custom edid" in str(exc).lower():
            print(
                "MONITORIZE_EVENT "
                + json.dumps(
                    {"type": "vkms_custom_edid_unsupported"}, separators=(",", ":")
                ),
                flush=True,
            )
        print(f"[ERROR] VKMS virtual display failed: {exc}", flush=True)
        return 1
    except MonitorizeVkmsError as exc:
        print(f"[ERROR] VKMS virtual display failed: {exc}", flush=True)
        return 1
    except Exception as exc:
        print(f"[ERROR] VKMS virtual display failed: {exc}", flush=True)
        return 1
    finally:
        cleanup()
