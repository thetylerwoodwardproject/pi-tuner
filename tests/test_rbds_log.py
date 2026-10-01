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
            out += logger.feed(data, when)
        out += logger.finish(events[-1][0])
        return out

    def bodies(self, events, logger=None):
        return [l[15:].lstrip(":").strip() for l in self.feed_all(events, logger)]

    def test_line_format(self):
        out = self.feed_all([(at(9, 20), {"ps": "WXTB"}),
                             (at(9, 21, 15), rt("Metallica - Enter Sandman"))])
        self.assertEqual(out, ["261001 09:21:15: Metallica - Enter Sandman (WXTB)"])

    def test_no_ps_logs_text_alone(self):
        self.assertEqual(self.feed_all([(at(9, 0), rt("A - B")), (at(9, 0, 30), rt("A - B"))]),
                         ["261001 09:00:00: A - B"])

    def test_every_change_is_logged_including_rotation(self):
        seq = ["Song A", "Slogan", "Song A", "Slogan", "Song C", "Slogan"]
        events = [(at(8, 59), {"ps": "WXTB"})] + [(at(9, i), rt(t)) for i, t in enumerate(seq)]
        self.assertEqual(self.bodies(events), [f"{t} (WXTB)" for t in seq])

    def test_repeated_rt_messages_are_one_entry(self):
        out = self.feed_all([(at(9, 0, i), rt("A - B")) for i in range(5)])
        self.assertEqual(len(out), 1)

    def test_rt_plus_is_ignored(self):
        events = [(at(9, 0), {"ps": "WXTB"}),
                  (at(9, 1), plus("Enter Sandman", "Metallica")),
                  (at(9, 2), rt("Metallica - Enter Sandman")),
                  (at(9, 3), plus("Bat Country", "Avenged Sevenfold")),
                  (at(9, 4), {"radiotext": "Metallica - Enter Sandman",
                              **plus("Other", "Artist")})]
        # only the plain RT change is logged; RT+ never creates a line
        self.assertEqual(self.feed_all(events),
                         ["261001 09:02:00: Metallica - Enter Sandman (WXTB)"])

    def test_blank_or_unusable_rt_plus_changes_nothing(self):
        empty = {"radiotext_plus": {"item_running": True, "item_toggle": 0, "tags": []}}
        texts = ["WPR Music", "WPR.org", "WLSU 88.9", "Wisconsin Public Radio"]
        events = [(at(9, 0), {"ps": "WLSU"})]
        for i, t in enumerate(texts):
            events += [(at(9, 0, 1 + i * 12), rt(t)), (at(9, 0, 2 + i * 12), empty)]
        self.assertEqual(self.bodies(events), [f"{t} (WLSU)" for t in texts])

    def test_seed_from_existing_log_prevents_relog_of_current(self):
        lg = tuner.RbdsLogger()
        lg.seed(["261001 09:21:15: Metallica - Enter Sandman (WXTB)", "garbage line"], at(9, 30))
        self.assertEqual(self.feed_all([(at(9, 30), {"ps": "WXTB"}),
                                        (at(9, 30, 5), rt("Metallica - Enter Sandman")),
                                        (at(9, 30, 30), {"ps": "WXTB"})], lg), [])


