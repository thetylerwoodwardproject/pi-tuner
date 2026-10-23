import io
import json
import math
import os
import random
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tuner  # noqa: E402
from test_config_edit import write  # noqa: E402

FM_RATE = 192000
WX_RATE = 25000


class Capture:
    """ZabbixSender stand-in that records what the meter sends."""
    rssi_monitor = True
    key_event = "pituner.event"

    def __init__(self, monitor=True):
        self.rssi_monitor = monitor
        self.sent = []
        self.flushed = False

    def tuner_key(self, metric, tid):
        return tuner.ZabbixSender.tuner_key(metric, tid)

    def send_async(self, items, mirror=False):
        self.sent.append(dict(items))

    def flush(self, timeout=5.0):
        self.flushed = True


def seconds_of(rate, seconds, tones, noise_sigma, seed=1):
    """Mono s16le PCM: only the first RSSI_BLOCK_SECS of each second matters."""
    rng = random.Random(seed)
    block = int(rate * tuner.RSSI_BLOCK_SECS)
    out = bytearray()
    for _ in range(seconds):
        samples = [sum(a * math.sin(2 * math.pi * f * i / rate) for f, a in tones)
                   + rng.gauss(0, noise_sigma) for i in range(block)]
        out += struct.pack(f"<{block}h", *[max(-32768, min(32767, int(x))) for x in samples])
        out += b"\x00\x00" * (rate - block)
    return bytes(out)


class RssiTests(unittest.TestCase):
    def setUp(self):
        self.log_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.log_dir.cleanup)
        patcher = mock.patch.object(tuner, "LOG_DIR", self.log_dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_meter(self, pcm, band, rate, interval=2, zbx=None):
        zbx = zbx or Capture()
        tuner.run_rssi_meter("/nowhere", "tuner1", "WLSU", band, 98.1, rate,
                             stream=io.BytesIO(pcm), interval=interval, zbx=zbx)
        return zbx

    def fm(self, pilot, noise):
        return seconds_of(FM_RATE, 2, [(19000, pilot)], noise)

    def test_fm_rssi_falls_as_noise_rises(self):
        strong = self.run_meter(self.fm(3000, 100), "fm", FM_RATE).sent[0]["pituner.tuner.rssi[tuner1]"]
        middle = self.run_meter(self.fm(3000, 1000), "fm", FM_RATE).sent[0]["pituner.tuner.rssi[tuner1]"]
        weak = self.run_meter(self.fm(3000, 6000), "fm", FM_RATE).sent[0]["pituner.tuner.rssi[tuner1]"]
        self.assertGreater(strong, middle)
        self.assertGreater(middle, weak)
        self.assertGreaterEqual(strong, tuner.RSSI_GOOD_DB)

    def test_wx_voice_over_hiss(self):
        voice = [(500, 3000), (1000, 3000), (1500, 3000), (2000, 3000), (2500, 3000)]
        clean = seconds_of(WX_RATE, 2, voice, 50)
        hissy = seconds_of(WX_RATE, 2, voice, 8000)
        a = self.run_meter(clean, "wx", WX_RATE).sent[0]["pituner.tuner.rssi[tuner1]"]
        b = self.run_meter(hissy, "wx", WX_RATE).sent[0]["pituner.tuner.rssi[tuner1]"]
        self.assertGreater(a, b)

    def test_silence_and_no_pilot_read_zero_not_an_error(self):
        z = self.run_meter(b"\x00\x00" * FM_RATE * 2, "fm", FM_RATE)
        self.assertEqual(z.sent[0]["pituner.tuner.rssi[tuner1]"], 0.0)
        self.assertEqual(tuner.rssi_from_powers(5.0, 0.0), tuner.RSSI_MAX_DB)
        self.assertEqual(tuner.rssi_from_powers(1.0, 100.0), 0.0)

    def test_one_value_per_interval_and_partial_second_dropped(self):
        pcm = self.fm(3000, 100) + self.fm(3000, 100) + b"\x00\x00" * 1000
        z = self.run_meter(pcm, "fm", FM_RATE, interval=2)
        self.assertEqual(len(z.sent), 2)
        self.assertTrue(z.flushed)

    def test_log_line_and_state_file(self):
        self.run_meter(self.fm(3000, 100), "fm", FM_RATE)
        with open(os.path.join(self.log_dir.name, "rssi.log")) as f:
            line = f.read().strip()
        self.assertRegex(line, r'^\[[\d: -]+\] tuner1 "WLSU" fm 98\.1 MHz rssi=\d+\.\d dB \((good|fair|poor)\)$')
        with open(tuner.rssi_state_path("tuner1")) as f:
            data = json.load(f)
        self.assertEqual((data["tuner"], data["name"], data["band"], data["freq"]),
                         ("tuner1", "WLSU", "fm", 98.1))
        self.assertIn(data["rating"], ("good", "fair", "poor"))
        self.assertFalse([n for n in os.listdir(self.log_dir.name) if n.endswith(".tmp")])

    def test_zabbix_send_follows_the_flag(self):
        off = self.run_meter(self.fm(3000, 100), "fm", FM_RATE, zbx=Capture(monitor=False))
        self.assertEqual(off.sent, [])
        self.assertTrue(os.path.exists(tuner.rssi_state_path("tuner1")))   # log/CLI still work

    def test_ratings(self):
        self.assertEqual([tuner.rssi_rating(v) for v in (40, tuner.RSSI_GOOD_DB, 15, 5)],
                         ["good", "good", "fair", "poor"])

    def test_rssi_subcommand_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "zabbix.conf"), "ENABLED=false\n")
            pcm = seconds_of(WX_RATE, 2, [(1000, 3000)], 100)
            with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(pcm))), \
                    mock.patch.object(tuner, "RSSI_INTERVAL_SECS", 2):
                self.assertEqual(tuner._main_rssi(["--dir", d, "--tuner", "tuner1", "--band", "wx",
                                                   "--freq", "162.55", "WX1", "25000"]), 0)
        self.assertTrue(os.path.exists(tuner.rssi_state_path("tuner1")))


