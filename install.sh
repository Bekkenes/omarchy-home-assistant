#!/usr/bin/env bash
#
# Installer for the Omarchy <-> Home Assistant integration.
#
#   ./install.sh                 interactive, installs both parts
#   ./install.sh --verbose       echo every command it runs
#   ./install.sh --dry-run -v    show the plan, change nothing
#
# Root work (packages, ufw) is invoked through sudo; run this as your desktop
# user, not as root, so the systemd --user units belong to the right account.

set -euo pipefail

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

VERBOSE=0
DRY_RUN=0
ASSUME_YES=0
DO_SYSTEM=1
DO_GLANCES=1
DO_MQTT=1
START=1
SUBNET=""
MQTT_HOST_OPT=""
MQTT_USER_OPT=""
MQTT_PASS_FILE_OPT=""

LOG_FILE=$(mktemp)
trap 'rm -f "$LOG_FILE"' EXIT

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Options:
  -v, --verbose        Echo every command before running it.
  -n, --dry-run        Print the plan and exit without changing anything.
  -y, --yes            Do not ask for confirmation.
      --no-system      Skip package installation and the ufw rule.
      --skip-glances   Do not install/configure Glances.
      --skip-mqtt      Do not install/configure the MQTT bridge.
      --no-start       Enable services but do not start/restart them.
      --subnet CIDR    Subnet allowed to reach Glances (default: detected).
      --mqtt-host HOST MQTT broker address (default: prompt).
      --mqtt-user USER MQTT username (default: prompt).
      --mqtt-pass-file FILE
                       Read the password from the first line of FILE.
  -h, --help           Show this help.

The MQTT password is never taken from an argument: it is read with a hidden
prompt, from the MQTT_PASS environment variable, or from a file
(--mqtt-pass-file / MQTT_PASS_FILE). Inline env assignments end up in shell
history, so prefer the prompt or a mode-600 file.

Examples:
  ./install.sh --dry-run --verbose
  ./install.sh --skip-mqtt
  printf 'secret\n' > ~/.config/ha-telemetry/pw && chmod 600 ~/.config/ha-telemetry/pw
  MQTT_PASS_FILE=~/.config/ha-telemetry/pw ./install.sh --yes \
    --mqtt-host 192.168.1.10 --mqtt-user workstation
EOF
}

