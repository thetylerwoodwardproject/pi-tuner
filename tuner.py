#!/usr/bin/env python3
"""Pi-Tuner -- a minimal multi-station SDR streamer.

Reads one KEY=value config file per station from the ``stations/`` directory,
resolves each dongle serial to a device index, and streams each station to a
local Icecast server. A small supervisor restarts a station if its pipeline
dies, and (optionally) pushes status/events to a Zabbix server over the
Zabbix trapper protocol.

No web UI, no auth, no alerts daemon -- just a config directory and this file,
run under systemd.

Usage:
    tuner.py [--dir /opt/pituner] [--check]

    --check  validate config, resolve serials, and print the pipeline
             command for each station without launching anything.

    tuner.py detect-eas --dir /opt/pituner <name> <rate> <channels>
             run the EAS attention-tone detector and SAME header decoder on
             raw s16le PCM from stdin.

    tuner.py record --dir /opt/pituner <name> <rate> <channels>
             record raw s16le PCM from stdin to 128 kbps MP3 files, one per
             15 minutes, under <dir>/recordings/<name>/YYYY/MM/DD/.

    tuner.py config --dir /opt/pituner
             interactive menu to edit stations, Icecast, Zabbix and email.

    tuner.py upgrade-config --dir /opt/pituner [--dry-run]
             add settings that newer versions understand to existing config
             files, without changing any value you've set.

    tuner.py backup-config --dir /opt/pituner
             copy the config files to backups/<timestamp>/ (keeps the last 5).

    tuner.py test-email --dir /opt/pituner [--to ADDRESS]
             send one test message using smtp.conf and report any SMTP error.

    tuner.py level --dir /opt/pituner --tuner ID <name> <rate> <channels>
             send the audio level (dBFS) of raw s16le PCM from stdin to Zabbix.

    tuner.py rbds-meta --dir /opt/pituner [--tuner ID] [--log-name NAME] <mount>
             read redsea JSON lines from stdin and push the decoded RBDS
             text to the Icecast now-playing metadata for <mount>. With
             --log-name, also append it to the station's daily RBDS.log.
"""
import argparse
import array
import base64
import cmath
import collections
import datetime
import email.message
import email.utils
import json
import math
import os
import queue
import re
import select
import shutil
import signal
import smtplib
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_DIR = "/opt/pituner"
TUNER_PATH = os.path.realpath(os.path.abspath(__file__))
LOG_DIR = "/var/log/pituner"
WX_GENRE = "Weather"
FM_DEFAULT_GENRE = "Radio"
REC_CHUNK_SECS = 900
REC_RETRY_SECS = 30
REC_PRUNE_SECS = 3600
RBDS_LOG_FILE = "RBDS.log"
RBDS_PS_SETTLE_SECS = 12
RBDS_PS_WINDOW_SECS = 60
RBDS_PS_DYNAMIC_CHANGES = 3
PTY_SCAN_SECS = 8.0
ENUMERATE_SECS = 4.0

# ------------------------------------------------------------------- logging

_log_failed = set()


_URL_SECRET_RE = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")


def redact_secrets(text):
    """Mask the password in user:password@host URLs, e.g. the Icecast source URL
    in a pipeline command, so logs can be read (or served) without leaking it."""
    return _URL_SECRET_RE.sub(r"\1***@", text)


def log_file(name, msg):
    """Append a timestamped line to /var/log/pituner/<name>. Best-effort.

    A failure never interrupts tuning, but it is reported once per file on
    stderr (journalctl -u pituner) so unwritable logs aren't silently blank."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, name), "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {redact_secrets(str(msg))}\n")
        _log_failed.discard(name)
    except OSError as e:
        if name not in _log_failed:
            _log_failed.add(name)
            print(f"cannot write {os.path.join(LOG_DIR, name)}: {e}",
                  file=sys.stderr, flush=True)


def sd_notify(state):
    """Tell systemd (READY=1, WATCHDOG=1, ...) when run as a service. No-op
    otherwise. With WatchdogSec= in the unit, systemd restarts the service if
    the supervisor loop stops pinging, so a hang can't leave it half-alive."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
    except OSError:
        pass


def log(msg, err=False):
    msg = redact_secrets(str(msg))
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
          file=sys.stderr if err else sys.stdout, flush=True)
    log_file("tuner.log", msg)


# -------------------------------------------------------------- config files

_QUOTED_VALUE_RE = re.compile(r'^"(.*?)"\s*(?:#.*)?$')


def parse_keyvalue(path):
    """Parse a flat KEY=value file into a lowercase-keyed dict.

    Blank lines and full-line comments are ignored; a trailing ``# comment``
    (preceded by whitespace) is stripped from a value. A value wrapped in double
    quotes is taken literally, so it may contain `` #``. A missing file yields
    an empty dict.
    """
    conf = {}
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return conf
    with f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            quoted = _QUOTED_VALUE_RE.match(value)
            if quoted:
                value = quoted.group(1)
            else:
                value = re.split(r"\s+#", value)[0].strip()
            conf[key.strip().lower()] = value
    return conf


# ------------------------------------------------- editing config files in place

USER_SETTINGS_END = "# ─── end user settings"


def format_conf_value(value):
    """Render a value so parse_keyvalue reads it back unchanged."""
    value = str(value)
    if value != value.strip() or re.search(r"\s#", value) or value.startswith('"'):
        return '"' + value + '"'
    return value


def _read_lines(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def _atomic_write(path, text, mode=None):
    """Write via a temp file + rename, keeping the file's mode and owner."""
    try:
        st = os.stat(path)
        mode, uid, gid = st.st_mode & 0o7777, st.st_uid, st.st_gid
    except OSError:
        uid = gid = None
        mode = 0o600 if mode is None else mode
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        if uid is not None:
            try:
                os.chown(tmp, uid, gid)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _insert_index(lines):
    """Where new lines go: just above the end-of-settings marker, else the end."""
    for i, line in enumerate(lines):
        if line.strip().startswith(USER_SETTINGS_END):
            return i
    return len(lines)


def update_keyvalue(path, changes):
    """Set keys in a KEY=value file without disturbing anything else.

    Comments, blank lines and ordering are kept. An existing key keeps its
    trailing ``# comment``; a commented-out hint (``# KEY=...``) is switched on;
    an unknown key is added above the end-of-settings marker (or appended).
    Returns True if the file changed.
    """
    lines = _read_lines(path)
    original = list(lines)
    for key, value in changes.items():
        rendered = format_conf_value(value)
        active = re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*)(.*)$", re.IGNORECASE)
        hint = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=\s*(.*)$", re.IGNORECASE)
        for i, line in enumerate(lines):
            m = active.match(line)
            if m:
                rest = m.group(3)
                comment = ""
                if not rest.lstrip().startswith('"'):
                    c = re.search(r"\s+#.*$", rest)
                    comment = c.group(0) if c else ""
                else:
                    q = _QUOTED_VALUE_RE.match(rest.strip())
                    if q:
                        c = re.search(r'"\s+(#.*)$', rest)
                        comment = "  " + c.group(1) if c else ""
                lines[i] = f"{m.group(1)}{key.upper()}{m.group(2)}{rendered}{comment}"
                break
        else:
            for i, line in enumerate(lines):
                m = hint.match(line)
                if m:
                    c = re.search(r"\s+#.*$", m.group(1))
                    lines[i] = f"{key.upper()}={rendered}{c.group(0) if c else ''}"
                    break
            else:
                lines.insert(_insert_index(lines), f"{key.upper()}={rendered}")
    if lines == original:
        return False
    _atomic_write(path, "\n".join(lines) + "\n")
    return True


# (key, default, written active?, comment, only for this band)
CONFIG_KEYS = {
    "icecast.conf": [
        ("HOST", "localhost", True, "Icecast host", None),
        ("PORT", "8000", True, "Icecast source port", None),
        ("SOURCE_PASSWORD", "", True, "must match <source-password> in icecast.xml", None),
        ("ADMIN_USER", "admin", False, "admin login for now-playing updates", None),
        ("ADMIN_PASSWORD", "", False, "if set, used instead of the source login", None),
    ],
    "zabbix.conf": [
        ("ENABLED", "false", True, "true to send alerts to Zabbix", None),
        ("SERVER", "", True, "your Zabbix server", None),
        ("PORT", "10051", True, "Zabbix trapper port", None),
        ("HOSTNAME", "pituner", True, "the Zabbix host the template is attached to", None),
        ("KEY_EVENT", "pituner.event", True, "item key, must match the template", None),
        ("KEY_ACTIVE", "pituner.stations_active", True, "item key, must match the template", None),
        ("KEY_HEARTBEAT", "pituner.heartbeat", True, "item key, must match the template", None),
        ("KEY_EAS", "pituner.eas", True, "item key, must match the template", None),
        ("INTERVAL", "60", True, "status snapshot / heartbeat interval, seconds", None),
        ("EAS_DETECT", "false", True, "true to detect the EAS attention tone", None),
        ("LEVEL_MONITOR", "true", True, "send each tuner's audio level (dBFS) to Zabbix", None),
        ("RSSI_MONITOR", "true", True, "send each tuner's reception level (RSSI, dB) to Zabbix", None),
    ],
    "smtp.conf": [
        ("ENABLED", "false", True, "true to send email alerts", None),
        ("HOST", "", True, "SMTP server", None),
        ("PORT", "587", True, "SMTP port", None),
        ("SECURITY", "starttls", True, "starttls, ssl or none", None),
        ("VERIFY_TLS", "true", True, "false for a self-signed internal relay", None),
        ("USERNAME", "", True, "blank if the server needs no login", None),
        ("PASSWORD", "", True, "keep this file mode 600", None),
        ("FROM", "", True, "sender address (default pituner@<host name>)", None),
        ("TO", "", True, "recipients, separated by commas", None),
        ("SUBJECT_PREFIX", "[Pi-Tuner]", True, "start of every subject", None),
        ("TIMEOUT", "10", True, "seconds to wait on the SMTP server", None),
        ("ALERT_STATION", "true", True, "station down / recovered emails", None),
        ("ALERT_EAS", "true", True, "EAS attention tone emails", None),
        ("ALERT_DISK", "true", True, "low recording disk space emails", None),
        ("ALERT_SERVICE", "true", True, "service started / stopped emails", None),
        ("ALERT_SIGNAL", "true", True, "low signal alarm raised / cleared emails", None),
        ("DOWN_DELAY", "120", True, "seconds a station must stay down before the email", None),
        ("DISK_MIN_GB", "2", True, "free space that triggers the disk email", None),
    ],
    "rssi.conf": [
        ("ALARM", "true", True, "Low Signal alarm on/off", None),
        ("MUTE", "false", True, "mute the Icecast stream while the alarm is raised", None),
        ("THRESHOLD_ON", "12", True, "dB: alarm raises when RSSI stays below this", None),
        ("THRESHOLD_OFF", "18", True, "dB: alarm clears when RSSI stays above this", None),
        ("TIME_ON", "60", True, "seconds below Threshold On before the alarm raises", None),
        ("TIME_OFF", "5", True, "seconds above Threshold Off before the alarm clears", None),
        ("OFFSET", "0", True, "dB added to the reading, to line it up with another meter", None),
    ],
    "station": [
        ("RBDS", "false", False, "fm only: RBDS text and PTY genre to Icecast", "fm"),
        ("RSSI_MONITOR", "true", False, "measure reception every minute (rssi.log, pituner signal)", None),
        ("RECORD", "false", False, "save 15-minute MP3 recordings", None),
        ("RECORD_KEEP_DAYS", "", False, "delete recordings older than this many days", None),
    ],
}

CONFIG_HEADERS = {
    "smtp.conf": "# Pi-Tuner SMTP email alerts. Keep this file mode 600: it holds the password.",
    "zabbix.conf": "# Pi-Tuner Zabbix trapper settings",
    "rssi.conf": ("# Pi-Tuner Low Signal alarm (RSSI). These are the defaults for every station;\n"
                  "# a station file can override any of them with RSSI_ALARM, RSSI_MUTE,\n"
                  "# RSSI_THRESHOLD_ON, RSSI_THRESHOLD_OFF, RSSI_TIME_ON, RSSI_TIME_OFF, RSSI_OFFSET.\n"
                  "# Changes apply within a minute, no restart needed."),
    "icecast.conf": "# Pi-Tuner Icecast connection (shared by all stations)",
}


def _spec_line(key, default, active, comment):
    body = f"{key}={format_conf_value(default)}"
    if comment:
        body += f"  # {comment}"
    return body if active else f"# {body}"


def render_config(name):
    """Text of a brand-new config file with every key at its default."""
    lines = [CONFIG_HEADERS[name], "# ─── user settings ─────────────────────────────────"]
    lines += [_spec_line(k, d, a, c) for k, d, a, c, _ in CONFIG_KEYS[name]]
    lines.append(USER_SETTINGS_END + " ─────────────────────────────")
    return "\n".join(lines) + "\n"


def _has_key(lines, key):
    pat = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=", re.IGNORECASE)
    return any(pat.match(line) for line in lines)


def _missing_specs(path, specs, band=None):
    lines = _read_lines(path)
    return [sp for sp in specs
            if (sp[4] is None or sp[4] == band) and not _has_key(lines, sp[0])]


# config files newer versions create when missing (upgrade-config)
NEW_FILES = ("smtp.conf", "rssi.conf")


def _config_targets(base_dir):
    """(display name, path, spec key, band) for each existing config file."""
    targets = []
    for name in ("icecast.conf", "zabbix.conf", "smtp.conf", "rssi.conf"):
        path = os.path.join(base_dir, name)
        if os.path.isfile(path):
            targets.append((name, path, name, None))
    stations = os.path.join(base_dir, "stations")
    if os.path.isdir(stations):
        for fname in sorted(os.listdir(stations)):
            if fname.endswith(".conf"):
                path = os.path.join(stations, fname)
                band = (parse_keyvalue(path).get("band") or "fm").lower()
                targets.append((f"stations/{fname}", path, "station", band))
    return targets


def missing_config_keys(base_dir):
    """{file: [keys]} that existing config files lack (read-only)."""
    out = {}
    for display, path, spec, band in _config_targets(base_dir):
        missing = [sp[0] for sp in _missing_specs(path, CONFIG_KEYS[spec], band)]
        if missing:
            out[display] = missing
    for name in NEW_FILES:
        if not os.path.isfile(os.path.join(base_dir, name)):
            out[name] = ["(new file)"]
    return out


