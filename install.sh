#!/usr/bin/env bash
# Pi-Tuner — interactive installer
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
#
# Re-running over an existing install asks whether to Upgrade (keep every
# setting, the default), Reconfigure (upgrade, then open `pituner config`) or
# start Fresh (reset Icecast and settings). Skip the question with --upgrade,
# --reconfigure or --fresh (or PITUNER_MODE=upgrade|reconfigure|fresh). Your
# settings are always backed up to /opt/pituner/backups/ first.

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

# ask_checked <prompt> <default> <validator-fn> <error-message>
# Prompts repeatedly until the answer passes the validator function.
ask_checked() {
  local prompt="$1" default="$2" validator="$3" errmsg="$4" ans
  while true; do
    ans=$(ask "$prompt" "$default")
    if "$validator" "$ans"; then
      printf '%s' "$ans"
      return 0
    fi
    printf "${RED}[ERROR]${RESET} %s\n" "$errmsg" >&2
  done
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

# A "stock" serial is the default an unprogrammed dongle ships with (all zeros
# or 00000001). Anything else is a custom serial the user already set.
is_stock_serial() {
  local s
  s="$(printf '%s' "$1" | tr -d '[:space:]')"
  [[ -z "$s" ]] || [[ "$s" =~ ^0*[01]$ ]]
}

# Station name: 1-8 chars, letters/digits with an optional single hyphen group
# (e.g. WXYZ, WXYZ-FM, WZYX-FM).
valid_name() {
  local n="$1"
  [[ -n "$n" ]] && [[ "${#n}" -le 8 ]] && [[ "$n" =~ ^[A-Za-z0-9]+(-[A-Za-z0-9]+)?$ ]]
}

# Band: only fm or wx (case-insensitive).
valid_band() {
  local b
  b="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
  [[ "$b" = "FM" || "$b" = "WX" ]]
}

# Frequency: a positive number within a sane broadcast range (80-170 MHz).
valid_freq() {
  local f="$1"
  [[ "$f" =~ ^[0-9]+([.][0-9]*)?$ ]] && awk -v f="$f" 'BEGIN { exit !(f >= 80 && f <= 170) }'
}

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
  info "Fetching Pi-Tuner files..."
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

# ------------------------------------------------------------- install mode
MODE="${PITUNER_MODE:-}"
for arg in "$@"; do
  case "${arg}" in
    --upgrade)     MODE="upgrade" ;;
    --reconfigure) MODE="reconfigure" ;;
    --fresh)       MODE="fresh" ;;
    -h|--help)
      sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" 2>/dev/null | sed '$d' | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "Unknown option '${arg}'. Use --upgrade, --reconfigure or --fresh." ;;
  esac
done

# An existing install = the app, or any settings file, is already in place.
EXISTING=0
if [ -f "${APP_DIR}/tuner.py" ] \
   || compgen -G "${APP_DIR}/*.conf" >/dev/null 2>&1 \
   || compgen -G "${APP_DIR}/stations/*.conf" >/dev/null 2>&1; then
  EXISTING=1
fi

if [ "${EXISTING}" = "1" ]; then
  case "${MODE}" in
    upgrade|reconfigure|fresh) ;;
    "")
      if [ "${INTERACTIVE}" = "1" ]; then
        echo ""
        echo "Pi-Tuner is already installed in ${APP_DIR}."
        echo "  1) Upgrade       keep all my settings, stations and Icecast passwords (recommended)"
        echo "  2) Reconfigure   upgrade, then open the settings menu"
        echo "  3) Fresh         reset Icecast and settings, then set everything up again"
        choice=$(ask "Choose" "1")
        case "${choice}" in
          2) MODE="reconfigure" ;;
          3) MODE="fresh" ;;
          *) MODE="upgrade" ;;
        esac
      else
        MODE="upgrade"
      fi ;;
    *) die "Unknown mode '${MODE}'. Use upgrade, reconfigure or fresh." ;;
  esac
  if [ "${MODE}" = "fresh" ] && [ "${INTERACTIVE}" = "1" ] \
     && ! confirm "Fresh install resets Icecast and your Pi-Tuner settings (a backup is kept). Continue?"; then
    die "Cancelled. Nothing was changed."
  fi
  BACKUP_NOTE="$(python3 "${SRC}/tuner.py" backup-config --dir "${APP_DIR}" 2>&1 || true)"
