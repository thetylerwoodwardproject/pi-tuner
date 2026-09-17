#!/usr/bin/env python3
"""Pi Tuner v2 -- a minimal multi-station SDR streamer.

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
"""
import argparse
import json
import math
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

DEFAULT_DIR = "/opt/pituner"
TUNER_PATH = os.path.realpath(os.path.abspath(__file__))
LOG_DIR = "/var/www/pituner"

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
        mount = _safe_mount(raw.get("mount") or ("/" + os.path.splitext(filename)[0]))

        stations.append({
            "name": name,
            "band": band,
            "freq": freq,
            "serial": serial,
            "gain": raw.get("gain", "").strip(),
            "mount": mount,
            "conf": filename,
        })
    return stations


def build_command(cfg, ice, device_index, eas=False, eas_dir=DEFAULT_DIR):
    """Assemble the shell pipeline for one station."""
    freq_hz = int(cfg["freq"] * 1_000_000)
    name = _safe_name(cfg["name"])
    ice_url = (f"icecast://source:{ice['password']}@{ice['host']}:"
               f"{ice['port']}{cfg['mount']}")
    ffmpeg = ("-nostdin -loglevel warning -acodec libmp3lame -b:a 128k -f mp3 "
              f'-ice_name "{name}" -content_type audio/mpeg {ice_url}')

    if cfg["band"] == "wx":
        cmd = f"rtl_fm -d {device_index} -f {freq_hz} -s 25000 -E deemp -F 9"
        if eas:
            cmd += f" | {_eas_tee(name, 25000, 1, eas_dir)}"
        return cmd + f" | ffmpeg -f s16le -ar 25000 -ac 1 -i pipe:0 {ffmpeg}"

    gain = f"-g {cfg['gain']} " if cfg.get("gain") else ""
    cmd = (f"rtl_fm -d {device_index} -M fm -l 0 -A std -p 0 -s 192000 {gain}"
           f"-F 9 -f {freq_hz} | "
           f"demux -r 192000 -R 48000 -d 75")
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
            zbx.send([
                (zbx.key_eas, 1),
                (zbx.key_event, msg),
            ])
            pulsing = True
            pulse_start = now
            hits = 0
    if pulsing:  # pipeline ended mid-pulse: don't leave the item stuck at 1
        zbx.send([(zbx.key_eas, 0)])


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
        self.eas_enabled = False
        self.stations = []
        self.devices = []
        self._last_scan = 0.0
        self._next_heartbeat = 0.0
        self.reload_requested = False
        self.running = False

    # -- config ----------------------------------------------------------

    def load_config(self):
        ice = parse_keyvalue(os.path.join(self.dir, "icecast.conf"))
        self.ice = {
            "host": ice.get("host", "localhost"),
            "port": ice.get("port", "8000"),
            "password": ice.get("source_password", "hackme"),
        }
        zbx = parse_keyvalue(os.path.join(self.dir, "zabbix.conf"))
        self.zbx = ZabbixSender(zbx)
        eas_on = str(zbx.get("eas_detect", "false")).lower() in ("1", "true", "yes", "on")
        self.eas_enabled = eas_on and self.zbx.enabled
        self.stations = [Station(c) for c in
                         load_stations(os.path.join(self.dir, "stations"))]

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
        cmd = build_command(st.cfg, self.ice, index,
                            eas=self.eas_enabled, eas_dir=self.dir)
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
        self.running = True
        while self.running:
            if self.reload_requested:
                self.reload_requested = False
                self.reload()
                self._next_heartbeat = time.time() + self.zbx.interval
                continue
            self.poll()
            if time.time() >= self._next_heartbeat:
                self.send_heartbeat()
                self._next_heartbeat = time.time() + self.zbx.interval
            time.sleep(1.0)
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
    log(f"zabbix: {'enabled -> ' + tuner.zbx.server + ':' + str(tuner.zbx.port)
                 if tuner.zbx.enabled else 'disabled'}")
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
            cmd = build_command(st.cfg, tuner.ice, index,
                                eas=tuner.eas_enabled, eas_dir=tuner.dir)
            log(f"station {st.name}: device {index} -> {cmd}")
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


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "detect-eas":
        return _main_detect_eas(argv[1:])

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

    log(f"Pi Tuner v2 starting (config: {args.dir})")
    tuner.run()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