def upgrade_config(base_dir):
    """Add keys that newer versions understand to existing config files.

    Never changes an existing value. A key (active or commented) that's already
    in the file counts as handled, so this is safe to run any number of times.
    Returns {file: [keys added]}.
    """
    added = {}
    for name in NEW_FILES:
        path = os.path.join(base_dir, name)
        if not os.path.isfile(path) and os.path.isdir(base_dir):
            _atomic_write(path, render_config(name), mode=0o600)
            try:
                st = os.stat(base_dir)
                os.chown(path, st.st_uid, st.st_gid)
            except OSError:
                pass
            added[name] = ["(new file)"]
    for display, path, spec, band in _config_targets(base_dir):
        if display in added:
            continue
        specs = _missing_specs(path, CONFIG_KEYS[spec], band)
        if not specs:
            continue
        lines = _read_lines(path)
        at = _insert_index(lines)
        lines[at:at] = [_spec_line(k, d, a, c) for k, d, a, c, _ in specs]
        _atomic_write(path, "\n".join(lines) + "\n")
        added[display] = [sp[0] for sp in specs]
    return added


def backup_config(base_dir, keep=5):
    """Copy the config files and stations/ to backups/<timestamp>/ (modes kept),
    keeping only the newest ``keep`` backups. Returns the new folder or None."""
    files = [(name, path) for name, path, _, _ in _config_targets(base_dir)]
    if not files:
        return None
    root = os.path.join(base_dir, "backups")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(root, stamp)
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(root, f"{stamp}-{n}")
    for name, path in files:
        target = os.path.join(dest, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(path, target)
    os.chmod(dest, 0o700)
    old = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    for name in old[:-keep]:
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    return dest


def _parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_bool(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_days(value):
    """Whole days to keep recordings; 0 (keep forever) if unset or invalid."""
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _safe_name(text):
    """Strip shell metacharacters so a name is safe inside a quoted ffmpeg arg."""
    return re.sub(r'["\'`$\\;|&<>()]', "", str(text)).strip()


def _safe_mount(text):
    """Restrict a mount path to URL-safe characters and ensure a leading '/'."""
    mount = re.sub(r'[^A-Za-z0-9/._-]', "", str(text))
    if not mount.startswith("/"):
        mount = "/" + mount
    return mount


def _eas_tee(name, sample_rate, channels, eas_dir, tid=""):
    """Return the pipeline fragment that taps audio to the EAS tone detector."""
    tuner_arg = f" --tuner {tid}" if tid else ""
    return (f"tee --output-error=warn >(python3 {TUNER_PATH} detect-eas "
            f'--dir "{eas_dir}"{tuner_arg} "{name}" {sample_rate} {channels})')


def _level_tee(name, sample_rate, channels, base_dir, tid):
    """Return the pipeline fragment that taps audio to the level meter."""
    return (f"tee --output-error=warn >(python3 {TUNER_PATH} level "
            f'--dir "{base_dir}" --tuner {tid} "{name}" {sample_rate} {channels})')


def _rssi_tee(name, band, freq, sample_rate, base_dir, tid, station_conf=""):
    """Return the pipeline fragment that taps mono demodulated signal to the
    reception (RSSI) meter: the 192 kHz MPX for FM, the 25 kHz audio for WX.
    ``station_conf`` (the station file name) lets it read per-station overrides."""
    conf_arg = f' --station-conf "{station_conf}"' if station_conf else ""
    return (f"tee --output-error=warn >(python3 {TUNER_PATH} rssi "
            f'--dir "{base_dir}" --tuner {tid} --band {band} --freq {freq:g}{conf_arg} '
            f'"{name}" {sample_rate})')


def _mute_gate(sample_rate, channels, tid):
    """Return the in-line pipeline fragment that silences the stream while the
    Low Signal alarm's mute is on. Placed after every tap, just before ffmpeg."""
    return f"python3 {TUNER_PATH} mute-gate --tuner {tid} {sample_rate} {channels}"


def _record_tee(name, sample_rate, channels, base_dir):
    """Return the pipeline fragment that taps audio to the 15-minute recorder."""
    return (f"tee --output-error=warn >(python3 {TUNER_PATH} record "
            f'--dir "{base_dir}" "{name}" {sample_rate} {channels})')


def _rbds_tee(mount, base_dir, log_name=None):
    """Return the pipeline fragment that taps the 192 kHz MPX to RBDS decoding.

    ``log_name`` (a station name) also writes the daily RBDS.log."""
    log_arg = f' --log-name "{log_name}"' if log_name else ""
    return (f"tee --output-error=warn >(redsea -u -r 192000 2>/dev/null | "
            f'python3 {TUNER_PATH} rbds-meta --dir "{base_dir}" --tuner {tuner_id(mount)}'
            f'{log_arg} "{mount}")')


# --------------------------------------------------------- device resolution

_RTL_DEVICE_RE = re.compile(r"^\s*(\d+):\s+.*?\bSN:\s*(\S+)", re.IGNORECASE)
_RTL_FOUND_RE = re.compile(r"Found\s+(\d+)\s+device", re.IGNORECASE)


def norm_serial(serial):
    """Lowercase a serial and collapse leading zeros so '1001', '0001001' and
    '00001001' all compare equal."""
    serial = str(serial).strip().lower()
    return str(int(serial)) if serial.isdigit() else serial


def enumerate_devices(timeout=ENUMERATE_SECS):
    """Return a list of (index, serial) from rtl_test's startup enumeration.

    rtl_test prints the device list to stderr and then runs forever (it begins
    tuning), so we read its output until all advertised devices have been seen
    or ``timeout`` passes, then kill it. The read is select()-based with a hard
    deadline: rtl_test can hang silently in libusb when a dongle is pulled, and
    a blocking read here would freeze the whole supervisor (no logs, no Zabbix
    heartbeat, stale "streaming" status).
    """
    try:
        proc = subprocess.Popen(
            ["rtl_test"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as e:
        log(f"cannot run rtl_test: {e}", err=True)
        return []
    devices = []
    buf = b""
    count = None
    fd = proc.stdout.fileno()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.time()))
            if not ready:
                break
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            buf += chunk
            done = False
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                m = _RTL_FOUND_RE.match(line)
                if m:
                    count = int(m.group(1))
                    if count == 0:
                        done = True
                        break
                    continue
                m = _RTL_DEVICE_RE.match(line)
                if m:
                    devices.append((m.group(1), m.group(2)))
                    if count is not None and len(devices) >= count:
                        done = True
                        break
            if done:
                break
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log("rtl_test would not exit after SIGKILL", err=True)
        proc.stdout.close()
    return devices


# ------------------------------------------------------------ audio watchdog
# A dongle can open and tune normally yet never deliver samples (bad USB power
# or port, a failed stick). rtl_fm then sits idle, ffmpeg never connects to
# Icecast and the station looks "streaming" while silent. We watch the bytes
# rtl_fm writes: if none appear for NO_AUDIO_SECS the station goes to the
# "no_audio" state (log, Zabbix, email); the pipeline is retried every
# NO_AUDIO_RESTART_SECS while it stays silent.

NO_AUDIO_SECS = 20
NO_AUDIO_RESTART_SECS = 300
# rtl_fm vanishing from a still-running pipeline (dongle unplugged, rtl_fm
# crashed) for this long means the pipeline is dead: tear it down and restart.
RTL_GONE_SECS = 15
NO_AUDIO_DETAIL = ("rtl_fm is producing no audio: the dongle opened but delivers no "
                   "samples (check its USB port/power, or replace it)")


def rtl_fm_pid(shell_pid, proc_root="/proc"):
    """Pid of the rtl_fm in a station's pipeline, or None if there is none.

    The pipeline runs as ``bash -c 'rtl_fm ... | ...'``, so rtl_fm is a child of
    ``shell_pid`` (or that pid itself)."""
    try:
        names = os.listdir(proc_root)
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"{proc_root}/{name}/stat") as f:
                stat = f.read()
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if comm == "rtl_fm" and (ppid == shell_pid or int(name) == shell_pid):
            return int(name)
    return None


def rtl_fm_bytes_written(shell_pid, proc_root="/proc"):
    """Bytes written so far by the rtl_fm in a station's pipeline, or None.

    None means "can't tell" (no such process yet, /proc/<pid>/io unreadable);
    callers must not treat it as silence.
    """
    pid = rtl_fm_pid(shell_pid, proc_root)
    if pid is None:
        return None
    try:
        with open(f"{proc_root}/{pid}/io") as f:
            for line in f:
                if line.startswith("wchar:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


# ------------------------------------------------------------- station config

def load_stations(stations_dir):
    """Read every *.conf in stations_dir into a list of station dicts."""
    stations = []
    if not os.path.isdir(stations_dir):
        return stations
    for filename in sorted(os.listdir(stations_dir)):
        if not filename.endswith(".conf"):
            continue
        path = os.path.join(stations_dir, filename)
        raw = parse_keyvalue(path)
        if not raw:
            log(f"station file {filename} is empty or unreadable, skipping", err=True)
            continue

        name = raw.get("name") or os.path.splitext(filename)[0]
        band = (raw.get("band") or "fm").lower()
        if band not in ("fm", "wx"):
            log(f"station {name}: unknown BAND '{band}', defaulting to fm", err=True)
            band = "fm"
        freq = _parse_float(raw.get("frequency"))
        if freq is None or freq <= 0:
            log(f"station {name}: missing/invalid FREQUENCY, skipping", err=True)
            continue
        serial = raw.get("serial", "").strip()
        mount = _safe_mount(raw.get("mount") or f"/tuner{len(stations) + 1}")

        stations.append({
            "name": name,
            "band": band,
            "freq": freq,
            "serial": serial,
            "gain": raw.get("gain", "").strip(),
            "mount": mount,
            "rbds": _parse_bool(raw.get("rbds")) and band == "fm",
            "rssi": _parse_bool(raw.get("rssi_monitor", "true")),
            "record": _parse_bool(raw.get("record")),
            "record_keep_days": _parse_days(raw.get("record_keep_days")),
            "conf": filename,
        })
    return stations


def _fm_source(cfg, device_index):
    """rtl_fm command that outputs the 192 kHz FM-demodulated MPX signal."""
    freq_hz = int(cfg["freq"] * 1_000_000)
    gain = f"-g {cfg['gain']} " if cfg.get("gain") else ""
    return (f"rtl_fm -d {device_index} -M fm -l 0 -A std -p 0 -s 192000 {gain}"
            f"-F 9 -f {freq_hz}")


def _genre_for(cfg, genre=""):
    """Icecast genre: Weather for WX; for FM the RBDS PTY, else Radio."""
    if cfg["band"] == "wx":
        return WX_GENRE
    return _safe_name(genre) or FM_DEFAULT_GENRE


def build_command(cfg, ice, device_index, eas=False, eas_dir=DEFAULT_DIR,
                  rbds=False, genre="", record=True, level=False, rssi=False):
    """Assemble the shell pipeline for one station.

    ``genre`` is the Icecast genre for FM (the RBDS PTY found at startup);
    FM falls back to "Radio" when there is none. WX is always "Weather".
    The recorder tap is added when ``record`` and the station's RECORD are set;
    the level-meter tap when ``level`` (Zabbix audio level) is. The reception
    (RSSI) tap is added when ``rssi`` and the station's RSSI_MONITOR are set.
    """
    tid = tuner_id(cfg["mount"])
    record = record and bool(cfg.get("record"))
    rssi = rssi and cfg.get("rssi", True)
    freq_hz = int(cfg["freq"] * 1_000_000)
    name = _safe_name(cfg["name"])
    genre = _genre_for(cfg, genre)
    ice_url = (f"icecast://source:{ice['password']}@{ice['host']}:"
               f"{ice['port']}{cfg['mount']}")
    ffmpeg = ("-nostdin -loglevel warning -acodec libmp3lame -b:a 128k -f mp3 "
              f'-ice_name "{name}" '
              f'-ice_genre "{genre}" '
              f"-content_type audio/mpeg {ice_url}")

    if cfg["band"] == "wx":
        cmd = f"rtl_fm -d {device_index} -f {freq_hz} -s 25000 -E deemp -F 9"
        if rssi:
            cmd += f" | {_rssi_tee(name, 'wx', cfg['freq'], 25000, eas_dir, tid, cfg.get('conf', ''))}"
        if record:
            cmd += f" | {_record_tee(name, 25000, 1, eas_dir)}"
        if level:
            cmd += f" | {_level_tee(name, 25000, 1, eas_dir, tid)}"
        if eas:
            cmd += f" | {_eas_tee(name, 25000, 1, eas_dir, tid)}"
        if rssi:
            cmd += f" | {_mute_gate(25000, 1, tid)}"
        return cmd + f" | ffmpeg -f s16le -ar 25000 -ac 1 -i pipe:0 {ffmpeg}"

    cmd = _fm_source(cfg, device_index)
    if rbds and cfg.get("rbds"):
        log_name = name if record else None
        cmd += f" | {_rbds_tee(cfg['mount'], eas_dir, log_name)}"
    if rssi:
        cmd += f" | {_rssi_tee(name, 'fm', cfg['freq'], 192000, eas_dir, tid, cfg.get('conf', ''))}"
    cmd += " | demux -r 192000 -R 48000 -d 75"
    if record:
        cmd += f" | {_record_tee(name, 48000, 2, eas_dir)}"
    if level:
        cmd += f" | {_level_tee(name, 48000, 2, eas_dir, tid)}"
    if eas:
        cmd += f" | {_eas_tee(name, 48000, 2, eas_dir, tid)}"
    if rssi:
        cmd += f" | {_mute_gate(48000, 2, tid)}"
    return cmd + f" | ffmpeg -f s16le -ar 48000 -ac 2 -i pipe:0 {ffmpeg}"


# ----------------------------------------------------------------- zabbix

def _recv_exact(sock, nbytes):
    buf = b""
    while len(buf) < nbytes:
        chunk = sock.recv(nbytes - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def tuner_id(mount):
    """Safe per-tuner id for Zabbix item keys, derived from the mount
    ('/tuner3' -> 'tuner3'). Mounts only contain [A-Za-z0-9/._-]."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(mount).strip("/")) or "tuner"


# metric -> (value kind, description). One source for the sender, the Zabbix
# template and the tests. Kinds: char, text, uint, float.
TUNER_METRICS = {
    "state": ("char", "Pipeline state: streaming, down, serial_not_found, no_audio or stopped"),
    "up": ("uint", "1 while the tuner is streaming, otherwise 0"),
    "name": ("char", "Station name from the station file"),
    "band": ("char", "fm or wx"),
    "frequency": ("float", "Tuned frequency in MHz"),
    "mount": ("char", "Icecast mount point"),
    "serial": ("char", "Dongle serial number"),
    "genre": ("char", "Icecast genre: the RBDS program type, Radio, or Weather"),
    "restarts": ("uint", "Times the pipeline has been restarted since the service started"),
    "recording": ("uint", "1 if this tuner records to disk (RECORD=true)"),
    "recording.age": ("float", "Seconds since the newest recording file was written, -1 if none"),
    "rbds.rt": ("text", "Current RBDS RadioText"),
    "rbds.ps": ("char", "Current RBDS station name (PS), or the callsign for a scrolling PS"),
    "eas": ("uint", "1 for about 10 seconds when the EAS attention tone is heard"),
    "level": ("float", "Audio level in dBFS (RMS), averaged over 10 seconds"),
    "rssi": ("float", "Reception level in dB (relative signal-to-noise estimate), averaged over 1 minute"),
    "rssi.alarm": ("uint", "1 while the Low Signal alarm is raised (RSSI below Threshold On for Time On)"),
}
LEVEL_INTERVAL_SECS = 10
RSSI_INTERVAL_SECS = 60
RSSI_GOOD_DB = 25.0
RSSI_FAIR_DB = 12.0


def newest_recording_age(base_dir, name, now):
    """Seconds since the newest .mp3 for a station was written, or -1.0.

    Only today's and yesterday's day folders are looked at, so it stays cheap."""
    newest = None
    for days_back in (0, 1):
        t = datetime.datetime.fromtimestamp(now) - datetime.timedelta(days=days_back)
        folder = os.path.join(recording_dir(base_dir, name), t.strftime("%Y"),
                              t.strftime("%m"), t.strftime("%d"))
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    if entry.name.endswith(".mp3"):
                        mtime = entry.stat().st_mtime
                        newest = mtime if newest is None else max(newest, mtime)
        except OSError:
            continue
        if newest is not None:
            break
    return round(now - newest, 1) if newest is not None else -1.0


class ZabbixSender:
    """Minimal Zabbix trapper (zabbix_sender protocol) client, stdlib only.

    Fire-and-forget: a failed or timed-out send is logged and otherwise ignored
    so Zabbix being unreachable can never block or crash tuning.
    """

    key_discovery = "pituner.tuners.discovery"

    def __init__(self, conf):
        self.enabled = str(conf.get("enabled", "false")).lower() in ("1", "true", "yes", "on")
        self.server = conf.get("server", "")
        try:
            self.port = int(conf.get("port", "10051"))
        except ValueError:
            self.port = 10051
        self.hostname = conf.get("hostname", "") or socket.gethostname()
        self.key_event = conf.get("key_event", "pituner.event")
        self.key_active = conf.get("key_active", "pituner.stations_active")
        self.key_heartbeat = conf.get("key_heartbeat", "pituner.heartbeat")
        self.key_eas = conf.get("key_eas", "pituner.eas")
        try:
            self.interval = int(conf.get("interval", "60"))
        except ValueError:
            self.interval = 60
        self.interval = max(10, self.interval)
        self.level_monitor = _conf_flag(conf, "level_monitor", True)
        self.rssi_monitor = _conf_flag(conf, "rssi_monitor", True)
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=200)
        self._worker = None

    def _available(self):
        return self.enabled and bool(self.server)

    @staticmethod
    def tuner_key(metric, tid):
        return f"pituner.tuner.{metric}[{tid}]"

    def send(self, items, mirror=True):
        """items: iterable of (key, value) pairs.

        When mirror is True the payload is also appended to zabbix.log so a
        local copy of everything sent to Zabbix (events only -- the periodic
        heartbeat snapshot passes mirror=False) is kept on disk.
        """
        items = list(items)
        if mirror:
            log_file("zabbix.log", json.dumps(dict(items)))
        if not self._available():
            return
        data = [{"host": self.hostname, "key": key, "value": str(value)}
                for key, value in items]
        payload = json.dumps({"request": "sender data", "data": data}).encode("utf-8")
        header = b"ZBXD\x01" + struct.pack("<Q", len(payload))
        try:
            with self._lock:
                with socket.create_connection((self.server, self.port), timeout=5) as s:
                    s.settimeout(5)
                    s.sendall(header + payload)
                    resp = _recv_exact(s, 13)
                    if len(resp) == 13 and resp[:5] == b"ZBXD\x01":
                        (dlen,) = struct.unpack("<Q", resp[5:13])
                        _recv_exact(s, dlen)
        except Exception as e:  # noqa: BLE001 - best-effort send, never fatal
            log(f"zabbix send failed: {e}", err=True)

    def _run(self):
        while True:
            items, mirror = self._queue.get()
            try:
                self.send(items, mirror)
            finally:
                self._queue.task_done()

    def send_async(self, items, mirror=False):
        """Queue a send on a background thread. For helpers whose stdin feeds an
        audio pipeline: a slow Zabbix server must never stall them."""
        if not self._available():
            return
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, daemon=True)
                self._worker.start()
        try:
            self._queue.put_nowait((list(items), mirror))
        except queue.Full:
            pass

    def flush(self, timeout=5.0):
        deadline = time.time() + timeout
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.1)

    def event(self, message):
        self.send([(self.key_event, message)])

    # -- per-tuner items --------------------------------------------------

    def state_items(self, info):
        tid = info["id"]
        return [(self.tuner_key("state", tid), info["state"]),
                (self.tuner_key("up", tid), 1 if info["state"] == "streaming" else 0)]

    def tuner_items(self, info):
        """Every per-tuner value the supervisor knows (RBDS text, level and EAS
        come from the helper processes)."""
        tid = info["id"]
        key = self.tuner_key
        return self.state_items(info) + [
            (key("name", tid), info["name"]),
            (key("band", tid), info["band"]),
            (key("frequency", tid), info["frequency"]),
            (key("mount", tid), info["mount"]),
            (key("serial", tid), info["serial"]),
            (key("genre", tid), info["genre"]),
            (key("restarts", tid), info["restarts"]),
            (key("recording", tid), 1 if info["recording"] else 0),
            (key("recording.age", tid), info["recording_age"]),
        ]

    def discovery(self, infos):
        data = [{"{#TUNER}": i["id"], "{#NAME}": i["name"], "{#BAND}": i["band"],
                 "{#FREQ}": str(i["frequency"]), "{#MOUNT}": i["mount"]} for i in infos]
        return (self.key_discovery, json.dumps({"data": data}))

    def snapshot(self, infos):
        """Discovery list, every tuner's values, and the global heartbeat items,
        in one request."""
        items = [self.discovery(infos)]
        for info in infos:
            items += self.tuner_items(info)
        items += [
            (self.key_active, sum(1 for i in infos if i["state"] == "streaming")),
            (self.key_heartbeat, 1),
        ]
        self.send(items, mirror=False)


# ------------------------------------------------------------- email alerts

def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _conf_flag(conf, key, default):
    value = conf.get(key)
    return default if value is None else _parse_bool(value)


def _conf_number(conf, key, default, cast=int):
    try:
        return cast(conf.get(key, default))
    except (TypeError, ValueError):
        return default


class Mailer:
    """SMTP client for alert emails, stdlib only.

    ``send`` queues the message for a background worker thread, so a slow or
    dead mail server can never stall the supervisor loop or an audio pipeline.
    Failures are logged to email.log (never the password) and otherwise
    ignored. ``send_sync`` delivers immediately and raises, for test-email.
    """

    ATTEMPT_DELAYS = (2.0, 5.0)  # waits between the 3 delivery attempts

    def __init__(self, conf):
        self.flag = _parse_bool(conf.get("enabled"))
        self.host = conf.get("host", "").strip()
        self.port = _conf_number(conf, "port", 587)
        security = conf.get("security", "starttls").strip().lower()
        self.security = security if security in ("starttls", "ssl", "none") else "starttls"
        self.verify_tls = _conf_flag(conf, "verify_tls", True)
        self.username = conf.get("username", "")
        self.password = conf.get("password", "")
        self.hostname = socket.gethostname()
        self.from_addr = conf.get("from", "").strip() or f"pituner@{self.hostname}"
        self.to = [a for a in re.split(r"[,;\s]+", conf.get("to", "")) if a]
        self.prefix = conf.get("subject_prefix", "[Pi-Tuner]").strip()
        self.timeout = max(1, _conf_number(conf, "timeout", 10))
        self.alert_station = _conf_flag(conf, "alert_station", True)
        self.alert_eas = _conf_flag(conf, "alert_eas", True)
        self.alert_disk = _conf_flag(conf, "alert_disk", True)
        self.alert_service = _conf_flag(conf, "alert_service", True)
        self.alert_signal = _conf_flag(conf, "alert_signal", True)
        self.down_delay = max(0, _conf_number(conf, "down_delay", 120))
        self.disk_min_gb = max(0.0, _conf_number(conf, "disk_min_gb", 2.0, float))
        self._queue = queue.Queue(maxsize=50)
        self._worker = None
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return self.flag and bool(self.host) and bool(self.to)

    def problem(self):
        """Why email isn't active, or "" if it is."""
        if not self.flag:
            return "ENABLED is not true"
        if not self.host:
            return "HOST is empty"
        if not self.to:
            return "TO has no recipients"
        return ""

    def build(self, subject, body):
        msg = email.message.EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to)
        msg["Subject"] = f"{self.prefix} {subject} ({self.hostname})".strip()
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=self.hostname)
        msg.set_content(body.rstrip() + "\n")
        return msg

    def _deliver(self, msg):
        context = ssl.create_default_context()
        if not self.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if self.security == "ssl":
            smtp = smtplib.SMTP_SSL(self.host, self.port, local_hostname=self.hostname,
                                    timeout=self.timeout, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, local_hostname=self.hostname,
                                timeout=self.timeout)
        with smtp:
            smtp.ehlo()
            if self.security == "starttls":
                smtp.starttls(context=context)
                smtp.ehlo()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(msg)

    def send_sync(self, subject, body):
        """Deliver now; raises on failure."""
        self._deliver(self.build(subject, body))

    def _run(self):
        while True:
            msg = self._queue.get()
            subject = msg["Subject"]
            try:
                for attempt in range(len(self.ATTEMPT_DELAYS) + 1):
                    try:
                        self._deliver(msg)
                        log_file("email.log", f"sent: {subject}")
                        break
                    except Exception as e:  # noqa: BLE001 - alerts are best effort
                        last = f"{type(e).__name__}: {e}"
                        if attempt < len(self.ATTEMPT_DELAYS):
                            time.sleep(self.ATTEMPT_DELAYS[attempt])
                        else:
                            log_file("email.log", f"FAILED ({last}): {subject}")
                            log(f"email send failed: {last}", err=True)
            finally:
                self._queue.task_done()

    def send(self, subject, body):
        """Queue an email without blocking. No-op when email isn't enabled."""
        if not self.enabled:
            return
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, daemon=True)
                self._worker.start()
        try:
            self._queue.put_nowait(self.build(subject, body))
        except queue.Full:
            log_file("email.log", f"queue full, dropped: {subject}")

    def flush(self, timeout=10.0):
        """Wait up to ``timeout`` seconds for queued emails to go out."""
        deadline = time.time() + timeout
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.1)


