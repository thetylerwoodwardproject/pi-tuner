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


class IcecastMissingXmlTests(IcecastKeepTests):
    """icecast.xml deleted (e.g. the old Icecast was purged): the installer must recreate it."""

    def backup(self):
        return os.path.join(self.app, "icecast.xml.backup")

    def test_fresh_creates_a_complete_config_when_missing(self):
        r, xml = self.run_step(keep=0)
        text = read(xml)
        self.assertIn("<source-password>GENERATED</source-password>", text)
        self.assertIn("<admin-password>GENERATED</admin-password>", text)
        self.assertIn("<port>8000</port>", text)
        self.assertIn("<sources>20</sources>", text)
        self.assertTrue(os.path.exists(self.backup()))
        self.assertEqual(read(self.backup()), text)

    def test_upgrade_creates_config_with_the_existing_password_from_icecast_conf(self):
        self.existing_install()
        r, xml = self.run_step(keep=1)
        self.assertIn("<source-password>keepme</source-password>", read(xml))
        self.assertIn("SOURCE_PASS=keepme", r.stdout)
        self.assertIn("SYSTEMCTL restart icecast2", self.calls_text())   # must be restarted to load it

    def test_upgrade_restores_from_the_opt_backup_with_the_current_password(self):
        self.existing_install()
        write(self.backup(), self.XML.replace("fromxml", "stalepw"))
        r, xml = self.run_step(keep=1)
        text = read(xml)
        self.assertIn("<source-password>keepme</source-password>", text)
        self.assertIn("<admin-password>adminpw</admin-password>", text)
        self.assertIn("Restored", r.stdout)

    def test_upgrade_with_no_password_anywhere_generates_one_and_updates_icecast_conf(self):
        self.existing_install()
        write(os.path.join(self.app, "icecast.conf"), "HOST=localhost\nPORT=8000\nSOURCE_PASSWORD=\n", 0o600)
        r, xml = self.run_step(keep=1)
        self.assertIn("<source-password>GENERATED</source-password>", read(xml))
        self.assertIn("SOURCE_PASSWORD=GENERATED", read(os.path.join(self.app, "icecast.conf")))

    def test_working_config_is_backed_up_next_to_the_app(self):
        self.existing_install()
        r, xml = self.run_step(keep=1, xml=self.XML)
        self.assertEqual(read(self.backup()), self.XML)
        self.assertEqual(oct(os.stat(self.backup()).st_mode & 0o777), "0o600")


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


