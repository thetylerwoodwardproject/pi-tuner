#!/usr/bin/env bash
# Pi Tuner v2 — uninstaller
#
# Removes the pituner service, application files, the pituner user, and the
# demux decoder binary. System packages (rtl-sdr, ffmpeg, icecast2) are left in
# place — remove them yourself if you no longer need them.
#
# Usage:
#   sudo ./uninstall.sh            # interactive (asks before removing each thing)
#   sudo ./uninstall.sh --yes      # non-interactive, remove everything

set -uo pipefail

APP_DIR="/opt/pituner"
DEMUX_BIN="/usr/local/bin/demux"
ICECAST_XML="/etc/icecast2/icecast.xml"

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

FORCE=0
if [ "${1:-}" = "--yes" ] || [ "${1:-}" = "-y" ] || [ "${1:-}" = "--purge" ]; then
  FORCE=1
fi

confirm() {
  local prompt="${1:-Continue?}" ans
  if [ "$FORCE" = "1" ]; then
    info "$prompt  (--yes)"
    return 0
  fi
  printf "${YELLOW}[?]${RESET} %s [y/N]: " "$prompt" >&2
  read -r ans
  [[ "$ans" =~ ^[Yy] ]]
}

# ---------------------------------------------------------------- preflight
if [ "${EUID}" -ne 0 ]; then
  die "Please run as root:  sudo ./uninstall.sh"
fi

clear 2>/dev/null || true
echo -e "${BOLD}${RED}Pi Tuner v2 uninstaller${RESET}"
echo "This removes the service, application files, the 'pituner' user, and the"
echo "demux decoder. It does not touch system packages or Icecast itself."
echo ""

# ------------------------------------------------------------- service
step "Stop and remove the service"
systemctl stop pituner.service 2>/dev/null || true
systemctl disable pituner.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/pituner.service
systemctl daemon-reload
ok "pituner.service stopped, disabled, and its unit file removed."

# ------------------------------------------------------------- files
step "Remove application files"
if [ -d "${APP_DIR}" ]; then
  if confirm "Delete ${APP_DIR}?"; then
    rm -rf "${APP_DIR}"
    ok "Removed ${APP_DIR}."
  else
    info "Kept ${APP_DIR}."
  fi
else
  info "${APP_DIR} not present."
fi

# ------------------------------------------------------------- user
step "Remove the pituner user"
if id -u pituner >/dev/null 2>&1; then
  if confirm "Delete the 'pituner' user?"; then
    userdel pituner
    ok "Removed the 'pituner' user."
  else
    info "Kept the 'pituner' user."
  fi
else
  info "'pituner' user not present."
fi

# ------------------------------------------------------------- demux
step "Remove the demux decoder"
if [ -f "${DEMUX_BIN}" ]; then
  if confirm "Delete ${DEMUX_BIN}?"; then
    rm -f "${DEMUX_BIN}"
    ok "Removed ${DEMUX_BIN}."
  else
    info "Kept ${DEMUX_BIN}."
  fi
else
  info "${DEMUX_BIN} not present."
fi

# ------------------------------------------------------------- icecast pw
step "Icecast passwords"
if [ -f "${ICECAST_XML}" ] && grep -q '<source-password>' "${ICECAST_XML}"; then
  info "install.sh changed the Icecast source/admin passwords."
  if confirm "Reset them back to 'hackme' (the Debian default)?"; then
    sed -i "s|<source-password>.*</source-password>|<source-password>hackme</source-password>|" "${ICECAST_XML}"
    sed -i "s|<admin-password>.*</admin-password>|<admin-password>hackme</admin-password>|" "${ICECAST_XML}"
    ok "Icecast passwords reset to 'hackme'."
  else
    info "Icecast passwords left as-is."
  fi
else
  info "Icecast config not found or already unmodified."
fi

# ------------------------------------------------------------- done
echo ""
ok "Uninstall complete."
echo ""
echo "  Left in place (remove manually if you no longer need them):"
echo "    sudo apt-get remove --purge rtl-sdr ffmpeg icecast2"
echo ""
