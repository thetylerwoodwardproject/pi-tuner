import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import configure  # noqa: E402
import tuner  # noqa: E402
from test_alerts import FakeSMTP  # noqa: E402
from test_config_edit import OLD_STATION, OLD_ZABBIX, read, write  # noqa: E402


class Script:
    """Feeds scripted answers to the menu and fails if it asks for more."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"menu asked for more input: {prompt!r}")
        return self.answers.pop(0)


class MenuCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        write(os.path.join(self.d, "icecast.conf"),
              "# ─── user settings ──\nHOST=localhost\nPORT=8000\nSOURCE_PASSWORD=sekret\n"
              "# ─── end user settings ──\n", 0o600)
        write(os.path.join(self.d, "zabbix.conf"), OLD_ZABBIX, 0o600)
        write(os.path.join(self.d, "stations", "station1.conf"), OLD_STATION)
        self.out = []
        self.reloads = []

    def menu(self, answers, secrets=(), devices=None):
        sec = Script(secrets)
        m = configure.Menu(
            self.d, inp=Script(answers), secret=sec, out=self.out.append,
            runner=lambda cmd, **kw: self.reloads.append(cmd) or type("R", (), {"returncode": 0})(),
            detect=lambda: devices or [])
        return m, sec

    def snapshot(self):
        snap = {}
        for root, _, files in os.walk(self.d):
            if "backups" in root:
                continue
            for f in files:
                p = os.path.join(root, f)
                snap[os.path.relpath(p, self.d)] = read(p)
        return snap

    def text(self):
        return "\n".join(str(x) for x in self.out)


class QuitTests(MenuCase):
    def test_quit_without_changes_touches_nothing(self):
        before = self.snapshot()
        m, _ = self.menu(["q"])
        self.assertEqual(m.run(), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.reloads, [])
        self.assertFalse(os.path.exists(os.path.join(self.d, "backups")))

    def test_pressing_enter_through_every_prompt_changes_nothing(self):
        before = self.snapshot()
        # Icecast (5 prompts; secrets use getpass), Zabbix (6), then quit
        m, _ = self.menu(["2"] + [""] * 3 + ["3"] + [""] * 10 + ["q"], secrets=["", ""])
        m.run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.reloads, [])

    def test_unwritable_directory_is_reported(self):
        m, _ = self.menu(["q"])
        m.dir = os.path.join(self.d, "nope")
        self.assertEqual(m.run(), 1)
        self.assertIn("sudo", self.text())


class StationTests(MenuCase):
    def test_add_station_writes_file_and_pins_mount(self):
        # a, band, name, freq, serial, gain, RBDS?, record?, keep days, back, quit, reload?
        m, _ = self.menu(["1", "a", "fm", "wltm", "104.5", "00001002", "30", "y", "y", "14",
                          "b", "q", "n"])
        m.run()
        stations = {s["name"]: s for s in tuner.load_stations(os.path.join(self.d, "stations"))}
        new = stations["WLTM"]
        self.assertEqual((new["freq"], new["serial"], new["gain"], new["mount"]),
                         (104.5, "00001002", "30", "/tuner2"))
        self.assertTrue(new["rbds"] and new["record"])
        self.assertEqual(new["record_keep_days"], 14)
        self.assertIn("station2.conf", self.text())
        self.assertEqual(self.reloads, [])             # said no to the reload

    def test_new_station_bad_input_reprompts_and_serial_is_required(self):
        m, _ = self.menu(["1", "a", "fm", "TOOLONGNAME", "W-X", "7", "abc", "99.5",
                          "", "00001003", "", "n", "n", "b", "q", "n"],
                         devices=[("0", "00001003")])
        m.run()
        text = self.text()
        self.assertIn("Use 1-8 letters/digits", text)
        self.assertIn("Enter a number between 80 and 170", text)
        self.assertIn("A dongle serial is required", text)
        added = [s for s in tuner.load_stations(os.path.join(self.d, "stations")) if s["name"] == "W-X"]
        self.assertEqual(added[0]["freq"], 99.5)

    def test_picking_a_detected_dongle_by_number(self):
        m, _ = self.menu(["1", "a", "wx", "WX1", "", "0", "", "n", "b", "q", "n"],
                         devices=[("0", "77770001")])
        m.run()
        wx = [s for s in tuner.load_stations(os.path.join(self.d, "stations")) if s["name"] == "WX1"][0]
        self.assertEqual((wx["serial"], wx["band"], wx["freq"]), ("77770001", "wx", 162.55))

    def test_edit_keeps_unchanged_fields_and_comments(self):
        write(os.path.join(self.d, "stations", "station1.conf"),
              OLD_STATION.replace("GAIN=40.2", "GAIN=40.2   # tuned for this antenna"))
        # e 1: band, name, freq, serial, gain, RBDS?, record?   (change only the frequency)
        m, _ = self.menu(["1", "e 1", "", "", "98.1", "", "", "", "", "b", "q", "n"])
        m.run()
        text = read(os.path.join(self.d, "stations", "station1.conf"))
        self.assertIn("FREQUENCY=98.1", text)
        self.assertIn("GAIN=40.2   # tuned for this antenna", text)
        self.assertIn("SERIAL=00001001", text)
        self.assertIn("NAME=WXTB", text)

    def test_edit_can_clear_gain_and_turn_features_on(self):
        m, _ = self.menu(["1", "e 1", "", "", "", "", "-", "y", "y", "7", "b", "q", "n"])
        m.run()
        st = tuner.load_stations(os.path.join(self.d, "stations"))[0]
        self.assertEqual(st["gain"], "")
        self.assertTrue(st["rbds"] and st["record"])
        self.assertEqual(st["record_keep_days"], 7)
        self.assertNotIn("GAIN=", read(os.path.join(self.d, "stations", "station1.conf")))

    def test_existing_odd_name_can_be_kept(self):
        write(os.path.join(self.d, "stations", "station1.conf"),
              OLD_STATION.replace("NAME=WXTB", "NAME=Station VYMXC"))
        m, _ = self.menu(["1", "e 1", "", "", "", "", "", "", "", "b", "q"])
        m.run()
        self.assertIn("NAME=Station VYMXC", read(os.path.join(self.d, "stations", "station1.conf")))
        self.assertEqual(self.reloads, [])

    def test_remove_keeps_other_stream_urls(self):
        write(os.path.join(self.d, "stations", "station1.conf"),
              OLD_STATION.replace("MOUNT=/tuner1\n", ""))
        write(os.path.join(self.d, "stations", "station2.conf"),
              OLD_STATION.replace("NAME=WXTB", "NAME=KTWO").replace("MOUNT=/tuner1\n", ""))
        write(os.path.join(self.d, "stations", "station3.conf"),
              OLD_STATION.replace("NAME=WXTB", "NAME=KTHR").replace("MOUNT=/tuner1\n", ""))
        before = {s["name"]: s["mount"] for s in tuner.load_stations(os.path.join(self.d, "stations"))}
        self.assertEqual(before, {"WXTB": "/tuner1", "KTWO": "/tuner2", "KTHR": "/tuner3"})
        m, _ = self.menu(["1", "r 1", "y", "b", "q", "n"])
        m.run()
        after = {s["name"]: s["mount"] for s in tuner.load_stations(os.path.join(self.d, "stations"))}
        self.assertEqual(after, {"KTWO": "/tuner2", "KTHR": "/tuner3"})

    def test_remove_can_be_declined(self):
        before = self.snapshot()
        m, _ = self.menu(["1", "r 1", "n", "b", "q"])
        m.run()
        self.assertEqual(self.snapshot(), before)


class SectionTests(MenuCase):
    def test_zabbix_edit(self):
        # ENABLED?, SERVER, PORT, HOSTNAME, INTERVAL, EAS_DETECT?, LEVEL_MONITOR?
        m, _ = self.menu(["3", "n", "zbx2.internal", "", "", "30", "y", "", "q", "n"])
        m.run()
        conf = tuner.parse_keyvalue(os.path.join(self.d, "zabbix.conf"))
        self.assertEqual((conf["enabled"], conf["server"], conf["interval"], conf["eas_detect"]),
                         ("false", "zbx2.internal", "30", "true"))
        self.assertIn("PORT=10051", read(os.path.join(self.d, "zabbix.conf")))

    def test_icecast_password_is_hidden_and_kept_on_enter(self):
        m, sec = self.menu(["2", "", "", "", "q"], secrets=["", ""])
        m.run()
        self.assertEqual(len(sec.prompts), 2)
        self.assertEqual(tuner.parse_keyvalue(os.path.join(self.d, "icecast.conf"))["source_password"],
                         "sekret")
        self.assertNotIn("sekret", self.text())
        m, _ = self.menu(["2", "", "", "", "q", "n"], secrets=["new pw #1", ""])
        m.run()
        self.assertEqual(tuner.parse_keyvalue(os.path.join(self.d, "icecast.conf"))["source_password"],
                         "new pw #1")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.d, "icecast.conf")).st_mode), 0o600)

    def test_email_setup_creates_smtp_conf_and_sends_test(self):
        srv = FakeSMTP(require_auth=True)
        self.addCleanup(srv.close)
        answers = ["4",
                   "y", "127.0.0.1", str(srv.port), "none", "n", "u", "me@pi", "a@example.com, b@example.com",
                   "y", "y", "y", "y", "90", "3",
                   "y",           # send a test email now
                   "q", "n"]
        m, _ = self.menu(answers, secrets=["p"])
        m.run()
        path = os.path.join(self.d, "smtp.conf")
        conf = tuner.parse_keyvalue(path)
        self.assertEqual((conf["enabled"], conf["host"], conf["security"], conf["username"],
                          conf["password"], conf["to"], conf["down_delay"], conf["disk_min_gb"]),
                         ("true", "127.0.0.1", "none", "u", "p", "a@example.com, b@example.com", "90", "3"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(srv.messages[0]["rcpts"], ["a@example.com", "b@example.com"])
        self.assertIn("Test email sent", self.text())
        self.assertNotIn("password: p", self.text())

    def test_email_test_reports_failures(self):
        write(os.path.join(self.d, "smtp.conf"),
              "ENABLED=true\nHOST=127.0.0.1\nPORT=1\nSECURITY=none\nTO=a@b.c\nTIMEOUT=2\n", 0o600)
        m, _ = self.menu(["4"] + [""] * 6 + ["", ""] + [""] * 6 + ["y", "q"], secrets=[""])
        m.run()
        self.assertIn("Test email FAILED", self.text())


class ApplyTests(MenuCase):
    def test_changes_offer_a_reload_and_run_it(self):
        m, _ = self.menu(["3", "n"] + [""] * 9 + ["q", "y"])
        m.run()
        self.assertEqual(self.reloads, [["systemctl", "reload", "pituner.service"]])

    def test_backup_is_made_once_before_the_first_write(self):
        m, _ = self.menu(["3", "n"] + [""] * 9 + ["3", "n"] + [""] * 9 + ["q", "n"])
        m.run()
        backups = os.listdir(os.path.join(self.d, "backups"))
        self.assertEqual(len(backups), 1)
        saved = read(os.path.join(self.d, "backups", backups[0], "zabbix.conf"))
        self.assertEqual(saved, OLD_ZABBIX)


if __name__ == "__main__":
    unittest.main()