class WebShareTests(Base):
    BLOCK = block("# ------------------------------------------------------------- recordings web listing",
                  '# ------------------------------------------------------------- service')

    def share(self, interactive=1, confirm="y", ask_answers=("8080", ""), nginx_ok=True,
              have_nginx=True, existing=None, env=""):
        self.conf = os.path.join(self.d, "conf.d", "pituner-recordings.conf")
        if existing is not None:
            write(self.conf, existing)
        port_ans, net_ans = ask_answers
        stubs = STUBS.replace("ask()  { printf '%s' \"${ASK_ANSWER:-$2}\"; }",
                              "ask()  { case \"$1\" in *Port*) printf '%s' \"${PORT_ANS:-$2}\";; "
                              "*network*) printf '%s' \"${NET_ANS:-$2}\";; *) printf '%s' \"$2\";; esac; }")
        stubs += ("nginx() { echo \"nginx $*\" >> \"$CALLS\"; return %d; }\n" % (0 if nginx_ok else 1)
                  + 'apt-get() { echo "apt-get $*" >> "$CALLS"; }\n'
                  + 'hostname() { echo pituner; }\n' + "sysctl() { return 1; }\n"
                  + ("" if have_nginx else
                     'command() { if [ "$1" = -v ] && [ "$2" = nginx ]; then return 1; fi; builtin command "$@"; }\n'))
        script = (stubs + f'APP_DIR="{self.app}"; INTERACTIVE={interactive}; CALLS="{self.calls}"\n'
                  f'CONFIRM_ANSWER="{confirm}"; WEB_CONF="{self.conf}"; PORT_ANS="{port_ans}"; NET_ANS="{net_ans}"\n'
                  + env + self.BLOCK)
        os.makedirs(self.app, exist_ok=True)
        return run_bash(script)

    def conf_text(self):
        return read(self.conf) if os.path.exists(self.conf) else ""

    def test_opt_in_serves_only_the_recordings_folder_read_only(self):
        r = self.share()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = self.conf_text()
        self.assertIn("listen 8080;", text)
        self.assertIn(f"root {self.app}/recordings;", text)
        self.assertIn("autoindex on;", text)
        self.assertIn("limit_except GET HEAD { deny all; }", text)
        self.assertIn(r"location ~ /\. { deny all; }", text)
        self.assertNotIn("allow ", text.replace("deny all", ""))          # no network limit asked for
        self.assertIn("nginx -t", self.calls_text())
        self.assertIn("SYSTEMCTL reload nginx", self.calls_text())
        self.assertIn("http://pituner:8080/", r.stdout)
        self.assertNotIn("apt-get", self.calls_text())                    # nginx already installed

    def test_ipv6_listener_only_where_the_kernel_has_ipv6(self):
        has_v6 = os.path.join(self.d, "if_inet6")
        write(has_v6, "fe80...\n")
        self.share(env=f'PROC_NET_INET6="{has_v6}"\n')
        self.assertIn("listen [::]:8080;", self.conf_text())
        os.remove(self.conf)
        self.share(env=f'PROC_NET_INET6="{self.d}/missing"\n')
        self.assertNotIn("[::]", self.conf_text())          # nginx would refuse the config

    def site_env(self):
        self.sites = os.path.join(self.d, "sites-enabled")
        write(os.path.join(self.sites, "default"), "server { listen 80 default_server; }\n")
        return f'NGINX_SITES_ENABLED="{self.sites}"\n'

    def test_fresh_nginx_gets_port_80_and_loses_the_welcome_site(self):
        env = self.site_env()
        r = self.share(have_nginx=False, ask_answers=("", ""), env=env)       # accept the default port
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("listen 80;", self.conf_text())
        self.assertFalse(os.path.exists(os.path.join(self.sites, "default")))
        self.assertIn("http://pituner/", r.stdout)                            # no :port on 80
        self.assertIn("apt-get install -y nginx", self.calls_text())

    def test_existing_nginx_defaults_to_8080_and_keeps_its_default_site(self):
        env = self.site_env()
        self.share(ask_answers=("", ""), env=env)
        self.assertIn("listen 8080;", self.conf_text())
        self.assertTrue(os.path.exists(os.path.join(self.sites, "default")))

    def test_existing_nginx_with_port_80_chosen_never_touches_its_sites(self):
        env = self.site_env()
        self.share(ask_answers=("80", ""), env=env)
        self.assertIn("listen 80;", self.conf_text())
        self.assertTrue(os.path.exists(os.path.join(self.sites, "default")))

    def test_explicit_port_overrides_the_existing_listing_on_refresh(self):
        self.share(ask_answers=("8081", ""))
        r = self.share(interactive=0, existing=self.conf_text(), env="PITUNER_WEB_PORT=80\n")
        text = self.conf_text()
        self.assertIn("listen 80;", text)
        self.assertNotIn("8081", text)

    def test_logs_are_served_as_plain_text_only_when_asked(self):
        self.share(confirm="n")                       # declined entirely
        self.assertEqual(self.conf_text(), "")
        self.share(interactive=0, env='PITUNER_WEB_RECORDINGS=1; PITUNER_WEB_PORT=8085\n')
        self.assertNotIn("/logs/", self.conf_text())  # recordings only by default
        os.remove(self.conf)
        self.share(interactive=0, env='PITUNER_WEB_RECORDINGS=1; PITUNER_WEB_LOGS=1; LOG_DIR="/var/log/pituner"\n')
        text = self.conf_text()
        self.assertIn("location /logs/ {", text)
        self.assertIn("alias /var/log/pituner/;", text)
        self.assertIn("types { text/plain log; }", text)
        self.assertIn("default_type application/octet-stream;", text)
        self.assertEqual(text.count("limit_except GET HEAD { deny all; }"), 2)

    def test_network_limit_sits_at_server_level_so_the_logs_inherit_it(self):
        self.share(interactive=0, env='PITUNER_WEB_RECORDINGS=1; PITUNER_WEB_LOGS=1; '
                                      'PITUNER_WEB_ALLOW=192.168.1.0/24\n')
        text = self.conf_text()
        self.assertLess(text.index("allow 192.168.1.0/24;"), text.index("location / {"))
        self.assertLess(text.index("deny all;"), text.index("location / {"))

    def test_refresh_keeps_the_logs_listing(self):
        self.share(interactive=0, env='PITUNER_WEB_RECORDINGS=1; PITUNER_WEB_LOGS=1\n')
        self.share(interactive=0, existing=self.conf_text())
        self.assertIn("location /logs/ {", self.conf_text())

    def test_upgrade_offers_the_logs_listing_to_an_existing_recordings_listing(self):
        self.share(interactive=0, env='PITUNER_WEB_RECORDINGS=1; PITUNER_WEB_PORT=80\n')
        before = self.conf_text()
        self.assertNotIn("/logs/", before)
        self.share(interactive=1, confirm="n", existing=before)               # declined: unchanged
        self.assertNotIn("/logs/", self.conf_text())
        self.assertIn("listen 80;", self.conf_text())
        self.share(interactive=1, confirm="y", existing=before)               # accepted
        text = self.conf_text()
        self.assertIn("location /logs/ {", text)
        self.assertIn("listen 80;", text)                                     # port kept
        r = self.share(interactive=1, confirm="n", existing=text)             # already on: not asked, stays on
        self.assertIn("location /logs/ {", self.conf_text())

    def test_existing_logs_are_scrubbed_of_the_icecast_password_before_publishing(self):
        import gzip
        logs = os.path.join(self.d, "logs")
        leaky = "[t] station A: device 0 -> rtl_fm | ffmpeg icecast://source:S3cr3tPw@localhost:8000/tuner1\n"
        write(os.path.join(logs, "tuner.log"), leaky + "keep this line\n")
        with gzip.open(os.path.join(logs, "rbds.log.1.gz"), "wt") as f:
            f.write(leaky)
        r = self.share(interactive=0, env=f'PITUNER_WEB_RECORDINGS=1; PITUNER_WEB_LOGS=1; LOG_DIR="{logs}"\n')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = read(os.path.join(logs, "tuner.log"))
        self.assertNotIn("S3cr3tPw", text)
        self.assertIn("icecast://source:***@localhost:8000/tuner1", text)
        self.assertIn("keep this line", text)
        with gzip.open(os.path.join(logs, "rbds.log.1.gz"), "rt") as f:
            gz = f.read()
        self.assertNotIn("S3cr3tPw", gz)
        self.assertIn("source:***@", gz)

    def test_logs_are_not_touched_when_they_are_not_published(self):
        logs = os.path.join(self.d, "logs")
        write(os.path.join(logs, "tuner.log"), "icecast://source:S3cr3tPw@localhost\n")
        self.share(interactive=0, env=f'PITUNER_WEB_RECORDINGS=1; LOG_DIR="{logs}"\n')
        self.assertIn("S3cr3tPw", read(os.path.join(logs, "tuner.log")))

    def test_installs_nginx_when_missing(self):
        self.share(have_nginx=False)
        self.assertIn("apt-get install -y nginx", self.calls_text())

    def test_declining_or_non_interactive_changes_nothing(self):
        self.share(confirm="n")
        self.assertEqual(self.conf_text(), "")
        self.share(interactive=0)
        self.assertEqual(self.conf_text(), "")
        self.assertNotIn("nginx -t", self.calls_text())

    def test_unattended_opt_in_via_environment(self):
        self.share(interactive=0, env='PITUNER_WEB_RECORDINGS=1; PITUNER_WEB_PORT=8090; '
                                      'PITUNER_WEB_ALLOW=10.0.0.0/8\n')
        text = self.conf_text()
        self.assertIn("listen 8090;", text)
        self.assertIn("allow 10.0.0.0/8;", text)

    def test_network_limit_is_written_and_kept_on_refresh(self):
        self.share(ask_answers=("8081", "192.168.1.0/24, 10.0.0.0/8"))
        text = self.conf_text()
        self.assertIn("listen 8081;", text)
        for line in ("allow 127.0.0.1;", "allow 192.168.1.0/24;", "allow 10.0.0.0/8;", "deny all;"):
            self.assertIn(line, text)
        r = self.share(interactive=0, existing=text)                       # re-run: refresh, no prompts
        again = self.conf_text()
        self.assertEqual(again.count("server {"), 1)
        for line in ("listen 8081;", "allow 192.168.1.0/24;", "allow 10.0.0.0/8;"):
            self.assertIn(line, again)
        self.assertIn("already set up", r.stdout)

    def test_bad_port_and_bad_network_are_ignored_with_warnings(self):
        r = self.share(ask_answers=("99999", "evil; rm -rf /"))
        self.assertIn("not a valid port", r.stdout)
        self.assertIn("not a valid network list", r.stdout)
        text = self.conf_text()
        self.assertIn("listen 8080;", text)
        self.assertNotIn("evil", text)

    def test_config_nginx_rejects_is_rolled_back(self):
        r = self.share(nginx_ok=False)
        self.assertEqual(self.conf_text(), "")
        self.assertIn("did not accept", r.stdout)
        old = "# previous\nserver { listen 8082; }\n"
        self.share(nginx_ok=False, interactive=0, existing=old)
        self.assertEqual(self.conf_text(), old)                            # refresh failure restores the old file