class RunTests(unittest.TestCase):
    def run_helper(self, d, lines, log_name="WXTB", times=None):
        clock_times = iter(times) if times else None
        sent = []
        tuner.run_rbds_meta(
            d, "/tuner1", io.StringIO("\n".join(json.dumps(x) for x in lines)),
            updater=lambda ice, m, s: sent.append(s) or True,
            log_name=log_name,
            clock=(lambda: next(clock_times)) if clock_times else self.ticking())
        return sent

    def ticking(self, start=at(9, 21, 15), step=8):
        t = [start - step]

        def clock():
            t[0] += step
            return t[0]
        return clock

    def test_writes_log_and_updates_icecast(self):
        with tempfile.TemporaryDirectory() as d:
            sent = self.run_helper(d, [{"ps": "WXTB"}, rt("Metallica - Enter Sandman"),
                                       {"ps": "WXTB"}, {"ps": "WXTB"}])
            path = os.path.join(d, "recordings", "WXTB", "2026", "10", "01", "RBDS.log")
            with open(path) as f:
                content = f.read()
            self.assertRegex(content, r"^261001 09:21:\d\d: Metallica - Enter Sandman \(WXTB\)\n$")
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
            # two startup calls (seed + initial), then one call per event
            times = [at(23, 59, 50), at(23, 59, 50), at(23, 59, 50), at(23, 59, 55),
                     at(0, 0, 5, day=2)]
            self.run_helper(d, [{"ps": "WXTB"}, rt("A - B"), rt("C - D")], times=times)
            base = os.path.join(d, "recordings", "WXTB", "2026", "10")
            self.assertTrue(os.path.exists(os.path.join(base, "01", "RBDS.log")))
            self.assertTrue(os.path.exists(os.path.join(base, "02", "RBDS.log")))

    def test_unwritable_folder_never_breaks_icecast_updates(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "recordings"), "w").close()  # a file, not a folder
            sent = self.run_helper(d, [{"ps": "WXTB"}, rt("A - B"), {"ps": "WXTB"},
                                       {"ps": "WXTB"}])
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


class DynamicPsTests(unittest.TestCase):
    """Z93-style station: PS scrolls 'Station', 'Z93 The', '#1 Hit', 'Music'."""

    def scroll(self, start, seconds, callsign=None):
        events, frags, t = [], ["Station", "Z93 The", "#1 Hit", "Music"], 0
        while t < seconds:
            d = {"ps": frags[(t // 3) % 4]}
            if callsign:
                d["callsign"] = callsign
            events.append((start + t, d))
            t += 3
        return events

    def run_logger(self, events):
        lg = tuner.RbdsLogger()
        out = []
        for when, data in events:
            out += lg.feed(data, when)
        out += lg.finish(events[-1][0])
        return [l.split(": ", 1)[1] for l in out]

    def test_scrolling_ps_uses_callsign_not_fragments(self):
        events = self.scroll(at(9, 0), 30, callsign="WZEE")
        events.insert(3, (at(9, 0, 7), rt("Z93 The #1 Hit Music Station")))
        self.assertEqual(self.run_logger(events), ["Z93 The #1 Hit Music Station (WZEE)"])

    def test_scrolling_ps_without_callsign_has_no_suffix(self):
        events = self.scroll(at(9, 0), 30)
        events.insert(3, (at(9, 0, 7), rt("Z93 The #1 Hit Music Station")))
        self.assertEqual(self.run_logger(events), ["Z93 The #1 Hit Music Station"])

    def test_static_ps_still_used_and_log_time_is_rt_arrival(self):
        lg = tuner.RbdsLogger()
        out = []
        out += lg.feed({"ps": "WXTB"}, at(9, 0, 0))
        out += lg.feed(rt("A - B"), at(9, 0, 1))      # PS not settled yet: held back
        self.assertEqual(out, [])
        out += lg.feed({"ps": "WXTB"}, at(9, 0, 20))  # settled: flushed with arrival time
        self.assertEqual(out, ["261001 09:00:01: A - B (WXTB)"])

    def test_icecast_label_matches(self):
        t = tuner.PsTracker()
        for when, data in self.scroll(at(9, 0), 30, callsign="WZEE"):
            t.update(data, when)
        self.assertEqual(t.label(at(9, 0, 30)), (True, "WZEE"))
        s = tuner.PsTracker()
        s.update({"ps": "WLSU"}, at(9, 0))
        self.assertEqual(s.label(at(9, 0, 1)), (False, ""))
        self.assertEqual(s.label(at(9, 0, 30)), (True, "WLSU"))
