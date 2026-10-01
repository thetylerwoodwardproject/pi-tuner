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
             run the EAS attention-tone detector on raw s16le PCM from stdin.

    tuner.py record --dir /opt/pituner <name> <rate> <channels>
             record raw s16le PCM from stdin to 128 kbps MP3 files, one per
             15 minutes, under <dir>/recordings/<name>/YYYY/MM/DD/.

    tuner.py test-email --dir /opt/pituner [--to ADDRESS]
             send one test message using smtp.conf and report any SMTP error.

    tuner.py rbds-meta --dir /opt/pituner [--log-name NAME] <mount>
             read redsea JSON lines from stdin and push the decoded RBDS
             text to the Icecast now-playing metadata for <mount>. With
             --log-name, also append it to the station's daily RBDS.log.
"""
import argparse
import base64
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
LOG_DIR = "/var/www/pituner"
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

# ------------------------------------------------------------------- logging

def log_file(name, msg):
    """Append a timestamped line to /var/www/pituner/<name>. Best-effort."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, name), "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def log(msg, err=False):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
          file=sys.stderr if err else sys.stdout, flush=True)
    log_file("tuner.log", msg)


# -------------------------------------------------------------- config files

def parse_keyvalue(path):
    """Parse a flat KEY=value file into a lowercase-keyed dict.

    Blank lines and full-line comments are ignored; a trailing ``# comment``
    (preceded by whitespace) is stripped from a value. A missing file yields an
    empty dict.
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
            value = re.split(r"\s+#", value)[0].strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            conf[key.strip().lower()] = value
    return conf


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


def _eas_tee(name, sample_rate, channels, eas_dir):
    """Return the pipeline fragment that taps audio to the EAS tone detector."""
    return (f"tee --output-error=warn >(python3 {TUNER_PATH} detect-eas "
            f'--dir "{eas_dir}" "{name}" {sample_rate} {channels})')


def _record_tee(name, sample_rate, channels, base_dir):
    """Return the pipeline fragment that taps audio to the 15-minute recorder."""
    return (f"tee --output-error=warn >(python3 {TUNER_PATH} record "
            f'--dir "{base_dir}" "{name}" {sample_rate} {channels})')


def _rbds_tee(mount, base_dir, log_name=None):
    """Return the pipeline fragment that taps the 192 kHz MPX to RBDS decoding.

    ``log_name`` (a station name) also writes the daily RBDS.log."""
    log_arg = f' --log-name "{log_name}"' if log_name else ""
    return (f"tee --output-error=warn >(redsea -u -r 192000 2>/dev/null | "
            f'python3 {TUNER_PATH} rbds-meta --dir "{base_dir}"{log_arg} "{mount}")')


# --------------------------------------------------------- device resolution

_RTL_DEVICE_RE = re.compile(r"^\s*(\d+):\s+.*?\bSN:\s*(\S+)", re.IGNORECASE)
_RTL_FOUND_RE = re.compile(r"Found\s+(\d+)\s+device", re.IGNORECASE)


def norm_serial(serial):
    """Lowercase a serial and collapse leading zeros so '1001', '0001001' and
    '00001001' all compare equal."""
    serial = str(serial).strip().lower()
    return str(int(serial)) if serial.isdigit() else serial


def enumerate_devices():
    """Return a list of (index, serial) from rtl_test's startup enumeration.

    rtl_test prints the device list to stderr and then runs forever (it begins
    tuning), so we read merged output line by line and terminate it as soon as
    all advertised devices have been seen (or after a short deadline).
    """
    proc = subprocess.Popen(
        ["rtl_test"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        count = None
        devices = []
        deadline = time.time() + 4.0
        for line in proc.stdout:
            line = line.rstrip("\n")
            m = _RTL_FOUND_RE.match(line)
            if m:
                count = int(m.group(1))
                if count == 0:
                    break
                continue
            m = _RTL_DEVICE_RE.match(line)
            if m:
                devices.append((m.group(1), m.group(2)))
                if count is not None and len(devices) >= count:
                    break
            if time.time() > deadline:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    return devices


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


def build_command(cfg, ice, device_index, eas=False, eas_dir=DEFAULT_DIR,
                  rbds=False, genre="", record=True):
    """Assemble the shell pipeline for one station.

    ``genre`` is the Icecast genre for FM (the RBDS PTY found at startup);
    FM falls back to "Radio" when there is none. WX is always "Weather".
    The recorder tap is added when ``record`` and the station's RECORD are set.
    """
    record = record and bool(cfg.get("record"))
    freq_hz = int(cfg["freq"] * 1_000_000)
    name = _safe_name(cfg["name"])
    genre = WX_GENRE if cfg["band"] == "wx" else (_safe_name(genre) or FM_DEFAULT_GENRE)
    ice_url = (f"icecast://source:{ice['password']}@{ice['host']}:"
               f"{ice['port']}{cfg['mount']}")
    ffmpeg = ("-nostdin -loglevel warning -acodec libmp3lame -b:a 128k -f mp3 "
              f'-ice_name "{name}" '
              f'-ice_genre "{genre}" '
              f"-content_type audio/mpeg {ice_url}")

    if cfg["band"] == "wx":
        cmd = f"rtl_fm -d {device_index} -f {freq_hz} -s 25000 -E deemp -F 9"
        if record:
            cmd += f" | {_record_tee(name, 25000, 1, eas_dir)}"
        if eas:
            cmd += f" | {_eas_tee(name, 25000, 1, eas_dir)}"
        return cmd + f" | ffmpeg -f s16le -ar 25000 -ac 1 -i pipe:0 {ffmpeg}"

    cmd = _fm_source(cfg, device_index)
    if rbds and cfg.get("rbds"):
        log_name = name if record else None
        cmd += f" | {_rbds_tee(cfg['mount'], eas_dir, log_name)}"
    cmd += " | demux -r 192000 -R 48000 -d 75"
    if record:
        cmd += f" | {_record_tee(name, 48000, 2, eas_dir)}"
    if eas:
        cmd += f" | {_eas_tee(name, 48000, 2, eas_dir)}"
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


class ZabbixSender:
    """Minimal Zabbix trapper (zabbix_sender protocol) client, stdlib only.

    Fire-and-forget: a failed or timed-out send is logged and otherwise ignored
    so Zabbix being unreachable can never block or crash tuning.
    """

    def __init__(self, conf):
        self.enabled = str(conf.get("enabled", "false")).lower() in ("1", "true", "yes", "on")
        self.server = conf.get("server", "")
        try:
            self.port = int(conf.get("port", "10051"))
        except ValueError:
            self.port = 10051
        self.hostname = conf.get("hostname", "") or socket.gethostname()
        self.key_event = conf.get("key_event", "pituner.event")
        self.key_status = conf.get("key_status", "pituner.status")
        self.key_active = conf.get("key_active", "pituner.stations_active")
        self.key_heartbeat = conf.get("key_heartbeat", "pituner.heartbeat")
        self.key_eas = conf.get("key_eas", "pituner.eas")
        try:
            self.interval = int(conf.get("interval", "60"))
        except ValueError:
            self.interval = 60
        self.interval = max(10, self.interval)
        self._lock = threading.Lock()

    def _available(self):
        return self.enabled and bool(self.server)

    def send(self, items, mirror=True):
        """items: iterable of (key, value) pairs.

        When mirror is True the payload is also appended to zabbix.log so a
        local copy of everything sent to Zabbix (events only -- the periodic
        heartbeat snapshot passes mirror=False) is kept on disk.
        """
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

    def event(self, message):
        self.send([(self.key_event, message)])

    def snapshot(self, station_statuses):
        status = {name: st for name, st in station_statuses.items()}
        active = sum(1 for st in station_statuses.values() if st == "streaming")
        self.send([
            (self.key_status, json.dumps(status)),
            (self.key_active, active),
            (self.key_heartbeat, 1),
        ], mirror=False)


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
        elif status in ("down", "serial_not_found"):
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
                what = ("its dongle serial was not found" if rec["status"] == "serial_not_found"
                        else "its stream stopped")
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


def run_eas_detector(base_dir, name, sample_rate, channels):
    """Read s16le PCM from stdin and pulse a Zabbix item on the EAS tone.

    Best-effort and non-blocking: a missing Zabbix server is ignored and the
    process exits cleanly on stdin EOF, never stalling the audio pipeline.
    """
    zbx = ZabbixSender(parse_keyvalue(os.path.join(base_dir, "zabbix.conf")))
    mailer = Mailer(parse_keyvalue(os.path.join(base_dir, "smtp.conf")))
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
        if pulsing and now - pulse_start >= EAS_HOLD_SECS:
            if zbx.enabled:
                zbx.send([(zbx.key_eas, 0)])
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
            log_file("eas.log", msg)
            if zbx.enabled:
                zbx.send([
                    (zbx.key_eas, 1),
                    (zbx.key_event, msg),
                ])
            if mailer.alert_eas:
                mailer.send(
                    f"EAS attention tone heard on {name}",
                    f"The EAS attention tone (853 Hz + 960 Hz) was detected on {name} at "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}.\n\n"
                    "This is tone detection only; Pi-Tuner does not decode the SAME message.")
            pulsing = True
            pulse_start = now
            hits = 0
    if pulsing and zbx.enabled:  # pipeline ended mid-pulse: don't leave the item stuck at 1
        zbx.send([(zbx.key_eas, 0)])
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
                  log_name=None, clock=time.time):
    """Read redsea JSON lines from stdin and keep Icecast's now-playing current.

    Best-effort: bad lines and HTTP errors are ignored, and stdin is always
    drained so the audio pipeline is never stalled. Exits cleanly on EOF.
    With ``log_name`` the decoded text is also appended to that station's
    daily RBDS.log (independent of whether the Icecast update succeeds).
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
        song = rbds_song(state, label)
        if not song or song == last_sent:
            continue
        if updater(ice, mount, song):
            last_sent = song
            log_file("rbds.log", f"{mount}: now playing: {song}")
    if logger is not None:
        for entry in logger.finish(now):
            _append_rbds_log(base_dir, log_name, entry)


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
        self.status = "stopped"   # streaming | down | serial_not_found
        self.retry_at = 0.0
        self.backoff = 2.0