class UninstallTests(unittest.TestCase):
    """Runs the whole uninstall.sh inside a scratch tree with stubbed system commands."""
    SCRIPT = read(os.path.join(REPO, "uninstall.sh")).replace('if [ "${EUID}" -ne 0 ]; then', "if false; then")

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        j = lambda *a: os.path.join(self.d, *a)
        self.p = {k: j(v) for k, v in dict(
            app="opt/pituner", bin="bin", unit="unit", lr="lr/pituner", bl="modprobe/rtl-sdr-blacklist.conf",
            ice="icecast2/icecast.xml", ngx="nginx", www="www", log="log",
            stubs="stubs", calls="calls.txt").items()}
        for f, c in [
            ("app/tuner.py", "x"), ("app/recordings/WLSU/2026/10/22/a.mp3", "audio"),
            ("bin/demux", "x"), ("bin/redsea", "x"), ("bin/pituner", "x"), ("bin/othertool", "keep"),
            ("unit/pituner.service", "x"), ("unit/other.service", "keep"),
            ("lr", None), ("bl", "blacklist"),
            ("ice", "<icecast><source-password>secret</source-password><admin-password>a</admin-password></icecast>"),
            ("ngx/sites-enabled/pituner", "x"), ("ngx/sites-available/pituner", "x"),
            ("ngx/conf.d/pituner-recordings.conf", "x"), ("ngx/conf.d/other.conf", "keep"),
            ("log/nginx/pituner-recordings.access.log", "x"), ("log/nginx/access.log", "keep"),
            ("ngx/sites-enabled/mysite", "keep"), ("ngx/conf.d/pi-tuner.conf", "x"),
            ("www/pituner/tuner.log", "x"), ("www/html/index.html", "keep"),
            ("log/pituner/tuner.log", "x"), ("log/pi-tuner/old.log", "x"), ("log/pituner.log", "x"),
            ("log/syslog", "keep"),
        ]:
            if c is not None:
                write(self.p[f.split("/")[0]] + f[len(f.split("/")[0]):], c)
        write(self.p["lr"], "rotate")
        os.makedirs(self.p["stubs"])
        write(os.path.join(self.p["stubs"], "clear"), "#!/bin/sh\nexit 0\n", 0o755)
        for name in ("systemctl", "userdel", "groupdel", "nginx", "apt-get", "icecast2"):
            write(os.path.join(self.p["stubs"], name),
                  f'#!/bin/sh\necho "{name} $*" >> "{self.p["calls"]}"\n', 0o755)
        for name in ("id", "getent"):       # pretend the pituner user and group exist
            write(os.path.join(self.p["stubs"], name), "#!/bin/sh\nexit 0\n", 0o755)
        open(self.p["calls"], "w").close()

    def run_uninstall(self, args=(), stdin="", **extra_env):
        env = dict(extra_env, APP_DIR=self.p["app"], BIN_DIR=self.p["bin"], UNIT_DIR=self.p["unit"],
                   LOGROTATE_FILE=self.p["lr"], BLACKLIST_FILE=self.p["bl"], ICECAST_XML=self.p["ice"],
                   NGINX_DIR=self.p["ngx"], WWW_DIR=self.p["www"],
                   VARLOG_DIR=self.p["log"],
                   PATH=self.p["stubs"] + os.pathsep + os.environ["PATH"])
        return subprocess.run(["bash", "-c", self.SCRIPT, "uninstall.sh", *args], input=stdin,
                              capture_output=True, text=True, env=dict(os.environ, **env))

    def calls(self):
        return read(self.p["calls"])

    def exists(self, key, *rest):
        return os.path.exists(os.path.join(self.p[key], *rest))

    def assert_pituner_is_gone_and_the_rest_is_not(self):
        self.assertFalse(self.exists("app"))
        for path in (("unit", "pituner.service"), ("lr",), ("bl",), ("bin", "demux"), ("bin", "redsea"),
                     ("bin", "pituner"), ("www", "pituner"), ("log", "pituner"), ("log", "pi-tuner"),
                     ("log", "pituner.log"), ("ngx", "sites-enabled", "pituner"),
                     ("ngx", "sites-available", "pituner"), ("ngx", "conf.d", "pi-tuner.conf"),
                     ("ngx", "conf.d", "pituner-recordings.conf"),
                     ("log", "nginx", "pituner-recordings.access.log")):
            self.assertFalse(self.exists(path[0], *path[1:]), path)
        for path in (("bin", "othertool"), ("unit", "other.service"), ("www", "html", "index.html"),
                     ("log", "syslog"), ("ngx", "sites-enabled", "mysite"), ("ngx", "conf.d", "other.conf"),
                     ("log", "nginx", "access.log")):
            self.assertTrue(self.exists(path[0], *path[1:]), path)
        self.assertIn("userdel pituner", self.calls())

    def test_interactive_wipe_asks_once_then_offers_to_keep_nginx_and_icecast(self):
        r = self.run_uninstall(stdin="y\n\n\nn\n")        # remove all / keep nginx / keep icecast / no pw reset
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_pituner_is_gone_and_the_rest_is_not()
        self.assertIn("audio recordings", r.stdout)           # the plan warns about the recordings
        self.assertIn("Keep Nginx installed?", r.stderr)
        self.assertIn("Keep Icecast installed?", r.stderr)
        self.assertNotIn("apt-get", self.calls())
        self.assertIn("secret", read(self.p["ice"]))         # kept Icecast, passwords left as-is

    def test_declining_the_first_question_changes_nothing(self):
        r = self.run_uninstall(stdin="n\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Nothing was changed", r.stdout)
        self.assertTrue(self.exists("app", "recordings", "WLSU", "2026", "10", "22", "a.mp3"))
        self.assertTrue(self.exists("unit", "pituner.service"))
        self.assertEqual(self.calls(), "")

    def test_saying_no_to_keeping_removes_the_packages(self):
        r = self.run_uninstall(stdin="y\nn\nn\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        self.assertIn("apt-get purge -y nginx nginx-common", calls)
        self.assertIn("apt-get purge -y icecast2", calls)
        self.assert_pituner_is_gone_and_the_rest_is_not()

    def test_yes_wipes_pituner_but_keeps_nginx_and_icecast(self):
        r = self.run_uninstall(["--yes"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_pituner_is_gone_and_the_rest_is_not()
        self.assertNotIn("apt-get", self.calls())
        self.assertIn("Kept Nginx", r.stdout)
        self.assertIn("Kept Icecast", r.stdout)

    def test_yes_with_remove_flags_purges_both(self):
        r = self.run_uninstall(["--yes", "--remove-icecast", "--remove-nginx"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("apt-get purge -y nginx nginx-common", self.calls())
        self.assertIn("apt-get purge -y icecast2", self.calls())

    def test_remove_flags_need_yes_and_unknown_options_are_rejected(self):
        for args in (["--remove-nginx"], ["--remove-icecast"], ["--bogus"]):
            r = self.run_uninstall(args)
            self.assertEqual(r.returncode, 1, args)
        self.assertTrue(self.exists("app"))

    def test_missing_nginx_and_icecast_are_skipped(self):
        import shutil as sh
        sh.rmtree(self.p["ngx"])
        os.remove(self.p["ice"])
        os.remove(os.path.join(self.p["stubs"], "nginx"))
        os.remove(os.path.join(self.p["stubs"], "icecast2"))
        r = self.run_uninstall(["--yes"], NGINX_BIN="no-such-nginx")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Nginx is not installed", r.stdout)
        self.assertIn("Icecast is not installed", r.stdout)


class LogMigrationTests(Base):
    """The log step runs for every mode, so an upgrade moves old logs to the new place."""
    BLOCK = block("# Local rotating logs", 'ok "Log directory ${LOG_DIR} and logrotate config installed."')

    def setUp(self):
        super().setUp()
        self.new = os.path.join(self.d, "var-log-pituner")
        self.old = os.path.join(self.d, "var-www-pituner")
        self.lr = os.path.join(self.d, "logrotate.d")

    def migrate(self, keep=1):
        script = (STUBS + f'KEEP_SETTINGS={keep}; SRC="{REPO}"; CALLS="{self.calls}"\n'
                  f'LOG_DIR="{self.new}"; OLD_LOG_DIR="{self.old}"; LOGROTATE_DIR="{self.lr}"\n'
                  + self.BLOCK)
        return run_bash(script)

    def test_upgrade_moves_old_logs_and_removes_the_old_directory(self):
        write(os.path.join(self.old, "tuner.log"), "old tuner\n")
        write(os.path.join(self.old, "eas.log"), "old eas\n")
        write(os.path.join(self.old, "rbds.log.1.gz"), "gz")
        write(os.path.join(self.old, "notes.txt"), "stray")
        r = self.migrate(keep=1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.old))
        for name, text in (("tuner.log", "old tuner\n"), ("eas.log", "old eas\n"),
                           ("rbds.log.1.gz", "gz"), ("notes.txt", "stray")):
            self.assertEqual(read(os.path.join(self.new, name)), text)
        self.assertTrue(os.path.isfile(os.path.join(self.lr, "pituner")))
        self.assertIn("/var/log/pituner/*.log", read(os.path.join(self.lr, "pituner")))

    def test_nothing_is_overwritten_when_both_places_have_a_log(self):
        write(os.path.join(self.new, "tuner.log"), "new\n")
        write(os.path.join(self.old, "tuner.log"), "old\n")
        r = self.migrate()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(read(os.path.join(self.new, "tuner.log")), "new\n")
        self.assertEqual(read(os.path.join(self.new, "tuner.log.from-www")), "old\n")
        self.assertFalse(os.path.exists(self.old))

    def test_fresh_install_without_an_old_directory_just_creates_the_new_one(self):
        r = self.migrate(keep=0)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isdir(self.new))
        self.assertFalse(os.path.exists(self.old))


class StaticTests(unittest.TestCase):
    def test_scripts_parse(self):
        for name in ("install.sh", "uninstall.sh", "pituner"):
            shell = "sh" if name == "pituner" else "bash"
            r = subprocess.run([shell, "-n", os.path.join(REPO, name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_new_installs_write_the_eas_and_level_keys(self):
        self.assertEqual(INSTALL.count("EAS_DETECT=true"), 2)
        self.assertEqual(INSTALL.count("KEY_EAS=pituner.eas"), 2)
        self.assertEqual(INSTALL.count("LEVEL_MONITOR=true"), 2)
        self.assertNotIn("KEY_STATUS", INSTALL)          # the JSON status item is gone

    def test_unit_supports_reload(self):
        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID",
                      read(os.path.join(REPO, "pituner.service")))

    def test_installer_ships_the_menu_and_command(self):
        self.assertIn('install -m 644 "${SRC}/configure.py"', INSTALL)
        self.assertIn('install -m 755 "${SRC}/pituner" /usr/local/bin/pituner', INSTALL)
        self.assertTrue(os.access(os.path.join(REPO, "pituner"), os.X_OK))
        self.assertIn("${BIN_DIR}/pituner", read(os.path.join(REPO, "uninstall.sh")))
        self.assertIn('BIN_DIR="${BIN_DIR:-/usr/local/bin}"', read(os.path.join(REPO, "uninstall.sh")))


class WrapperTests(unittest.TestCase):
    def test_unknown_and_missing_commands_show_usage(self):
        for args in ([], ["bogus"]):
            r = subprocess.run([os.path.join(REPO, "pituner"), *args], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn("pituner config", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
