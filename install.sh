#!/usr/bin/env bash
# Pi Tuner v2 — interactive installer
#
# Walks through every step: installs dependencies (including building the FM
# stereo `demux` decoder), configures Icecast, programs dongle serials, writes
# your station configs, sets up Zabbix (optional), installs the systemd
# service, and verifies the whole thing is streaming.
#
# Run as root on a Raspberry Pi (Raspberry Pi OS / Debian).
#
#   One-liner (downloads then runs — keeps the prompts interactive):
#     curl -fsSL https://raw.githubusercontent.com/thetylerwoodwardproject/pi-tuner/main/install.sh -o /tmp/pituner-install.sh && sudo bash /tmp/pituner-install.sh
#
#   Or clone first, then run from the project directory:
#     sudo ./install.sh
#
# When run via the one-liner the script downloads the rest of the project into
# a temp dir; when run from a clone it installs from beside tuner.py.
# If stdin is not a terminal (e.g. piped `curl | bash`), prompts are skipped
# and the example station files are deployed instead.

set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
APP_DIR="/opt/pituner"
REPO="https://github.com/thetylerwoodwardproject/pi-tuner"

# ------------------------------------------------------------- text helpers
if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
  RED=$(tput setaf 1); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3)
  CYAN=$(tput setaf 6); BOLD=$(tput bold); RESET=$(tput sgr0)
else
  RED=""; GREEN=""; YELLOW=""; CYAN=""; BOLD=""; RESET=""
fi

info() { echo -e "${CYAN}[>]${RESET} $*"; }
ok()   { echo -e "${GREEN}[OK]${RESET} $*"; }
warn() { echo -e "${YELLOW}[!]${RESET} $*"; }
fail() { echo -e "${RED}[ERROR]${RESET} $*"; }
step() { echo -e "\n${BOLD}${CYAN}===== $* =====${RESET}"; }

die() { fail "$*"; exit 1; }

ask() {
  local prompt="$1" default="$2" ans
  if [ -n "$default" ]; then
    printf "${CYAN}[?]${RESET} %s [%s]: " "$prompt" "$default" >&2
    read -r ans
    printf '%s' "${ans:-$default}"
  else
    printf "${CYAN}[?]${RESET} %s: " "$prompt" >&2
    read -r ans
    printf '%s' "$ans"
  fi
}

confirm() {
  local prompt="${1:-Continue?}" ans
  printf "${YELLOW}[?]${RESET} %s [Y/n]: " "$prompt" >&2
  read -r ans
  [ -z "$ans" ] || [[ "$ans" =~ ^[Yy] ]]
}

press_enter() {
  printf "${CYAN}[>]${RESET} %s" "$1" >&2
  read -r _
}

gen_pass() { head -c 16 /dev/urandom | md5sum | awk '{print $1}'; }

# Prompts need a terminal on stdin. When piped (e.g. `curl ... | sudo bash`)
# stdin is not a TTY, so run non-interactively and deploy the example stations.
if [ -t 0 ]; then
  INTERACTIVE=1
else
  INTERACTIVE=0
fi

# ---------------------------------------------------------------- preflight
if [ "${EUID}" -ne 0 ]; then
  die "Please run as root. Try:  curl -fsSL ${REPO}/raw/main/install.sh | sudo bash"
fi

# When run via `curl ... | sudo bash` the script is read from stdin and
# tuner.py is not beside it, so download the project into a temp dir.
if [ ! -f "${SRC}/tuner.py" ]; then
  info "Fetching Pi Tuner v2 files..."
  SRC_TMP="$(mktemp -d)"
  trap 'rm -rf "${SRC_TMP}"' EXIT
  if ! curl -fsSL "${REPO}/archive/refs/heads/main.tar.gz" | tar xz -C "${SRC_TMP}"; then
    die "Failed to download the project. Clone it manually:  git clone ${REPO}.git && cd pi-tuner && sudo ./install.sh"
  fi
  SRC="$(find "${SRC_TMP}" -mindepth 1 -maxdepth 1 -type d | head -n1)"
