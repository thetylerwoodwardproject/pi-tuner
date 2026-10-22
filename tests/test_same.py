import datetime
import io
import math
import os
import random
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import same_audio  # noqa: E402
import tuner  # noqa: E402

HEADER = "ZCZC-WXR-TOR-022071-022051+0030-2891403-KLOX/NWS-"
UTC = datetime.timezone.utc


def decode(pcm, rate):
    dec = tuner.SameDecoder(rate)
    events, step = [], rate // 4
    for i in range(0, len(pcm), step):
        events += dec.feed(pcm[i:i + step])
    return events


class FakeMailer:
    alert_eas = True

    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))

    def flush(self, timeout=10.0):
        pass


class DemodTests(unittest.TestCase):
    def test_decodes_at_each_pipeline_rate(self):
        for rate in (25000, 48000):
            with self.subTest(rate=rate):
                events = decode(same_audio.burst_stream(HEADER, rate), rate)
                self.assertIn(("header", HEADER), events)
                self.assertEqual({t for k, t in events if k == "header"}, {HEADER})

    def test_survives_heavy_noise(self):
        random.seed(3)
        events = decode(same_audio.burst_stream(HEADER, 48000, noise=4000), 48000)
        self.assertIn(("header", HEADER), events)

    def test_silence_noise_and_tone_decode_nothing(self):
        rate = 48000
        rnd = random.Random(5)
        noise = [int(rnd.gauss(0, 6000)) for _ in range(rate * 10)]
        tone = [int(8000 * (math.sin(2 * math.pi * 853 * i / rate)
                            + math.sin(2 * math.pi * 960 * i / rate))) for i in range(rate * 5)]
        self.assertEqual(decode([0] * rate * 3, rate), [])
        self.assertEqual(decode(noise, rate), [])
        self.assertEqual(decode(tone, rate), [])

    def test_end_of_message_is_seen(self):
        rate = 25000
        pcm = same_audio.afsk("NNNN", rate) + [0] * rate
        self.assertIn(("eom", ""), decode(pcm, rate))

    def test_odd_block_sizes_keep_state(self):
        rate = 25000
        pcm = same_audio.burst_stream(HEADER, rate)
        dec, events = tuner.SameDecoder(rate), []
        for i in range(0, len(pcm), 997):
            events += dec.feed(pcm[i:i + 997])
        self.assertIn(("header", HEADER), events)


class ParseTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 16, 15, 0, tzinfo=UTC)   # day 289

    def test_parses_a_warning(self):
        info = tuner.parse_same(HEADER, self.NOW)
        self.assertEqual(info["event_name"], "Tornado Warning")
        self.assertEqual(info["originator"], "WXR")
        self.assertEqual(info["sender"], "KLOX/NWS")
        self.assertEqual(info["locations"], ["22071", "22051"])
        self.assertIn("Louisiana, county 071", info["location_names"][0])
        self.assertEqual(info["purge_minutes"], 30)
        self.assertEqual(info["issued"], datetime.datetime(2026, 10, 16, 14, 3, tzinfo=UTC))
        self.assertEqual(info["expires"], datetime.datetime(2026, 10, 16, 14, 33, tzinfo=UTC))

    def test_special_locations_and_unknown_codes(self):
        info = tuner.parse_same("ZCZC-PEP-EAN-000000+0100-0010000-WHITEHSE-", self.NOW)
        self.assertEqual(info["location_names"], ["the entire United States"])
        info = tuner.parse_same("ZCZC-CIV-XYZ-322000+0015-2891403-WLSU FM -", self.NOW)
        self.assertEqual(info["event_name"], "Unknown event")
        self.assertEqual(info["location_names"], ["northeast part of Louisiana"])
        self.assertEqual(info["sender"], "WLSU FM")
        info = tuner.parse_same("ZCZC-CIV-RWT-012000+0015-2891403-WLSU-", self.NOW)
        self.assertEqual(info["location_names"], ["Florida"])

    def test_issue_year_near_new_year(self):
        now = datetime.datetime(2027, 1, 1, 0, 5, tzinfo=UTC)
        info = tuner.parse_same("ZCZC-WXR-SVR-022071+0030-3652355-KLOX/NWS-", now)
        self.assertEqual(info["issued"], datetime.datetime(2026, 12, 31, 23, 55, tzinfo=UTC))

    def test_rejects_invalid_headers(self):
        for bad in ("ZCZC-WXR-TOR-022071+0030-2891403-KLOX/NWS",       # no trailing dash
                    "ZCZC-WXR-TOR-02207+0030-2891403-KLOX/NWS-",        # short location
                    "ZCZC-WXR-TOR-022071+0030-9991403-KLOX/NWS-",       # day 999
                    "ZCZC-WXR-TOR-022071+0030-2892503-KLOX/NWS-",       # hour 25
                    "ZCZC-WXR-TOR-022071+0070-2891403-KLOX/NWS-"):      # purge minutes 70
            self.assertIsNone(tuner.parse_same(bad, self.NOW), bad)

    def test_email_body_has_the_details(self):
        info = tuner.parse_same(HEADER, self.NOW)
        body = tuner.same_email_body(info, "WLSU", time.localtime(0))
        for text in ("Tornado Warning (TOR)", "National Weather Service", "KLOX/NWS",
                     "county 071", "county 051", "0h 30m", HEADER):
            self.assertIn(text, body)


