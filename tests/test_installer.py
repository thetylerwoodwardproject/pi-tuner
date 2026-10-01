import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from test_config_edit import OLD_STATION, OLD_ZABBIX, read, write  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL = read(os.path.join(REPO, "install.sh"))

STUBS = r'''
set -u
step() { echo "STEP $*"; }
info() { echo "INFO $*"; }
ok()   { echo "OK $*"; }
warn() { echo "WARN $*"; }
fail() { echo "FAIL $*"; }
die()  { echo "DIE $*"; exit 1; }
ask()  { printf '%s' "${ASK_ANSWER:-$2}"; }
confirm() { [ "${CONFIRM_ANSWER:-y}" = "y" ]; }
chown() { :; }
sleep() { :; }
systemctl() { echo "SYSTEMCTL $*" >> "$CALLS"; }
curl() { return 0; }
gen_pass() { echo GENERATED; echo "gen_pass called" >> "$CALLS"; }
'''


def block(start, end):
    """Text of install.sh from the line containing `start` up to (not including) `end`."""
    a = INSTALL.index(start)
    b = INSTALL.index(end, a)
    return INSTALL[a:b]


def run_bash(script, env=None):
    full_env = dict(os.environ, **(env or {}))
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full_env)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.app = os.path.join(self.d, "pituner")
        self.calls = os.path.join(self.d, "calls.txt")
        open(self.calls, "w").close()

    def existing_install(self):
        write(os.path.join(self.app, "tuner.py"), "# old\n")
        write(os.path.join(self.app, "icecast.conf"),
              "HOST=localhost\nPORT=8000\nSOURCE_PASSWORD=keepme\n", 0o600)
        write(os.path.join(self.app, "zabbix.conf"), OLD_ZABBIX, 0o600)
        write(os.path.join(self.app, "stations", "fm-example.conf"),
              OLD_STATION.replace("NAME=WXTB", "NAME=MYSTN"))

    def calls_text(self):
        return read(self.calls)


class ModeTests(Base):
    MODE_BLOCK = block("# ------------------------------------------------------------- install mode",
                       "clear 2>/dev/null || true")

    def mode(self, args="", interactive=0, ask="", confirm="y", env=None):
        script = (STUBS + f'APP_DIR="{self.app}"; SRC="{REPO}"; INTERACTIVE={interactive}\n'
                  f'ASK_ANSWER="{ask}"; CONFIRM_ANSWER="{confirm}"; CALLS="{self.calls}"\n'
                  f'set -- {args}\n' + self.MODE_BLOCK +
                  'echo "RESULT mode=$MODE keep=$KEEP_SETTINGS existing=$EXISTING"\n'
                  'echo "BACKUP=$BACKUP_NOTE"\n')
        r = run_bash(script, env)
        m = re.search(r"RESULT (.*)", r.stdout)
        return r, (m.group(1) if m else None)

    def test_first_install_is_not_an_upgrade(self):
        r, res = self.mode()
        self.assertEqual(res, "mode=first keep=0 existing=0")
        self.assertFalse(os.path.exists(os.path.join(self.app, "backups")))

    def test_non_interactive_over_existing_install_upgrades_and_backs_up(self):
        self.existing_install()
        r, res = self.mode()
        self.assertEqual(res, "mode=upgrade keep=1 existing=1", r.stdout + r.stderr)
        backups = os.listdir(os.path.join(self.app, "backups"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(read(os.path.join(self.app, "backups", backups[0], "icecast.conf")),
                         "HOST=localhost\nPORT=8000\nSOURCE_PASSWORD=keepme\n")
        self.assertIn("Backed up settings", r.stdout)

    def test_interactive_choices(self):
        self.existing_install()
        for answer, expected in (("", "upgrade"), ("1", "upgrade"), ("2", "reconfigure"),
                                 ("3", "fresh"), ("x", "upgrade")):
            r, res = self.mode(interactive=1, ask=answer)
            self.assertIn(f"mode={expected} ", res + " ", answer)

    def test_flags_and_env_choose_the_mode(self):
        self.existing_install()
        for flag, expected in (("--upgrade", "upgrade"), ("--reconfigure", "reconfigure"),
                               ("--fresh", "fresh")):
            r, res = self.mode(args=flag)
            self.assertIn(f"mode={expected} ", res + " ")
        r, res = self.mode(env={"PITUNER_MODE": "reconfigure"})
        self.assertIn("mode=reconfigure ", res + " ")

    def test_fresh_needs_confirmation_interactively_and_stops_if_declined(self):
        self.existing_install()
        r, res = self.mode(interactive=1, ask="3", confirm="n")
        self.assertIsNone(res)
        self.assertIn("DIE Cancelled", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.app, "backups")))   # nothing touched

    def test_unknown_option_or_mode_is_rejected(self):
        self.existing_install()
        r, res = self.mode(args="--bogus")
        self.assertIsNone(res)
        self.assertIn("Unknown option", r.stdout)
        r, res = self.mode(env={"PITUNER_MODE": "nonsense"})
        self.assertIsNone(res)
        self.assertIn("Unknown mode", r.stdout)

    def test_conf_files_alone_count_as_an_existing_install(self):
        write(os.path.join(self.app, "stations", "s.conf"), OLD_STATION)
        r, res = self.mode()
        self.assertEqual(res, "mode=upgrade keep=1 existing=1")


