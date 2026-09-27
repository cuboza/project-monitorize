"""Read XRandR output identity and advertised timings for stock VKMS."""

from __future__ import annotations

import re
import subprocess

from monitorize.platform.stock_vkms_output import StockVkmsError


def xrandr_outputs() -> list[dict]:
    try:
        result = subprocess.run(["xrandr", "--query", "--prop"], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StockVkmsError(f"Could not query XRandR: {exc}") from exc
    if result.returncode:
        raise StockVkmsError(result.stderr.strip() or "XRandR query failed")
    outputs = []
    current = None
    for line in result.stdout.splitlines():
        header = re.match(r"^(\S+)\s+(connected|disconnected)\b", line)
        if header:
            geometry = re.search(r"(?:^|\s)(\d+)x(\d+)\+(-?\d+)\+(-?\d+)(?:\s|$)", line)
            current = {
                "name": header.group(1), "connected": header.group(2) == "connected",
                "active": geometry is not None, "primary": " primary " in f" {line} ",
                "x": int(geometry.group(3)) if geometry else 0,
                "y": int(geometry.group(4)) if geometry else 0,
                "connector_id": None, "modes": [],
            }
            outputs.append(current)
            continue
        if current is None:
            continue
        connector_id = re.match(r"^\s*CONNECTOR_ID:\s+(\d+)\s*$", line)
        if connector_id:
            current["connector_id"] = int(connector_id.group(1))
            continue
        mode = re.match(r"^\s+(\d+)x(\d+)\S*\s+(.+)$", line)
        if not mode:
            continue
        rates = []
        for token in mode.group(3).split():
            try:
                rates.append({"rate": float(token.rstrip("*+")),
                              "current": "*" in token})
            except ValueError:
                pass
        current["modes"].append({
            "name": line.split()[0], "width": int(mode.group(1)),
            "height": int(mode.group(2)), "rates": rates,
        })
    return outputs