fi

if [ ! -f "${SRC}/tuner.py" ]; then
  die "tuner.py not found next to install.sh (looking in ${SRC}). Run from the project directory."
fi

clear 2>/dev/null || true
echo -e "${BOLD}${GREEN}Pi Tuner v2 installer${RESET}"
echo "A minimal multi-station SDR streamer for Raspberry Pi."
echo "This will install dependencies, set up Icecast, configure your stations,"
echo "and verify everything is running."
echo ""
warn "This RESETS your Icecast configuration: it regenerates the source and"
warn "admin passwords (overwriting any existing Icecast setup) and restarts"
warn "Icecast. If you use Icecast for other streams, back it up first."
echo ""

# ------------------------------------------------------------- dependencies
step "1 of 8: Install system packages"
info "Installing: rtl-sdr, ffmpeg, icecast2, build tools, and liquid-dsp (for the FM stereo decoder)."

if ! command -v apt-get >/dev/null 2>&1; then
  die "apt-get not found. This installer targets Debian / Raspberry Pi OS."
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y || warn "apt-get update had warnings; continuing."

apt-get install -y \
  rtl-sdr ffmpeg icecast2 curl \
  git build-essential meson ninja-build pkg-config \
  || die "Package installation failed."

ok "System packages installed."

# Forcefully free the RTL-SDR dongles from anything that may be holding them:
# a previous install's service, lingering rtl_* processes, or the kernel's DVB
# driver. Essential when re-running the installer so the dongles can be
# re-programmed and re-tuned cleanly.
info "Stopping any previous pituner service and killing SDR processes..."
systemctl stop pituner.service 2>/dev/null || true
pkill -f 'rtl_fm|rtl_tcp|rtl_test|rtl_eeprom|rtl_biast|nrsc5' 2>/dev/null || true
sleep 1

info "Blacklisting and unloading the DVB kernel driver..."
BLACKLIST_FILE="/etc/modprobe.d/rtl-sdr-blacklist.conf"
cat > "${BLACKLIST_FILE}" <<'EOF'
# Let rtl_fm/rtl_test own the RTL-SDR dongles instead of the DVB driver.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
for mod in dvb_usb_rtl28xxu rtl2832 rtl2830 dvb_usb_v2 dvb_core; do
  rmmod "$mod" 2>/dev/null || true
done
ok "Dongles freed (service stopped, SDR processes killed, DVB driver unloaded)."

# ------------------------------------------------------------- build demux
step "2 of 8: Build FM stereo decoder (demux)"
info "demux turns the raw FM signal into stereo audio with de-emphasis."
info "It is built from source (windytan/stereodemux)."

if command -v demux >/dev/null 2>&1; then
  ok "demux is already installed: $(command -v demux)"
else
  if apt-get install -y libliquid-dev; then
    BUILD_TMP="$(mktemp -d)"
    if git clone --depth 1 https://github.com/windytan/stereodemux.git "${BUILD_TMP}/stereodemux"; then
      (
        cd "${BUILD_TMP}/stereodemux"
        if meson setup build >/dev/null 2>&1; then
          meson compile -C build >/dev/null 2>&1
        else
          make >/dev/null 2>&1
        fi
      )
      DEMUX_BIN="$(find "${BUILD_TMP}" -name demux -type f | head -n1)"
      if [ -n "${DEMUX_BIN}" ]; then
        install -m 755 "${DEMUX_BIN}" /usr/local/bin/demux
        ok "demux installed to /usr/local/bin/demux"
      else
        warn "demux failed to build. FM stations will stream MONO; WX is unaffected."
        warn "To fix later:  git clone https://github.com/windytan/stereodemux && meson setup build && meson compile -C build"
      fi
    else
      warn "Could not download stereodemux source. FM stereo unavailable (WX still works)."
    fi
    rm -rf "${BUILD_TMP}"
  else
    warn "libliquid-dev is not available in apt. FM stereo unavailable (WX still works)."
  fi
fi

