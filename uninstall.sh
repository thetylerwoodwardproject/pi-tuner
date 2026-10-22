#!/usr/bin/env bash
# Pi-Tuner — uninstaller
#
# Wipes everything Pi-Tuner put on the system: the service, /opt/pituner (with
# the recordings), the pituner user, the demux/redsea/pituner commands, the logs
# (/var/log and /var/www), the logrotate rule, the SMB recordings share, the
# DVB driver blacklist and any nginx config named after pituner.
#
# It then asks whether to keep Icecast and Nginx. Other packages that
# install.sh pulled in (rtl-sdr, ffmpeg, samba) are left alone.
#
# Usage:
#   sudo ./uninstall.sh                  # shows the plan, asks once, then asks about Icecast / Nginx
#   sudo ./uninstall.sh --yes            # no questions; wipes Pi-Tuner and KEEPS Icecast and Nginx
#   sudo ./uninstall.sh --yes --remove-icecast --remove-nginx
#                                        # also purge those packages (--yes only)

set -uo pipefail

# Paths can be overridden from the environment (the tests do this).
APP_DIR="${APP_DIR:-/opt/pituner}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
LOGROTATE_FILE="${LOGROTATE_FILE:-/etc/logrotate.d/pituner}"
BLACKLIST_FILE="${BLACKLIST_FILE:-/etc/modprobe.d/rtl-sdr-blacklist.conf}"
ICECAST_XML="${ICECAST_XML:-/etc/icecast2/icecast.xml}"
SMB_CONF="${SMB_CONF:-/etc/samba/smb.conf}"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
WWW_DIR="${WWW_DIR:-/var/www}"
VARLOG_DIR="${VARLOG_DIR:-/var/log}"

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
REMOVE_ICECAST=0
REMOVE_NGINX=0
for arg in "$@"; do
  case "${arg}" in
    --yes|-y|--purge) FORCE=1 ;;
    --remove-icecast) REMOVE_ICECAST=1 ;;
    --remove-nginx)   REMOVE_NGINX=1 ;;
    *) die "Unknown option: ${arg}  (use --yes, --remove-icecast, --remove-nginx)" ;;
  esac
done
if [ "${FORCE}" = "0" ] && { [ "${REMOVE_ICECAST}" = "1" ] || [ "${REMOVE_NGINX}" = "1" ]; }; then
  die "--remove-icecast and --remove-nginx only apply together with --yes; without it you are asked."
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

# "Keep X?" defaults to keeping it; returns 0 to keep, 1 to remove.
keep() {
  local prompt="$1" ans
  [ "$FORCE" = "1" ] && return 0
  printf "${YELLOW}[?]${RESET} %s [Y/n]: " "$prompt" >&2
  read -r ans
  [[ ! "$ans" =~ ^[Nn] ]]
}

# Files and folders in $1 (not recursive) named after pituner / pi-tuner.
pituner_named() {
  find "$1" -maxdepth 1 \( -iname '*pituner*' -o -iname '*pi-tuner*' \) 2>/dev/null
}

# ---------------------------------------------------------------- preflight
if [ "${EUID}" -ne 0 ]; then
  die "Please run as root:  sudo ./uninstall.sh"
fi

clear 2>/dev/null || true
echo -e "${BOLD}${RED}Pi-Tuner uninstaller${RESET}"
echo "This removes everything Pi-Tuner put on this machine:"
echo "  - the pituner service and the 'pituner' user"
echo "  - ${APP_DIR}, including its configuration"
REC_NOTE=""
if [ -n "$(find "${APP_DIR}/recordings" -type f 2>/dev/null | head -n1)" ]; then
  REC_NOTE="$(du -sh "${APP_DIR}/recordings" 2>/dev/null | cut -f1)"
  echo -e "    ${BOLD}including the audio recordings (${REC_NOTE})${RESET}"
fi
echo "  - Pi-Tuner logs and anything named pituner under ${VARLOG_DIR} and ${WWW_DIR}"
echo "  - the logrotate rule, the recordings SMB share, the DVB driver blacklist"
echo "  - the demux, redsea and pituner commands"
echo "  - any nginx config named after pituner"
echo ""
echo "You will then be asked whether to keep Icecast and Nginx."
echo ""
if ! confirm "Remove all of this?"; then
  info "Nothing was changed."
  exit 0
fi

# ------------------------------------------------------------- service
step "Stop and remove the service"
systemctl stop pituner.service 2>/dev/null || true
systemctl disable pituner.service >/dev/null 2>&1 || true
rm -f "${UNIT_DIR}/pituner.service"
rm -rf "${UNIT_DIR}/pituner.service.d"
systemctl daemon-reload
systemctl reset-failed pituner.service 2>/dev/null || true
ok "pituner.service stopped, disabled, and its unit file removed."

# ------------------------------------------------------------- files
step "Remove application files"
if [ -d "${APP_DIR}" ]; then
  rm -rf "${APP_DIR}"
  ok "Removed ${APP_DIR}${REC_NOTE:+ (and ${REC_NOTE} of recordings)}."
else
  info "${APP_DIR} not present."
fi

