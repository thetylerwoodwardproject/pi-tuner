#!/usr/bin/env python3
"""Interactive configuration menu for Pi-Tuner (``pituner config``).

Edits the same flat KEY=value files you could edit by hand, keeping their
comments, with your current values offered as defaults. Standard library only.
"""
import getpass
import os
import re
import subprocess
import sys

import tuner

CLEAR = "-"   # typed at an optional prompt to clear the setting


def valid_name(value):
    return bool(re.fullmatch(r"[A-Za-z0-9]+(-[A-Za-z0-9]+)?", value)) and len(value) <= 8


def valid_band(value):
    return value.lower() in ("fm", "wx")


def valid_freq(value):
    try:
        return 80.0 <= float(value) <= 170.0
    except ValueError:
        return False


def valid_port(value):
    return value.isdigit() and 1 <= int(value) <= 65535


def valid_uint(value):
    return value.isdigit()


def valid_interval(value):
    return value.isdigit() and int(value) >= 10


def valid_float(value):
    try:
        return float(value) >= 0
    except ValueError:
        return False


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# (key, label, kind, validator, error)
ICECAST_FIELDS = [
    ("HOST", "Icecast host", "text", None, ""),
    ("PORT", "Icecast port", "text", valid_port, "Enter a port number (1-65535)."),
    ("SOURCE_PASSWORD", "Source password", "secret", None, ""),
    ("ADMIN_USER", "Admin user (for now-playing updates, optional)", "text", None, ""),
    ("ADMIN_PASSWORD", "Admin password (optional, replaces the source login)", "secret", None, ""),
]
ZABBIX_FIELDS = [
    ("ENABLED", "Send alerts to Zabbix", "bool", None, ""),
    ("SERVER", "Zabbix server address", "text", None, ""),
    ("PORT", "Zabbix trapper port", "text", valid_port, "Enter a port number (1-65535)."),
    ("HOSTNAME", "Zabbix host name", "text", None, ""),
    ("INTERVAL", "Heartbeat interval, seconds", "text", valid_interval, "Enter a number, 10 or more."),
    ("EAS_DETECT", "Detect the EAS attention tone", "bool", None, ""),
    ("LEVEL_MONITOR", "Send each tuner's audio level (for dead-air alerts)", "bool", None, ""),
]
EMAIL_FIELDS = [
    ("ENABLED", "Send email alerts", "bool", None, ""),
    ("HOST", "SMTP server", "text", None, ""),
    ("PORT", "SMTP port", "text", valid_port, "Enter a port number (1-65535)."),
    ("SECURITY", "Security (starttls, ssl or none)", "choice:starttls,ssl,none", None, ""),
    ("VERIFY_TLS", "Verify the server's TLS certificate", "bool", None, ""),
    ("USERNAME", "SMTP username (blank = no login)", "text", None, ""),
    ("PASSWORD", "SMTP password", "secret", None, ""),
    ("FROM", "From address", "text", None, ""),
    ("TO", "Send alerts to (comma-separated)", "text", None, ""),
    ("ALERT_STATION", "Email when a station goes down / recovers", "bool", None, ""),
    ("ALERT_EAS", "Email on the EAS attention tone", "bool", None, ""),
    ("ALERT_DISK", "Email when recording disk space is low", "bool", None, ""),
    ("ALERT_SERVICE", "Email when the service starts / stops", "bool", None, ""),
    ("DOWN_DELAY", "Seconds down before the 'down' email", "text", valid_uint, "Enter a whole number."),
    ("DISK_MIN_GB", "Low disk space threshold, GB", "text", valid_float, "Enter a number."),
]