class AlertManager:
    """Decides when to email about stations, disk space and the service.

    A station that goes down only triggers an email once it has stayed down for
    the mailer's down_delay; a recovery email follows only if the down email was
    sent, so brief self-healing blips send nothing.
    """

    DISK_CHECK_SECS = 300
    DISK_RECOVER_FACTOR = 1.25

    def __init__(self, mailer=None, disk_usage=shutil.disk_usage):
        self.mailer = mailer or Mailer({})
        self.disk_usage = disk_usage
        self.stations = {}   # name -> {"status", "detail", "since", "alerted"}
        self.disk_low = False
        self._next_disk = 0.0

    def sync_stations(self, names):
        """Forget stations that no longer exist after a config reload."""
        for name in list(self.stations):
            if name not in names:
                del self.stations[name]

    def station_status(self, name, status, detail, now):
        if not (self.mailer.enabled and self.mailer.alert_station):
            return
        rec = self.stations.get(name)
        if status == "streaming":
            if rec is not None and rec["alerted"]:
                self.mailer.send(
                    f"{name} is back up",
                    f"Station {name} is streaming again.\n\n"
                    f"It was down for {_fmt_duration(now - rec['since'])}.\n"
                    f"Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}")
            self.stations.pop(name, None)
        elif status in ("down", "serial_not_found", "no_audio"):
            if rec is None:
                self.stations[name] = {"status": status, "detail": detail,
                                       "since": now, "alerted": False}
            else:
                rec["status"], rec["detail"] = status, detail

    def tick(self, now):
        """Send the delayed 'down' emails that are due."""
        if not (self.mailer.enabled and self.mailer.alert_station):
            return
        for name, rec in self.stations.items():
            if not rec["alerted"] and now - rec["since"] >= self.mailer.down_delay:
                rec["alerted"] = True
                what = {"serial_not_found": "its dongle serial was not found",
                        "no_audio": "its dongle is not delivering any audio"
                        }.get(rec["status"], "its stream stopped")
                self.mailer.send(
                    f"{name} is DOWN",
                    f"Station {name} has been down for {_fmt_duration(now - rec['since'])}: "
                    f"{what}.\n"
                    + (f"Detail: {rec['detail']}\n" if rec["detail"] else "")
                    + f"Down since: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(rec['since']))}\n\n"
                    "Check the log with: journalctl -u pituner -n 50")

    def check_disk(self, now, path, recording):
        """Email when free space under ``path`` drops below the threshold."""
        m = self.mailer
        if not (m.enabled and m.alert_disk and recording) or now < self._next_disk:
            return
        self._next_disk = now + self.DISK_CHECK_SECS
        try:
            free_gb = self.disk_usage(path).free / 1024 ** 3
        except OSError:
            return
        if not self.disk_low and free_gb < m.disk_min_gb:
            self.disk_low = True
            m.send("Low disk space",
                   f"Only {free_gb:.1f} GB is free where Pi-Tuner stores recordings ({path}).\n"
                   f"The alert level is {m.disk_min_gb:g} GB. Recording will fail when the "
                   "disk is full; delete old recordings or set RECORD_KEEP_DAYS in a station file.")
        elif self.disk_low and free_gb >= m.disk_min_gb * self.DISK_RECOVER_FACTOR:
            self.disk_low = False
            m.send("Disk space recovered",
                   f"{free_gb:.1f} GB is free again where Pi-Tuner stores recordings ({path}).")

    def service_started(self, statuses):
        m = self.mailer
        if m.enabled and m.alert_service:
            lines = "\n".join(f"  {n}: {s}" for n, s in statuses.items()) or "  (no stations configured)"
            m.send("Pi-Tuner started", f"Pi-Tuner started with {len(statuses)} station(s):\n{lines}")

    def service_stopped(self):
        m = self.mailer
        if m.enabled and m.alert_service:
            m.send("Pi-Tuner stopped", "Pi-Tuner is shutting down (service stop, restart or reboot).")
            m.flush(6.0)


