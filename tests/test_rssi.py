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
        self.assertRegex(text, r"ST1 .*31\.2 dB\s+good\s+\d+s ago\n")
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