S = tuner.rssi_settings({"time_on": "10", "time_off": "3"})   # 12 / 18 dB, 10 s / 3 s


class AlarmTests(unittest.TestCase):
    def run_series(self, series, settings=S):
        alarm, events = tuner.RssiAlarm(), []
        for t, value in enumerate(series, 1):
            ev = alarm.feed(value, t, settings)
            if ev:
                events.append((t, ev))
        return events

    def test_raises_only_after_time_on_and_clears_only_after_time_off(self):
        self.assertEqual(self.run_series([5] * 10 + [25] * 5), [(10, "raised"), (13, "cleared")])
        self.assertEqual(self.run_series([5] * 9), [])                    # one second short

    def test_hysteresis_band_holds_state(self):
        # 15 dB is above On (12) but below Off (18): neither raises nor clears
        self.assertEqual(self.run_series([15] * 30), [])
        self.assertEqual(self.run_series([5] * 10 + [15] * 30), [(10, "raised")])

    def test_flapping_resets_the_timers(self):
        self.assertEqual(self.run_series(([5] * 6 + [30]) * 5), [])
        self.assertEqual(self.run_series([5] * 10 + [25, 25, 5, 25, 25]), [(10, "raised")])

    def test_alarm_off_never_raises_and_clears_an_active_one(self):
        off = dict(S, alarm=False)
        self.assertEqual(self.run_series([0] * 100, off), [])
        alarm = tuner.RssiAlarm()
        alarm.active = True
        self.assertEqual(alarm.feed(0, 1, off), "cleared")


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(tuner.rssi_settings({}), {
            "alarm": True, "mute": False, "threshold_on": 12.0, "threshold_off": 18.0,
            "time_on": 60, "time_off": 5, "offset": 0.0})

    def test_station_overrides_global(self):
        s = tuner.rssi_settings({"threshold_on": "20", "mute": "true", "time_on": "30"},
                                {"rssi_threshold_on": "8", "rssi_mute": "false", "rssi_offset": "-3.5"})
        self.assertEqual((s["threshold_on"], s["mute"], s["time_on"], s["offset"]), (8.0, False, 30, -3.5))

    def test_bad_values_fall_back_and_off_never_below_on(self):
        s = tuner.rssi_settings({"threshold_on": "abc", "time_on": "x", "threshold_off": "5",
                                 "offset": "nope"}, {"rssi_threshold_on": "  "})
        self.assertEqual((s["threshold_on"], s["time_on"], s["offset"]), (12.0, 60, 0.0))
        self.assertEqual(s["threshold_off"], 12.0)             # 5 < On, so it is raised to On

    def test_load_reads_both_files(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "rssi.conf"), "THRESHOLD_ON=15\nOFFSET=17\n")
            write(os.path.join(d, "stations", "a.conf"), "RSSI_THRESHOLD_ON=9\n")
            self.assertEqual(tuner.load_rssi_settings(d, "a.conf")["threshold_on"], 9.0)
            self.assertEqual(tuner.load_rssi_settings(d, "")["threshold_on"], 15.0)
            self.assertEqual(tuner.load_rssi_settings(d, "")["offset"], 17.0)