class CollectorTests(unittest.TestCase):
    def test_majority_wins_and_duplicates_are_dropped(self):
        c = tuner.SameCollector()
        good, bad = HEADER, HEADER.replace("022071", "022072")
        c.add(bad, 100.0)
        c.add(good, 100.5)
        c.add(good, 101.0)
        self.assertIsNone(c.poll(102.0))              # window still open
        self.assertTrue(c.recent(102.0))
        self.assertEqual(c.poll(105.5), good)
        c.add(good, 120.0)
        self.assertIsNone(c.poll(126.0))              # repeat of the same alert
        c.add(good, 400.0)
        self.assertEqual(c.poll(406.0), good)         # a later, new alert


class DetectorTests(unittest.TestCase):
    def run_detector(self, pcm, rate, tid=""):
        fake = FakeMailer()
        stdin = mock.Mock(buffer=io.BytesIO(struct.pack("<%dh" % len(pcm), *pcm)))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(tuner, "Mailer", lambda conf: fake), \
                mock.patch.object(tuner, "log_file") as lf, \
                mock.patch.object(sys, "stdin", stdin):
            tuner.run_eas_detector(d, "WLSU", rate, 1, tid)
        return fake, lf

    def tone(self, rate, secs):
        return [int(8000 * (math.sin(2 * math.pi * 853 * i / rate)
                            + math.sin(2 * math.pi * 960 * i / rate)))
                for i in range(int(rate * secs))]

    def test_header_then_tone_sends_one_decoded_email(self):
        rate = 25000
        now = datetime.datetime.now(UTC)
        stamp = f"{now.timetuple().tm_yday:03d}{now.hour:02d}{now.minute:02d}"
        header = f"ZCZC-WXR-TOR-022071+0030-{stamp}-KLOX/NWS-"
        pcm = same_audio.burst_stream(header, rate) + self.tone(rate, 3)
        fake, lf = self.run_detector(pcm, rate)
        self.assertEqual(len(fake.sent), 1, fake.sent)
        subject, body = fake.sent[0]
        self.assertEqual(subject, "EAS alert on WLSU: Tornado Warning")
        self.assertIn("KLOX/NWS", body)
        logged = " ".join(str(c.args) for c in lf.call_args_list)
        self.assertIn("Tornado Warning", logged)

    def test_events_are_logged_per_station_and_combined(self):
        rate = 25000
        now = datetime.datetime.now(UTC)
        stamp = f"{now.timetuple().tm_yday:03d}{now.hour:02d}{now.minute:02d}"
        header = f"ZCZC-WXR-TOR-022071+0030-{stamp}-KLOX/NWS-"
        pcm = same_audio.burst_stream(header, rate) + self.tone(rate, 3)
        _, lf = self.run_detector(pcm, rate)
        files = {}
        for c in lf.call_args_list:
            files.setdefault(c.args[0], []).append(c.args[1])
        self.assertEqual(set(files), {"eas.log", "eas-WLSU.log"})
        self.assertEqual(files["eas.log"], files["eas-WLSU.log"])
        joined = " ".join(files["eas-WLSU.log"])
        self.assertIn("Tornado Warning", joined)
        self.assertIn("attention tone heard on WLSU", joined)

    def test_tone_line_says_whether_a_header_came_with_it(self):
        rate = 25000
        now = datetime.datetime.now(UTC)
        stamp = f"{now.timetuple().tm_yday:03d}{now.hour:02d}{now.minute:02d}"
        header = f"ZCZC-WXR-TOR-022071+0030-{stamp}-KLOX/NWS-"
        _, lf = self.run_detector(same_audio.burst_stream(header, rate) + self.tone(rate, 3), rate)
        tones = [c.args[1] for c in lf.call_args_list if "attention tone" in c.args[1]]
        self.assertTrue(tones and all(t.endswith("(SAME header decoded)") for t in tones), tones)
        _, lf = self.run_detector(self.tone(8000, 3), 8000)
        tones = [c.args[1] for c in lf.call_args_list if "attention tone" in c.args[1]]
        self.assertTrue(tones and all(t.endswith("(no SAME header decoded)") for t in tones), tones)

    def test_undecodable_header_is_logged_not_dropped(self):
        rate = 25000
        header = "ZCZC-WXR-TOR-022071+0030-9992599-KLOX/NWS-"   # day 999: valid shape, invalid content
        _, lf = self.run_detector(same_audio.burst_stream(header, rate), rate)
        logged = [c.args for c in lf.call_args_list]
        self.assertTrue(any("could not be decoded" in m and header in m for _, m in logged), logged)
        self.assertEqual({n for n, _ in logged}, {"eas.log", "eas-WLSU.log"})

    def test_log_lines_point_at_the_recording(self):
        rate = 25000
        now = datetime.datetime.now(UTC)
        stamp = f"{now.timetuple().tm_yday:03d}{now.hour:02d}{now.minute:02d}"
        header = f"ZCZC-CIV-RWT-022071+0015-{stamp}-WLSU-"
        pcm = same_audio.burst_stream(header, rate) + self.tone(rate, 3)
        rec = "recordings/WLSU/2026/10/22/261022_150000_WLSU.mp3"
        with mock.patch.object(tuner, "recording_covering", return_value=rec):
            _, lf = self.run_detector(pcm, rate)
        lines = [c.args[1] for c in lf.call_args_list if c.args[0] == "eas-WLSU.log"]
        self.assertTrue(lines and all(l.endswith(f"[recording: {rec}]") for l in lines), lines)
        self.assertTrue(any("Required Weekly Test" in l for l in lines))

    def test_recording_covering_picks_the_file_in_progress(self):
        with tempfile.TemporaryDirectory() as d:
            when = time.mktime((2026, 10, 22, 15, 7, 30, 0, 0, -1))
            day = os.path.join(d, "recordings", "WLSU", "2026", "10", "22")
            os.makedirs(day)
            for n in ("261022_144500_WLSU.mp3", "261022_150000_WLSU.mp3", "261022_151500_WLSU.mp3"):
                open(os.path.join(day, n), "w").close()
            self.assertEqual(tuner.recording_covering(d, "WLSU", when),
                             os.path.join("recordings", "WLSU", "2026", "10", "22",
                                          "261022_150000_WLSU.mp3"))
            self.assertEqual(tuner.recording_covering(d, "NOPE", when), "")

    def test_station_log_name_is_filesystem_safe(self):
        self.assertEqual(tuner.eas_log_name("WLSU"), "eas-WLSU.log")
        self.assertEqual(tuner.eas_log_name("My Station/../x"), "eas-My_Station..x.log")

    def test_logrotate_covers_the_station_logs(self):
        import fnmatch
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "logrotate.conf")) as f:
            globs = [l.split("{")[0].strip() for l in f if l.strip().startswith("/")]
        path = os.path.join(tuner.LOG_DIR, tuner.eas_log_name("WLSU"))
        self.assertTrue(any(fnmatch.fnmatch(path, g) for g in globs), globs)

    def test_tone_alone_still_sends_the_tone_email(self):
        fake, _ = self.run_detector(self.tone(8000, 3), 8000)
        self.assertEqual([s for s, _ in fake.sent], ["EAS attention tone heard on WLSU"])

    def test_header_without_a_tone_is_still_emailed(self):
        rate = 25000
        now = datetime.datetime.now(UTC)
        stamp = f"{now.timetuple().tm_yday:03d}{now.hour:02d}{now.minute:02d}"
        header = f"ZCZC-CIV-RWT-022071+0015-{stamp}-WLSU-"
        fake, _ = self.run_detector(same_audio.burst_stream(header, rate), rate)
        self.assertEqual([s for s, _ in fake.sent], ["EAS alert on WLSU: Required Weekly Test"])

    def test_zabbix_event_carries_the_decoded_message(self):
        rate = 25000
        now = datetime.datetime.now(UTC)
        stamp = f"{now.timetuple().tm_yday:03d}{now.hour:02d}{now.minute:02d}"
        pcm = same_audio.burst_stream(f"ZCZC-WXR-SVR-022071+0030-{stamp}-KLOX/NWS-", rate)
        sent = []
        zbx = mock.Mock(enabled=True, key_eas="pituner.eas", key_event="pituner.event")
        zbx.tuner_key = tuner.ZabbixSender.tuner_key
        zbx.send.side_effect = lambda items, mirror=True: sent.append(dict(items))
        stdin = mock.Mock(buffer=io.BytesIO(struct.pack("<%dh" % len(pcm), *pcm)))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(tuner, "ZabbixSender", lambda conf: zbx), \
                mock.patch.object(tuner, "Mailer", lambda conf: FakeMailer()), \
                mock.patch.object(tuner, "log_file"), \
                mock.patch.object(sys, "stdin", stdin):
            tuner.run_eas_detector(d, "WLSU", rate, 1)
        self.assertTrue(any("Severe Thunderstorm Warning" in s.get("pituner.event", "")
                            for s in sent), sent)


if __name__ == "__main__":
    unittest.main()