# ------------------------------------------------------------- user
step "Remove the pituner user"
if id -u pituner >/dev/null 2>&1; then
  userdel pituner 2>/dev/null && ok "Removed the 'pituner' user." || warn "Could not remove the 'pituner' user."
else
  info "'pituner' user not present."
fi
if getent group pituner >/dev/null 2>&1; then
  groupdel pituner 2>/dev/null && ok "Removed the 'pituner' group." || true
fi

# ------------------------------------------------------------- commands
step "Remove the demux, redsea and pituner commands"
for f in "${BIN_DIR}/demux" "${BIN_DIR}/redsea" "${BIN_DIR}/pituner"; do
  if [ -f "${f}" ]; then
    rm -f "${f}"
    ok "Removed ${f}."
  else
    info "${f} not present."
  fi
done

# ------------------------------------------------------------- DVB blacklist
step "Remove the DVB driver blacklist"
if [ -f "${BLACKLIST_FILE}" ]; then
  rm -f "${BLACKLIST_FILE}"
  ok "Removed ${BLACKLIST_FILE} (the DVB TV driver may claim RTL dongles again after a reboot)."
else
  info "${BLACKLIST_FILE} not present."
fi

# ------------------------------------------------------------- recordings share
if grep -qF "# BEGIN pituner recordings share" "${SMB_CONF}" 2>/dev/null; then
  step "Remove the recordings SMB share"
  sed -i "/^# BEGIN pituner recordings share/,/^# END pituner recordings share/d" "${SMB_CONF}"
  systemctl restart smbd 2>/dev/null || true
  ok "Removed the [recordings] share from ${SMB_CONF} (Samba itself is left installed)."
fi

# ------------------------------------------------------------- logs
step "Remove log rotation and logs"
rm -f "${LOGROTATE_FILE}"
ok "Removed ${LOGROTATE_FILE}."
removed_any=0
for base in "${VARLOG_DIR}" "${WWW_DIR}"; do
  while IFS= read -r path; do
    [ -n "${path}" ] || continue
    rm -rf "${path}"
    ok "Removed ${path}."
    removed_any=1
  done < <(pituner_named "${base}")
done
[ "${removed_any}" = "1" ] || info "No Pi-Tuner logs found under ${VARLOG_DIR} or ${WWW_DIR}."

# ------------------------------------------------------------- nginx
step "Nginx"
if command -v nginx >/dev/null 2>&1 || [ -d "${NGINX_DIR}" ]; then
  nginx_changed=0
  for sub in sites-enabled sites-available conf.d snippets; do
    [ -d "${NGINX_DIR}/${sub}" ] || continue
    while IFS= read -r path; do
      [ -n "${path}" ] || continue
      rm -rf "${path}"
      ok "Removed nginx config ${path}."
      nginx_changed=1
    done < <(pituner_named "${NGINX_DIR}/${sub}")
  done
  if [ "${nginx_changed}" = "1" ]; then
    if nginx -t >/dev/null 2>&1; then
      systemctl reload nginx 2>/dev/null || true
    else
      warn "nginx -t failed after removing the pituner config; check your nginx configuration."
    fi
  else
    info "No nginx config named after pituner."
  fi
  if keep "Keep Nginx installed?" && [ "${REMOVE_NGINX}" = "0" ]; then
    info "Kept Nginx."
  else
    systemctl stop nginx 2>/dev/null || true
    systemctl disable nginx >/dev/null 2>&1 || true
    if apt-get purge -y nginx nginx-common >/dev/null 2>&1; then
      ok "Removed Nginx."
    else
      warn "Could not remove Nginx; run: sudo apt-get purge nginx nginx-common"
    fi
  fi
else
  info "Nginx is not installed."
fi

# ------------------------------------------------------------- icecast
step "Icecast"
if command -v icecast2 >/dev/null 2>&1 || [ -f "${ICECAST_XML}" ]; then
  if keep "Keep Icecast installed?" && [ "${REMOVE_ICECAST}" = "0" ]; then
    info "Kept Icecast."
    if [ -f "${ICECAST_XML}" ] && grep -q '<source-password>' "${ICECAST_XML}"; then
      info "install.sh changed the Icecast source/admin passwords."
      if confirm "Reset them back to 'hackme' (the Debian default)?"; then
        sed -i "s|<source-password>.*</source-password>|<source-password>hackme</source-password>|" "${ICECAST_XML}"
        sed -i "s|<admin-password>.*</admin-password>|<admin-password>hackme</admin-password>|" "${ICECAST_XML}"
        ok "Icecast passwords reset to 'hackme'."
      else
        info "Icecast passwords left as-is."
      fi
    fi
  else
    systemctl stop icecast2 2>/dev/null || true
    systemctl disable icecast2 >/dev/null 2>&1 || true
    if apt-get purge -y icecast2 >/dev/null 2>&1; then
      ok "Removed Icecast and its configuration."
    else
      warn "Could not remove Icecast; run: sudo apt-get purge icecast2"
    fi
  fi
else
  info "Icecast is not installed."
fi

# ------------------------------------------------------------- done
echo ""
ok "Uninstall complete."
echo ""
echo "  Left in place (remove manually if you no longer need them):"
echo "    sudo apt-get remove --purge rtl-sdr ffmpeg samba"
echo ""
