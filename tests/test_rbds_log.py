import datetime
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tuner  # noqa: E402

ICE = {"host": "localhost", "port": "8000", "password": "pw"}


def ts(*args):
    return datetime.datetime(*args).timestamp()


def at(h, m, s=0, day=1):
    return ts(2026, 10, day, h, m, s)


def rt(text):
    return {"radiotext": text}


def plus(title, artist=None, running=True):
    tags = [{"content-type": "item.title", "data": title}]
    if artist:
        tags.append({"content-type": "item.artist", "data": artist})
    return {"radiotext_plus": {"item_running": running, "item_toggle": 1, "tags": tags}}


def station(**kw):
    cfg = {"name": "WXTB", "band": "fm", "freq": 97.9, "serial": "1",
           "gain": "", "mount": "/tuner1", "rbds": True, "record": True}
    cfg.update(kw)
    return cfg


class PathTests(unittest.TestCase):
    def test_path_uses_yyyy_and_daily_file(self):
        self.assertEqual(
            tuner.rbds_log_path("/opt/pituner", "WXTB", at(9, 21, 15)),
            "/opt/pituner/recordings/WXTB/2026/10/01/RBDS.log")


class LoggerTests(unittest.TestCase):
    def feed_all(self, events, logger=None):
        logger = logger or tuner.RbdsLogger()
        out = []
        for when, data in events:
            line = logger.feed(data, when)
            if line:
                out.append(line)
        return out

    def test_line_format(self):
        out = self.feed_all([(at(9, 20), {"ps": "WXTB"}),
                             (at(9, 21, 15), rt("Metallica - Enter Sandman"))])
        self.assertEqual(out, ["261001 09:21:15: Metallica - Enter Sandman (WXTB)"])

    def test_no_ps_yet_logs_text_alone(self):
        self.assertEqual(self.feed_all([(at(9, 0), rt("A - B"))]),
                         ["261001 09:00:00: A - B"])

    def test_rotating_rt_logs_each_text_once(self):
        song_a, song_c, slogan = "Artist A - Song A", "Artist C - Song C", "Listen at wxtb.com"
        seq = [song_a, slogan, song_a, slogan, song_a, slogan,
               song_c, slogan, song_c, slogan]
        events = [(at(9, i), rt(t)) for i, t in enumerate(seq)]
        events.insert(0, (at(8, 59), {"ps": "WXTB"}))
        out = self.feed_all(events)
        self.assertEqual([l.split(": ", 1)[1] for l in out],
                         [f"{song_a} (WXTB)", f"{slogan} (WXTB)", f"{song_c} (WXTB)"])

    def test_repeated_rt_messages_are_one_entry(self):
        out = self.feed_all([(at(9, 0, i), rt("A - B")) for i in range(5)])
        self.assertEqual(len(out), 1)

    def test_song_replayed_hours_later_logs_again(self):
        out = self.feed_all([(at(9, 0), rt("A - B")), (at(9, 5), rt("C - D")),
                             (at(11, 30), rt("A - B"))])
        self.assertEqual(len(out), 3)

    def test_rt_plus_logs_artist_title_only_while_running(self):
        lg = tuner.RbdsLogger()
        out = self.feed_all([
            (at(9, 0), {"ps": "WXTB"}),
            (at(9, 1), plus("Enter Sandman", "Metallica")),
            (at(9, 2), rt("Listen at wxtb.com")),          # ignored: station has RT+
            (at(9, 3), plus("Enter Sandman", "Metallica")),
            (at(9, 4), plus("Bat Country", "Avenged Sevenfold")),
            (at(9, 5), plus("Some Ad", "Sponsor", running=False)),
            (at(9, 6), plus("Title Only")),
        ], lg)
        self.assertEqual([l.split(": ", 1)[1] for l in out], [
            "Metallica - Enter Sandman (WXTB)",
            "Avenged Sevenfold - Bat Country (WXTB)",
            "Title Only (WXTB)"])

    def test_seed_from_existing_log_prevents_relog(self):
        lg = tuner.RbdsLogger()
        lg.seed(["261001 09:21:15: Metallica - Enter Sandman (WXTB)",
                 "garbage line"], at(9, 30))
        self.assertEqual(self.feed_all([(at(9, 30), {"ps": "WXTB"}),
                                        (at(9, 30, 5), rt("Metallica - Enter Sandman"))], lg), [])
        # old entries (outside the gap) are not remembered
        lg2 = tuner.RbdsLogger()
        lg2.seed(["261001 06:00:00: Old - Song (WXTB)"], at(9, 30))
        self.assertEqual(len(self.feed_all([(at(9, 30), rt("Old - Song"))], lg2)), 1)