# ---------------------------------------------------------------- tuner

class Tuner:
    def __init__(self, base_dir):
        self.dir = base_dir
        self.ice = {"host": "localhost", "port": "8000", "password": "hackme"}
        self.zbx = ZabbixSender({})
        self.mailer = Mailer({})
        self.alerts = AlertManager(self.mailer)
        self.eas_enabled = False
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
        cmd = build_command(st.cfg, self.ice, index,
                            eas=self.eas_enabled, eas_dir=self.dir, rbds=rbds,
                            genre=genre)
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

    def poll(self):
        for st in self.stations:
            if st.proc is not None:
                rc = st.proc.poll()
                if rc is not None:
                    st.proc = None
                    self.set_status(st, "down", f"exit code {rc}")
                    st.retry_at = time.time() + st.backoff
                    st.backoff = min(st.backoff * 2, 60)
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

    def send_heartbeat(self):
        statuses = {st.name: st.status for st in self.stations}
        self.zbx.snapshot(statuses)

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

    def run(self):
        self.load_config()
        self.refresh_devices()
        for st in self.stations:
            self.start_station(st)
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
            self.poll()
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
    parser.add_argument("name", help="station name (for the Zabbix event)")
    parser.add_argument("rate", type=int, help="PCM sample rate in Hz")
    parser.add_argument("channels", type=int, help="audio channel count (1 or 2)")
    args = parser.parse_args(argv)
    run_eas_detector(args.dir, args.name, args.rate, args.channels)
    return 0


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
    parser.add_argument("mount", help="Icecast mount point, e.g. /tuner1")
    args = parser.parse_args(argv)
    run_rbds_meta(args.dir, args.mount, log_name=args.log_name)
    return 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "detect-eas":
        return _main_detect_eas(argv[1:])
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