# ------------------------------------------------------------- icecast
step "3 of 8: Configure Icecast"
warn "This resets the Icecast source/admin passwords and restarts Icecast,"
warn "replacing any existing Icecast settings."
if [ "${INTERACTIVE}" = "1" ] && ! confirm "Reset Icecast and continue?"; then
  die "Installation cancelled. The Icecast reset is required so the tuner can stream."
fi

info "Generating a random source password and applying it to Icecast."

ICECAST_XML="/etc/icecast2/icecast.xml"
SOURCE_PASS="$(gen_pass)"
ADMIN_PASS="$(gen_pass)"

if [ ! -f "${ICECAST_XML}" ]; then
  warn "Icecast config not found at ${ICECAST_XML}. If you installed Icecast elsewhere,"
  warn "set the source password manually and update ${APP_DIR}/icecast.conf."
else
  if grep -q '<source-password>' "${ICECAST_XML}"; then
    sed -i "s|<source-password>.*</source-password>|<source-password>${SOURCE_PASS}</source-password>|" "${ICECAST_XML}"
    sed -i "s|<admin-password>.*</admin-password>|<admin-password>${ADMIN_PASS}</admin-password>|" "${ICECAST_XML}"
    ok "Icecast source/admin password set."
  else
    warn "Could not find <source-password> in icecast.xml; leaving Icecast config unchanged."
  fi
fi

systemctl enable icecast2 >/dev/null 2>&1 || warn "Could not enable icecast2 (may need manual start)."
systemctl restart icecast2 >/dev/null 2>&1 || warn "Could not restart icecast2."
sleep 1
if curl -sf --max-time 5 http://localhost:8000/status-json.xsl >/dev/null; then
  ok "Icecast is responding on port 8000."
else
  warn "Icecast not responding yet — it will be re-checked at the end."
fi

# ------------------------------------------------------------- app install
step "4 of 8: Install Pi Tuner files"
info "Installing to ${APP_DIR}."

mkdir -p "${APP_DIR}/stations"
install -m 755 "${SRC}/tuner.py" "${APP_DIR}/tuner.py"
[ -f "${SRC}/zabbix_template.xml" ] && install -m 644 "${SRC}/zabbix_template.xml" "${APP_DIR}/zabbix_template.xml"

cat > "${APP_DIR}/icecast.conf" <<EOF
# Icecast connection — shared by all stations
# ─── user settings ─────────────────────────────────
HOST=localhost
PORT=8000
SOURCE_PASSWORD=${SOURCE_PASS}
# ─── end user settings ─────────────────────────────
EOF

# service user
if ! id -u pituner >/dev/null 2>&1; then
  useradd --system --no-create-home --home "${APP_DIR}" pituner
  ok "Created 'pituner' service user."
else
  ok "'pituner' user already exists."
fi
usermod -aG plugdev pituner
chown -R pituner:pituner "${APP_DIR}"
chmod 600 "${APP_DIR}/icecast.conf"

ok "Application files installed."

# ------------------------------------------------------------- serials
step "5 of 8: Program dongle serials"
SERIALS=()
count=0

if [ "${INTERACTIVE}" = "1" ]; then
  info "Each station needs a dongle with a unique serial number."
  info "We'll program them one at a time (requires plugging in each dongle alone)."

  info "Detecting currently-connected devices..."
  timeout 4 rtl_test 2>&1 | grep -E '^[[:space:]]*[0-9]+:' || info "  (no devices detected right now)"

  count=$(ask "How many dongles / stations will you use?" "1")
  for i in $(seq 1 "${count}"); do
    info "Dongle ${i} of ${count}:"
    echo "   1. Unplug ALL dongles."
    echo "   2. Plug in ONLY dongle ${i}."
    press_enter "  Press Enter when ready... "
    default_serial="$(printf '0000100%d' "${i}")"
    s=$(ask "Serial number for dongle ${i}" "${default_serial}")
    if echo y | timeout 20 rtl_eeprom -d 0 -s "${s}"; then
      ok "Wrote serial ${s} to dongle ${i}."
    else
      warn "rtl_eeprom failed (or timed out) for dongle ${i}. Check it is plugged in and not in use."
    fi
    SERIALS+=("${s}")
    echo ""
  done

  info "Unplug and replug all dongles so the new serials take effect, then continue."
  press_enter "Press Enter when all dongles are plugged back in... "