# ----------------------------------------------------------- audio level meter

def dbfs_from_sumsq(sumsq, count):
    """RMS level in dBFS from a sum of squared 16-bit samples (a full-scale sine
    is about -3 dBFS); floor -90."""
    if count <= 0 or sumsq <= 0:
        return -90.0
    return round(max(-90.0, 20 * math.log10(math.sqrt(sumsq / count) / 32768.0)), 1)


def run_level_meter(base_dir, tid, name, sample_rate, channels, stream=None,
                    interval=LEVEL_INTERVAL_SECS, zbx=None, stride=4):
    """Read s16le PCM from stdin and send the audio level to Zabbix.

    One value per ``interval`` seconds of audio: the RMS level (dBFS) over that
    window, measured on every ``stride``-th sample to keep the CPU cost small.
    Sends through the background queue, so a slow Zabbix server can't stall the
    audio pipeline this taps. Exits cleanly on stdin EOF.
    """
    stream = stream if stream is not None else sys.stdin.buffer
    zbx = zbx or ZabbixSender(parse_keyvalue(os.path.join(base_dir, "zabbix.conf")))
    key = zbx.tuner_key("level", tid)
    chunk = int(sample_rate * 0.5) * 2 * channels
    window = int(sample_rate * channels * interval)   # samples per value
    sumsq = count = seen = 0
    while True:
        raw = stream.read(chunk)
        if not raw:
            break
        raw = raw[: len(raw) // 2 * 2]
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder == "big":
            samples.byteswap()
        picked = samples[::stride]
        sumsq += sum(x * x for x in picked)
        count += len(picked)
        seen += len(samples)
        if seen >= window:
            zbx.send_async([(key, dbfs_from_sumsq(sumsq, count))])
            sumsq = count = seen = 0
    zbx.flush(5.0)



# ------------------------------------------------------ reception level (RSSI)
# rtl_fm gives no RF signal strength, so reception is estimated from the
# demodulated signal. FM: the 19 kHz stereo pilot against the noise in empty
# MPX bins (a weak signal raises the noise floor and buries the pilot). WX has
# no pilot, so voice-band energy is compared with high-band hiss. The figure is
# a relative dB value: good for trending and alerting, not calibrated dBm.
# A mono-only FM station has no pilot and reads low whatever its signal.

RSSI_BANDS = {
    # band: (sample rate, signal bins in Hz, noise bins in Hz)
    "fm": (192000, (19000.0,), (70000.0, 75000.0, 80000.0, 85000.0)),
    "wx": (25000, (500.0, 1000.0, 1500.0, 2000.0, 2500.0), (8000.0, 9000.0, 10000.0, 11000.0)),
}
RSSI_BLOCK_SECS = 0.05
RSSI_MAX_DB = 60.0


def rssi_rating(rssi):
    if rssi >= RSSI_GOOD_DB:
        return "good"
    return "fair" if rssi >= RSSI_FAIR_DB else "poor"


def rssi_state_path(tid):
    return os.path.join(LOG_DIR, f"rssi-{tid}.json")


def rssi_block_powers(samples, band, sample_rate=None):
    """(signal power, noise power) of one block, mean over the band's bins."""
    rate, sig, noise = RSSI_BANDS[band]
    rate = sample_rate or rate
    n = len(samples)
    sp = sum(_goertzel(samples, f, rate) for f in sig) / len(sig) / n
    npow = sum(_goertzel(samples, f, rate) for f in noise) / len(noise) / n
    return sp, npow


def rssi_from_powers(sig, noise):
    """Signal-to-noise ratio in dB from summed powers, clamped to 0..RSSI_MAX_DB."""
    if sig <= 0:
        return 0.0
    if noise <= 0:
        return RSSI_MAX_DB
    return round(min(RSSI_MAX_DB, max(0.0, 10 * math.log10(sig / noise))), 1)


RSSI_ALARM_SMOOTH_SECS = 5
RSSI_SETTINGS_RELOAD_SECS = 60
RSSI_MUTE_STALE_SECS = 5.0
RSSI_DEFAULTS = {"alarm": True, "mute": False, "threshold_on": 12.0,
                 "threshold_off": 18.0, "time_on": 60, "time_off": 5, "offset": 0.0}


def rssi_settings(glob, station=None):
    """Effective Low Signal alarm settings: ``rssi.conf`` values, overridden by
    a station file's RSSI_* keys. Invalid values fall back to the defaults, and
    Threshold Off is never allowed below Threshold On."""
    station = station or {}
    merged = {}
    for key in RSSI_DEFAULTS:
        value = station.get("rssi_" + key)
        if value is None or str(value).strip() == "":
            value = glob.get(key)
        if value is not None:
            merged[key] = value
    d = RSSI_DEFAULTS
    out = {
        "alarm": _conf_flag(merged, "alarm", d["alarm"]),
        "mute": _conf_flag(merged, "mute", d["mute"]),
        "threshold_on": _conf_number(merged, "threshold_on", d["threshold_on"], float),
        "threshold_off": _conf_number(merged, "threshold_off", d["threshold_off"], float),
        "time_on": max(0, _conf_number(merged, "time_on", d["time_on"])),
        "time_off": max(0, _conf_number(merged, "time_off", d["time_off"])),
        "offset": _conf_number(merged, "offset", d["offset"], float),
    }
    out["threshold_off"] = max(out["threshold_on"], out["threshold_off"])
    return out


def load_rssi_settings(base_dir, station_conf=""):
    """Read rssi.conf and the station file (if named) and merge them."""
    station = {}
    if station_conf:
        station = parse_keyvalue(os.path.join(base_dir, "stations", os.path.basename(station_conf)))
    return rssi_settings(parse_keyvalue(os.path.join(base_dir, "rssi.conf")), station)


class RssiAlarm:
    """Low Signal alarm with hysteresis, like an Inovonics 541.

    Raises once the level has stayed below Threshold On for Time On seconds;
    clears once it has stayed above Threshold Off for Time Off seconds. A level
    between the two thresholds holds the current state.
    """

    def __init__(self):
        self.active = False
        self._since = None

    def feed(self, value, now, settings, step=1):
        """Feed one reading covering the ``step`` seconds up to ``now``; returns
        "raised", "cleared" or None."""
        if not settings["alarm"]:
            was, self.active, self._since = self.active, False, None
            return "cleared" if was else None
        if not self.active:
            if value < settings["threshold_on"]:
                if self._since is None:
                    self._since = now - step
                if now - self._since >= settings["time_on"]:
                    self.active, self._since = True, None
                    return "raised"
            else:
                self._since = None
        else:
            if value > settings["threshold_off"]:
                if self._since is None:
                    self._since = now - step
                if now - self._since >= settings["time_off"]:
                    self.active, self._since = False, None
                    return "cleared"
            else:
                self._since = None
        return None


def mute_flag_path(tid):
    return os.path.join(LOG_DIR, f"mute-{tid}")


def _set_mute_flag(tid, muted):
    """Refresh (muted) or remove the flag file the mute gate watches."""
    path = mute_flag_path(tid)
    try:
        if muted:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(path, "w"):
                pass
            os.utime(path, None)
        elif os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def run_mute_gate(tid, sample_rate, channels, src=None, dst=None, flag=None, now=time.time):
    """Pass PCM from stdin to stdout, replacing it with silence while the mute
    flag file is fresh. The RSSI meter refreshes the flag every second while the
    alarm is muting; a stale flag (meter died) counts as not muted, so a failure
    can never leave a station silent."""
    src = src if src is not None else sys.stdin.buffer
    dst = dst if dst is not None else sys.stdout.buffer
    flag = flag or mute_flag_path(tid)
    chunk = max(2, int(sample_rate * channels * 2 * 0.1))
    read = getattr(src, "read1", src.read)
    while True:
        data = read(chunk)
        if not data:
            break
        try:
            muted = now() - os.stat(flag).st_mtime < RSSI_MUTE_STALE_SECS
        except OSError:
            muted = False
        try:
            dst.write(bytes(len(data)) if muted else data)
            dst.flush()
        except (BrokenPipeError, ValueError):
            break


def run_rssi_meter(base_dir, tid, name, band, freq, sample_rate, stream=None,
                   interval=None, zbx=None, station_conf="", mailer=None):
    """Read mono s16le PCM from stdin; once per ``interval`` seconds of signal,
    log the reception level to rssi.log, update the state file ``pituner
    signal`` reads, and send it to Zabbix. Also runs the Low Signal alarm.

    Each second of signal contributes one short block (RSSI_BLOCK_SECS), which
    keeps the pure-Python CPU cost small. The alarm watches a short rolling mean
    of those one-second values; the logged value is the average over the whole
    interval. Time is counted in seconds of signal read. Settings in rssi.conf
    and the station file are re-read every minute. Exits cleanly on stdin EOF.
    """
    stream = stream if stream is not None else sys.stdin.buffer
    interval = interval or RSSI_INTERVAL_SECS
    zbx = zbx or ZabbixSender(parse_keyvalue(os.path.join(base_dir, "zabbix.conf")))
    if mailer is None:
        mailer = Mailer(parse_keyvalue(os.path.join(base_dir, "smtp.conf")))
    key = zbx.tuner_key("rssi", tid)
    alarm_key = zbx.tuner_key("rssi.alarm", tid)
    settings = load_rssi_settings(base_dir, station_conf)
    alarm = RssiAlarm()
    recent = collections.deque(maxlen=RSSI_ALARM_SMOOTH_SECS)
    second = int(sample_rate) * 2
    block = int(sample_rate * RSSI_BLOCK_SECS)
    sig = noise = 0.0
    seconds = elapsed = 0
    last = None
    _set_mute_flag(tid, False)

    def write_state(value):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            _atomic_write(rssi_state_path(tid), json.dumps({
                "tuner": tid, "name": name, "band": band, "freq": freq,
                "rssi": value, "rating": rssi_rating(value),
                "alarm": alarm.active, "muted": alarm.active and settings["mute"],
                "alarm_enabled": settings["alarm"],
                "threshold_on": settings["threshold_on"], "threshold_off": settings["threshold_off"],
                "time_on": settings["time_on"], "time_off": settings["time_off"],
                "offset": settings["offset"], "updated": time.time()}), mode=0o644)
        except OSError:
            pass

    def announce(event, value):
        if event == "raised":
            text = (f"LOW SIGNAL alarm RAISED: RSSI {value:.1f} dB stayed below "
                    f"{settings['threshold_on']:g} dB for {settings['time_on']}s"
                    f"{' (stream muted)' if settings['mute'] else ''}")
        else:
            text = f"LOW SIGNAL alarm CLEARED: RSSI {value:.1f} dB"
        log_file("rssi.log", f'{tid} "{name}" {band} {freq:g} MHz {text}')
        if zbx.rssi_monitor:
            zbx.send_async([(alarm_key, 1 if alarm.active else 0),
                            (zbx.key_event, f"{name}: {text}")])
        if mailer.alert_signal:
            mailer.send(f"Low signal on {name}: {event}",
                        f"{text}\n\nStation: {name} ({tid}, {freq:g} MHz {band.upper()})\n"
                        f"Time on / off: {settings['time_on']}s / {settings['time_off']}s; "
                        f"thresholds on / off: {settings['threshold_on']:g} / "
                        f"{settings['threshold_off']:g} dB.")

    try:
        while True:
            raw = stream.read(second)
            if len(raw) < second:
                break
            samples = array.array("h")
            samples.frombytes(raw[: block * 2])
            if sys.byteorder == "big":
                samples.byteswap()
            s, n = rssi_block_powers(samples, band, sample_rate)
            sig += s
            noise += n
            seconds += 1
            elapsed += 1
            now_value = rssi_from_powers(s, n) + settings["offset"]
            recent.append(now_value)
            last = round(sum(recent) / len(recent), 1)
            event = alarm.feed(last, elapsed, settings)
            if event:
                announce(event, last)
                write_state(last)
            _set_mute_flag(tid, alarm.active and settings["mute"])
            if seconds >= interval:
                value = round(rssi_from_powers(sig, noise) + settings["offset"], 1)
                rating = rssi_rating(value)
                log_file("rssi.log", f'{tid} "{name}" {band} {freq:g} MHz '
                                     f"rssi={value:.1f} dB ({rating})"
                                     f"{' LOW SIGNAL' if alarm.active else ''}")
                write_state(value)
                if zbx.rssi_monitor:
                    zbx.send_async([(key, value), (alarm_key, 1 if alarm.active else 0)])
                sig = noise = 0.0
                seconds = 0
                settings = load_rssi_settings(base_dir, station_conf)
    finally:
        _set_mute_flag(tid, False)
    zbx.flush(5.0)
    mailer.flush(10.0)


def read_rssi_states(stations):
    """Latest reading per station (None if never measured) for ``pituner signal``."""
    rows = []
    for st in stations:
        tid = tuner_id(st["mount"])
        try:
            with open(rssi_state_path(tid), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        rows.append((tid, st, data))
    return rows


# ------------------------------------------------- EAS attention-tone detection
# The EAS/SAME attention signal is a simultaneous 853 Hz + 960 Hz dual-tone.
# We detect it with a Goertzel filter over short windows and pulse a Zabbix
# item (1 for EAS_HOLD_SECS, then back to 0) so a trigger on last()=1 fires
# and then auto-recovers.

EAS_FREQ1 = 853.0
EAS_FREQ2 = 960.0
EAS_WINDOW_SECS = 0.25
EAS_CONFIRM_WINDOWS = 2
EAS_HOLD_SECS = 10
EAS_COOLDOWN_SECS = 60
# Each tone must carry at least this fraction of the window's total energy to
# count. A clean 853+960 dual-tone puts ~0.25 into each tone; noise/music is
# far lower. Raise it to reduce false positives, lower it if detection is
# missed on weak signals.
EAS_TONE_RATIO = 0.10


def _goertzel(samples, freq, sample_rate):
    """Return the raw power of `freq` present in `samples`."""
    n = len(samples)
    k = int(round(n * freq / sample_rate))
    if k <= 0 or k >= n:
        return 0.0
    coeff = 2.0 * math.cos(2.0 * math.pi * k / n)
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2


def _eas_tone_present(samples, sample_rate):
    total = sum(float(x) * x for x in samples)
    if total <= 0:
        return False
    n = len(samples)
    p1 = _goertzel(samples, EAS_FREQ1, sample_rate) / (n * total)
    p2 = _goertzel(samples, EAS_FREQ2, sample_rate) / (n * total)
    return p1 > EAS_TONE_RATIO and p2 > EAS_TONE_RATIO


# ---------------------------------------------------- SAME header decoding
# The EAS attention tone is preceded by the SAME header: three bursts of 520.83
# baud AFSK (mark 2083.33 Hz = 1, space 1562.5 Hz = 0, LSB first, no framing)
# carrying e.g. ZCZC-WXR-TOR-022071+0030-2891403-KLOX/NWS-. We demodulate it
# in pure Python at ~8 kHz: boxcar decimate, correlate one bit period against
# each tone, and sample the decision at several bit phases at once so no clock
# recovery is needed. A header is accepted only if it parses cleanly; the most
# common copy among the bursts heard wins, which repairs most bit errors.

SAME_BAUD = 520.83
SAME_MARK = 2083.33
SAME_SPACE = 1562.5
SAME_TARGET_RATE = 8000.0
SAME_PHASES = 8
SAME_COLLECT_SECS = 5.0
SAME_DUP_SECS = 120.0
SAME_MAX_LEN = 280
SAME_HEADER_RE = re.compile(
    r"^ZCZC-([A-Z]{3})-([A-Z0-9]{3})((?:-\d{6}){1,31})\+(\d{4})-(\d{3})(\d{2})(\d{2})-([A-Za-z0-9/ ]{1,8})-$")
_ZCZC = int.from_bytes(b"ZCZC", "little")
_NNNN = int.from_bytes(b"NNNN", "little")
_SAME_CHARS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-/ ")

SAME_ORIGINATORS = {
    "EAS": "an EAS participant (broadcaster / cable)",
    "CIV": "civil authorities",
    "WXR": "the National Weather Service",
    "PEP": "the Primary Entry Point system (national)",
}

SAME_EVENTS = {
    "EAN": "Emergency Action Notification", "NPT": "National Periodic Test",
    "EAT": "Emergency Action Termination", "RMT": "Required Monthly Test",
    "RWT": "Required Weekly Test", "ADR": "Administrative Message",
    "AVA": "Avalanche Watch", "AVW": "Avalanche Warning", "BZW": "Blizzard Warning",
    "CAE": "Child Abduction Emergency", "CDW": "Civil Danger Warning",
    "CEM": "Civil Emergency Message", "CFA": "Coastal Flood Watch",
    "CFW": "Coastal Flood Warning", "DSW": "Dust Storm Warning",
    "EQW": "Earthquake Warning", "EVI": "Evacuation Immediate",
    "EWW": "Extreme Wind Warning", "FRW": "Fire Warning",
    "FFA": "Flash Flood Watch", "FFS": "Flash Flood Statement",
    "FFW": "Flash Flood Warning", "FLA": "Flood Watch", "FLS": "Flood Statement",
    "FLW": "Flood Warning", "FSW": "Flash Freeze Warning", "FZW": "Freeze Warning",
    "HMW": "Hazardous Materials Warning", "HWA": "High Wind Watch",
    "HWW": "High Wind Warning", "HUA": "Hurricane Watch", "HUW": "Hurricane Warning",
    "HLS": "Hurricane Statement", "LEW": "Law Enforcement Warning",
    "LAE": "Local Area Emergency", "NMN": "Network Message Notification",
    "TOE": "911 Telephone Outage Emergency", "NUW": "Nuclear Power Plant Warning",
    "DMO": "Practice / Demo Warning", "RHW": "Radiological Hazard Warning",
    "SVA": "Severe Thunderstorm Watch", "SVR": "Severe Thunderstorm Warning",
    "SVS": "Severe Weather Statement", "SPW": "Shelter in Place Warning",
    "SMW": "Special Marine Warning", "SPS": "Special Weather Statement",
    "SQW": "Snow Squall Warning", "SSA": "Storm Surge Watch",
    "SSW": "Storm Surge Warning", "TOA": "Tornado Watch", "TOR": "Tornado Warning",
    "TRA": "Tropical Storm Watch", "TRW": "Tropical Storm Warning",
    "TSA": "Tsunami Watch", "TSW": "Tsunami Warning", "VOW": "Volcano Warning",
    "WSA": "Winter Storm Watch", "WSW": "Winter Storm Warning",
}

SAME_STATES = {
    "01": "Alabama", "02": "Alaska", "04": "Arizona", "05": "Arkansas",
    "06": "California", "08": "Colorado", "09": "Connecticut", "10": "Delaware",
    "11": "District of Columbia", "12": "Florida", "13": "Georgia", "15": "Hawaii",
    "16": "Idaho", "17": "Illinois", "18": "Indiana", "19": "Iowa", "20": "Kansas",
    "21": "Kentucky", "22": "Louisiana", "23": "Maine", "24": "Maryland",
    "25": "Massachusetts", "26": "Michigan", "27": "Minnesota", "28": "Mississippi",
    "29": "Missouri", "30": "Montana", "31": "Nebraska", "32": "Nevada",
    "33": "New Hampshire", "34": "New Jersey", "35": "New Mexico", "36": "New York",
    "37": "North Carolina", "38": "North Dakota", "39": "Ohio", "40": "Oklahoma",
    "41": "Oregon", "42": "Pennsylvania", "44": "Rhode Island", "45": "South Carolina",
    "46": "South Dakota", "47": "Tennessee", "48": "Texas", "49": "Utah",
    "50": "Vermont", "51": "Virginia", "53": "Washington", "54": "West Virginia",
    "55": "Wisconsin", "56": "Wyoming", "60": "American Samoa", "66": "Guam",
    "69": "Northern Mariana Islands", "72": "Puerto Rico", "78": "US Virgin Islands",
}
SAME_PORTIONS = {"1": "northwest part of ", "2": "north part of ", "3": "northeast part of ",
                 "4": "west part of ", "5": "central part of ", "6": "east part of ",
                 "7": "southwest part of ", "8": "south part of ", "9": "southeast part of "}


def _same_location(code):
    portion, fips = code[0], code[1:]
    if fips == "00000":
        return "the entire United States"
    state = SAME_STATES.get(fips[:2], f"area {fips[:2]}")
    where = state if fips[2:] == "000" else f"{state}, county {fips[2:]} (FIPS {fips})"
    return SAME_PORTIONS.get(portion, "") + where


def _same_issued(julian, hour, minute, now):
    """UTC issue time for a JJJHHMM stamp, in the year nearest ``now``."""
    best = None
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            when = (datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc)
                    + datetime.timedelta(days=julian - 1, hours=hour, minutes=minute))
        except OverflowError:
            continue
        if when.year == year and (best is None or abs(when - now) < abs(best - now)):
            best = when
    return best


def parse_same(header, now=None):
    """Decode a SAME header string into a dict, or return None if it isn't valid."""
    m = SAME_HEADER_RE.match(header)
    if not m:
        return None
    org, event, locs, purge, julian, hour, minute, sender = m.groups()
    julian, hour, minute = int(julian), int(hour), int(minute)
    if not (1 <= julian <= 366 and hour < 24 and minute < 60 and int(purge[2:]) < 60):
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    issued = _same_issued(julian, hour, minute, now)
    if issued is None:
        return None
    minutes = int(purge[:2]) * 60 + int(purge[2:])
    codes = [loc for loc in locs.split("-") if loc]
    return {
        "raw": header,
        "originator": org,
        "originator_name": SAME_ORIGINATORS.get(org, "unknown originator"),
        "event": event,
        "event_name": SAME_EVENTS.get(event, "Unknown event"),
        "locations": [c[1:] for c in codes],
        "location_names": [_same_location(c) for c in codes],
        "purge_minutes": minutes,
        "issued": issued,
        "expires": issued + datetime.timedelta(minutes=minutes),
        "sender": sender.strip(),
    }


def same_summary(info):
    """One line, e.g. 'Tornado Warning (TOR) for Louisiana, county 071 (FIPS 22071), from KLOX/NWS'."""
    where = "; ".join(info["location_names"])
    return f"{info['event_name']} ({info['event']}) for {where}, from {info['sender'] or 'unknown'}"


def same_email_body(info, name, when):
    def utc(dt):
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    mins = info["purge_minutes"]
    return (
        f"A SAME (EAS) message was decoded on {name} at {time.strftime('%Y-%m-%d %H:%M:%S', when)}.\n\n"
        f"Event:      {info['event_name']} ({info['event']})\n"
        f"Originator: {info['originator_name']} ({info['originator']})\n"
        f"Sender ID:  {info['sender'] or 'unknown'}\n"
        "Areas:      " + "\n            ".join(info["location_names"]) + "\n"
        f"Issued:     {utc(info['issued'])}\n"
        f"Valid for:  {mins // 60}h {mins % 60:02d}m (until {utc(info['expires'])})\n\n"
        f"Raw header: {info['raw']}\n\n"
        "Counties are shown as their 3-digit FIPS code, not looked up by name. "
        "The spoken message itself is not decoded.")


class _SameBits:
    """One bit phase: finds ZCZC/NNNN in a bit stream and collects the header."""

    def __init__(self):
        self.reg = 0
        self.collecting = False
        self.byte = 0
        self.nbits = 0
        self.text = []

    def feed(self, bit):
        """Take one bit; returns ("header", text), ("eom", "") or None."""
        if not self.collecting:
            self.reg = (self.reg >> 1) | (bit << 31)
            if self.reg == _ZCZC:
                self.collecting, self.text = True, list("ZCZC")
                self.byte = self.nbits = 0
            elif self.reg == _NNNN:
                self.reg = 0
                return ("eom", "")
            return None
        self.byte |= bit << self.nbits
        self.nbits += 1
        if self.nbits < 8:
            return None
        ch, self.byte, self.nbits = self.byte, 0, 0
        if ch not in _SAME_CHARS or len(self.text) >= SAME_MAX_LEN:
            self.collecting, self.reg = False, 0
            return None
        self.text.append(chr(ch))
        if ch == 0x2D and "+" in self.text:        # '-' after the time group
            text = "".join(self.text)
            if SAME_HEADER_RE.match(text):
                self.collecting, self.reg = False, 0
                return ("header", text)
        return None


class SameDecoder:
    """Streaming SAME demodulator. ``feed`` takes mono int samples and returns
    a list of ("header" | "eom", text) events, one per decoded burst copy."""

    def __init__(self, sample_rate):
        self.dec = max(1, int(round(sample_rate / SAME_TARGET_RATE)))
        self.rate = sample_rate / self.dec
        self.spb = self.rate / SAME_BAUD
        self.win = max(2, int(round(self.spb)))
        self.rm = cmath.exp(-2j * math.pi * SAME_MARK / self.rate)
        self.rs = cmath.exp(-2j * math.pi * SAME_SPACE / self.rate)
        self.ring_m = [0j] * self.win
        self.ring_s = [0j] * self.win
        self.pos = 0
        self.sum_m = self.sum_s = 0j
        self.n = 0                       # decimated samples consumed so far
        self.rem = []                    # samples left over from the last block
        self.next_t = [self.spb * (p + 1) / SAME_PHASES for p in range(SAME_PHASES)]
        self.bits = [_SameBits() for _ in range(SAME_PHASES)]

    def feed(self, samples):
        samples = self.rem + list(samples) if self.rem else samples
        dec = self.dec
        count = len(samples) // dec
        self.rem = list(samples[count * dec:])
        if count == 0:
            return []
        if dec > 1:
            x = [sum(samples[i * dec:(i + 1) * dec]) for i in range(count)]
        else:
            x = samples
        n0 = self.n
        zm = cmath.exp(-2j * math.pi * SAME_MARK * n0 / self.rate)
        zs = cmath.exp(-2j * math.pi * SAME_SPACE * n0 / self.rate)
        rm, rs, win = self.rm, self.rs, self.win
        ring_m, ring_s, pos = self.ring_m, self.ring_s, self.pos
        sm, ss = self.sum_m, self.sum_s
        decision = []
        append = decision.append
        for v in x:
            pm = v * zm
            ps = v * zs
            zm *= rm
            zs *= rs
            sm += pm - ring_m[pos]
            ss += ps - ring_s[pos]
            ring_m[pos] = pm
            ring_s[pos] = ps
            pos += 1
            if pos == win:
                pos = 0
            append(sm.real * sm.real + sm.imag * sm.imag - ss.real * ss.real - ss.imag * ss.imag)
        self.pos, self.sum_m, self.sum_s = pos, sm, ss
        self.n = n0 + count
        events = []
        for p in range(SAME_PHASES):
            t = self.next_t[p]
            bits = self.bits[p]
            while t < self.n:
                ev = bits.feed(1 if decision[int(t) - n0] > 0 else 0)
                if ev:
                    events.append(ev)
                t += self.spb
            self.next_t[p] = t
        return events


class SameCollector:
    """Turns raw burst decodes into one message per alert.

    The first valid header opens a short window; every valid copy heard in it
    is a vote, and the most common one is reported. The same message is not
    reported again for SAME_DUP_SECS.
    """

    def __init__(self):
        self.votes = {}
        self.first = 0.0
        self.last_sent = {}      # header -> time reported
        self.last_any = -SAME_DUP_SECS

    def recent(self, now):
        """True if a message is being collected or was reported moments ago."""
        return bool(self.votes) or now - self.last_any < SAME_DUP_SECS

    def add(self, text, now):
        if not self.votes:
            self.first = now
        self.votes[text] = self.votes.get(text, 0) + 1

    def poll(self, now):
        """Returns the winning header once the window closes, else None."""
        if not self.votes or now - self.first < SAME_COLLECT_SECS:
            return None
        best = max(self.votes, key=lambda t: self.votes[t])
        self.votes = {}
        if now - self.last_sent.get(best, -SAME_DUP_SECS) < SAME_DUP_SECS:
            return None
        self.last_sent[best] = self.last_any = now
        return best


def recording_covering(base_dir, name, when):
    """Path (relative to ``base_dir``) of the station's MP3 that holds the audio
    at ``when`` (epoch seconds): the newest file started at or before it, in
    that day's folder. "" if the station isn't recording or nothing matches."""
    path = recording_path(base_dir, name, when)
    day = os.path.dirname(path)
    stamp = os.path.basename(path)[:13]        # YYMMDD_HHMMSS of ``when``
    try:
        names = [n for n in os.listdir(day) if n.endswith(".mp3") and n[:13] <= stamp]
    except OSError:
        return ""
    return os.path.relpath(os.path.join(day, max(names)), base_dir) if names else ""


def eas_log_name(name):
    """Per-station EAS log: eas-NAME.log (spaces and odd characters dropped)."""
    return f"eas-{_rec_name(name)}.log"


def log_eas(name, msg):
    """Record an EAS event in the combined eas.log and the station's own
    eas-NAME.log, so each tuner keeps its own history. Best-effort like
    log_file; both files sit under LOG_DIR and rotate with the other logs."""
    log_file("eas.log", msg)
    log_file(eas_log_name(name), msg)


def run_eas_detector(base_dir, name, sample_rate, channels, tid=""):
    """Read s16le PCM from stdin; pulse a Zabbix item on the EAS tone and
    decode/report the SAME header that precedes it.

    Best-effort and non-blocking: a missing Zabbix server is ignored and the
    process exits cleanly on stdin EOF, never stalling the audio pipeline.
    """
    zbx = ZabbixSender(parse_keyvalue(os.path.join(base_dir, "zabbix.conf")))
    mailer = Mailer(parse_keyvalue(os.path.join(base_dir, "smtp.conf")))

    def on_record(msg, when):
        """Log line plus the recording that holds this audio, if there is one."""
        rec = recording_covering(base_dir, name, when)
        log_eas(name, f"{msg} [recording: {rec}]" if rec else msg)

    same = SameDecoder(sample_rate)
    collector = SameCollector()

    def eas_items(value):
        """The any-tuner pulse, plus this tuner's own item."""
        items = [(zbx.key_eas, value)]
        if tid:
            items.append((zbx.tuner_key("eas", tid), value))
        return items

    def report_same(header, now):
        info = parse_same(header)
        if info is None:
            # Bit errors can leave a header that matches the format but isn't a
            # valid message; keep the raw text rather than losing the alert.
            msg = f"SAME header heard on {name} but could not be decoded [{header}]"
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
            on_record(msg, now)
            return
        summary = same_summary(info)
        msg = f"EAS message on {name}: {summary}"
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
        on_record(f"{msg} [{header}]", now)
        if zbx.enabled:
            zbx.send([(zbx.key_event, msg)])
        if mailer.alert_eas:
            mailer.send(f"EAS alert on {name}: {info['event_name']}",
                        same_email_body(info, name, time.localtime(now)))

    block = int(sample_rate * EAS_WINDOW_SECS)
    chunk = block * 2 * channels  # bytes per window (2 bytes per sample)
    hits = 0
    pulsing = False
    pulse_start = 0.0
    cooldown_until = 0.0
    while True:
        raw = sys.stdin.buffer.read(chunk)
        if len(raw) < chunk:
            break
        samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
        if channels == 2:
            samples = samples[0::2]  # left channel (attention tone is mono)
        now = time.time()
        for kind, text in same.feed(samples):
            if kind == "header":
                collector.add(text, now)
            else:
                on_record(f"EAS end of message heard on {name}", now)
        header = collector.poll(now)
        if header:
            report_same(header, now)
        if pulsing and now - pulse_start >= EAS_HOLD_SECS:
            if zbx.enabled:
                zbx.send(eas_items(0))
            pulsing = False
            cooldown_until = now + EAS_COOLDOWN_SECS
        if pulsing or now < cooldown_until:
            hits = 0
            continue
        if _eas_tone_present(samples, sample_rate):
            hits += 1
        else:
            hits = 0
        if hits >= EAS_CONFIRM_WINDOWS:
            msg = f"EAS attention tone heard on {name}"
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
                  file=sys.stderr, flush=True)
            on_record(msg + (" (SAME header decoded)" if collector.recent(now)
                             else " (no SAME header decoded)"), now)
            if zbx.enabled:
                zbx.send(eas_items(1) + [(zbx.key_event, msg)])
            # A decoded SAME email already covers this alert; send the bare
            # tone email only when no header was (or is about to be) decoded.
            if mailer.alert_eas and not collector.recent(now):
                mailer.send(
                    f"EAS attention tone heard on {name}",
                    f"The EAS attention tone (853 Hz + 960 Hz) was detected on {name} at "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}.\n\n"
                    "The SAME header was not decoded (weak signal, or it came before "
                    "Pi-Tuner was listening).")
            pulsing = True
            pulse_start = now
            hits = 0
    header = collector.poll(time.time() + SAME_COLLECT_SECS)  # stream ended mid-window
    if header:
        report_same(header, time.time())
    if pulsing and zbx.enabled:  # pipeline ended mid-pulse: don't leave the item stuck at 1
        zbx.send(eas_items(0))
    mailer.flush(10.0)


