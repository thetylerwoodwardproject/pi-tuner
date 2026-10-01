import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tuner  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")

OLD_ZABBIX = """# Zabbix trapper settings
# ─── user settings ─────────────────────────────────
ENABLED=true
SERVER=zbx.internal
PORT=10051
HOSTNAME=pituner
KEY_EVENT=pituner.event
KEY_STATUS=pituner.status
KEY_ACTIVE=pituner.stations_active
KEY_HEARTBEAT=pituner.heartbeat
INTERVAL=60
# ─── end user settings ─────────────────────────────────
"""

OLD_STATION = """# Pi-Tuner station
# ─── user settings ─────────────────────────────────
NAME=WXTB
BAND=fm
FREQUENCY=97.9
SERIAL=00001001
GAIN=40.2
MOUNT=/tuner1
# ─── end user settings ─────────────────────────────
"""


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


def read(path):
    with open(path) as f:
        return f.read()


class UpdateKeyvalueTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.path = os.path.join(self.d, "x.conf")

    def test_replaces_value_keeps_everything_else(self):
        write(self.path, "# top\n\nA=1   # note\nB=2\n# ─── end user settings ──\n")
        self.assertTrue(tuner.update_keyvalue(self.path, {"a": "9"}))
        self.assertEqual(read(self.path), "# top\n\nA=9   # note\nB=2\n# ─── end user settings ──\n")

    def test_no_change_means_no_write(self):
        write(self.path, "A=1\n")
        before = os.stat(self.path).st_mtime_ns
        self.assertFalse(tuner.update_keyvalue(self.path, {"A": "1"}))
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)

    def test_new_key_goes_above_end_marker_or_at_end(self):
        write(self.path, "A=1\n# ─── end user settings ──\n")
        tuner.update_keyvalue(self.path, {"NEW": "x"})
        self.assertEqual(read(self.path), "A=1\nNEW=x\n# ─── end user settings ──\n")
        write(self.path, "A=1\n")
        tuner.update_keyvalue(self.path, {"NEW": "x"})
        self.assertEqual(read(self.path), "A=1\nNEW=x\n")

    def test_commented_hint_is_switched_on_in_place(self):
        write(self.path, "A=1\n# RECORD=false  # save recordings\nB=2\n")
        tuner.update_keyvalue(self.path, {"RECORD": "true"})
        self.assertEqual(read(self.path), "A=1\nRECORD=true  # save recordings\nB=2\n")

    def test_values_round_trip_including_hash_and_quotes(self):
        write(self.path, "PASSWORD=old  # secret\nOTHER=1\n")
        for pw in ("plain", "has #hash", ' padded ', 'q"uote', '"starts', "a b # c d"):
            tuner.update_keyvalue(self.path, {"PASSWORD": pw})
            self.assertEqual(tuner.parse_keyvalue(self.path)["password"], pw, pw)
            self.assertEqual(tuner.parse_keyvalue(self.path)["other"], "1")

    def test_trailing_comment_survives_a_quoted_value(self):
        write(self.path, 'PASSWORD="a #b"  # keep me\n')
        tuner.update_keyvalue(self.path, {"PASSWORD": "c #d"})
        self.assertEqual(read(self.path), 'PASSWORD="c #d"  # keep me\n')

    def test_case_insensitive_key_match(self):
        write(self.path, "host=old\n")
        tuner.update_keyvalue(self.path, {"HOST": "new"})
        self.assertEqual(tuner.parse_keyvalue(self.path)["host"], "new")
        self.assertEqual(read(self.path).count("="), 1)

    def test_mode_is_preserved(self):
        write(self.path, "A=1\n", mode=0o600)
        tuner.update_keyvalue(self.path, {"A": "2"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual([f for f in os.listdir(self.d) if f.startswith(".")], [])

    def test_idempotent(self):
        write(self.path, "A=1\n")
        tuner.update_keyvalue(self.path, {"A": "2", "B": "3"})
        once = read(self.path)
        self.assertFalse(tuner.update_keyvalue(self.path, {"A": "2", "B": "3"}))
        self.assertEqual(read(self.path), once)


class ParserTests(unittest.TestCase):
    def test_quoted_value_may_contain_hash_and_still_takes_comments(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.conf")
            write(p, 'A="x #y"\nB="z" # comment\nC=plain # comment\nD=  spaced value  \n')
            conf = tuner.parse_keyvalue(p)
        self.assertEqual((conf["a"], conf["b"], conf["c"], conf["d"]),
                         ("x #y", "z", "plain", "spaced value"))


class UpgradeConfigTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        write(os.path.join(self.d, "icecast.conf"),
              "# ice\n# ─── user settings ──\nHOST=localhost\nPORT=8000\nSOURCE_PASSWORD=sekret\n"
              "# ─── end user settings ──\n", 0o600)
        write(os.path.join(self.d, "zabbix.conf"), OLD_ZABBIX, 0o600)
        write(os.path.join(self.d, "stations", "station1.conf"), OLD_STATION)
        write(os.path.join(self.d, "stations", "station2.conf"),
              OLD_STATION.replace("BAND=fm", "BAND=wx").replace("NAME=WXTB", "NAME=WX1"))

    def effective(self):
        t = tuner.Tuner(self.d)
        t.load_config()
        return (t.eas_enabled, t.ice, [dict(st.cfg) for st in t.stations],
                t.zbx.enabled, t.zbx.key_eas, t.mailer.enabled)

    def test_adds_missing_keys_without_touching_values(self):
        before = {n: tuner.parse_keyvalue(os.path.join(self.d, n))
                  for n in ("icecast.conf", "zabbix.conf")}
        added = tuner.upgrade_config(self.d)
        self.assertEqual(added["zabbix.conf"], ["KEY_EAS", "EAS_DETECT", "LEVEL_MONITOR"])
        self.assertEqual(added["icecast.conf"], ["ADMIN_USER", "ADMIN_PASSWORD"])
        self.assertEqual(added["smtp.conf"], ["(new file)"])
        self.assertEqual(added["stations/station1.conf"], ["RBDS", "RECORD", "RECORD_KEEP_DAYS"])
        self.assertEqual(added["stations/station2.conf"], ["RECORD", "RECORD_KEEP_DAYS"])  # wx: no RBDS
        for name, old in before.items():
            now = tuner.parse_keyvalue(os.path.join(self.d, name))
            for key, value in old.items():
                self.assertEqual(now[key], value, f"{name}:{key}")

    def test_behaviour_is_unchanged_and_modes_kept(self):
        before = self.effective()
        tuner.upgrade_config(self.d)
        self.assertEqual(self.effective(), before)
        self.assertFalse(before[0])    # EAS stays off for a config that never enabled it
        for name in ("icecast.conf", "zabbix.conf", "smtp.conf"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.d, name)).st_mode), 0o600)

    def test_new_smtp_conf_is_disabled_and_parses(self):
        tuner.upgrade_config(self.d)
        m = tuner.Mailer(tuner.parse_keyvalue(os.path.join(self.d, "smtp.conf")))
        self.assertFalse(m.enabled)
        self.assertEqual((m.port, m.security, m.down_delay), (587, "starttls", 120))

    def test_idempotent_and_dry_run(self):
        self.assertIn("zabbix.conf", tuner.missing_config_keys(self.d))
        snapshot = {n: read(os.path.join(self.d, n)) for n in ("zabbix.conf", "icecast.conf")}
        tuner.missing_config_keys(self.d)
        self.assertEqual(snapshot["zabbix.conf"], read(os.path.join(self.d, "zabbix.conf")))
        tuner.upgrade_config(self.d)
        self.assertEqual(tuner.upgrade_config(self.d), {})
        self.assertEqual(tuner.missing_config_keys(self.d), {})

    def test_commented_hint_counts_as_present(self):
        write(os.path.join(self.d, "stations", "station1.conf"),
              OLD_STATION.replace("MOUNT=/tuner1", "MOUNT=/tuner1\n# RBDS=false"))
        added = tuner.upgrade_config(self.d)
        self.assertNotIn("RBDS", added["stations/station1.conf"])

    def test_station_without_user_markers_still_gets_keys(self):
        write(os.path.join(self.d, "stations", "bare.conf"), "NAME=B\nFREQUENCY=100.1\n")
        tuner.upgrade_config(self.d)
        self.assertIn("RECORD", read(os.path.join(self.d, "stations", "bare.conf")))
        self.assertEqual(tuner.parse_keyvalue(os.path.join(self.d, "stations", "bare.conf"))["name"], "B")