else
  info "Skipping serial programming (no interactive terminal)."
  info "Program each dongle manually, one at a time:"
  info "    sudo rtl_eeprom -d 0 -s 00001001"
  info "  then unplug/replug, and use 00001002, 00001003, ... for the others."
fi

# ------------------------------------------------------------- stations
step "6 of 8: Configure stations"

# Always deploy the example station files as editable starting templates.
if compgen -G "${SRC}/stations/*.conf" >/dev/null 2>&1; then
  cp "${SRC}"/stations/*.conf "${APP_DIR}/stations/"
  ok "Deployed example station files to ${APP_DIR}/stations/"
else
  warn "No example station files found in ${SRC}/stations/."
fi

write_station() {
  local name="$1" band="$2" freq="$3" serial="$4" gain="$5" mount="$6" file="$7"
  {
    echo "# Pi Tuner v2 station"
    echo "#"
    echo "# ─── user settings ─────────────────────────────────"
    echo "NAME=${name}"
    echo "BAND=${band}"
    echo "FREQUENCY=${freq}"
    echo "SERIAL=${serial}"
    [ -n "${gain}" ] && echo "GAIN=${gain}"
    echo "MOUNT=${mount}"
    echo "# ─── end user settings ─────────────────────────────"
  } > "${file}"
}

if [ "${INTERACTIVE}" = "1" ] && [ "${count}" -gt 0 ] \
   && confirm "Configure stations now (name, band, frequency, serial)?"; then
  rm -f "${APP_DIR}"/stations/*.conf
  for i in $(seq 1 "${count}"); do
    info "Station ${i} of ${count}"
    name=$(ask "  Station name" "Station ${i}")
    band=$(ask "  Band (fm or wx)" "fm")
    freq=$(ask "  Frequency in MHz" "98.1")
    serial="${SERIALS[$((i-1))]:-$(printf '0000100%d' "${i}")}"
    serial=$(ask "  Dongle serial" "${serial}")
    gain=$(ask "  Gain in dB (press Enter for auto-gain)" "")
    mount=$(ask "  Icecast mount path" "/tuner${i}")
    write_station "${name}" "${band}" "${freq}" "${serial}" "${gain}" "${mount}" \
      "${APP_DIR}/stations/$(printf 'station%d.conf' "${i}")"
    ok "Wrote station ${i} (${name}, ${band} ${freq} MHz)."
  done
else
  info "Edit ${APP_DIR}/stations/*.conf to set your serials and frequencies,"
  info "then run:  sudo systemctl reload pituner"
fi

chown -R pituner:pituner "${APP_DIR}/stations"

# ------------------------------------------------------------- zabbix
step "7 of 8: Zabbix alerts (optional)"
ZABBIX_ENABLED="false"
if [ "${INTERACTIVE}" = "1" ] && confirm "Enable Zabbix trapper alerts now?"; then
  server=$(ask "  Zabbix server address" "zabbix.internal.example.com")
  port=$(ask "  Zabbix trapper port" "10051")
  hostname=$(ask "  Zabbix host name (the host you'll attach the template to)" "pituner")
  cat > "${APP_DIR}/zabbix.conf" <<EOF
# Zabbix trapper settings
# ─── user settings ─────────────────────────────────
ENABLED=true
SERVER=${server}
PORT=${port}
HOSTNAME=${hostname}
KEY_EVENT=pituner.event
KEY_STATUS=pituner.status
KEY_ACTIVE=pituner.stations_active
KEY_HEARTBEAT=pituner.heartbeat
INTERVAL=60
# ─── end user settings ─────────────────────────────
EOF
  chown pituner:pituner "${APP_DIR}/zabbix.conf"
  chmod 600 "${APP_DIR}/zabbix.conf"
  ZABBIX_ENABLED="true"
  ok "Zabbix configured. Import ${APP_DIR}/zabbix_template.xml on your Zabbix server,"
  ok "then create a host named '${hostname}' and attach the 'Pi Tuner v2' template."
else
  cat > "${APP_DIR}/zabbix.conf" <<'EOF'
# Zabbix trapper settings (disabled)
# ─── user settings ─────────────────────────────────
ENABLED=false
SERVER=
PORT=10051
HOSTNAME=pituner
KEY_EVENT=pituner.event
KEY_STATUS=pituner.status
KEY_ACTIVE=pituner.stations_active
KEY_HEARTBEAT=pituner.heartbeat
INTERVAL=60
# ─── end user settings ─────────────────────────────
EOF
  chown pituner:pituner "${APP_DIR}/zabbix.conf"
  chmod 600 "${APP_DIR}/zabbix.conf"
  info "Zabbix skipped. You can enable it later by editing ${APP_DIR}/zabbix.conf."
fi

# ------------------------------------------------------------- service
step "8 of 8: Install and start the service"
install -m 644 "${SRC}/pituner.service" /etc/systemd/system/pituner.service \
  || die "Could not copy pituner.service to /etc/systemd/system/"
systemctl daemon-reload
if ! systemctl enable pituner.service 2>&1; then
  fail "Failed to enable pituner.service (see the error above)."
  die "Run 'sudo systemctl enable pituner.service' to see the full message."
fi
if ! systemctl restart pituner.service 2>&1; then
  fail "Failed to start pituner.service."
  echo ""
  systemctl status pituner.service --no-pager -l 2>&1 || true
  echo ""
  fail "Run 'sudo journalctl -u pituner.service --no-pager -n 50' for details."
  die "Service did not start."
fi
ok "pituner.service installed and started."

# ------------------------------------------------------------- verify
step "Verification"
sleep 3
echo ""

info "Service status:"
if systemctl is-active --quiet pituner; then
  ok "pituner.service is running."
else
  fail "pituner.service is not running."
  systemctl status pituner --no-pager -l || true
fi

info "Dongle detection (tuner.py --check):"
python3 "${APP_DIR}/tuner.py" --dir "${APP_DIR}" --check || true

echo ""
info "Icecast mounts (should list one per station once streaming):"
sleep 2
curl -sf --max-time 5 http://localhost:8000/status-json.xsl \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); s=d.get("icestats",{}).get("source",[]); [print("  /"+x["listenurl"].rsplit("/",1)[-1]) for x in (s if isinstance(s,list) else [s])]' 2>/dev/null \
  || warn "No mounts yet — a station may still be tuning/connecting."

echo ""
info "Recent service log:"
journalctl -u pituner -n 15 --no-pager 2>/dev/null || true

echo ""
echo -e "${BOLD}${GREEN}Installation complete.${RESET}"
echo ""
echo "  Stream URLs:     http://<this-pi>:8000/<mount>"
echo "  Station configs: ${APP_DIR}/stations/*.conf"
echo "  View logs:       journalctl -u pituner -f"
echo "  Icecast status:  http://<this-pi>:8000/status-json.xsl"
echo ""
echo "  To add (or edit) a station later, put a file like this in"
echo "  ${APP_DIR}/stations/ and run:  sudo systemctl reload pituner"
echo ""
echo "    # my-station.conf"
echo "    NAME=My Station"
echo "    BAND=fm            # fm | wx"
echo "    FREQUENCY=98.1"
echo "    SERIAL=00001001"
echo "    GAIN=40.2          # optional; delete for auto-gain"
echo "    MOUNT=/mystation"
echo ""
echo "  Icecast admin password: ${ADMIN_PASS}"
echo "  Icecast source password (also in icecast.conf): ${SOURCE_PASS}"
if [ "${ZABBIX_ENABLED}" = "true" ]; then
  echo "  Zabbix: import ${APP_DIR}/zabbix_template.xml and attach it to your host."
fi
echo ""