# --------------------------------------------------------------- recording

def _rec_name(name):
    """Filesystem-safe station name: spaces become '_', anything else odd goes."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "", str(name).strip().replace(" ", "_"))
    return safe.strip(".") or "station"


def recording_dir(base_dir, name):
    return os.path.join(base_dir, "recordings", _rec_name(name))


def recording_path(base_dir, name, when):
    """recordings/NAME/YYYY/MM/DD/YYMMDD_HHMMSS_NAME.mp3 for a start time
    (epoch seconds, Pi local time)."""
    t = datetime.datetime.fromtimestamp(when)
    safe = _rec_name(name)
    return os.path.join(recording_dir(base_dir, name), t.strftime("%Y"),
                        t.strftime("%m"), t.strftime("%d"),
                        f"{t.strftime('%y%m%d_%H%M%S')}_{safe}.mp3")


def next_boundary(now, seconds=REC_CHUNK_SECS):
    """Epoch time of the next quarter-hour (:00/:15/:30/:45) after ``now``."""
    minutes = seconds // 60
    t = datetime.datetime.fromtimestamp(now)
    t = t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)
    return (t + datetime.timedelta(minutes=minutes)).timestamp()


class _Mp3Writer:
    """One ffmpeg MP3 encoder fed from a bounded queue by its own thread, so a
    slow disk or dead encoder can never back up into the audio pipeline."""

    def __init__(self, path, rate, channels):
        self.path = path
        self.dropped = False
        self.queue = queue.Queue(maxsize=120)  # ~60 s of 0.5 s chunks
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "s16le",
             "-ar", str(rate), "-ac", str(channels), "-i", "pipe:0",
             "-acodec", "libmp3lame", "-b:a", "128k", "-f", "mp3", path],
            stdin=subprocess.PIPE)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self):
        broken = False
        while True:
            chunk = self.queue.get()
            if chunk is None:
                break
            if broken:
                continue
            try:
                self.proc.stdin.write(chunk)
            except (OSError, ValueError):
                broken = True
                log_file("recordings.log", f"{self.path}: encoder write failed")
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    @property
    def alive(self):
        return self.proc.poll() is None

    def write(self, chunk):
        try:
            self.queue.put_nowait(chunk)
        except queue.Full:
            if not self.dropped:
                self.dropped = True
                log_file("recordings.log", f"{self.path}: disk too slow, dropping audio")

    def close(self, wait=False):
        self.queue.put(None)
        if wait:
            self.thread.join(timeout=20)


def run_recorder(base_dir, name, rate, channels, stream=None,
                 clock=time.time, boundary=next_boundary, chunk_secs=0.5):
    """Read s16le PCM from stdin and write 128 kbps MP3 files, cut on every
    quarter hour.

    Best-effort and non-blocking: any failure (disk full, no ffmpeg, bad
    permissions) is logged to recordings.log, the recorder retries shortly, and
    stdin is always drained so the audio pipeline is never stalled.
    """
    stream = stream if stream is not None else sys.stdin.buffer
    chunk = int(rate * chunk_secs) * 2 * channels
    writer = None
    closed = []  # finished writers still flushing; joined at EOF
    next_cut = 0.0
    while True:
        raw = stream.read(chunk)
        if not raw:
            break
        now = clock()
        if writer is not None and not writer.alive:
            log_file("recordings.log", f"{writer.path}: encoder exited early")
            writer.close()
            closed.append(writer)
            writer, next_cut = None, now + REC_RETRY_SECS
        if now >= next_cut:
            if writer is not None:
                writer.close()
                closed.append(writer)
                writer = None
            closed = [w for w in closed if w.thread.is_alive()]
            try:
                writer = _Mp3Writer(recording_path(base_dir, name, now), rate, channels)
                next_cut = boundary(now)
            except (OSError, ValueError) as e:
                log_file("recordings.log", f"{name}: cannot start recording: {e}")
                next_cut = now + REC_RETRY_SECS
        if writer is not None:
            writer.write(raw)
    if writer is not None:
        writer.close(wait=True)
    for w in closed:
        w.thread.join(timeout=20)


def prune_recordings(base_dir, name, keep_days, now=None):
    """Delete a station's MP3s and RBDS.logs older than ``keep_days`` and any
    emptied date folders. Returns the number of files removed. ``keep_days`` 0 does nothing."""
    if keep_days <= 0:
        return 0
    now = time.time() if now is None else now
    cutoff = now - keep_days * 86400
    root = recording_dir(base_dir, name)
    removed = 0
    for dirpath, _dirs, files in os.walk(root, topdown=False):
        for fname in files:
            path = os.path.join(dirpath, fname)
            try:
                if (fname.endswith(".mp3") or fname == RBDS_LOG_FILE) \
                        and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        if dirpath != root:
            try:
                os.rmdir(dirpath)  # only succeeds when empty
            except OSError:
                pass
    return removed


# ------------------------------------------------------------------- RBDS

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean_rds_text(text):
    """Collapse control characters and padding in an RDS string."""
    return re.sub(r"\s+", " ", _CTRL_RE.sub(" ", str(text or ""))).strip()


def rbds_song(state, label=None):
    """Now-playing text: 'RadioText (label)', or whichever of the two we have.

    ``label`` is the station label to show (see PsTracker); it defaults to the
    raw PS name."""
    rt = state.get("radiotext", "")
    ps = state.get("ps", "") if label is None else label
    if rt and ps:
        return f"{rt} ({ps})"
    return rt or ps


def update_icecast_metadata(ice, mount, song):
    """Set the Icecast now-playing text for a mount. Returns True on success."""
    user, password = "source", ice["password"]
    if ice.get("admin_password"):
        user, password = ice["admin_user"], ice["admin_password"]
    query = urllib.parse.urlencode(
        {"mode": "updinfo", "mount": mount, "song": song, "charset": "UTF-8"})
    url = f"http://{ice['host']}:{ice['port']}/admin/metadata?{query}"
    req = urllib.request.Request(url)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except (urllib.error.URLError, OSError) as e:
        log_file("rbds.log", f"{mount}: metadata update failed: {e}")
        return False


def rbds_log_path(base_dir, name, when):
    """recordings/NAME/YYYY/MM/DD/RBDS.log for a time (epoch seconds, local)."""
    t = datetime.datetime.fromtimestamp(when)
    return os.path.join(recording_dir(base_dir, name), t.strftime("%Y"),
                        t.strftime("%m"), t.strftime("%d"), RBDS_LOG_FILE)


_RBDS_LOG_LINE_RE = re.compile(r"^(\d{6} \d{2}:\d{2}:\d{2}): (.*)$")
_PS_SUFFIX_RE = re.compile(r" \([^()]{1,8}\)$")


class PsTracker:
    """Tracks a station's PS name and decides what to show beside the text.

    Some stations scroll their PS ("Station", "Z93 The", "#1 Hit", "Music"),
    which isn't a name. When the PS changes several times within a minute we
    treat it as dynamic and use the callsign redsea derives from the PI code
    instead (or nothing). The decision waits RBDS_PS_SETTLE_SECS after the
    first data so a scrolling PS isn't mistaken for a static one.
    """

    def __init__(self):
        self.ps = ""
        self.callsign = ""
        self.started = None
        self.changes = []

    def update(self, data, now):
        if self.started is None:
            self.started = now
        if "ps" in data:
            ps = _clean_rds_text(data["ps"])
            if ps and ps != self.ps:
                if self.ps:
                    self.changes.append(now)
                self.ps = ps
        call = data.get("callsign")
        if isinstance(call, str) and re.fullmatch(r"[A-Z0-9]{3,5}", call.strip()):
            self.callsign = call.strip()

    def dynamic(self, now):
        recent = [t for t in self.changes if now - t <= RBDS_PS_WINDOW_SECS]
        return len(recent) >= RBDS_PS_DYNAMIC_CHANGES

    def label(self, now, final=False):
        """(decided, text). Undecided until the PS has had time to settle."""
        if self.started is None:
            return (final, "")
        if self.dynamic(now):
            return (True, self.callsign)
        if final or now - self.started >= RBDS_PS_SETTLE_SECS:
            return (True, self.ps)
        return (False, "")


def _log_line_time(line):
    return time.mktime(time.strptime(line[:15], "%y%m%d %H:%M:%S"))


class RbdsLogger:
    """Turn redsea output into RBDS.log lines: every RadioText change is logged.

    Each line is ``YYMMDD HH:MM:SS: text (PS)``. A repeat of the text that is
    already showing isn't logged again, but a text coming back after another
    one is. RadioText Plus is ignored. Lines are held until the PS label
    settles (see PsTracker), then written with the time the text arrived.
    """

    def __init__(self, ps_tracker=None):
        self.tracker = ps_tracker if ps_tracker is not None else PsTracker()
        self._owns_tracker = ps_tracker is None
        self.current = None   # last RT text
        self.pending = []     # [(when, text)] waiting for the PS label

    def seed(self, lines, now):
        """Prime from today's log so a restart doesn't re-log the current text."""
        for line in lines:
            m = _RBDS_LOG_LINE_RE.match(line.strip())
            if m:
                self.current = _PS_SUFFIX_RE.sub("", m.group(2))

    def _flush(self, now, final=False):
        decided, label = self.tracker.label(now, final)
        if not decided:
            return []
        lines = []
        for when, text in self.pending:
            shown = f"{text} ({label})" if label else text
            lines.append(f"{time.strftime('%y%m%d %H:%M:%S', time.localtime(when))}: {shown}")
        self.pending = []
        return lines

    def feed(self, data, now):
        """Take one redsea JSON dict; return the log lines now ready (maybe none)."""
        if self._owns_tracker:
            self.tracker.update(data, now)
        if "radiotext" in data:
            text = _clean_rds_text(data["radiotext"])
            if text and text != self.current:
                self.current = text
                self.pending.append((now, text))
        return self._flush(now)

    def finish(self, now):
        """End of stream: write anything still held back."""
        return self._flush(now, final=True)


def _read_rbds_log_tail(base_dir, name, now):
    try:
        with open(rbds_log_path(base_dir, name, now), encoding="utf-8",
                  errors="replace") as f:
            return f.read().splitlines()[-200:]
    except OSError:
        return []


def _append_rbds_log(base_dir, name, line):
    """Append a log line to the day folder its own timestamp belongs to."""
    path = rbds_log_path(base_dir, name, _log_line_time(line))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        log_file("recordings.log", f"{path}: cannot write RBDS log: {e}")


def run_rbds_meta(base_dir, mount, stream=None, updater=update_icecast_metadata,
                  log_name=None, clock=time.time, zbx=None, tid=""):
    """Read redsea JSON lines from stdin and keep Icecast's now-playing current.

    Best-effort: bad lines and HTTP errors are ignored, and stdin is always
    drained so the audio pipeline is never stalled. Exits cleanly on EOF.
    With ``log_name`` the decoded text is also appended to that station's
    daily RBDS.log (independent of whether the Icecast update succeeds). With
    ``zbx`` and ``tid`` the current RadioText and PS are sent to that tuner's
    Zabbix items when they change and again every Zabbix interval.
    """
    stream = stream if stream is not None else sys.stdin
    conf = parse_keyvalue(os.path.join(base_dir, "icecast.conf"))
    ice = {
        "host": conf.get("host", "localhost"),
        "port": conf.get("port", "8000"),
        "password": conf.get("source_password", "hackme"),
        "admin_user": conf.get("admin_user", "admin"),
        "admin_password": conf.get("admin_password", ""),
    }
    state = {}
    last_sent = None
    tracker = PsTracker()
    logger = None
    if log_name:
        logger = RbdsLogger(ps_tracker=tracker)
        started = clock()
        logger.seed(_read_rbds_log_tail(base_dir, log_name, started), started)
    now = clock()
    last_zbx, last_zbx_at = None, 0.0
    for line in stream:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        now = clock()
        tracker.update(data, now)
        if logger is not None:
            for entry in logger.feed(data, now):
                _append_rbds_log(base_dir, log_name, entry)
        if "radiotext" in data:
            state["radiotext"] = _clean_rds_text(data["radiotext"])
        decided, label = tracker.label(now)
        if not decided:
            continue
        if zbx is not None and tid:
            seen = (state.get("radiotext", ""), label)
            if seen != last_zbx or now - last_zbx_at >= zbx.interval:
                if seen != ("", ""):
                    zbx.send_async([(zbx.tuner_key("rbds.rt", tid), seen[0]),
                                    (zbx.tuner_key("rbds.ps", tid), seen[1])])
                last_zbx, last_zbx_at = seen, now
        song = rbds_song(state, label)
        if not song or song == last_sent:
            continue
        if updater(ice, mount, song):
            last_sent = song
            log_file("rbds.log", f"{mount}: now playing: {song}")
    if logger is not None:
        for entry in logger.finish(now):
            _append_rbds_log(base_dir, log_name, entry)
    if zbx is not None:
        zbx.flush(5.0)


def pty_from_json_line(line):
    """Return the RBDS program type from a redsea JSON line, or "" if none.

    redsea reports "No PTY" for 0 and "" or "Unknown" for reserved codes;
    those all mean there is no PTY to use as a genre.
    """
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or "prog_type" not in data:
        return None
    pty = _clean_rds_text(data["prog_type"])
    return "" if pty.lower() in ("", "no pty", "unknown") else pty


def scan_pty(cfg, device_index, timeout=PTY_SCAN_SECS):
    """Listen briefly to an FM station and return its RBDS PTY ("" if none).

    Icecast reads the genre only when the source connects, so this runs before
    the real pipeline starts. The dongle is released before returning.
    """
    cmd = f"{_fm_source(cfg, device_index)} | redsea -u -r 192000 2>/dev/null"
    proc = subprocess.Popen(cmd, shell=True, executable="/bin/bash",
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    pty = ""
    buf = b""
    fd = proc.stdout.fileno()
    deadline = time.time() + timeout
    try:
        # Read the raw fd: select() can't see lines already pulled into a
        # buffered reader's buffer.
        while pty == "" and time.time() < deadline:
            ready, _, _ = select.select([fd], [], [],
                                        max(0.0, deadline - time.time()))
            if not ready:
                break
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                found = pty_from_json_line(line.decode("utf-8", "replace"))
                if found is not None:
                    pty = found
                    break
            else:
                continue
            break
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait()
        proc.stdout.close()
    time.sleep(0.5)  # let the USB device settle before it's reopened
    return pty


# ---------------------------------------------------------------- station

class Station:
    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg["name"]
        self.proc = None          # subprocess.Popen while running
        self.status = "stopped"   # streaming | down | serial_not_found | no_audio
        self.audio_bytes = None   # rtl_fm bytes written at the last change
        self.audio_at = 0.0       # when audio last advanced (or the pipeline started)
        self.retry_at = 0.0
        self.backoff = 2.0
        self.restarts = 0         # pipeline deaths since the service started
        self.rtl_gone_at = None   # when rtl_fm was first missing from a live pipeline
        self.genre = _genre_for(cfg)


# ---------------------------------------------------------------- tuner

class Tuner:
    def __init__(self, base_dir):
        self.dir = base_dir
        self.ice = {"host": "localhost", "port": "8000", "password": "hackme"}
        self.zbx = ZabbixSender({})
        self.mailer = Mailer({})
        self.alerts = AlertManager(self.mailer)
        self.eas_enabled = False
        self.level_enabled = False
        self.stations = []
        self.devices = []
        self._last_scan = 0.0
        self._next_heartbeat = 0.0
        self.reload_requested = False
        self.running = False
        self._warned_redsea = False

    # -- config ----------------------------------------------------------

    def load_config(self):
        ice = parse_keyvalue(os.path.join(self.dir, "icecast.conf"))
        self.ice = {
            "host": ice.get("host", "localhost"),
            "port": ice.get("port", "8000"),
            "password": ice.get("source_password", "hackme"),
        }
        self._warned_redsea = False
        zbx = parse_keyvalue(os.path.join(self.dir, "zabbix.conf"))
        self.zbx = ZabbixSender(zbx)
        eas_on = str(zbx.get("eas_detect", "false")).lower() in ("1", "true", "yes", "on")
        self.mailer = Mailer(parse_keyvalue(os.path.join(self.dir, "smtp.conf")))
        self.alerts.mailer = self.mailer
        eas_by_email = self.mailer.enabled and self.mailer.alert_eas
        self.eas_enabled = (eas_on and self.zbx.enabled) or eas_by_email
        self.level_enabled = self.zbx.enabled and self.zbx.level_monitor
        self.stations = [Station(c) for c in
                         load_stations(os.path.join(self.dir, "stations"))]
        self.alerts.sync_stations({st.name for st in self.stations})

    def rbds_available(self):
        """True if redsea is installed; warns once per load if not."""
        if shutil.which("redsea"):
            return True
        if not self._warned_redsea:
            log("RBDS requested but redsea is not installed, skipping RBDS", err=True)
            self._warned_redsea = True
        return False

    # -- devices ---------------------------------------------------------

    def refresh_devices(self):
        now = time.time()
        if now - self._last_scan < 5.0:
            return
        self._last_scan = now
        self.devices = enumerate_devices()

    def resolve_serial(self, serial):
        target = norm_serial(serial)
        for index, found in self.devices:
            if norm_serial(found) == target:
                return index
        return None

    # -- station lifecycle ------------------------------------------------

    def set_status(self, st, status, detail=""):
        if st.status == status:
            return
        prev = st.status
        st.status = status
        msg = f"station {st.name}: {prev} -> {status}"
        if detail:
            msg += f" ({detail})"
        log(msg)
        self.zbx.event(f"station {st.name} {status}{(' ' + detail) if detail else ''}")
        self.zbx.send(self.zbx.state_items(self.station_info(st)), mirror=False)
        self.alerts.station_status(st.name, status, detail, time.time())

    def start_station(self, st):
        if not st.cfg["serial"]:
            self.set_status(st, "serial_not_found", "no SERIAL configured")
            st.retry_at = time.time() + 10
            return
        index = self.resolve_serial(st.cfg["serial"])
        if index is None:
            self.set_status(st, "serial_not_found", f"serial {st.cfg['serial']} not found")
            st.retry_at = time.time() + 10
            return
        rbds = bool(st.cfg.get("rbds")) and self.rbds_available()
        genre = ""
        if rbds:
            genre = scan_pty(st.cfg, index)
            log(f"station {st.name}: RBDS PTY: {genre or 'none heard, using ' + FM_DEFAULT_GENRE}")
        st.genre = _genre_for(st.cfg, genre)
        cmd = build_command(st.cfg, self.ice, index,
                            eas=self.eas_enabled, eas_dir=self.dir, rbds=rbds,
                            genre=genre, level=self.level_enabled, rssi=True)
        try:
            st.proc = subprocess.Popen(
                cmd, shell=True, stdout=subprocess.DEVNULL,
                executable="/bin/bash", start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001 - surfaced via status/log
            st.proc = None
            self.set_status(st, "down", f"launch failed: {e}")
            st.retry_at = time.time() + st.backoff
            st.backoff = min(st.backoff * 2, 60)
            return
        st.audio_bytes, st.audio_at = None, time.time()
        st.rtl_gone_at = None
        if st.status != "no_audio":    # a silent dongle stays "no_audio" until audio flows
            self.set_status(st, "streaming", f"device {index} {st.cfg['band']} {st.cfg['freq']} MHz")
        st.backoff = 2.0

    def stop_station(self, st):
        if st.proc is None:
            return
        try:
            os.killpg(os.getpgid(st.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            st.proc.terminate()
        try:
            st.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(st.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                st.proc.kill()
            try:
                st.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        st.proc = None

    # -- supervisor -------------------------------------------------------

    def check_audio(self, st, now):
        """Flag a running pipeline whose rtl_fm has stopped producing audio."""
        written = rtl_fm_bytes_written(st.proc.pid)
        if written is None:
            if rtl_fm_pid(st.proc.pid) is not None:
                st.rtl_gone_at = None      # running, just can't read its counters
                return
            if st.rtl_gone_at is None:
                st.rtl_gone_at = now
            elif now - st.rtl_gone_at >= RTL_GONE_SECS:
                log(f"station {st.name}: rtl_fm is gone but the pipeline is still up "
                    "(dongle unplugged?), restarting it", err=True)
                self.stop_station(st)
                st.restarts += 1
                self.set_status(st, "down", "rtl_fm exited (dongle unplugged?)")
                st.retry_at = now + st.backoff
                st.backoff = min(st.backoff * 2, 60)
            return
        st.rtl_gone_at = None
        if st.audio_bytes is None or written != st.audio_bytes:
            st.audio_bytes, st.audio_at = written, now
            if st.status == "no_audio":
                self.set_status(st, "streaming", "audio is flowing again")
            return
        silent = now - st.audio_at
        if silent < NO_AUDIO_SECS:
            return
        if st.status != "no_audio":
            log(f"station {st.name}: ERROR no audio for {int(silent)}s - {NO_AUDIO_DETAIL}", err=True)
            self.set_status(st, "no_audio", NO_AUDIO_DETAIL)
        elif silent >= NO_AUDIO_RESTART_SECS:
            log(f"station {st.name}: still no audio, restarting its pipeline", err=True)
            self.stop_station(st)
            st.retry_at = now

    def poll(self):
        for st in self.stations:
            if st.proc is not None:
                rc = st.proc.poll()
                if rc is not None:
                    st.proc = None
                    st.restarts += 1
                    self.set_status(st, "down", f"exit code {rc}")
                    st.retry_at = time.time() + st.backoff
                    st.backoff = min(st.backoff * 2, 60)
                else:
                    self.check_audio(st, time.time())
            elif time.time() >= st.retry_at:
                self.refresh_devices()
                self.start_station(st)

    def prune_recordings(self):
        for st in self.stations:
            days = st.cfg.get("record_keep_days", 0)
            if st.cfg.get("record") and days > 0:
                n = prune_recordings(self.dir, st.name, days)
                if n:
                    log(f"station {st.name}: removed {n} recording(s) older than {days} days")

    def station_info(self, st, now=None):
        """What Zabbix is told about one tuner."""
        now = time.time() if now is None else now
        record = bool(st.cfg.get("record"))
        return {
            "id": tuner_id(st.cfg["mount"]),
            "name": st.name,
            "band": st.cfg["band"],
            "frequency": st.cfg["freq"],
            "mount": st.cfg["mount"],
            "serial": st.cfg["serial"],
            "state": st.status,
            "genre": st.genre,
            "restarts": st.restarts,
            "recording": record,
            "recording_age": newest_recording_age(self.dir, st.name, now) if record else -1.0,
        }

    def send_heartbeat(self):
        self.zbx.snapshot([self.station_info(st) for st in self.stations])

    def reload(self):
        log("reloading config")
        for st in self.stations:
            self.stop_station(st)
        self.load_config()
        self._last_scan = 0.0
        self.refresh_devices()
        for st in self.stations:
            st.retry_at = time.time()
            self.start_station(st)
            sd_notify("WATCHDOG=1")

    def run(self):
        self.load_config()
        self.refresh_devices()
        # Ready as soon as the config is loaded: starting stations (an RBDS scan
        # takes several seconds each) must not run into systemd's start timeout.
        sd_notify("READY=1")
        for st in self.stations:
            self.start_station(st)
            sd_notify("WATCHDOG=1")
        self._next_heartbeat = time.time() + self.zbx.interval
        next_prune = 0.0
        self.running = True
        self.alerts.service_started({st.name: st.status for st in self.stations})
        while self.running:
            if self.reload_requested:
                self.reload_requested = False
                self.reload()
                self._next_heartbeat = time.time() + self.zbx.interval
                continue
            try:
                self.poll()
            except Exception as e:  # noqa: BLE001 - one bad pass must not kill supervision
                log(f"supervisor error: {type(e).__name__}: {e}", err=True)
            sd_notify("WATCHDOG=1")
            now = time.time()
            self.alerts.tick(now)
            self.alerts.check_disk(now, self.dir, any(st.cfg.get("record") for st in self.stations))
            if time.time() >= next_prune:
                self.prune_recordings()
                next_prune = time.time() + REC_PRUNE_SECS
            if time.time() >= self._next_heartbeat:
                self.send_heartbeat()
                self._next_heartbeat = time.time() + self.zbx.interval
            time.sleep(1.0)
        self.alerts.service_stopped()
        for st in self.stations:
            self.stop_station(st)

    def request_reload(self):
        self.reload_requested = True

    def shutdown(self):
        self.running = False


# --------------------------------------------------------------- check mode

def run_check(base_dir):
    tuner = Tuner(base_dir)
    tuner.load_config()
    tuner.refresh_devices()
    log(f"icecast: source@{tuner.ice['host']}:{tuner.ice['port']} "
        f"(password {'set' if tuner.ice['password'] else 'MISSING'})")
    zbx_state = (f"enabled -> {tuner.zbx.server}:{tuner.zbx.port}"
                 if tuner.zbx.enabled else "disabled")
    log(f"zabbix: {zbx_state}")
    missing = missing_config_keys(base_dir)
    if missing:
        total = sum(len(v) for v in missing.values())
        log(f"config: {total} setting(s) from newer versions are missing in "
            f"{len(missing)} file(s); run: tuner.py upgrade-config --dir {base_dir}")
    mail = tuner.mailer
    if mail.enabled:
        log(f"email: {mail.host}:{mail.port} ({mail.security}) from {mail.from_addr} to "
            f"{', '.join(mail.to)}; password {'set' if mail.password else 'not set'}")
    else:
        log(f"email: disabled ({mail.problem()})")
    if not tuner.devices:
        log("no RTL-SDR devices found via rtl_test", err=True)
    else:
        for index, serial in tuner.devices:
            log(f"device {index}: SN {serial}")
    if not tuner.stations:
        log("no stations configured (stations/*.conf)", err=True)
        return 1
    for st in tuner.stations:
        index = tuner.resolve_serial(st.cfg["serial"])
        if index is None:
            log(f"station {st.name}: SERIAL {st.cfg['serial'] or '(none)'} NOT FOUND",
                err=True)
        else:
            rbds = bool(st.cfg.get("rbds")) and tuner.rbds_available()
            cmd = build_command(st.cfg, tuner.ice, index,
                                eas=tuner.eas_enabled, eas_dir=tuner.dir, rbds=rbds)
            log(f"station {st.name}: device {index} -> {cmd}")
            if rbds:
                log(f"station {st.name}: genre comes from the RBDS PTY, read at startup")
            if st.cfg.get("record"):
                log(f"station {st.name}: recording to {recording_dir(tuner.dir, st.name)}")
    return 0


# ------------------------------------------------------------------- main

def _main_detect_eas(argv):
    parser = argparse.ArgumentParser(prog="tuner.py detect-eas")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory")
    parser.add_argument("--tuner", default="", help="tuner id for the per-tuner Zabbix item")
    parser.add_argument("name", help="station name (for the Zabbix event)")
    parser.add_argument("rate", type=int, help="PCM sample rate in Hz")
    parser.add_argument("channels", type=int, help="audio channel count (1 or 2)")
    args = parser.parse_args(argv)
    run_eas_detector(args.dir, args.name, args.rate, args.channels, args.tuner)
    return 0


def _main_upgrade_config(argv):
    parser = argparse.ArgumentParser(prog="tuner.py upgrade-config")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory")
    parser.add_argument("--dry-run", action="store_true", help="only list what's missing")
    parser.add_argument("--no-restart", action="store_true",
                        help="don't offer to restart the service afterwards")
    args = parser.parse_args(argv)
    result = missing_config_keys(args.dir) if args.dry_run else upgrade_config(args.dir)
    if not result:
        print("Config files are up to date; nothing to add.")
        return 0
    verb = "Missing from" if args.dry_run else "Added to"
    for name, keys in result.items():
        print(f"{verb} {name}: {', '.join(keys)}")
    if not args.dry_run and not args.no_restart:
        offer_service_restart()
    return 0


def offer_service_restart(ask=input, run=subprocess.run, interactive=None):
    """After new settings were added, offer to restart the service: a reload
    only restarts stations whose own config changed, so new pipeline pieces need
    a full restart. Only asks on a terminal; otherwise says what to run."""
    command = "sudo systemctl restart pituner"
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        print(f"Restart the service to use the new settings: {command}")
        return False
    try:
        answer = ask("Restart the Pi-Tuner service now so the new settings take effect? [Y/n] ")
    except EOFError:
        answer = "n"
    if answer.strip().lower() not in ("", "y", "yes"):
        print(f"Later, run: {command}")
        return False
    try:
        ok = run(["systemctl", "restart", "pituner.service"], check=False).returncode == 0
    except OSError:
        ok = False
    print("Restarted; stations are starting again." if ok else
          f"Couldn't restart automatically. Run: {command}")
    return ok


def _main_backup_config(argv):
    parser = argparse.ArgumentParser(prog="tuner.py backup-config")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory")
    args = parser.parse_args(argv)
    dest = backup_config(args.dir)
    print(f"Backed up settings to {dest}" if dest else "No settings to back up.")
    return 0


def _main_level(argv):
    parser = argparse.ArgumentParser(prog="tuner.py level")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory")
    parser.add_argument("--tuner", required=True, help="tuner id, e.g. tuner1")
    parser.add_argument("name", help="station name")
    parser.add_argument("rate", type=int, help="PCM sample rate in Hz")
    parser.add_argument("channels", type=int, help="audio channel count (1 or 2)")
    args = parser.parse_args(argv)
    run_level_meter(args.dir, args.tuner, args.name, args.rate, args.channels)
    return 0


def _main_rssi(argv):
    parser = argparse.ArgumentParser(prog="tuner.py rssi")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory")
    parser.add_argument("--tuner", required=True, help="tuner id, e.g. tuner1")
    parser.add_argument("--band", choices=sorted(RSSI_BANDS), required=True)
    parser.add_argument("--freq", type=float, required=True, help="frequency in MHz")
    parser.add_argument("--station-conf", default="", help="station file name, for its RSSI_* overrides")
    parser.add_argument("name", help="station name")
    parser.add_argument("rate", type=int, help="mono PCM sample rate in Hz")
    args = parser.parse_args(argv)
    run_rssi_meter(args.dir, args.tuner, args.name, args.band, args.freq, args.rate,
                   station_conf=args.station_conf)
    return 0


def _main_mute_gate(argv):
    parser = argparse.ArgumentParser(prog="tuner.py mute-gate")
    parser.add_argument("--tuner", required=True, help="tuner id, e.g. tuner1")
    parser.add_argument("rate", type=int, help="PCM sample rate in Hz")
    parser.add_argument("channels", type=int, help="audio channel count (1 or 2)")
    args = parser.parse_args(argv)
    run_mute_gate(args.tuner, args.rate, args.channels)
    return 0


def _alarm_label(data, enabled):
    if not enabled:
        return "off"
    if data.get("muted"):
        return "MUTED"
    if data.get("alarm"):
        return "LOW SIGNAL"
    return "ok"


def _main_signal(argv):
    parser = argparse.ArgumentParser(prog="tuner.py signal")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory")
    parser.add_argument("--lines", type=int, default=5, help="recent rssi.log lines to show")
    args = parser.parse_args(argv)
    stations = load_stations(os.path.join(args.dir, "stations"))
    if not stations:
        print(f"No stations found in {args.dir}/stations")
        return 1
    now = time.time()
    print(f"{'TUNER':<8} {'STATION':<20} {'FREQ':>8}  {'RSSI':>8}  {'RATING':<7} {'ALARM':<11} AGE")
    settings_rows = []
    for tid, st, data in read_rssi_states(stations):
        head = f"{tid:<8} {st['name'][:20]:<20} {st['freq']:>8g}"
        cfg = load_rssi_settings(args.dir, st["conf"])
        settings_rows.append((tid, st, cfg))
        if not st.get("rssi", True):
            print(f"{head}  {'-':>8}  {'off':<7} {'-':<11} RSSI_MONITOR=false")
        elif not data:
            print(f"{head}  {'-':>8}  {'-':<7} {'-':<11} no reading yet")
        else:
            age = max(0, int(now - data.get("updated", 0)))
            stale = "  (stale: is the service running?)" if age > 3 * RSSI_INTERVAL_SECS else ""
            alarm = _alarm_label(data, cfg["alarm"])
            print(f"{head}  {data['rssi']:>5.1f} dB  {data['rating']:<7} {alarm:<11} {age}s ago{stale}")
    print(f"\nRSSI is a relative signal-to-noise estimate: good >= {RSSI_GOOD_DB:g} dB, "
          f"fair >= {RSSI_FAIR_DB:g} dB.")
    print("Low Signal alarm settings (pituner config > Low signal alarm):")
    for tid, st, cfg in settings_rows:
        if not cfg["alarm"]:
            print(f"  {tid:<8} alarm off")
            continue
        print(f"  {tid:<8} raise < {cfg['threshold_on']:g} dB for {cfg['time_on']}s, "
              f"clear > {cfg['threshold_off']:g} dB for {cfg['time_off']}s, "
              f"mute {'on' if cfg['mute'] else 'off'}, offset {cfg['offset']:+g} dB")
    try:
        with open(os.path.join(LOG_DIR, "rssi.log"), encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-max(0, args.lines):]
    except OSError:
        tail = []
    if tail:
        print(f"\nRecent {LOG_DIR}/rssi.log:")
        for line in tail:
            print("  " + line.rstrip())
    return 0


def _main_config(argv):
    parser = argparse.ArgumentParser(prog="tuner.py config")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory")
    args = parser.parse_args(argv)
    sys.path.insert(0, os.path.dirname(TUNER_PATH))
    import configure  # noqa: PLC0415 - only needed for the interactive menu
    return configure.main(args.dir)


def _main_test_email(argv):
    parser = argparse.ArgumentParser(prog="tuner.py test-email")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory")
    parser.add_argument("--to", default=None, help="send to this address instead of TO")
    args = parser.parse_args(argv)
    conf = parse_keyvalue(os.path.join(args.dir, "smtp.conf"))
    if args.to:
        conf["to"] = args.to
    mailer = Mailer(conf)
    if not mailer.enabled:
        print(f"Email is not enabled: {mailer.problem()} (see {args.dir}/smtp.conf)")
        return 1
    try:
        mailer.send_sync("Test email",
                         "This is a test message from Pi-Tuner.\n\n"
                         f"Sent {time.strftime('%Y-%m-%d %H:%M:%S')} via "
                         f"{mailer.host}:{mailer.port} ({mailer.security}).")
    except Exception as e:  # noqa: BLE001 - report any failure to the user
        print(f"Test email FAILED: {type(e).__name__}: {e}")
        return 1
    print(f"Test email sent to {', '.join(mailer.to)}")
    return 0


def _main_record(argv):
    parser = argparse.ArgumentParser(prog="tuner.py record")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory")
    parser.add_argument("name", help="station name (used in the file names)")
    parser.add_argument("rate", type=int, help="PCM sample rate in Hz")
    parser.add_argument("channels", type=int, help="audio channel count (1 or 2)")
    args = parser.parse_args(argv)
    run_recorder(args.dir, args.name, args.rate, args.channels)
    return 0


def _main_rbds_meta(argv):
    parser = argparse.ArgumentParser(prog="tuner.py rbds-meta")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory")
    parser.add_argument("--log-name", default=None,
                        help="station name: also append to its daily RBDS.log")
    parser.add_argument("--tuner", default="", help="tuner id: also send RT/PS to its Zabbix items")
    parser.add_argument("mount", help="Icecast mount point, e.g. /tuner1")
    args = parser.parse_args(argv)
    zbx = None
    if args.tuner:
        zbx = ZabbixSender(parse_keyvalue(os.path.join(args.dir, "zabbix.conf")))
        zbx = zbx if zbx.enabled else None
    run_rbds_meta(args.dir, args.mount, log_name=args.log_name, zbx=zbx, tid=args.tuner)
    return 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "detect-eas":
        return _main_detect_eas(argv[1:])
    if argv and argv[0] == "level":
        return _main_level(argv[1:])
    if argv and argv[0] == "rssi":
        return _main_rssi(argv[1:])
    if argv and argv[0] == "mute-gate":
        return _main_mute_gate(argv[1:])
    if argv and argv[0] == "signal":
        return _main_signal(argv[1:])
    if argv and argv[0] == "upgrade-config":
        return _main_upgrade_config(argv[1:])
    if argv and argv[0] == "backup-config":
        return _main_backup_config(argv[1:])
    if argv and argv[0] == "config":
        return _main_config(argv[1:])
    if argv and argv[0] == "test-email":
        return _main_test_email(argv[1:])
    if argv and argv[0] == "record":
        return _main_record(argv[1:])
    if argv and argv[0] == "rbds-meta":
        return _main_rbds_meta(argv[1:])

    parser = argparse.ArgumentParser(prog="tuner.py", description=__doc__)
    parser.add_argument("--dir", default=DEFAULT_DIR, help="install directory (default %(default)s)")
    parser.add_argument("--check", action="store_true", help="validate config and print pipelines, then exit")
    args = parser.parse_args(argv)

    if args.check:
        return run_check(args.dir)

    tuner = Tuner(args.dir)

    def _reload(signum, frame):
        tuner.request_reload()

    def _stop(signum, frame):
        log("received SIGTERM, shutting down")
        tuner.shutdown()

    signal.signal(signal.SIGHUP, _reload)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log(f"Pi-Tuner starting (config: {args.dir})")
    tuner.run()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