else
  MODE="first"
  BACKUP_NOTE=""
fi
KEEP_SETTINGS=0
if [ "${MODE}" = "upgrade" ] || [ "${MODE}" = "reconfigure" ]; then
  KEEP_SETTINGS=1
fi

clear 2>/dev/null || true
echo -e "${BOLD}${GREEN}Pi-Tuner installer${RESET}"
echo "A minimal multi-station SDR streamer for Raspberry Pi."
echo "This will install dependencies, set up Icecast, configure your stations,"
echo "and verify everything is running."
echo ""
if [ "${KEEP_SETTINGS}" = "1" ]; then
  info "Mode: ${MODE}. Your settings, stations and Icecast configuration are kept."
  [ -n "${BACKUP_NOTE}" ] && info "${BACKUP_NOTE}"
else
  warn "This RESETS your Icecast configuration: it regenerates the source and"
  warn "admin passwords (overwriting any existing Icecast setup) and restarts"
  warn "Icecast. If you use Icecast for other streams, back it up first."
  [ -n "${BACKUP_NOTE}" ] && info "${BACKUP_NOTE}"
fi
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

info "redsea decodes RBDS (station name / RadioText) for the Icecast now-playing."
info "It is built from source (windytan/redsea)."

if command -v redsea >/dev/null 2>&1; then
  ok "redsea is already installed: $(command -v redsea)"
else
  if apt-get install -y libsndfile1-dev libliquid-dev nlohmann-json3-dev; then
    BUILD_TMP="$(mktemp -d)"
    if git clone --depth 1 https://github.com/windytan/redsea.git "${BUILD_TMP}/redsea"; then
      (
        cd "${BUILD_TMP}/redsea"
        meson setup build >/dev/null 2>&1 && meson compile -C build >/dev/null 2>&1
      )
      REDSEA_BIN="$(find "${BUILD_TMP}/redsea/build" -name redsea -type f -perm -u+x 2>/dev/null | head -n1)"
      if [ -n "${REDSEA_BIN}" ]; then
        install -m 755 "${REDSEA_BIN}" /usr/local/bin/redsea
        ok "redsea installed to /usr/local/bin/redsea"
      else
        warn "redsea failed to build. RBDS now-playing will be disabled; audio is unaffected."
        warn "To fix later:  git clone https://github.com/windytan/redsea && meson setup build && meson compile -C build"
      fi
    else
      warn "Could not download redsea source. RBDS now-playing unavailable."
    fi
    rm -rf "${BUILD_TMP}"
  else
    warn "redsea build dependencies are not available in apt. RBDS now-playing unavailable."
  fi
fi

# ------------------------------------------------------------- icecast
step "3 of 8: Configure Icecast"
ICECAST_XML="/etc/icecast2/icecast.xml"

if [ "${KEEP_SETTINGS}" = "1" ]; then
  info "Keeping your existing Icecast configuration and passwords."
  SOURCE_PASS="$(sed -n 's/^SOURCE_PASSWORD=//p' "${APP_DIR}/icecast.conf" 2>/dev/null | head -n1)"
  if [ -z "${SOURCE_PASS}" ] && [ -f "${ICECAST_XML}" ]; then
    SOURCE_PASS="$(sed -n 's|.*<source-password>\(.*\)</source-password>.*|\1|p' "${ICECAST_XML}" | head -n1)"
    [ -n "${SOURCE_PASS}" ] && info "icecast.conf had no password; using the one from ${ICECAST_XML}."
  fi
  ADMIN_PASS=""
  systemctl enable icecast2 >/dev/null 2>&1 || warn "Could not enable icecast2 (may need manual start)."
  systemctl start icecast2 >/dev/null 2>&1 || warn "Could not start icecast2."
  sleep 1
  if curl -sf --max-time 5 http://localhost:8000/status-json.xsl >/dev/null; then
    ok "Icecast is responding on port 8000."
  else
    warn "Icecast not responding yet — it will be re-checked at the end."
  fi
else
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
fi

# ------------------------------------------------------------- app install
step "4 of 8: Install Pi-Tuner files"
info "Installing to ${APP_DIR}."