say()  { printf '==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Resolve a command but refuse anything outside system directories. Root
# commands are later executed by absolute path so a hijacked PATH cannot make
# the script run an attacker-controlled binary as root.
system_bin() {
  local resolved
  resolved=$(command -v "$1" 2>/dev/null) || return 1
  case $resolved in
    /usr/bin/*|/usr/sbin/*|/bin/*|/sbin/*|/usr/share/omarchy/bin/*) printf '%s' "$resolved" ;;
    *) return 1 ;;
  esac
}

normalize_cidr() {
  "$PYTHON_BIN" - "$1" <<'PY'
import ipaddress
import sys

try:
    print(ipaddress.ip_network(sys.argv[1], strict=False))
except ValueError:
    sys.exit(1)
PY
}

cmd() {
  if (( DRY_RUN )); then
    printf '    [dry-run] %s\n' "$*"
    return 0
  fi
  if (( VERBOSE )); then
    printf '    + %s\n' "$*"
  fi
  "$@"
}

cmd_quiet() {
  if (( DRY_RUN )); then
    printf '    [dry-run] %s\n' "$*"
    return 0
  fi
  if (( VERBOSE )); then
    printf '    + %s\n' "$*"
    "$@"
    return
  fi
  if ! "$@" >"$LOG_FILE" 2>&1; then
    warn "command failed: $*"
    sed 's/^/    /' "$LOG_FILE" >&2
    return 1
  fi
}

run_root() {
  if (( EUID == 0 )); then
    cmd "$@"
  else
    cmd "$SUDO_BIN" "$@"
  fi
}

detect_subnet() {
  local iface cidr
  iface=$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')
  [[ -n $iface ]] || return 1
  cidr=$(ip -o -4 addr show dev "$iface" scope global 2>/dev/null | awk '{print $4; exit}')
  [[ -n $cidr ]] || return 1
  "$PYTHON_BIN" - "$cidr" <<'PY'
import ipaddress
import sys

print(ipaddress.ip_interface(sys.argv[1]).network)
PY
}

while (( $# )); do
  case $1 in
    -v|--verbose) VERBOSE=1 ;;
    -n|--dry-run) DRY_RUN=1; VERBOSE=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    --no-system) DO_SYSTEM=0 ;;
    --skip-glances) DO_GLANCES=0 ;;
    --skip-mqtt) DO_MQTT=0 ;;
    --no-start) START=0 ;;
    --subnet) SUBNET=${2:?--subnet needs a CIDR}; shift ;;
    --mqtt-host) MQTT_HOST_OPT=${2:?--mqtt-host needs a value}; shift ;;
    --mqtt-user) MQTT_USER_OPT=${2:?--mqtt-user needs a value}; shift ;;
    --mqtt-pass-file) MQTT_PASS_FILE_OPT=${2:?--mqtt-pass-file needs a value}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

(( EUID == 0 )) && die "run as your desktop user; sudo is used only for packages and ufw"

PYTHON_BIN=$(system_bin python3) || die "python3 must resolve to a system path"
command -v systemctl >/dev/null || die "systemctl is required"

if (( DO_SYSTEM )); then
  PACMAN_BIN=$(system_bin pacman) || die "pacman must resolve to a system path (Arch-based system)"
  SUDO_BIN=$(system_bin sudo) || die "sudo must resolve to a system path"
  UFW_BIN=$(system_bin ufw) || UFW_BIN=""
  OMARCHY_BIN=$(system_bin omarchy) || OMARCHY_BIN=""
fi

say "Install plan"
info "system packages + ufw : $( (( DO_SYSTEM )) && echo yes || echo no )"
info "Glances               : $( (( DO_GLANCES )) && echo yes || echo no )"
info "MQTT bridge           : $( (( DO_MQTT )) && echo yes || echo no )"
info "start/restart units   : $( (( START )) && echo yes || echo no )"
info "repo                  : $REPO_DIR"
(( DRY_RUN )) && info "mode                  : dry run, nothing will change"

if (( ! ASSUME_YES && ! DRY_RUN )); then
  read -r -p "Proceed? [y/N] " reply
  [[ $reply =~ ^[Yy]$ ]] || { say "aborted"; exit 0; }
fi

if (( DO_SYSTEM )); then
  say "System packages"
  missing=()
  for pkg in glances python-fastapi uvicorn; do
    "$PACMAN_BIN" -Q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if (( ${#missing[@]} )); then
    info "missing: ${missing[*]}"
    if [[ -n $OMARCHY_BIN ]]; then
      run_root "$OMARCHY_BIN" pkg add "${missing[@]}"
    else
      run_root "$PACMAN_BIN" -S --needed --noconfirm "${missing[@]}"
    fi
  else
    info "already installed"
  fi

  say "Firewall"
  if systemctl is-active --quiet ufw; then
    if [[ -n $SUBNET ]]; then
      raw_subnet=$SUBNET
      if ! SUBNET=$(normalize_cidr "$raw_subnet"); then
        die "invalid --subnet: $raw_subnet"
      fi
    else
      SUBNET=$(detect_subnet) || warn "could not detect the LAN subnet; pass --subnet"
    fi
    if [[ -z $UFW_BIN ]]; then
      warn "ufw is active but its binary was not found in a system path; skipping"
    elif [[ -n $SUBNET ]]; then
      info "allowing $SUBNET to reach port 61208"
      run_root "$UFW_BIN" allow from "$SUBNET" to any port 61208 proto tcp comment 'glances for home assistant'
    fi
  else
    info "ufw is not active; skipping the rule"
  fi
fi

UNIT_DIR="$HOME/.config/systemd/user"
ENV_DIR="$HOME/.config/ha-telemetry"
APP_DIR="$HOME/.local/share/ha-telemetry"
VENV="$APP_DIR/venv"
ENV_FILE="$ENV_DIR/mqtt.env"

install_file() {
  local src=$1 dst=$2 mode=$3
  if [[ -f $dst ]] && cmp -s "$src" "$dst"; then
    return 0
  fi
  cmd mkdir -p "$(dirname -- "$dst")"
  cmd install -m "$mode" "$src" "$dst"
}

if (( DO_GLANCES )); then
  say "Glances configuration"
  glances_conf="$HOME/.config/glances/glances.conf"
  if [[ -f $glances_conf ]]; then
    info "keeping existing $glances_conf"
  elif [[ ! -f /etc/glances/glances.conf ]]; then
    warn "/etc/glances/glances.conf not found; skipping trim patch"
  else
    cmd mkdir -p "$(dirname -- "$glances_conf")"
    cmd cp /etc/glances/glances.conf "$glances_conf"
    if ! command -v patch >/dev/null; then
      warn "patch is not installed; skipping the Glances trim patch"
    elif (( DRY_RUN )); then
      info "[dry-run] patch -d $(dirname -- "$glances_conf") -p1 < examples/glances.conf.diff"
    elif ! patch -d "$(dirname -- "$glances_conf")" -p1 < "$REPO_DIR/examples/glances.conf.diff"; then
      warn "trim patch did not apply cleanly; $glances_conf is unmodified apart from the copy"
    else
      info "applied entity-trimming patch"
    fi
  fi

  say "Glances service"
  install_file "$REPO_DIR/systemd/glances.service" "$UNIT_DIR/glances.service" 644
  cmd systemctl --user daemon-reload
  if (( START )); then
    cmd systemctl --user enable glances.service
    cmd systemctl --user restart glances.service
  else
    cmd systemctl --user enable glances.service
  fi
fi

if (( DO_MQTT )); then
  say "MQTT bridge"
  MQTT_READY=0
  cmd mkdir -p "$APP_DIR" "$ENV_DIR"
  install_file "$REPO_DIR/ha-telemetry/ha_telemetry.py" "$APP_DIR/ha_telemetry.py" 755

  if [[ ! -x $VENV/bin/python ]]; then
    cmd "$PYTHON_BIN" -m venv "$VENV"
  fi
  if ! "$VENV/bin/python" -c 'import paho.mqtt.client' >/dev/null 2>&1; then
    cmd_quiet "$VENV/bin/pip" install --quiet --disable-pip-version-check 'paho-mqtt==2.1.0'
  else
    info "paho-mqtt already present"
  fi

  if [[ -f $ENV_FILE ]]; then
    info "keeping existing $ENV_FILE (credentials are never overwritten)"
    if ! grep -q '^MQTT_PASS=CHANGE_ME' "$ENV_FILE"; then
      MQTT_READY=1
    fi
  else
    host=${MQTT_HOST_OPT:-}
    user=${MQTT_USER_OPT:-}
    pass=${MQTT_PASS:-}
    pass_file=${MQTT_PASS_FILE_OPT:-${MQTT_PASS_FILE:-}}
    if [[ -z $pass && -n $pass_file ]]; then
      [[ -r $pass_file ]] || die "cannot read password file: $pass_file"
      pass=$(head -n1 -- "$pass_file")
      info "read MQTT password from $pass_file"
      case $(stat -c '%a' "$pass_file") in
        600|400) ;;
        *) warn "$pass_file is group/world readable; chmod 600 it" ;;
      esac
    fi
    if (( ! DRY_RUN )) && [[ -t 0 ]]; then
      [[ -n $host ]] || read -r -p "MQTT broker host: " host
      [[ -n $host ]] || die "a broker host is required"
      [[ -n $user ]] || read -r -p "MQTT username: " user
      [[ -n $user ]] || die "a username is required"
      if [[ -z $pass ]]; then
        read -r -s -p "MQTT password: " pass
        printf '\n'
      fi
    fi

    if [[ -z $host || -z $user || -z $pass ]]; then
      host=${host:-192.168.1.10}
      user=${user:-workstation}
      pass=${pass:-CHANGE_ME}
      warn "incomplete MQTT settings; writing $ENV_FILE with placeholders"
    fi

    for value in "$host" "$user" "$pass"; do
      if [[ $value == *$'\n'* || $value == *$'\r'* ]]; then
        die "MQTT host, username and password must be single-line values"
      fi
    done

    if (( DRY_RUN )); then
      info "[dry-run] write $ENV_FILE (password hidden)"
    else
      umask 077
      cat >"$ENV_FILE" <<EOF
# Home Assistant MQTT broker (shared with other clients is fine)
MQTT_HOST=$host
MQTT_PORT=1883
MQTT_USER=$user
MQTT_PASS=$pass
# Only needed if HA's MQTT discovery prefix is not "homeassistant":
# MQTT_DISCOVERY_PREFIX=homeassistant
EOF
      cmd chmod 600 "$ENV_FILE"
    fi
    if [[ $pass != CHANGE_ME && -n $host && -n $user ]]; then
      MQTT_READY=1
    fi
  fi

  say "MQTT service"
  install_file "$REPO_DIR/systemd/ha-telemetry.service" "$UNIT_DIR/ha-telemetry.service" 644
  cmd systemctl --user daemon-reload
  cmd systemctl --user enable ha-telemetry.service

  if (( MQTT_READY )); then
    if (( START )); then
      cmd systemctl --user restart ha-telemetry.service
    fi
  else
    info "not starting: edit $ENV_FILE with MQTT_USER/MQTT_PASS, then"
    info "  systemctl --user restart ha-telemetry"
  fi
fi

say "Done"
if (( DO_GLANCES )); then
  info "Home Assistant: Settings -> Devices & Services -> Add Integration -> Glances"
  info "  host = this machine's LAN IP, port = 61208"
fi
if (( DO_MQTT )); then
  info "MQTT discovery appears automatically under Settings -> Devices & Services -> MQTT"
  info "logs: journalctl --user -u ha-telemetry -f"
fi
(( VERBOSE )) || info "re-run with --verbose to see every command"