class AlarmMeterTests(RssiTests):
    """The meter end to end, with a short Time On/Off and a weak-then-strong signal."""

    def setUp(self):
        super().setUp()
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        write(os.path.join(self.d.name, "rssi.conf"),
              "TIME_ON=4\nTIME_OFF=2\nMUTE=true\nTHRESHOLD_ON=12\nTHRESHOLD_OFF=18\n")

    def go(self, pcm, mailer=None, zbx=None):
        zbx = zbx or Capture()
        mailer = mailer or mock.Mock(alert_signal=True)
        tuner.run_rssi_meter(self.d.name, "tuner1", "WLSU", "fm", 98.1, FM_RATE,
                             stream=io.BytesIO(pcm), interval=60, zbx=zbx, mailer=mailer)
        return zbx, mailer

    def log_text(self):
        with open(os.path.join(self.log_dir.name, "rssi.log")) as f:
            return f.read()

    def test_raise_then_clear_logs_sends_and_emails(self):
        weak = seconds_of(FM_RATE, 8, [(19000, 300)], 9000)
        strong = seconds_of(FM_RATE, 8, [(19000, 3000)], 100)
        zbx, mailer = self.go(weak + strong)
        log = self.log_text()
        self.assertIn("LOW SIGNAL alarm RAISED", log)
        self.assertIn("(stream muted)", log)
        self.assertIn("LOW SIGNAL alarm CLEARED", log)
        self.assertLess(log.index("RAISED"), log.index("CLEARED"))
        alarms = [v["pituner.tuner.rssi.alarm[tuner1]"] for v in zbx.sent
                  if "pituner.tuner.rssi.alarm[tuner1]" in v]
        self.assertEqual(alarms, [1, 0])
        self.assertEqual([c.args[0] for c in mailer.send.call_args_list],
                         ["Low signal on WLSU: raised", "Low signal on WLSU: cleared"])
        self.assertFalse(os.path.exists(tuner.mute_flag_path("tuner1")))   # removed at the end

    def test_mute_flag_is_present_while_alarm_is_raised(self):
        seen = []
        real = tuner._set_mute_flag
        with mock.patch.object(tuner, "_set_mute_flag",
                               side_effect=lambda tid, muted: (seen.append(muted), real(tid, muted))[1]):
            self.go(seconds_of(FM_RATE, 8, [(19000, 300)], 9000))
        self.assertIn(True, seen)
        self.assertFalse(seen[-1])

    def test_no_mute_when_mute_is_off(self):
        write(os.path.join(self.d.name, "rssi.conf"), "TIME_ON=4\nMUTE=false\n")
        seen = []
        with mock.patch.object(tuner, "_set_mute_flag", side_effect=lambda t, m: seen.append(m)):
            self.go(seconds_of(FM_RATE, 8, [(19000, 300)], 9000))
        self.assertNotIn(True, seen)
        self.assertIn("RAISED", self.log_text())

    def test_alarm_off_and_email_flag(self):
        write(os.path.join(self.d.name, "rssi.conf"), "ALARM=false\nTIME_ON=1\n")
        self.go(seconds_of(FM_RATE, 6, [(19000, 300)], 9000))
        self.assertNotIn("LOW SIGNAL", self.log_text() if os.path.exists(
            os.path.join(self.log_dir.name, "rssi.log")) else "")
        mailer = mock.Mock(alert_signal=False)
        write(os.path.join(self.d.name, "rssi.conf"), "TIME_ON=2\n")
        self.go(seconds_of(FM_RATE, 6, [(19000, 300)], 9000), mailer=mailer)
        mailer.send.assert_not_called()

    def test_offset_shifts_reading_and_state_has_alarm_fields(self):
        write(os.path.join(self.d.name, "rssi.conf"), "OFFSET=17\nALARM=false\n")
        pcm = seconds_of(FM_RATE, 2, [(19000, 3000)], 1000)
        base = self.run_meter(pcm, "fm", FM_RATE).sent[0]["pituner.tuner.rssi[tuner1]"]
        zbx, _ = self.go(pcm)
        shifted = [v for v in zbx.sent if "pituner.tuner.rssi[tuner1]" in v]
        self.assertEqual(shifted, [])                       # interval=60: no minute report yet
        zbx = Capture()
        tuner.run_rssi_meter(self.d.name, "tuner1", "WLSU", "fm", 98.1, FM_RATE, stream=io.BytesIO(pcm),
                             interval=2, zbx=zbx, mailer=mock.Mock(alert_signal=False))
        self.assertAlmostEqual(zbx.sent[0]["pituner.tuner.rssi[tuner1]"], base + 17, delta=0.2)
        with open(tuner.rssi_state_path("tuner1")) as f:
            data = json.load(f)
        self.assertEqual((data["offset"], data["alarm"], data["alarm_enabled"]), (17.0, False, False))


class MuteGateTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.flag = os.path.join(self.d.name, "mute-tuner1")
        self.pcm = bytes(range(256)) * 400

    def run_gate(self, now=None):
        out = io.BytesIO()
        kwargs = {"now": now} if now else {}
        tuner.run_mute_gate("tuner1", 48000, 2, src=io.BytesIO(self.pcm), dst=out, flag=self.flag, **kwargs)
        return out.getvalue()

    def test_passes_audio_through_unchanged_without_a_flag(self):
        self.assertEqual(self.run_gate(), self.pcm)

    def test_silences_while_flag_is_fresh(self):
        open(self.flag, "w").close()
        out = self.run_gate()
        self.assertEqual(out, bytes(len(self.pcm)))

    def test_stale_flag_fails_open(self):
        open(self.flag, "w").close()
        later = os.stat(self.flag).st_mtime + tuner.RSSI_MUTE_STALE_SECS + 1
        self.assertEqual(self.run_gate(now=lambda: later), self.pcm)

    def test_broken_pipe_ends_quietly(self):
        class Dead:
            def write(self, _):
                raise BrokenPipeError

            def flush(self):
                pass
        tuner.run_mute_gate("tuner1", 48000, 2, src=io.BytesIO(self.pcm), dst=Dead(), flag=self.flag)

    def test_subcommand(self):
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(b"abcd"))), \
                mock.patch.object(sys, "stdout", mock.Mock(buffer=io.BytesIO())), \
                mock.patch.object(tuner, "LOG_DIR", self.d.name):
            self.assertEqual(tuner.main(["mute-gate", "--tuner", "tuner1", "8000", "1"]), 0)