class BackupTests(unittest.TestCase):
    def test_backup_copies_configs_and_prunes_to_five(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "zabbix.conf"), OLD_ZABBIX, 0o600)
            write(os.path.join(d, "stations", "station1.conf"), OLD_STATION)
            dest = tuner.backup_config(d)
            self.assertEqual(read(os.path.join(dest, "zabbix.conf")), OLD_ZABBIX)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dest, "zabbix.conf")).st_mode), 0o600)
            self.assertEqual(read(os.path.join(dest, "stations", "station1.conf")), OLD_STATION)
            for _ in range(8):
                tuner.backup_config(d)
            self.assertEqual(len(os.listdir(os.path.join(d, "backups"))), 5)
            # backups are not backed up again
            self.assertFalse(any("backups" in n for n, _, _, _ in tuner._config_targets(d)))

    def test_nothing_to_back_up(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(tuner.backup_config(d))


class ShippedFilesTests(unittest.TestCase):
    def test_new_installs_cover_every_key_in_the_table(self):
        for name in ("zabbix.conf", "smtp.conf", "icecast.conf"):
            path = os.path.join(REPO, name)
            lines = tuner._read_lines(path)
            for spec in tuner.CONFIG_KEYS[name]:
                self.assertTrue(tuner._has_key(lines, spec[0]), f"{name} lacks {spec[0]}")


if __name__ == "__main__":
    unittest.main()
