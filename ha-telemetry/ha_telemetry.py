#!/usr/bin/env python3
"""Publish workstation state to Home Assistant via MQTT discovery.

Entities are grouped under one HA device ("Workstation"):

  sensor.workstation_active_app
  sensor.workstation_active_window
  sensor.workstation_workspace
  sensor.workstation_steam_game
  binary_sensor.workstation_fullscreen
  binary_sensor.workstation_steam_game_fullscreen
  binary_sensor.workstation_teams_meeting
  binary_sensor.workstation_microphone_in_use

Callable from HA (MQTT discovery: switch/button entities):

  switch.workstation_notification_silencing   -> workstation/command/notification_silencing  ON/OFF
  switch.workstation_nightlight               -> workstation/command/nightlight              ON/OFF
  switch.workstation_stay_awake               -> workstation/command/stay_awake              ON/OFF
  button.workstation_lock_screen              -> workstation/command/lock_screen             PRESS

Commands are a fixed allowlist; payloads are validated, nothing is passed to a shell.

Credentials come from ~/.config/ha-telemetry/mqtt.env (KEY=VALUE lines):
  MQTT_HOST=192.168.1.10
  MQTT_PORT=1883
  MQTT_USER=workstation
  MQTT_PASS=...
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqtt

NODE_ID = "workstation"
BASE_TOPIC = f"{NODE_ID}/telemetry"
COMMAND_PREFIX = f"{NODE_ID}/command"
AVAIL_TOPIC = f"{BASE_TOPIC}/availability"
DISCOVERY_PREFIX = "homeassistant"
INTERVAL = 2.0

ENV_FILE = Path(os.environ.get("HA_TELEMETRY_ENV") or Path.home() / ".config" / "ha-telemetry" / "mqtt.env")
LOG = logging.getLogger("ha-telemetry")

DEVICE = {
    "identifiers": [NODE_ID],
    "name": "Workstation",
    "manufacturer": "Arch Linux",
    "model": "Omarchy desktop",
}

ORIGIN = {"name": "ha-telemetry", "sw_version": "1.0"}

# entity key -> (platform, name, icon, extra config)
ENTITIES = {
    "active_app": ("sensor", "Active app", "mdi:application-outline", {}),
    "active_window": ("sensor", "Active window", "mdi:window-maximize", {}),
    "workspace": ("sensor", "Workspace", "mdi:view-dashboard-outline", {"state_class": "measurement"}),
    "fullscreen": ("binary_sensor", "Fullscreen window", "mdi:fullscreen", {}),
    "steam_game_fullscreen": ("binary_sensor", "Steam game fullscreen", "mdi:steam", {}),
    "steam_game": ("sensor", "Steam game", "mdi:steam", {}),
    "teams_meeting": ("binary_sensor", "Teams meeting", "mdi:microsoft-teams", {}),
    "microphone_in_use": ("binary_sensor", "Microphone in use", "mdi:microphone", {}),
    "notification_silencing": ("switch", "Notification silencing", "mdi:bell-off", {}),
    "nightlight": ("switch", "Night light", "mdi:weather-night", {}),
    "stay_awake": ("switch", "Stay awake", "mdi:coffee", {}),
    "lock_screen": ("button", "Lock screen", "mdi:lock", {}),
}

STEAM_CLASS_RE = re.compile(r"^(steam_app_\d+|gamescope)$", re.IGNORECASE)
STEAM_ROOTS = (
    Path.home() / ".local" / "share" / "Steam" / "steamapps",
    Path.home() / ".steam" / "steam" / "steamapps",
)
STEAM_ANCESTORS = {"steam", "steamwebhelper", "reaper", "pressure-vessel"}
TEAMS_RE = re.compile(r"(teams\.cloud\.microsoft|teams\.microsoft|microsoft teams)", re.IGNORECASE)
MEETING_TITLE_RE = re.compile(r"\b(meeting|call|meet now)\b", re.IGNORECASE)
BROWSER_APP_RE = re.compile(r"(chrome|chromium|brave|edge|teams)", re.IGNORECASE)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def ensure_hyprland_env() -> None:
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return
    root = Path(f"/run/user/{os.getuid()}/hypr")
    try:
        signatures = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    if signatures:
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = signatures[-1].name


def hypr_json(args: list[str]):
    try:
        return json.loads(sh(["hyprctl", "-j", *args]))
    except json.JSONDecodeError:
        return None


def proc_ppid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def proc_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def has_steam_game_env(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    for entry in raw.split(b"\0"):
        if entry.startswith((b"SteamGameId=", b"SteamAppId=")):
            value = entry.partition(b"=")[2].strip()
            if value and value not in (b"0", b"None"):
                return True
    return False


def has_steam_ancestor(pid: int, depth: int = 12) -> bool:
    pid = proc_ppid(pid) or 0
    for _ in range(depth):
        if pid <= 1:
            return False
        if proc_comm(pid).lower() in STEAM_ANCESTORS:
            return True
        pid = proc_ppid(pid) or 0
    return False


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def is_fullscreen(window: dict) -> bool:
    return max(_as_int(window.get("fullscreen")), _as_int(window.get("fullscreenClient"))) >= 2


def is_steam_game(window: dict) -> bool:
    cls = str(window.get("class") or "")
    if STEAM_CLASS_RE.match(cls):
        return True
    pid = window.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    return has_steam_game_env(pid) or has_steam_ancestor(pid)


def steam_app_id(window: dict) -> str:
    match = re.match(r"steam_app_(\d+)$", str(window.get("class") or ""), re.IGNORECASE)
    if match:
        return match.group(1)
    pid = window.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return ""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return ""
    for entry in raw.split(b"\0"):
        if entry.startswith((b"SteamGameId=", b"SteamAppId=")):
            value = entry.partition(b"=")[2].decode(errors="replace").strip()
            if value.isdigit() and value != "0":
                return value
    return ""


def steam_library_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for base in STEAM_ROOTS:
        if base not in roots:
            roots.append(base)
        try:
            text = (base / "libraryfolders.vdf").read_text(errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            library = Path(match.group(1)) / "steamapps"
            if library not in roots:
                roots.append(library)
    return tuple(roots)


def steam_app_name(app_id: str, roots: tuple[Path, ...] | None = None) -> str:
    if not app_id or not (app_id.isascii() and app_id.isdigit()):
        return ""
    for root in roots or steam_library_roots():
        try:
            text = (root / f"appmanifest_{app_id}.acf").read_text(errors="replace")
        except OSError:
            continue
        match = re.search(r'"name"\s+"([^"]+)"', text)
        if match:
            return match.group(1)
    return ""


def teams_window(clients: list[dict]) -> dict | None:
    for window in clients:
        if not isinstance(window, dict):
            continue
        haystack = f"{window.get('class') or ''} {window.get('title') or ''}"
        if TEAMS_RE.search(haystack):
            return window
    return None


def parse_pw_dump(dump: list[dict]) -> list[dict[str, str]]:
    """Return active capture streams, excluding captures of monitor sources."""
    if not isinstance(dump, list):
        return []

    nodes: dict[int, tuple[str, dict]] = {}
    for obj in dump:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Node":
            continue
        info = obj.get("info")
        if not isinstance(info, dict):
            continue
        props = info.get("props")
        if not isinstance(props, dict):
            continue
        node_id = obj.get("id")
        if isinstance(node_id, int):
            nodes[node_id] = (str(info.get("state") or ""), props)

    links: list[tuple[int, int]] = []
    for obj in dump:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Link":
            continue
        info = obj.get("info")
        if not isinstance(info, dict) or str(info.get("state") or "") != "active":
            continue
        props = info.get("props")
        if not isinstance(props, dict):
            continue
        in_node = props.get("link.input.node")
        out_node = props.get("link.output.node")
        if isinstance(in_node, int) and isinstance(out_node, int):
            links.append((in_node, out_node))

    streams: list[dict[str, str]] = []
    for node_id, (state, props) in nodes.items():
        if props.get("media.class") != "Stream/Input/Audio" or state not in ("running", "idle"):
            continue
        sources = {out_node for in_node, out_node in links if in_node == node_id}
        for source_id in sources:
            _, source_props = nodes.get(source_id, ("", {}))
            source_name = str(source_props.get("node.name") or "")
            if not source_name or source_name.endswith(".monitor"):
                continue
            app = str(props.get("application.name") or props.get("application.process.binary") or "")
            streams.append({"app": app, "source": source_name})
    return streams


def microphone_state() -> tuple[bool, bool]:
    """(any real capture stream, any browser/teams capture stream)."""
    try:
        dump = json.loads(sh(["pw-dump"]))
    except json.JSONDecodeError:
        return False, False
    streams = parse_pw_dump(dump)
    return bool(streams), any(BROWSER_APP_RE.search(s["app"]) for s in streams)


def dnd_state() -> bool:
    return sh(["omarchy-shell", "notifications", "dndState"]).strip().lower() == "on"


def _json_status(args: list[str]) -> dict:
    try:
        return json.loads(sh(args))
    except json.JSONDecodeError:
        return {}


def nightlight_state() -> bool:
    return bool(_json_status(["omarchy", "toggle", "nightlight", "--status"]).get("enabled"))


def stay_awake_state() -> bool:
    return bool(_json_status(["omarchy", "toggle", "idle", "status"]).get("enabled"))


def run_command(argv: list[str]) -> None:
    if os.environ.get("HA_TELEMETRY_DRY_RUN"):
        LOG.info("dry-run: %s", " ".join(argv))
        return
    try:
        subprocess.run(argv, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as err:
        LOG.error("command failed: %s (%s)", " ".join(argv), err)


def set_notification_silencing(on: bool) -> None:
    run_command(["omarchy-shell", "notifications", "setDnd", "on" if on else "off"])


def set_nightlight(on: bool) -> None:
    if nightlight_state() != on:
        run_command(["omarchy", "toggle", "nightlight"])


def set_stay_awake(on: bool) -> None:
    run_command(["omarchy", "toggle", "idle", "stay-awake" if on else "allow-idle"])


def lock_screen() -> None:
    run_command(["omarchy", "system", "lock"])


COMMANDS = {
    "notification_silencing": set_notification_silencing,
    "nightlight": set_nightlight,
    "stay_awake": set_stay_awake,
}


def collect_state() -> dict[str, str]:
    ensure_hyprland_env()
    active = hypr_json(["activewindow"])
    if not isinstance(active, dict):
        active = {}
    clients = hypr_json(["clients"])
    if not isinstance(clients, list):
        clients = []
    workspace = hypr_json(["activeworkspace"])
    if not isinstance(workspace, dict):
        workspace = {}

    mic_any, browser_mic = microphone_state()

    teams = teams_window(clients)
    teams_title = str(teams.get("title") or "") if teams else ""
    teams_meeting = bool(teams) and bool(MEETING_TITLE_RE.search(teams_title) or browser_mic)

    steam_windows = [w for w in clients if isinstance(w, dict) and is_steam_game(w)]
    steam_windows.sort(key=lambda w: not is_fullscreen(w))
    steam_fullscreen = any(is_fullscreen(w) for w in steam_windows)
    steam_game = "None"
    if steam_windows:
        window = steam_windows[0]
        steam_game = steam_app_name(steam_app_id(window)) or str(window.get("title") or "None")

    return {
        "active_app": str(active.get("class") or ""),
        "active_window": str(active.get("title") or "")[:255],
        "workspace": str(workspace.get("id") if workspace.get("id") is not None else ""),
        "fullscreen": "ON" if is_fullscreen(active) else "OFF",
        "steam_game_fullscreen": "ON" if steam_fullscreen else "OFF",
        "steam_game": steam_game,
        "teams_meeting": "ON" if teams_meeting else "OFF",
        "microphone_in_use": "ON" if mic_any else "OFF",
        "notification_silencing": "ON" if dnd_state() else "OFF",
        "nightlight": "ON" if nightlight_state() else "OFF",
        "stay_awake": "ON" if stay_awake_state() else "OFF",
    }


def discovery_payloads(prefix: str = DISCOVERY_PREFIX) -> dict[str, str]:
    prefix = prefix.strip("/") or DISCOVERY_PREFIX
    if not re.fullmatch(r"[A-Za-z0-9_/-]+", prefix):
        prefix = DISCOVERY_PREFIX
    payloads: dict[str, str] = {}
    for key, (platform, name, icon, extra) in ENTITIES.items():
        config = {
            "name": name,
            "unique_id": f"{NODE_ID}_{key}",
            "default_entity_id": f"{platform}.{NODE_ID}_{key}",
            "availability_topic": AVAIL_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "icon": icon,
            "device": DEVICE,
            "origin": ORIGIN,
        }
        if platform == "button":
            config["command_topic"] = f"{COMMAND_PREFIX}/{key}"
            config["payload_press"] = "PRESS"
        else:
            config["state_topic"] = f"{BASE_TOPIC}/{key}"
            if platform in ("binary_sensor", "switch"):
                config["payload_on"] = "ON"
                config["payload_off"] = "OFF"
            if platform == "switch":
                config["command_topic"] = f"{COMMAND_PREFIX}/{key}"
        config.update(extra)
        payloads[f"{prefix}/{platform}/{NODE_ID}_{key}/config"] = json.dumps(config)
    return payloads


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dry_run = "--once" in sys.argv
    if dry_run:
        print(json.dumps(collect_state(), indent=2))
        return 0

    settings = read_env_file(ENV_FILE)
    host = settings.get("MQTT_HOST")
    port = int(settings.get("MQTT_PORT") or 1883)
    discovery_prefix = settings.get("MQTT_DISCOVERY_PREFIX") or DISCOVERY_PREFIX
    user = settings.get("MQTT_USER")
    password = settings.get("MQTT_PASS")
    if not host or (user and password in (None, "", "CHANGE_ME")):
        LOG.error("Configure %s with MQTT_HOST and a valid MQTT_USER/MQTT_PASS", ENV_FILE)
        return 2

    auth_failed = False
    last_state: dict[str, str] = {}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        nonlocal auth_failed, last_state
        if reason_code == 0:
            LOG.info("Connected to %s:%s", host, port)
            for topic, payload in discovery_payloads(discovery_prefix).items():
                client.publish(topic, payload, retain=True)
            client.publish(AVAIL_TOPIC, "online", retain=True)
            client.subscribe(f"{COMMAND_PREFIX}/#")
            last_state = {}
        else:
            auth_failed = True
            LOG.error("MQTT connection rejected: %s", reason_code)

    def publish_state(client, state_cache: dict[str, str]) -> None:
        for key, value in collect_state().items():
            if state_cache.get(key) != value:
                client.publish(f"{BASE_TOPIC}/{key}", value, retain=True)
                state_cache[key] = value

    def handle_command(topic: str, payload_bytes: bytes) -> None:
        key = topic.rsplit("/", 1)[-1]
        payload = payload_bytes.decode(errors="replace").strip()
        if key == "lock_screen":
            if payload.upper() != "PRESS":
                LOG.warning("ignoring payload %r for lock_screen", payload)
                return
            lock_screen()
            return
        handler = COMMANDS.get(key)
        if handler is None:
            LOG.warning("unknown command topic %s", topic)
            return
        lowered = payload.lower()
        if lowered not in ("on", "off", "1", "0", "true", "false", "yes", "no"):
            LOG.warning("invalid payload %r for %s", payload, key)
            return
        handler(lowered in ("on", "1", "true", "yes"))

    pending_commands: deque[tuple[str, bytes]] = deque()

    def on_message(_client, _userdata, msg) -> None:
        # Never run subprocesses in the network thread: it would stall pings.
        pending_commands.append((msg.topic, msg.payload))

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{NODE_ID}-telemetry",
    )
    if user:
        client.username_pw_set(user, password)
    client.will_set(AVAIL_TOPIC, "offline", retain=True)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(5, 60)

    client.connect_async(host, port, keepalive=30)
    client.loop_start()

    try:
        while True:
            if auth_failed:
                LOG.error("Giving up until credentials are fixed")
                return 2
            while pending_commands:
                handle_command(*pending_commands.popleft())
            if client.is_connected():
                publish_state(client, last_state)
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        return 0
    finally:
        client.loop_stop()
        if client.is_connected():
            client.publish(AVAIL_TOPIC, "offline", retain=True).wait_for_publish(timeout=2)
        client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