class IcecastKeepTests(Base):
    def run_step(self, keep=1, xml=None):
        xml_path = os.path.join(self.d, "icecast.xml")
        if xml is not None:
            write(xml_path, xml)
        text = block('step "3 of 8: Configure Icecast"', "# ------------------------------------------------------------- app install")
        text = text.replace('ICECAST_XML="/etc/icecast2/icecast.xml"', f'ICECAST_XML="{xml_path}"')
        script = (STUBS + f'APP_DIR="{self.app}"; INTERACTIVE=0; KEEP_SETTINGS={keep}; CALLS="{self.calls}"\n'
                  + text + 'echo "SOURCE_PASS=$SOURCE_PASS"\n')
        return run_bash(script), xml_path

    def sha(self, path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    XML = "<icecast><authentication><source-password>fromxml</source-password>" \
          "<admin-password>adminpw</admin-password></authentication></icecast>\n"

    def test_upgrade_keeps_password_and_never_edits_icecast_xml(self):
        self.existing_install()
        before = None
        r, xml = self.run_step(xml=self.XML)
        before = self.sha(xml)
        r, xml = self.run_step()
        self.assertIn("SOURCE_PASS=keepme", r.stdout)
        self.assertEqual(self.sha(xml), before)
        self.assertNotIn("gen_pass", self.calls_text())
        self.assertNotIn("restart", self.calls_text())       # Icecast isn't restarted either

    def test_missing_icecast_conf_falls_back_to_the_password_in_icecast_xml(self):
        self.existing_install()
        os.remove(os.path.join(self.app, "icecast.conf"))
        r, _ = self.run_step(xml=self.XML)
        self.assertIn("SOURCE_PASS=fromxml", r.stdout)
        self.assertNotIn("gen_pass", self.calls_text())

    def test_fresh_still_resets_icecast(self):
        r, xml = self.run_step(keep=0, xml=self.XML)
        self.assertIn("gen_pass called", self.calls_text())
        self.assertIn("SOURCE_PASS=GENERATED", r.stdout)
        self.assertIn("<source-password>GENERATED</source-password>", read(xml))


class StationsKeepTests(Base):
    def test_existing_stations_are_not_overwritten_by_examples(self):
        self.existing_install()
        text = block('step "6 of 8: Configure stations"', "write_station() {")
        script = (STUBS + f'APP_DIR="{self.app}"; SRC="{REPO}"; KEEP_SETTINGS=1\n' + text)
        r = run_bash(script)
        self.assertIn("Keeping your existing stations", r.stdout)
        self.assertIn("NAME=MYSTN", read(os.path.join(self.app, "stations", "fm-example.conf")))

    def test_examples_are_deployed_when_there_are_no_stations(self):
        os.makedirs(os.path.join(self.app, "stations"))
        text = block('step "6 of 8: Configure stations"', "write_station() {")
        script = (STUBS + f'APP_DIR="{self.app}"; SRC="{REPO}"; KEEP_SETTINGS=1\n' + text)
        run_bash(script)
        self.assertTrue(os.path.isfile(os.path.join(self.app, "stations", "fm-example.conf")))


class AlertsKeepTests(Base):
    def test_upgrade_keeps_alert_settings_and_adds_new_keys(self):
        self.existing_install()
        # by step 7 the installer has already put the new tuner.py in place
        shutil.copy(os.path.join(REPO, "tuner.py"), os.path.join(self.app, "tuner.py"))
        text = block('step "7 of 8: Zabbix and email alerts (optional)"',
                     "# ------------------------------------------------------------- service")
        script = (STUBS + f'APP_DIR="{self.app}"; SRC="{REPO}"; KEEP_SETTINGS=1; INTERACTIVE=0\n'
                  'chmod() { command chmod "$@"; }\nchown() { :; }\n'
                  + text + 'echo "FLAGS zabbix=$ZABBIX_ENABLED email=$EMAIL_ENABLED"\n')
        r = run_bash(script)
        self.assertIn("FLAGS zabbix=true email=false", r.stdout, r.stdout + r.stderr)
        zabbix = read(os.path.join(self.app, "zabbix.conf"))
        self.assertIn("SERVER=zbx.internal", zabbix)
        self.assertIn("KEY_EAS=pituner.eas", zabbix)
        self.assertIn("EAS_DETECT=false", zabbix)        # existing installs keep EAS off
        self.assertTrue(os.path.isfile(os.path.join(self.app, "smtp.conf")))
        self.assertIn("Added to zabbix.conf", r.stdout)


class StaticTests(unittest.TestCase):
    def test_scripts_parse(self):
        for name in ("install.sh", "uninstall.sh", "pituner"):
            shell = "sh" if name == "pituner" else "bash"
            r = subprocess.run([shell, "-n", os.path.join(REPO, name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_new_installs_write_the_eas_keys(self):
        self.assertEqual(INSTALL.count("EAS_DETECT=true"), 2)
        self.assertEqual(INSTALL.count("KEY_EAS=pituner.eas"), 2)

    def test_unit_supports_reload(self):
        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID",
                      read(os.path.join(REPO, "pituner.service")))

    def test_installer_ships_the_menu_and_command(self):
        self.assertIn('install -m 644 "${SRC}/configure.py"', INSTALL)
        self.assertIn('install -m 755 "${SRC}/pituner" /usr/local/bin/pituner', INSTALL)
        self.assertTrue(os.access(os.path.join(REPO, "pituner"), os.X_OK))
        self.assertIn("/usr/local/bin/pituner", read(os.path.join(REPO, "uninstall.sh")))


class WrapperTests(unittest.TestCase):
    def test_unknown_and_missing_commands_show_usage(self):
        for args in ([], ["bogus"]):
            r = subprocess.run([os.path.join(REPO, "pituner"), *args], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn("pituner config", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