class RunTests(unittest.TestCase):
    def run_helper(self, d, lines, log_name="WXTB", times=None):
        clock_times = iter(times) if times else None
        sent = []
        tuner.run_rbds_meta(
            d, "/tuner1", io.StringIO("\n".join(json.dumps(x) for x in lines)),
            updater=lambda ice, m, s: sent.append(s) or True,
            log_name=log_name,
            clock=(lambda: next(clock_times)) if clock_times else (lambda: at(9, 21, 15)))
        return sent

    def test_writes_log_and_updates_icecast(self):
        with tempfile.TemporaryDirectory() as d:
            sent = self.run_helper(d, [{"ps": "WXTB"}, rt("Metallica - Enter Sandman")])
            path = os.path.join(d, "recordings", "WXTB", "2026", "10", "01", "RBDS.log")
            with open(path) as f:
                self.assertEqual(f.read(),
                                 "261001 09:21:15: Metallica - Enter Sandman (WXTB)\n")
            self.assertEqual(sent[-1], "Metallica - Enter Sandman (WXTB)")

    def test_no_log_without_log_name(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_helper(d, [{"ps": "WXTB"}, rt("A - B")], log_name=None)
            self.assertFalse(os.path.exists(os.path.join(d, "recordings")))

    def test_restart_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_helper(d, [{"ps": "WXTB"}, rt("A - B")])
            self.run_helper(d, [{"ps": "WXTB"}, rt("A - B")])
            path = os.path.join(d, "recordings", "WXTB", "2026", "10", "01", "RBDS.log")
            with open(path) as f:
                self.assertEqual(len(f.read().splitlines()), 1)

    def test_midnight_rolls_to_next_day_folder(self):
        with tempfile.TemporaryDirectory() as d:
            # first call seeds at startup, then one call per event
            times = [at(23, 59, 50), at(23, 59, 50), at(23, 59, 55), at(0, 0, 5, day=2)]
            self.run_helper(d, [{"ps": "WXTB"}, rt("A - B"), rt("C - D")], times=times)
            base = os.path.join(d, "recordings", "WXTB", "2026", "10")
            self.assertTrue(os.path.exists(os.path.join(base, "01", "RBDS.log")))
            self.assertTrue(os.path.exists(os.path.join(base, "02", "RBDS.log")))

    def test_unwritable_folder_never_breaks_icecast_updates(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "recordings"), "w").close()  # a file, not a folder
            sent = self.run_helper(d, [{"ps": "WXTB"}, rt("A - B")])
            self.assertEqual(sent[-1], "A - B (WXTB)")


class CommandTests(unittest.TestCase):
    def test_log_name_only_for_fm_with_rbds_and_record(self):
        on = tuner.build_command(station(), ICE, 0, rbds=True)
        self.assertIn('--log-name "WXTB"', on)
        self.assertNotIn("--log-name", tuner.build_command(station(record=False), ICE, 0, rbds=True))
        self.assertNotIn("--log-name", tuner.build_command(station(), ICE, 0, rbds=False))
        wx = tuner.build_command(station(band="wx"), ICE, 0, rbds=True)
        self.assertNotIn("--log-name", wx)
        self.assertNotIn("rbds-meta", wx)


class PruneTests(unittest.TestCase):
    def test_prune_removes_old_rbds_log_and_empty_folders(self):
        now = at(12, 0, day=20)
        with tempfile.TemporaryDirectory() as d:
            day = os.path.join(d, "recordings", "S", "2026", "10", "01")
            os.makedirs(day)
            path = os.path.join(day, "RBDS.log")
            open(path, "w").close()
            os.utime(path, (at(12, 0), at(12, 0)))
            self.assertEqual(tuner.prune_recordings(d, "S", 14, now), 1)
            self.assertFalse(os.path.exists(day))


if __name__ == "__main__":
    unittest.main()