mkdir -p "${APP_DIR}/stations" "${APP_DIR}/recordings"
install -m 755 "${SRC}/tuner.py" "${APP_DIR}/tuner.py"
install -m 644 "${SRC}/configure.py" "${APP_DIR}/configure.py"
[ -f "${SRC}/zabbix_template.xml" ] && install -m 644 "${SRC}/zabbix_template.xml" "${APP_DIR}/zabbix_template.xml"
install -m 755 "${SRC}/pituner" /usr/local/bin/pituner
ok "Installed the 'pituner' command (try: sudo pituner config)."

# Keep an existing icecast.conf on upgrade; only (re)write it on a first or fresh install.
if [ "${KEEP_SETTINGS}" = "1" ] && [ -f "${APP_DIR}/icecast.conf" ]; then
  info "Keeping your existing ${APP_DIR}/icecast.conf."
else
cat > "${APP_DIR}/icecast.conf" <<EOF
# Icecast connection — shared by all stations
# ─── user settings ─────────────────────────────────
HOST=localhost
PORT=8000
SOURCE_PASSWORD=${SOURCE_PASS}
# Optional: admin credentials for RBDS now-playing updates (defaults to the source password)
# ADMIN_USER=admin
# ADMIN_PASSWORD=
# ─── end user settings ─────────────────────────────
EOF
fi

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

# Local rotating logs (tuner health / EAS / Zabbix mirror).
LOG_DIR="/var/www/pituner"
mkdir -p "${LOG_DIR}"
chown pituner:pituner "${LOG_DIR}"
[ -f "${SRC}/logrotate.conf" ] && install -m 644 "${SRC}/logrotate.conf" /etc/logrotate.d/pituner
ok "Log directory ${LOG_DIR} and logrotate config installed."

ok "Application files installed."

# ------------------------------------------------------------- serials
step "5 of 8: Detect dongle serials"
SERIALS=()
DONGLE_COUNT=0

if [ "${KEEP_SETTINGS}" = "1" ]; then
  info "Keeping your dongle serials. Check them any time with:  rtl_test"
