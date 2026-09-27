"""Desktop mode control for an already connected in-tree VKMS output.

This module never creates or disconnects a DRM device. Every operation targets
the connector chosen in the UI and preserves enough state to restore it.
"""

from __future__ import annotations

import os
from pathlib import Path
import json
import re
import subprocess
import tempfile
import time


class StockVkmsError(RuntimeError):
    pass


_RECOVERY_FILE = Path.home() / ".config" / "monitorize" / "stock-vkms-recovery.json"


def remember_disabled_output(connector_id: str):
    """Record a disabled pre-session output before enabling it for capture."""
    if not re.fullmatch(r"card\d+-Virtual-\d+", connector_id):
        raise StockVkmsError("Invalid stock VKMS connector for recovery")
    _RECOVERY_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=_RECOVERY_FILE.parent,
            prefix=".stock-vkms-", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump({"connector_id": connector_id}, stream)
        os.replace(temporary, _RECOVERY_FILE)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise StockVkmsError(f"Could not save stock VKMS recovery state: {exc}") from exc


def clear_disabled_output(connector_id: str):
    try:
        saved = json.loads(_RECOVERY_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        raise StockVkmsError(f"Could not read stock VKMS recovery state: {exc}") from exc
    if saved.get("connector_id") == connector_id:
        try:
            _RECOVERY_FILE.unlink(missing_ok=True)
        except OSError as exc:
            raise StockVkmsError(f"Could not clear stock VKMS recovery state: {exc}") from exc


def recover_disabled_output(desktop: str) -> int:
    """Disable an output left enabled by an interrupted stock VKMS session."""
    try:
        saved = json.loads(_RECOVERY_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        raise StockVkmsError(f"Could not read stock VKMS recovery state: {exc}") from exc
    connector_id = saved.get("connector_id")
    if not isinstance(connector_id, str) or not re.fullmatch(r"card\d+-Virtual-\d+", connector_id):
        raise StockVkmsError("Invalid stock VKMS recovery state")
    from monitorize.platform.vkms_backend import stock_vkms_connectors
    connectors = stock_vkms_connectors()
    if not connectors:
        return 0
    matches = [entry for entry in connectors if entry["id"] == connector_id]
    if not matches and len(connectors) == 1:
        matches = connectors
    if len(matches) != 1:
        raise StockVkmsError("Cannot identify the stock VKMS output left by the previous session")
    output = StockVkmsOutput(matches[0]["id"], desktop, matches[0]["connector_id"])
    output.snapshot()
    was_active = output.before_mode is not None
    output.disable()
    _RECOVERY_FILE.unlink(missing_ok=True)
    return int(was_active)


def _run(command: list[str], timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StockVkmsError(f"Could not run {command[0]}: {exc}") from exc
    if result.returncode:
        raise StockVkmsError(
            f"{command[0]} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _unique(entries, name: str):
    matches = [entry for entry in entries if str(entry.get("name") or "") == name]
    if len(matches) != 1:
        raise StockVkmsError(f"Cannot uniquely identify {name} in the desktop output list")
    return matches[0]


def _mode(width, height, refresh, token="") -> dict:
    return {"width": int(width), "height": int(height),
            "refresh_rate": float(refresh), "token": str(token)}


def _select(modes: list[dict], width: int, height: int, refresh: float) -> dict:
    matches = [mode for mode in modes
               if mode["width"] == width and mode["height"] == height
               and abs(mode["refresh_rate"] - refresh) <= 0.75]
    if not matches:
        raise StockVkmsError(
            f"The selected VKMS output does not advertise {width}x{height}@{refresh:g}Hz"
        )
    return min(matches, key=lambda mode: abs(mode["refresh_rate"] - refresh))


class StockVkmsOutput:
    def __init__(self, connector_id: str, desktop: str, connector_number: int | None = None):
        self.connector_id = connector_id
        self.connector_number = connector_number
        self.name = connector_id.split("-", 1)[1]
        self.desktop = desktop.lower()
        self.before = None
        self.before_mode = None
        self.output_name = self.name

    def _kind(self):
        if "cosmic" in self.desktop:
            raise StockVkmsError("Stock VKMS mode control on COSMIC is still WIP")
        if "cinnamon" in self.desktop:
            session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
            is_x11 = session_type == "x11" or (
                not session_type and bool(os.environ.get("DISPLAY"))
                and not os.environ.get("WAYLAND_DISPLAY")
            )
            return "x11" if is_x11 else "mutter"
        if "gnome" in self.desktop or "ubuntu" in self.desktop:
            return "mutter"
        if "kde" in self.desktop or "plasma" in self.desktop:
            return "kde"
        if "hyprland" in self.desktop:
            return "hyprland"
        if "sway" in self.desktop:
            return "sway"
        raise StockVkmsError(f"Stock VKMS output management is unavailable on {self.desktop}")

    def snapshot(self):
        kind = self._kind()
        if kind != "x11":
            try:
                same_name = [entry.name for entry in Path("/sys/class/drm").iterdir()
                             if re.fullmatch(r"card\d+-.+", entry.name)
                             and entry.name.split("-", 1)[1] == self.name]
            except OSError as exc:
                raise StockVkmsError(f"Could not verify DRM output identity: {exc}") from exc
            if same_name != [self.connector_id]:
                raise StockVkmsError(
                    f"The desktop output name {self.name} is ambiguous across DRM cards"
                )
        if kind == "kde":
            from monitorize.platform import kde_virtual_monitor as km
            self.before = _unique(km.kde_outputs(), self.name)
            if km._has_replication_source(self.before):
                raise StockVkmsError(
                    "The selected stock VKMS output is mirrored in KDE; choose an independent output"
                )
        elif kind == "mutter":
            from monitorize.platform import gnome_virtual_monitor as gm
            interface = self._mutter_interface()
            state = interface.GetCurrentState()
            if self.name not in gm.physical_connector_names(state):
                raise StockVkmsError(f"Mutter did not expose {self.name}")
            if any(self.name in gm._logical_connector_names(logical)
                   and len(gm._logical_connector_names(logical)) != 1
                   for logical in state[2]):
                raise StockVkmsError(
                    "The selected stock VKMS output is mirrored in the desktop layout"
                )
            self.before = state
        elif kind == "x11":
            from monitorize.platform.stock_vkms_xrandr import xrandr_outputs
            outputs = xrandr_outputs()
            matches = [entry for entry in outputs
                       if entry.get("connector_id") == self.connector_number]
            if not matches and not any(entry.get("connector_id") is not None for entry in outputs):
                matches = [entry for entry in outputs if entry["name"] == self.name]
            if len(matches) != 1:
                raise StockVkmsError("Cannot uniquely map the DRM connector to XRandR")
            self.before = matches[0]
            self.output_name = self.before["name"]
            if self.before["active"] and any(
                entry["active"] and entry["name"] != self.output_name
                and (entry["x"], entry["y"]) == (self.before["x"], self.before["y"])
                for entry in outputs
            ):
                raise StockVkmsError(
                    "The selected stock VKMS output overlaps another X11 output"
                )
        elif kind == "hyprland":
            from monitorize.platform.display_controller import DisplayController
            controller = DisplayController("hyprland")
            self.before = _unique(controller._monitor_json() or [], self.name)
        elif kind == "sway":
            from monitorize.platform.display_controller import DisplayController
            controller = DisplayController("sway")
            self.before = _unique(controller.sway_outputs(), self.name)
        self.before_mode = self._current()
        return self.before

    def _mutter_interface(self):
        from monitorize.platform import gnome_virtual_monitor as gm
        if "cinnamon" not in self.desktop:
            return gm.display_config_interface()
        dbus = gm._dbus()
        service = "org.cinnamon.Muffin.DisplayConfig"
        obj = dbus.SessionBus().get_object(service, "/org/cinnamon/Muffin/DisplayConfig")
        return dbus.Interface(obj, service)

    def _mutter_global_properties(self, state):
        from monitorize.platform import gnome_virtual_monitor as gm
        if "cinnamon" in self.desktop:
            return {}
        return gm._allowed_properties(state[3], gm.GLOBAL_CONFIG_PROPERTY_KEYS)

    def modes(self) -> list[dict]:
        if self.before is None:
            self.snapshot()
        kind = self._kind()
        output = self.before
        if kind == "kde":
            return [_mode((m.get("size") or {}).get("width"),
                          (m.get("size") or {}).get("height"), m.get("refreshRate"), m.get("id"))
                    for m in output.get("modes", [])
                    if (m.get("size") or {}).get("width")
                    and (m.get("size") or {}).get("height")
                    and m.get("refreshRate") and m.get("id")]
        if kind == "mutter":
            from monitorize.platform import gnome_virtual_monitor as gm
            monitor = gm._physical_monitor(output, self.name)
            if monitor is None:
                raise StockVkmsError(f"Mutter no longer exposes {self.name}")
            return [_mode(m[1], m[2], m[3], m[0]) for m in monitor[1]]
        if kind == "x11":
            return [_mode(m["width"], m["height"], r["rate"], m["name"])
                    for m in output["modes"] for r in m["rates"]]
        if kind == "hyprland":
            modes = []
            for raw in output.get("availableModes") or []:
                match = re.fullmatch(r"(\d+)x(\d+)@(\d+(?:\.\d+)?)Hz", str(raw))
                if match:
                    modes.append(_mode(*match.groups(), raw))
            return modes
        if kind == "sway":
            return [_mode(m["width"], m["height"], float(m["refresh"]) / 1000)
                    for m in output.get("modes") or []
                    if isinstance(m, dict) and m.get("width") and m.get("height")
                    and m.get("refresh")]
        return []

    def apply(self, width: int, height: int, refresh: float) -> dict:
        if self.before is None:
            self.snapshot()
        selected = _select(self.modes(), width, height, refresh)
        kind = self._kind()
        if kind == "kde":
            from monitorize.platform import kde_virtual_monitor as km
            selector = str(self.before.get("id"))
            commands = [f"output.{selector}.enable",
                        f"output.{selector}.mode.{selected['token']}"]
            others = [entry for entry in km.kde_outputs()
                      if entry.get("enabled") and entry.get("name") != self.name]
            target_position = None
            if others:
                rightmost = max(others, key=lambda entry:
                                km._position(entry)[0] + km._logical_width(entry))
                target_position = (
                    km._position(rightmost)[0] + km._logical_width(rightmost),
                    km._position(rightmost)[1],
                )
                commands.append(
                    f"output.{selector}.position.{target_position[0]},{target_position[1]}"
                )
            _run(["kscreen-doctor", *commands])
        elif kind == "mutter":
            self._apply_mutter(selected)
        elif kind == "x11":
            command = ["xrandr", "--output", self.output_name, "--mode", selected["token"],
                       "--rate", f"{selected['refresh_rate']:g}"]
            if not self.before["active"]:
                from monitorize.platform.stock_vkms_xrandr import xrandr_outputs
                others = [entry for entry in xrandr_outputs()
                          if entry["name"] != self.output_name and entry["active"]]
                reference = next((entry for entry in others if entry["primary"]),
                                 others[0] if others else None)
                if reference:
                    command.extend(["--right-of", reference["name"]])
            _run(command)
        elif kind == "hyprland":
            mode = f"{width}x{height}@{selected['refresh_rate']:g}"
            _run(["hyprctl", "keyword", "monitor", f"{self.name},{mode},auto,1"])
        elif kind == "sway":
            mode = f"{width}x{height}@{selected['refresh_rate']:g}Hz"
            command = ["swaymsg", "output", self.name, "enable", "mode", mode]
            if not self.before.get("active"):
                from monitorize.platform.display_controller import DisplayController
                others = [entry for entry in DisplayController("sway").sway_outputs()
                          if entry.get("active") and entry.get("name") != self.name]
                right_edge = max((int((entry.get("rect") or {}).get("x") or 0)
                                  + int((entry.get("rect") or {}).get("width") or 0)
                                  for entry in others), default=0)
                command.extend(["pos", str(right_edge), "0"])
            _run(command)
        self._verify(selected)
        if kind == "kde" and target_position is not None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = next((entry for entry in km.kde_outputs()
                                if entry.get("name") == self.name), None)
                if current and km._position(current) == target_position:
                    break
                time.sleep(0.1)
            else:
                raise StockVkmsError(f"The desktop did not attach {self.name} to the active layout")
        return dict(selected, name=self.output_name)

    def _apply_mutter(self, selected):
        from monitorize.platform import gnome_virtual_monitor as gm
        dbus = gm._dbus()
        interface = self._mutter_interface()
        state = interface.GetCurrentState()
        if gm.is_monitor_logically_active(state, self.name):
            if any(self.name in gm._logical_connector_names(logical)
                   and len(gm._logical_connector_names(logical)) != 1
                   for logical in state[2]):
                raise StockVkmsError(
                    "The selected stock VKMS output is mirrored in the desktop layout"
                )
            current_modes = gm._current_modes(state[1])
            if current_modes is None or self.name not in current_modes:
                raise StockVkmsError("Mutter did not report the current desktop modes")
            current_modes[self.name] = dict(
                current_modes[self.name], id=selected["token"],
            )
            monitor = gm._physical_monitor(state, self.name)
            matching = next((mode for mode in monitor[1]
                             if str(mode[0]) == selected["token"]), None)
            if matching is None:
                raise StockVkmsError("Mutter lost the selected mode")
            current_modes[self.name]["supported_scales"] = list(matching[5])
            properties = gm._monitor_properties_by_connector(state[1])
            configs = []
            for logical in state[2]:
                target = {"x": logical[0], "y": logical[1], "scale": logical[2],
                          "transform": logical[3], "primary": logical[4]}
                config = gm._logical_monitor_config(
                    dbus, logical, current_modes, properties, target)
                if config is None:
                    raise StockVkmsError("Mutter cannot preserve the current display layout")
                configs.append(config)
            interface.ApplyMonitorsConfig(
                gm._typed(dbus, "UInt32", int(state[0])),
                gm._typed(dbus, "UInt32", gm.APPLY_METHOD_TEMPORARY),
                dbus.Array(configs, signature="(iiduba(ssa{sv}))"),
                gm._variant_dict(dbus, self._mutter_global_properties(state)),
            )
            return
        payload, details, error = gm.build_vkms_activation_config(
            state, self.name, selected["width"], selected["height"],
            selected["refresh_rate"], dbus)
        if payload is None:
            raise StockVkmsError(error)
        interface.ApplyMonitorsConfig(
            gm._typed(dbus, "UInt32", int(state[0])),
            gm._typed(dbus, "UInt32", gm.APPLY_METHOD_TEMPORARY),
            payload,
            gm._variant_dict(dbus, self._mutter_global_properties(state)),
        )

    def _current(self):
        kind = self._kind()
        if kind == "kde":
            from monitorize.platform.kde_virtual_monitor import kde_outputs
            output = _unique(kde_outputs(), self.name)
            mode_id = str(output.get("currentModeId") or "")
            mode = next((m for m in output.get("modes", [])
                         if str(m.get("id")) == mode_id), None)
            if not mode or not output.get("enabled", True):
                return None
            size = mode.get("size") or {}
            return _mode(size["width"], size["height"], mode["refreshRate"])
        if kind == "mutter":
            from monitorize.platform import gnome_virtual_monitor as gm
            return gm.active_output_modes(self._mutter_interface().GetCurrentState()).get(self.name)
        if kind == "x11":
            from monitorize.platform.stock_vkms_xrandr import xrandr_outputs
            outputs = xrandr_outputs()
            matches = [entry for entry in outputs
                       if entry.get("connector_id") == self.connector_number]
            if len(matches) == 1:
                output = matches[0]
            elif not matches and not any(entry.get("connector_id") is not None for entry in outputs):
                output = _unique(outputs, self.output_name)
            else:
                raise StockVkmsError("XRandR no longer exposes the selected DRM connector ID")
            if not output["active"]:
                return None
            for mode in output["modes"]:
                for rate in mode["rates"]:
                    if rate["current"]:
                        return _mode(mode["width"], mode["height"], rate["rate"])
        if kind == "hyprland":
            from monitorize.platform.display_controller import DisplayController
            output = _unique(DisplayController("hyprland")._monitor_json() or [], self.name)
            if output.get("disabled"):
                return None
            return _mode(output["width"], output["height"], output["refreshRate"])
        if kind == "sway":
            from monitorize.platform.display_controller import DisplayController
            output = _unique(DisplayController("sway").sway_outputs(), self.name)
            if not output.get("active"):
                return None
            current = output.get("current_mode") or {}
            return _mode(current["width"], current["height"],
                         float(current["refresh"]) / 1000)
        return None

    def _verify(self, selected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self._current()
            if current and current["width"] == selected["width"] and \
                    current["height"] == selected["height"] and \
                    abs(current["refresh_rate"] - selected["refresh_rate"]) <= 0.75:
                return
            time.sleep(0.1)
        raise StockVkmsError(f"The desktop did not activate {self.name} at the requested mode")

    def disable(self):
        """Hide a newly created stock output without disconnecting its DRM connector."""
        if self.before is None:
            self.snapshot()
        if self.before_mode is None:
            return
        kind = self._kind()
        if kind == "kde":
            from monitorize.platform import kde_virtual_monitor as km
            if sum(bool(entry.get("enabled")) for entry in km.kde_outputs()) < 2:
                raise StockVkmsError("Cannot disable the last active desktop output")
            _run(["kscreen-doctor", f"output.{self.before['id']}.disable"])
        elif kind == "mutter":
            from monitorize.platform import gnome_virtual_monitor as gm
            dbus = gm._dbus()
            interface = self._mutter_interface()
            state = interface.GetCurrentState()
            logical = [entry for entry in state[2]
                       if self.name not in gm._logical_connector_names(entry)]
            if not logical:
                raise StockVkmsError("Cannot disable the last active desktop output")
            current_modes = gm._current_modes(state[1])
            properties = gm._monitor_properties_by_connector(state[1])
            configs = []
            for entry in logical:
                target = {"x": entry[0], "y": entry[1], "scale": entry[2],
                          "transform": entry[3], "primary": entry[4]}
                config = gm._logical_monitor_config(
                    dbus, entry, current_modes, properties, target)
                if config is None:
                    raise StockVkmsError("Mutter cannot preserve the current display layout")
                configs.append(config)
            interface.ApplyMonitorsConfig(
                gm._typed(dbus, "UInt32", int(state[0])),
                gm._typed(dbus, "UInt32", gm.APPLY_METHOD_TEMPORARY),
                dbus.Array(configs, signature="(iiduba(ssa{sv}))"),
                gm._variant_dict(dbus, self._mutter_global_properties(state)),
            )
        elif kind == "x11":
            from monitorize.platform.stock_vkms_xrandr import xrandr_outputs
            if sum(bool(entry["active"]) for entry in xrandr_outputs()) < 2:
                raise StockVkmsError("Cannot disable the last active desktop output")
            _run(["xrandr", "--output", self.output_name, "--off"])
        elif kind == "hyprland":
            from monitorize.platform.display_controller import DisplayController
            outputs = DisplayController("hyprland")._monitor_json() or []
            if sum(not entry.get("disabled", False) for entry in outputs) < 2:
                raise StockVkmsError("Cannot disable the last active desktop output")
            _run(["hyprctl", "keyword", "monitor", f"{self.name},disable"])
        elif kind == "sway":
            from monitorize.platform.display_controller import DisplayController
            outputs = DisplayController("sway").sway_outputs()
            if sum(bool(entry.get("active")) for entry in outputs) < 2:
                raise StockVkmsError("Cannot disable the last active desktop output")
            _run(["swaymsg", "output", self.name, "disable"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._current() is None:
                return
            time.sleep(0.1)
        raise StockVkmsError(f"The desktop did not disable {self.name}")

    def restore(self):
        if self.before is None:
            return
        kind = self._kind()
        if kind == "kde":
            selector = str(self.before.get("id"))
            commands = []
            if self.before.get("enabled", True):
                commands.append(f"output.{selector}.enable")
                mode_id = str(self.before.get("currentModeId") or "")
                if mode_id:
                    commands.append(f"output.{selector}.mode.{mode_id}")
                position = self.before.get("pos") or {}
                if position:
                    commands.append(f"output.{selector}.position.{position['x']},{position['y']}")
            else:
                commands.append(f"output.{selector}.disable")
            _run(["kscreen-doctor", *commands])
        elif kind == "mutter":
            self._restore_mutter()
        elif kind == "x11":
            before = self.before
            if before["active"]:
                current_rate = next((r["rate"] for m in before["modes"]
                                     for r in m["rates"] if r["current"]), None)
                if current_rate is None:
                    raise StockVkmsError("Cannot restore the original XRandR refresh rate")
                mode = next(m["name"] for m in before["modes"]
                            if any(r["current"] for r in m["rates"]))
                _run(["xrandr", "--output", self.output_name, "--mode", mode,
                      "--rate", f"{current_rate:g}", "--pos",
                      f"{before['x']}x{before['y']}"])
            else:
                _run(["xrandr", "--output", self.output_name, "--off"])
        elif kind == "hyprland":
            before = self.before
            if before.get("disabled"):
                _run(["hyprctl", "keyword", "monitor", f"{self.name},disable"])
            else:
                mode = f"{before['width']}x{before['height']}@{before['refreshRate']:g}"
                _run(["hyprctl", "keyword", "monitor",
                      f"{self.name},{mode},{before['x']}x{before['y']},{before['scale']}"])
        elif kind == "sway":
            before = self.before
            if not before.get("active"):
                _run(["swaymsg", "output", self.name, "disable"])
            else:
                mode = before["current_mode"]
                rect = before.get("rect") or {}
                _run(["swaymsg", "output", self.name, "enable", "mode",
                      f"{mode['width']}x{mode['height']}@{float(mode['refresh']) / 1000:g}Hz",
                      "pos", str(rect.get("x", 0)), str(rect.get("y", 0)),
                      "scale", str(before.get("scale") or 1)])
        self._verify_restored()

    def _verify_restored(self):
        kind = self._kind()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self._current()
            old = self.before_mode
            if old is None and current is None:
                return
            if old and current and all(
                abs(float(current[key]) - float(old[key])) <= (0.75 if key == "refresh_rate" else 0)
                for key in ("width", "height", "refresh_rate")
            ):
                if kind != "kde":
                    return
                from monitorize.platform import kde_virtual_monitor as km
                output = next((entry for entry in km.kde_outputs()
                               if entry.get("name") == self.name), None)
                if output and km._position(output) == km._position(self.before):
                    return
            time.sleep(0.1)
        raise StockVkmsError(f"The desktop did not restore {self.name} to its previous mode and layout")

    def _restore_mutter(self):
        from monitorize.platform import gnome_virtual_monitor as gm
        dbus = gm._dbus()
        interface = self._mutter_interface()
        current = interface.GetCurrentState()
        original = self.before
        original_modes = gm._current_modes(original[1])
        properties = gm._monitor_properties_by_connector(current[1])
        configs = []
        for logical in original[2]:
            target = {"x": logical[0], "y": logical[1], "scale": logical[2],
                      "transform": logical[3], "primary": logical[4]}
            config = gm._logical_monitor_config(
                dbus, logical, original_modes, properties, target)
            if config is None:
                raise StockVkmsError("Mutter cannot restore the previous display layout")
            configs.append(config)
        interface.ApplyMonitorsConfig(
            gm._typed(dbus, "UInt32", int(current[0])),
            gm._typed(dbus, "UInt32", gm.APPLY_METHOD_TEMPORARY),
            dbus.Array(configs, signature="(iiduba(ssa{sv}))"),
            gm._variant_dict(dbus, self._mutter_global_properties(original)),
        )