class PipelineTests(unittest.TestCase):
    CFG = {"name": "WLSU", "band": "fm", "freq": 88.9, "mount": "/tuner3", "serial": "1", "gain": "",
           "rbds": False, "record": False, "rssi": True}
    ICE = {"host": "h", "port": "8000", "password": "pw"}

    def test_fm_tap_sits_on_the_mpx_before_demux(self):
        cmd = tuner.build_command(self.CFG, self.ICE, 0, rssi=True)
        tap = ' rssi --dir "/opt/pituner" --tuner tuner3 --band fm --freq 88.9 "WLSU" 192000)'
        self.assertIn(tap, cmd)
        self.assertLess(cmd.index(" rssi --dir "), cmd.index("demux"))

    def test_wx_tap(self):
        cmd = tuner.build_command(dict(self.CFG, band="wx", freq=162.55), self.ICE, 0, rssi=True)
        self.assertIn('--band wx --freq 162.55 "WLSU" 25000)', cmd)

    def test_off_when_not_asked_or_station_opts_out(self):
        self.assertNotIn(" rssi --dir ", tuner.build_command(self.CFG, self.ICE, 0))
        self.assertNotIn(" rssi --dir ", tuner.build_command(dict(self.CFG, rssi=False), self.ICE, 0, rssi=True))

    def test_mute_gate_sits_right_before_ffmpeg_after_every_tap(self):
        for cfg, rate in ((self.CFG, 48000), (dict(self.CFG, band="wx", freq=162.55), 25000)):
            cmd = tuner.build_command(dict(cfg, record=True), self.ICE, 0, eas=True, level=True, rssi=True)
            gate = f"mute-gate --tuner tuner3 {rate} "
            self.assertIn(gate, cmd)
            for tap in (" record --dir ", " level --dir ", "detect-eas", " rssi --dir "):
                self.assertLess(cmd.index(tap), cmd.index("mute-gate"), tap)
            self.assertLess(cmd.index("mute-gate"), cmd.index("ffmpeg"))
            self.assertEqual(cmd.count("mute-gate"), 1)
        off = tuner.build_command(self.CFG, self.ICE, 0)
        self.assertNotIn("mute-gate", off)

    def test_tap_carries_the_station_file_name(self):
        cmd = tuner.build_command(dict(self.CFG, conf="station2.conf"), self.ICE, 0, rssi=True)
        self.assertIn('--freq 88.9 --station-conf "station2.conf" "WLSU" 192000)', cmd)

    def test_station_key(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "a.conf"), "NAME=A\nFREQUENCY=98.1\nSERIAL=1\n")
            write(os.path.join(d, "b.conf"), "NAME=B\nFREQUENCY=99.1\nSERIAL=2\nRSSI_MONITOR=false\n")
            a, b = tuner.load_stations(d)
        self.assertEqual((a["rssi"], b["rssi"]), (True, False))


class SignalCommandTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.logs = os.path.join(self.d.name, "logs")
        os.makedirs(self.logs)
        os.makedirs(os.path.join(self.d.name, "stations"))
        for i, extra in enumerate(("", "RSSI_MONITOR=false\n", ""), 1):
            write(os.path.join(self.d.name, "stations", f"s{i}.conf"),
                  f"NAME=ST{i}\nFREQUENCY=9{i}.1\nSERIAL={i}\nMOUNT=/tuner{i}\n{extra}")
        patcher = mock.patch.object(tuner, "LOG_DIR", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cmd(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = tuner._main_signal(["--dir", self.d.name])
        return rc, out.getvalue()

    def test_table_stale_off_and_missing(self):
        import time
        write(tuner.rssi_state_path("tuner1"),
              json.dumps({"rssi": 31.2, "rating": "good", "updated": time.time() - 20}))
        write(tuner.rssi_state_path("tuner3"),
              json.dumps({"rssi": 4.0, "rating": "poor", "updated": time.time() - 3600}))
        write(os.path.join(self.logs, "rssi.log"), "[2026-01-01 00:00:00] tuner1 line one\n")
        rc, text = self.run_cmd()
        self.assertEqual(rc, 0)
        self.assertRegex(text, r"ST1 .*31\.2 dB\s+good\s+ok\s+\d+s ago\n")
        self.assertIn("RSSI_MONITOR=false", text)
        self.assertRegex(text, r"ST3 .*4\.0 dB\s+poor .*stale")
        self.assertIn("tuner1 line one", text)

    def test_no_reading_yet_and_no_stations(self):
        _, text = self.run_cmd()
        self.assertIn("no reading yet", text)
        with tempfile.TemporaryDirectory() as empty:
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                self.assertEqual(tuner._main_signal(["--dir", empty]), 1)


if __name__ == "__main__":
    unittest.main()