elif [ "${INTERACTIVE}" = "1" ]; then
  info "Detecting connected dongles..."

  # Diagnostics: show what rtl_test sees.
  info "rtl_test reports:"
  timeout 4 rtl_test 2>&1 | grep -E 'Found|SN:' | sed 's/^/      /' || true

  # Device count comes from rtl_test's "Found N device(s):" line.
  DEVICE_COUNT=$(timeout 4 rtl_test 2>&1 | sed -nE 's/^Found[[:space:]]+([0-9]+)[[:space:]]+device.*/\1/p' | head -n1)
  DEVICE_COUNT=${DEVICE_COUNT:-0}

  if [ "${DEVICE_COUNT}" -eq 0 ]; then
    warn "No dongles detected. Check they are plugged in and the DVB driver is unloaded."
  else
    for idx in $(seq 0 "$(( DEVICE_COUNT - 1 ))"); do
      # Read the EEPROM serial directly from each device index (authoritative;
      # rtl_test's name string can vary, this "Serial number:" field cannot).
      serial=$(timeout 10 rtl_eeprom -d "${idx}" 2>&1 | awk -F'\t' '/^Serial number:/{print $NF}' | tr -d '[:space:]')
      if [ -z "${serial}" ]; then
        warn "Could not read serial from dongle index ${idx} (skipping)."
        continue
      fi
      if is_stock_serial "${serial}"; then
        default_serial="$(printf '0000100%d' "$(( ${#SERIALS[@]} + 1 ))")"
        s=$(ask "Dongle index ${idx} has stock serial '${serial}' — new serial?" "${default_serial}")
        if echo y | timeout 20 rtl_eeprom -d "${idx}" -s "${s}"; then
          ok "Programmed dongle index ${idx} with serial ${s}."
        else
          warn "rtl_eeprom failed for dongle index ${idx}. Check it is plugged in and not in use."
        fi
        SERIALS+=("${s}")
      else
        ok "Dongle index ${idx}: serial ${serial} (kept as-is)."
        SERIALS+=("${serial}")
      fi
    done
    DONGLE_COUNT=${#SERIALS[@]}
    info "Found ${DONGLE_COUNT} dongle(s): ${SERIALS[*]}"

    # Warn about duplicate serials (two dongles reporting the same number).
    DUP_SERIALS=$(printf '%s\n' "${SERIALS[@]}" | sort | uniq -d)
    if [ -n "${DUP_SERIALS}" ]; then
      warn "DUPLICATE SERIAL detected: ${DUP_SERIALS//$'\n'/ }."
      warn "Each dongle must have a unique serial. Fix with:"
      warn "    sudo rtl_eeprom -d <index> -s <unique-serial>   (then unplug/replug)"
    fi
  fi
else
  info "Skipping serial detection (no interactive terminal)."
  info "Program each dongle manually, one at a time:"
  info "    sudo rtl_eeprom -d 0 -s 00001001"
  info "  then unplug/replug, and use 00001002, 00001003, ... for the others."
fi

# ------------------------------------------------------------- stations
step "6 of 8: Configure stations"

# Deploy the example station files as editable starting templates, but never
# over stations that are already configured (they may be named like the examples).
if [ "${KEEP_SETTINGS}" = "1" ] && compgen -G "${APP_DIR}/stations/*.conf" >/dev/null 2>&1; then
  ok "Keeping your existing stations in ${APP_DIR}/stations/."
elif compgen -G "${SRC}/stations/*.conf" >/dev/null 2>&1; then
  cp "${SRC}"/stations/*.conf "${APP_DIR}/stations/"
  ok "Deployed example station files to ${APP_DIR}/stations/"
else
  warn "No example station files found in ${SRC}/stations/."
fi

write_station() {
  local name="$1" band="$2" freq="$3" serial="$4" gain="$5" rbds="$6"
  local record="$7" keep="$8" file="$9"
  {
    echo "# Pi-Tuner station"
    echo "#"
    echo "# ─── user settings ─────────────────────────────────"
    echo "NAME=${name}"
    echo "BAND=${band}"
    echo "FREQUENCY=${freq}"
    echo "SERIAL=${serial}"
    [ -n "${gain}" ] && echo "GAIN=${gain}"
    [ "${band}" = "fm" ] && echo "RBDS=${rbds}"
    if [ "${record}" = "true" ]; then
      echo "RECORD=true"
      [ -n "${keep}" ] && echo "RECORD_KEEP_DAYS=${keep}"
    fi
    echo "# ─── end user settings ─────────────────────────────"
  } > "${file}"
}

if [ "${KEEP_SETTINGS}" = "1" ]; then
  info "To add or change stations later:  sudo pituner config"
elif [ "${INTERACTIVE}" = "1" ] \
   && confirm "Configure stations now (name, band, frequency, serial)?"; then
  count=$(ask "How many stations will you configure?" "${DONGLE_COUNT:-1}")
  rm -f "${APP_DIR}"/stations/*.conf
  for i in $(seq 1 "${count}"); do
    info "Station ${i} of ${count}"
    name=$(ask_checked "  Station name (call sign, max 8 chars)" "" valid_name \
      "Name must be 1-8 characters: letters/digits with an optional hyphen, e.g. WXYZ, WXYZ-FM, K244FM, or WXJ86.")
    name=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')
    band=$(ask_checked "  Band (FM or WX)" "FM" valid_band "Band must be FM or WX.")
    band=$(printf '%s' "$band" | tr '[:upper:]' '[:lower:]')
    freq=$(ask_checked "  Frequency in MHz (FM e.g. 98.1, WX e.g. 162.55)" "98.1" valid_freq "Frequency must be a number between 80 and 170 MHz.")
    serial="${SERIALS[$((i-1))]:-$(printf '0000100%d' "${i}")}"
    serial=$(ask "  Dongle serial" "${serial}")
    gain=$(ask "  Gain in dB (press Enter for auto-gain)" "")
    rbds=false
    if [ "${band}" = "fm" ] && confirm "  Send RBDS text to Icecast now-playing?"; then
      rbds=true
    fi
    record=false
    keep=""
    if confirm "  Record this station to ${APP_DIR}/recordings (15-minute MP3 files, ~1.4 GB/day)?"; then
      record=true
      keep=$(ask "  Days of recordings to keep (press Enter to keep everything)" "")
      case "${keep}" in ''|*[!0-9]*) keep="" ;; esac
    fi
    write_station "${name}" "${band}" "${freq}" "${serial}" "${gain}" "${rbds}" \
      "${record}" "${keep}" "${APP_DIR}/stations/$(printf 'station%d.conf' "${i}")"
    ok "Wrote station ${i} (${name}, ${band} ${freq} MHz)."
  done
else
  info "Edit ${APP_DIR}/stations/*.conf to set your serials and frequencies,"
  info "then run:  sudo systemctl reload pituner"
fi

chown -R pituner:pituner "${APP_DIR}/stations"

# ------------------------------------------------------------- zabbix
step "7 of 8: Zabbix and email alerts (optional)"
ZABBIX_ENABLED="false"
EMAIL_ENABLED="false"
smtp_to=""

if [ "${KEEP_SETTINGS}" = "1" ]; then
  info "Keeping your existing Zabbix and email settings."
  grep -qi '^ENABLED=true' "${APP_DIR}/zabbix.conf" 2>/dev/null && ZABBIX_ENABLED="true"
  if grep -qi '^ENABLED=true' "${APP_DIR}/smtp.conf" 2>/dev/null; then
    EMAIL_ENABLED="true"
    smtp_to="$(sed -n 's/^TO=//p' "${APP_DIR}/smtp.conf" | head -n1)"
  fi
  # Add settings that newer versions understand (never changes a value you set).
  python3 "${APP_DIR}/tuner.py" upgrade-config --dir "${APP_DIR}" || warn "Could not update the config files."
  chown -R pituner:pituner "${APP_DIR}"
  chmod 600 "${APP_DIR}"/*.conf
else
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
KEY_ACTIVE=pituner.stations_active
KEY_HEARTBEAT=pituner.heartbeat
KEY_EAS=pituner.eas
INTERVAL=60
EAS_DETECT=true
LEVEL_MONITOR=true
# ─── end user settings ─────────────────────────────
EOF
  chown pituner:pituner "${APP_DIR}/zabbix.conf"
  chmod 600 "${APP_DIR}/zabbix.conf"
  ZABBIX_ENABLED="true"
  ok "Zabbix configured. Import ${APP_DIR}/zabbix_template.xml on your Zabbix server,"
  ok "then create a host named '${hostname}' and attach the 'Pi-Tuner' template."
else
  cat > "${APP_DIR}/zabbix.conf" <<'EOF'
# Zabbix trapper settings (disabled)
# ─── user settings ─────────────────────────────────
ENABLED=false
SERVER=
PORT=10051
HOSTNAME=pituner
KEY_EVENT=pituner.event
KEY_ACTIVE=pituner.stations_active
KEY_HEARTBEAT=pituner.heartbeat
KEY_EAS=pituner.eas
INTERVAL=60
EAS_DETECT=true
LEVEL_MONITOR=true
# ─── end user settings ─────────────────────────────
EOF
  chown pituner:pituner "${APP_DIR}/zabbix.conf"
  chmod 600 "${APP_DIR}/zabbix.conf"
  info "Zabbix skipped. You can enable it later by editing ${APP_DIR}/zabbix.conf."
fi

# ------------------------------------------------------------- email
EMAIL_ENABLED="false"
smtp_to=""
if [ "${INTERACTIVE}" = "1" ] && confirm "Enable SMTP email alerts now?"; then
  smtp_host=$(ask "  SMTP server (e.g. smtp.example.com)" "")
  smtp_port=$(ask "  SMTP port" "587")
  smtp_sec=$(ask "  Security (starttls, ssl or none)" "starttls")
  case "${smtp_sec}" in
    starttls|ssl|none) ;;
    *) warn "Unknown security '${smtp_sec}', using starttls."; smtp_sec="starttls" ;;
  esac
  smtp_user=$(ask "  SMTP username (press Enter if the server needs no login)" "")
  smtp_pass=""
  if [ -n "${smtp_user}" ]; then
    printf "${CYAN}[?]${RESET} %s: " "  SMTP password (hidden)" >&2
    read -r -s smtp_pass
    echo "" >&2
  fi
  smtp_from=$(ask "  From address" "pituner@$(hostname)")
  smtp_to=$(ask "  Send alerts to (comma-separated addresses)" "")
  if [ -z "${smtp_host}" ] || [ -z "${smtp_to}" ]; then
    warn "SMTP server and recipient are both required; email alerts left disabled."
    install -m 600 "${SRC}/smtp.conf" "${APP_DIR}/smtp.conf"
  else
    cat > "${APP_DIR}/smtp.conf" <<EOF
# Pi-Tuner SMTP email alerts. Keep this file mode 600: it holds the password.
# ─── user settings ─────────────────────────────────
ENABLED=true
HOST=${smtp_host}
PORT=${smtp_port}
SECURITY=${smtp_sec}
VERIFY_TLS=true
USERNAME=${smtp_user}
PASSWORD=${smtp_pass}
FROM=${smtp_from}
TO=${smtp_to}
SUBJECT_PREFIX=[Pi-Tuner]
TIMEOUT=10
ALERT_STATION=true
ALERT_EAS=true
ALERT_DISK=true
ALERT_SERVICE=true
DOWN_DELAY=120
DISK_MIN_GB=2
# ─── end user settings ─────────────────────────────
EOF
    EMAIL_ENABLED="true"
  fi
  chown pituner:pituner "${APP_DIR}/smtp.conf"
  chmod 600 "${APP_DIR}/smtp.conf"
  if [ "${EMAIL_ENABLED}" = "true" ]; then
    ok "Email alerts configured (${APP_DIR}/smtp.conf)."
    if confirm "  Send a test email now?"; then
      if python3 "${APP_DIR}/tuner.py" test-email --dir "${APP_DIR}"; then
        ok "Test email sent. Check ${smtp_to}."
      else
        warn "The test email failed. Fix ${APP_DIR}/smtp.conf and retry with:"
        warn "  sudo python3 ${APP_DIR}/tuner.py test-email --dir ${APP_DIR}"
      fi
    fi
  fi
else
  install -m 600 "${SRC}/smtp.conf" "${APP_DIR}/smtp.conf"
  chown pituner:pituner "${APP_DIR}/smtp.conf"
  info "Email alerts skipped. You can enable them later by editing ${APP_DIR}/smtp.conf."
fi
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

# ------------------------------------------------------------- reconfigure
if [ "${MODE}" = "reconfigure" ]; then
  step "Reconfigure"
  info "Opening the settings menu (you can run it any time with:  sudo pituner config)."
  python3 "${APP_DIR}/tuner.py" config --dir "${APP_DIR}" || true
fi

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
if [ "${KEEP_SETTINGS}" = "1" ]; then
  echo -e "${BOLD}${GREEN}Upgrade complete.${RESET} Your settings, stations and Icecast passwords were kept."
  [ -n "${BACKUP_NOTE}" ] && echo "  ${BACKUP_NOTE}"
else
  echo -e "${BOLD}${GREEN}Installation complete.${RESET}"
fi
echo ""
echo "  Stream URLs:     http://<this-pi>:8000/<mount>"
echo "  Station configs: ${APP_DIR}/stations/*.conf"
echo "  View logs:       journalctl -u pituner -f"
echo "  Icecast status:  http://<this-pi>:8000/status-json.xsl"
echo ""
echo "  Change settings any time with:  sudo pituner config"
echo "  (or edit the files by hand and run:  sudo systemctl reload pituner)"
echo ""
echo "  To add (or edit) a station by hand, put a file like this in"
echo "  ${APP_DIR}/stations/ and run:  sudo systemctl reload pituner"
echo ""
echo "    # my-station.conf"
echo "    NAME=My Station"
echo "    BAND=fm            # fm | wx"
echo "    FREQUENCY=98.1"
echo "    SERIAL=00001001"
echo "    GAIN=40.2          # optional; delete for auto-gain"
echo "    RBDS=true          # optional (fm only); RBDS text -> Icecast now-playing"
echo ""
if [ "${KEEP_SETTINGS}" = "1" ]; then
  echo "  Icecast passwords are unchanged (admin password: see /etc/icecast2/icecast.xml)."
else
  echo "  Icecast admin password: ${ADMIN_PASS}"
  echo "  Icecast source password (also in icecast.conf): ${SOURCE_PASS}"
fi
if [ "${ZABBIX_ENABLED}" = "true" ]; then
  echo "  Zabbix: import ${APP_DIR}/zabbix_template.xml and attach it to your host."
fi
if [ "${EMAIL_ENABLED}" = "true" ]; then
  echo "  Email alerts go to ${smtp_to}. Test: sudo python3 ${APP_DIR}/tuner.py test-email --dir ${APP_DIR}"
fi
echo ""