class Menu:
    def __init__(self, base_dir, inp=input, secret=getpass.getpass, out=print,
                 runner=subprocess.run, detect=tuner.enumerate_devices):
        self.dir = base_dir
        self.inp, self.secret, self.out = inp, secret, out
        self.runner, self.detect = runner, detect
        self.changed = False
        self._backed_up = False

    # -- prompts ----------------------------------------------------------

    def ask(self, label, default="", validator=None, error="Not valid, try again.",
            allow_clear=False):
        """Prompt with the current value as the default. Enter keeps it."""
        shown = f" [{default}]" if default else ""
        hint = f" ({CLEAR} to clear)" if allow_clear and default else ""
        while True:
            answer = self.inp(f"  {label}{shown}{hint}: ").strip()
            if answer == "":
                return default
            if allow_clear and answer == CLEAR:
                return ""
            if validator is None or validator(answer):
                return answer
            self.out(f"  {error}")

    def ask_bool(self, label, default):
        shown = "Y/n" if default else "y/N"
        while True:
            answer = self.inp(f"  {label}? [{shown}]: ").strip().lower()
            if answer == "":
                return default
            if answer in ("y", "yes", "true"):
                return True
            if answer in ("n", "no", "false"):
                return False
            self.out("  Please answer y or n.")

    def ask_secret(self, label, current):
        state = "set" if current else "not set"
        answer = self.secret(f"  {label} [{state}; Enter keeps it, {CLEAR} clears]: ")
        if answer == "":
            return current
        return "" if answer == CLEAR else answer

    def ask_choice(self, label, options, default):
        while True:
            answer = self.inp(f"  {label} [{default}]: ").strip().lower()
            if answer == "":
                return default
            if answer in options:
                return answer
            self.out(f"  Choose one of: {', '.join(options)}.")

    # -- files ------------------------------------------------------------

    def _backup_once(self):
        if not self._backed_up:
            self._backed_up = True
            dest = tuner.backup_config(self.dir)
            if dest:
                self.out(f"  (Backed up your settings to {dest} first.)")

    def _path(self, name):
        return os.path.join(self.dir, name)

    def _ensure(self, name):
        path = self._path(name)
        if not os.path.isfile(path):
            tuner._atomic_write(path, tuner.render_config(name), mode=0o600)
            try:
                st = os.stat(self.dir)
                os.chown(path, st.st_uid, st.st_gid)
            except OSError:
                pass
        return path

    def _save(self, path, changes):
        if not changes:
            return False
        self._backup_once()
        if tuner.update_keyvalue(path, changes):
            self.changed = True
            return True
        return False

    def edit_file(self, name, fields):
        path = self._ensure(name)
        conf = tuner.parse_keyvalue(path)
        specs = {sp[0]: sp for sp in tuner.CONFIG_KEYS[name]}
        changes = {}
        for key, label, kind, validator, error in fields:
            current = conf.get(key.lower(), specs[key][1])
            if kind == "bool":
                value = "true" if self.ask_bool(label, truthy(current)) else "false"
            elif kind == "secret":
                value = self.ask_secret(label, current)
            elif kind.startswith("choice:"):
                value = self.ask_choice(label, kind.split(":", 1)[1].split(","), current or "starttls")
            else:
                value = self.ask(label, current, validator, error or "Not valid, try again.",
                                 allow_clear=not specs[key][2] or key in ("USERNAME", "FROM"))
            if value != current:
                changes[key] = value
        if self._save(path, changes):
            self.out(f"  Saved {name}.")
        else:
            self.out("  No changes.")

    # -- stations ---------------------------------------------------------

    def _station_files(self):
        folder = self._path("stations")
        if not os.path.isdir(folder):
            return []
        return [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.endswith(".conf")]

    def _loaded(self):
        return tuner.load_stations(self._path("stations"))

    def _show_stations(self):
        files = self._station_files()
        loaded = {s["conf"]: s for s in self._loaded()}
        if not files:
            self.out("  (no stations configured)")
        for i, path in enumerate(files, 1):
            raw = tuner.parse_keyvalue(path)
            st = loaded.get(os.path.basename(path))
            mount = st["mount"] if st else "(invalid, skipped)"
            flags = ", ".join(x for x, on in (("RBDS", st and st["rbds"]),
                                              ("record", st and st["record"])) if on)
            self.out(f"  {i}) {raw.get('name', os.path.basename(path))}  "
                     f"{raw.get('band', 'fm')} {raw.get('frequency', '?')} MHz  "
                     f"serial {raw.get('serial') or '-'}  {mount}"
                     + (f"  [{flags}]" if flags else ""))
        return files

    def _pick_serial(self, current):
        try:
            devices = self.detect()
        except Exception:  # noqa: BLE001 - rtl_test missing or failing is fine
            devices = []
        if devices:
            self.out("  Detected dongles:")
            for idx, serial in devices:
                self.out(f"    {idx}: serial {serial}")
        answer = self.ask("Dongle serial (type it, or the number above to use that dongle)", current)
        for idx, serial in devices:
            if answer == idx and answer != current:
                return serial
        return answer

    def _station_form(self, current, is_new):
        band = self.ask_choice("Band (fm or wx)", ["fm", "wx"], (current.get("band") or "fm").lower())
        name = self.ask("Station name (call sign, up to 8 characters)", current.get("name", ""),
                        valid_name, "Use 1-8 letters/digits with an optional hyphen, e.g. WXYZ or WXYZ-FM.")
        while not name:
            self.out("  A station name is required.")
            name = self.ask("Station name (call sign, up to 8 characters)", "", valid_name,
                            "Use 1-8 letters/digits with an optional hyphen, e.g. WXYZ or WXYZ-FM.")
        if valid_name(name) and name != current.get("name"):
            name = name.upper()
        freq_default = current.get("frequency") or ("162.55" if band == "wx" else "98.1")
        freq = self.ask("Frequency in MHz (FM e.g. 98.1, WX e.g. 162.55)", freq_default,
                        valid_freq, "Enter a number between 80 and 170.")
        serial = self._pick_serial(current.get("serial", ""))
        while not serial:
            self.out("  A dongle serial is required.")
            serial = self._pick_serial("")
        gain = self.ask("Gain in dB (blank = auto)", current.get("gain", ""),
                        valid_float, "Enter a number.", allow_clear=True)
        values = {"NAME": name, "BAND": band, "FREQUENCY": freq, "SERIAL": serial, "GAIN": gain}
        if band == "fm":
            values["RBDS"] = "true" if self.ask_bool(
                "Send RBDS text and genre to Icecast now-playing", truthy(current.get("rbds", "false"))) else "false"
        record = self.ask_bool("Record this station (15-minute MP3 files)", truthy(current.get("record", "false")))
        values["RECORD"] = "true" if record else "false"
        if record:
            values["RECORD_KEEP_DAYS"] = self.ask(
                "Days of recordings to keep (blank = keep everything)",
                current.get("record_keep_days", ""), valid_uint, "Enter a whole number.", allow_clear=True)
        return values

    def _new_station_text(self, values):
        lines = ["# Pi-Tuner station", "#", "# ─── user settings ─────────────────────────────────"]
        for key in ("NAME", "BAND", "FREQUENCY", "SERIAL", "GAIN", "MOUNT", "RBDS", "RECORD",
                    "RECORD_KEEP_DAYS"):
            if values.get(key, "") != "":
                lines.append(f"{key}={tuner.format_conf_value(values[key])}")
        lines.append("# ─── end user settings ─────────────────────────────")
        return "\n".join(lines) + "\n"

    def add_station(self):
        values = self._station_form({}, True)
        used = {s["mount"] for s in self._loaded()}
        k = 1
        while f"/tuner{k}" in used:
            k += 1
        values["MOUNT"] = f"/tuner{k}"
        folder = self._path("stations")
        os.makedirs(folder, exist_ok=True)
        n = 1
        while os.path.exists(os.path.join(folder, f"station{n}.conf")):
            n += 1
        path = os.path.join(folder, f"station{n}.conf")
        self._backup_once()
        tuner._atomic_write(path, self._new_station_text(values), mode=0o644)
        try:
            st = os.stat(self.dir)
            os.chown(path, st.st_uid, st.st_gid)
        except OSError:
            pass
        self.changed = True
        self.out(f"  Added {values['NAME']} as {values['MOUNT']} ({os.path.basename(path)}).")

    def edit_station(self, path):
        raw = tuner.parse_keyvalue(path)
        values = self._station_form(raw, False)
        # a setting that's absent from the file already means its default
        implicit = {"RBDS": "false", "RECORD": "false"}
        changes = {k: v for k, v in values.items()
                   if v != raw.get(k.lower(), implicit.get(k, ""))}
        # a cleared optional setting is written as an empty value, so drop it instead
        clears = [k for k, v in changes.items() if v == "" and k in ("GAIN", "RECORD_KEEP_DAYS")]
        for key in clears:
            del changes[key]
        if self._save(path, changes) or clears:
            for key in clears:
                self._backup_once()
                if self._drop_key(path, key):
                    self.changed = True
            self.out("  Saved.")
        else:
            self.out("  No changes.")

    def _drop_key(self, path, key):
        lines = tuner._read_lines(path)
        pat = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
        kept = [line for line in lines if not pat.match(line)]
        if kept == lines:
            return False
        tuner._atomic_write(path, "\n".join(kept) + "\n")
        return True

    def remove_station(self, path):
        raw = tuner.parse_keyvalue(path)
        if not self.ask_bool(f"Remove station {raw.get('name', os.path.basename(path))}", False):
            return
        before = {s["conf"]: s["mount"] for s in self._loaded()}
        self._backup_once()
        os.remove(path)
        after = {s["conf"]: s["mount"] for s in self._loaded()}
        for other in self._station_files():
            base = os.path.basename(other)
            if "mount" not in tuner.parse_keyvalue(other) and base in before \
                    and before[base] != after.get(base):
                tuner.update_keyvalue(other, {"MOUNT": before[base]})   # keep its URL stable
        self.changed = True
        self.out("  Removed. Other stations keep their stream URLs.")

    def stations_menu(self):
        while True:
            self.out("\nStations")
            files = self._show_stations()
            choice = self.inp("  (a)dd, (e)dit N, (r)emove N, (b)ack: ").strip().lower()
            if choice in ("", "b", "back"):
                return
            if choice in ("a", "add"):
                self.add_station()
                continue
            m = re.fullmatch(r"([er])\w*\s+(\d+)", choice)
            if m and 1 <= int(m.group(2)) <= len(files):
                path = files[int(m.group(2)) - 1]
                (self.edit_station if m.group(1) == "e" else self.remove_station)(path)
            else:
                self.out("  Type a, e N, r N or b (for example: e 1).")

    # -- email test, apply ------------------------------------------------

    def test_email(self):
        mailer = tuner.Mailer(tuner.parse_keyvalue(self._path("smtp.conf")))
        if not mailer.enabled:
            self.out(f"  Email isn't enabled yet: {mailer.problem()}.")
            return
        try:
            mailer.send_sync("Test email", "This is a test message from Pi-Tuner.")
        except Exception as e:  # noqa: BLE001 - show the user whatever went wrong
            self.out(f"  Test email FAILED: {type(e).__name__}: {e}")
        else:
            self.out(f"  Test email sent to {', '.join(mailer.to)}.")

    def email_menu(self):
        self.out("\nEmail alerts (Enter keeps the value shown)")
        self.edit_file("smtp.conf", EMAIL_FIELDS)
        if self.ask_bool("Send a test email now", False):
            self.test_email()

    def apply(self):
        """Offer to reload the running service so changes take effect."""
        if not self.changed:
            return
        if not self.ask_bool("Reload the Pi-Tuner service now so the changes take effect", True):
            self.out("  Later, run: sudo systemctl reload pituner")
            return
        try:
            result = self.runner(["systemctl", "reload", "pituner.service"], check=False)
            ok = getattr(result, "returncode", 0) == 0
        except OSError:
            ok = False
        self.out("  Reloaded; stations are restarting." if ok else
                 "  Couldn't reload automatically. Run: sudo systemctl reload pituner")

    # -- main loop --------------------------------------------------------

    def run(self):
        if not os.access(self.dir, os.W_OK):
            self.out(f"Can't write to {self.dir}. Run this with sudo.")
            return 1
        self.out(f"Pi-Tuner configuration ({self.dir})")
        while True:
            self.out("\n  1) Stations\n  2) Icecast connection\n  3) Zabbix\n"
                     "  4) Email alerts\n  q) Quit")
            choice = self.inp("Choose: ").strip().lower()
            if choice == "1":
                self.stations_menu()
            elif choice == "2":
                self.out("\nIcecast connection (this edits Pi-Tuner's copy; it must match "
                         "/etc/icecast2/icecast.xml)")
                self.edit_file("icecast.conf", ICECAST_FIELDS)
            elif choice == "3":
                self.out("\nZabbix (Enter keeps the value shown)")
                self.edit_file("zabbix.conf", ZABBIX_FIELDS)
            elif choice == "4":
                self.email_menu()
            elif choice in ("q", "quit", "exit", ""):
                break
            else:
                self.out("  Choose 1-4 or q.")
        self.apply()
        return 0


def main(base_dir):
    try:
        return Menu(base_dir).run()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Anything already saved is kept.")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else tuner.DEFAULT_DIR))
