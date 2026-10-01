import array
import io
import json
import math
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import tuner  # noqa: E402
from test_config_edit import write  # noqa: E402


class FakeZabbix:
    """Zabbix trapper stand-in: records every sender-data request."""

    def __init__(self):
        self.requests = []          # list of {key: value} per request
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop = True
        self._thread.join(timeout=3)
        self._srv.close()

    def _read(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def _loop(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.settimeout(3)
                try:
                    header = self._read(conn, 13)
                    (length,) = struct.unpack("<Q", header[5:13])
                    payload = json.loads(self._read(conn, length))
                    self.requests.append({d["key"]: d["value"] for d in payload["data"]})
                    body = json.dumps({"response": "success", "info": "ok"}).encode()
                    conn.sendall(b"ZBXD\x01" + struct.pack("<Q", len(body)) + body)
                except (OSError, ValueError, struct.error):
                    pass

    def wait_for(self, count=1, timeout=5.0):
        end = time.time() + timeout
        while len(self.requests) < count and time.time() < end:
            time.sleep(0.05)
        return self.requests

    def all_items(self):
        merged = {}
        for req in self.requests:
            merged.update(req)
        return merged


class Captured:
    """Stands in for a ZabbixSender in the helpers: records async sends."""

    def __init__(self, interval=60):
        self.interval = interval
        self.enabled = True
        self.sent = []
        self.flushed = False

    tuner_key = staticmethod(tuner.ZabbixSender.tuner_key)

    def send_async(self, items, mirror=False):
        self.sent.append(dict(items))

    def flush(self, timeout=0):
        self.flushed = True


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.zbx = FakeZabbix()
        self.addCleanup(self.zbx.close)

    def make_tuner(self, extra_zabbix=""):
        write(os.path.join(self.d, "zabbix.conf"),
              f"ENABLED=true\nSERVER=127.0.0.1\nPORT={self.zbx.port}\nHOSTNAME=pituner\n{extra_zabbix}")
        write(os.path.join(self.d, "stations", "station1.conf"),
              "NAME=WLSU\nBAND=fm\nFREQUENCY=88.9\nSERIAL=00001001\n")
        write(os.path.join(self.d, "stations", "station2.conf"),
              "NAME=Station Z4J3D\nBAND=wx\nFREQUENCY=162.4\nSERIAL=00001002\nMOUNT=/my.wx\nRECORD=true\n")
        t = tuner.Tuner(self.d)
        t.load_config()
        return t


class IdTests(unittest.TestCase):
    def test_tuner_ids_are_key_safe(self):
        self.assertEqual(tuner.tuner_id("/tuner3"), "tuner3")
        self.assertEqual(tuner.tuner_id("/my.wx"), "my.wx")
        self.assertEqual(tuner.tuner_id("/a/b"), "a_b")
        self.assertEqual(tuner.tuner_id("/"), "tuner")
        self.assertEqual(tuner.ZabbixSender.tuner_key("level", "tuner3"), "pituner.tuner.level[tuner3]")


class SnapshotTests(Base):
    def test_discovery_and_per_tuner_items(self):
        t = self.make_tuner()
        t.stations[0].status = "streaming"
        t.stations[0].restarts = 2
        t.stations[0].genre = "Country"
        t.stations[1].status = "serial_not_found"
        t.send_heartbeat()
        req = self.zbx.wait_for(1)[0]

        disc = json.loads(req["pituner.tuners.discovery"])
        self.assertEqual(disc["data"][0], {"{#TUNER}": "tuner1", "{#NAME}": "WLSU", "{#BAND}": "fm",
                                           "{#FREQ}": "88.9", "{#MOUNT}": "/tuner1"})
        self.assertEqual(disc["data"][1]["{#TUNER}"], "my.wx")
        self.assertEqual(disc["data"][1]["{#NAME}"], "Station Z4J3D")

        k = tuner.ZabbixSender.tuner_key
        self.assertEqual(req[k("state", "tuner1")], "streaming")
        self.assertEqual(req[k("up", "tuner1")], "1")
        self.assertEqual(req[k("name", "tuner1")], "WLSU")
        self.assertEqual(req[k("band", "tuner1")], "fm")
        self.assertEqual(req[k("frequency", "tuner1")], "88.9")
        self.assertEqual(req[k("mount", "tuner1")], "/tuner1")
        self.assertEqual(req[k("serial", "tuner1")], "00001001")
        self.assertEqual(req[k("genre", "tuner1")], "Country")
        self.assertEqual(req[k("restarts", "tuner1")], "2")
        self.assertEqual(req[k("recording", "tuner1")], "0")
        self.assertEqual(req[k("recording.age", "tuner1")], "-1.0")

        self.assertEqual(req[k("state", "my.wx")], "serial_not_found")
        self.assertEqual(req[k("up", "my.wx")], "0")
        self.assertEqual(req[k("band", "my.wx")], "wx")
        self.assertEqual(req[k("genre", "my.wx")], "Weather")
        self.assertEqual(req[k("recording", "my.wx")], "1")

        self.assertEqual(req["pituner.stations_active"], "1")
        self.assertEqual(req["pituner.heartbeat"], "1")
        self.assertNotIn("pituner.status", req)               # the JSON blob is gone

    def test_every_metric_sent_by_the_supervisor_is_a_known_metric(self):
        t = self.make_tuner()
        t.send_heartbeat()
        req = self.zbx.wait_for(1)[0]
        sent = {key.split("[")[0].replace("pituner.tuner.", "") for key in req if key.startswith("pituner.tuner.")}
        # RBDS text, level, deviation and EAS come from the helper processes
        self.assertEqual(sent | {"rbds.rt", "rbds.ps", "eas", "level", "deviation", "modulation"},
                         set(tuner.TUNER_METRICS))

    def test_status_change_is_sent_immediately(self):
        t = self.make_tuner()
        with mock.patch.object(tuner, "log"):
            t.set_status(t.stations[0], "streaming")
            t.set_status(t.stations[0], "down", "exit code 1")
        reqs = self.zbx.wait_for(4)
        merged = [r for r in reqs if "pituner.tuner.state[tuner1]" in r]
        self.assertEqual([r["pituner.tuner.state[tuner1]"] for r in merged], ["streaming", "down"])
        self.assertEqual([r["pituner.tuner.up[tuner1]"] for r in merged], ["1", "0"])

    def test_pipeline_death_counts_a_restart(self):
        t = self.make_tuner()
        st = t.stations[0]
        st.proc = mock.Mock(poll=lambda: 1)
        st.status = "streaming"
        t.refresh_devices = lambda: None
        t.start_station = lambda station: None
        with mock.patch.object(tuner, "log"):
            t.poll()
        self.assertEqual(st.restarts, 1)
        self.assertEqual(st.status, "down")

    def test_start_records_the_genre_that_was_launched(self):
        t = self.make_tuner()
        st = t.stations[0]
        self.assertEqual(st.genre, "Radio")
        self.assertEqual(t.stations[1].genre, "Weather")


class RecordingAgeTests(unittest.TestCase):
    def test_age_from_newest_mp3_today_or_yesterday(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            self.assertEqual(tuner.newest_recording_age(d, "WLSU", now), -1.0)
            day = os.path.join(tuner.recording_dir(d, "WLSU"),
                               time.strftime("%Y/%m/%d", time.localtime(now)))
            os.makedirs(day)
            for name, age in (("old.mp3", 500), ("new.mp3", 30), ("RBDS.log", 1)):
                p = os.path.join(day, name)
                open(p, "w").close()
                os.utime(p, (now - age, now - age))
            self.assertAlmostEqual(tuner.newest_recording_age(d, "WLSU", now), 30.0, delta=0.5)
            # just after midnight the newest file may be in yesterday's folder
            tomorrow = now + 86400
            os.utime(os.path.join(day, "new.mp3"), (tomorrow - 40, tomorrow - 40))
            self.assertAlmostEqual(tuner.newest_recording_age(d, "WLSU", tomorrow), 40.0, delta=0.5)


class LevelMeterTests(unittest.TestCase):
    RATE = 8000

    def tone(self, seconds, amp):
        n = int(self.RATE * seconds)
        return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 440 * i / self.RATE)))
                        for i in range(n))

    def run_meter(self, pcm, channels=1, interval=1):
        z = Captured()
        tuner.run_level_meter("/nowhere", "tuner1", "WLSU", self.RATE, channels,
                              stream=io.BytesIO(pcm), interval=interval, zbx=z)
        return z

    def test_full_scale_half_scale_and_silence(self):
        full = self.run_meter(self.tone(2, 32767))
        half = self.run_meter(self.tone(2, 16384))
        quiet = self.run_meter(b"\x00\x00" * self.RATE * 2)
        self.assertEqual(len(full.sent), 2)
        self.assertAlmostEqual(full.sent[0]["pituner.tuner.level[tuner1]"], -3.0, delta=0.2)
        self.assertAlmostEqual(half.sent[0]["pituner.tuner.level[tuner1]"], -9.0, delta=0.2)
        self.assertEqual(quiet.sent[0]["pituner.tuner.level[tuner1]"], -90.0)
        self.assertTrue(full.flushed)

    def test_stereo_and_partial_window(self):
        pcm = self.tone(1.5, 20000)
        z = self.run_meter(b"".join(pcm[i:i + 2] * 2 for i in range(0, len(pcm), 2)), channels=2)
        self.assertEqual(len(z.sent), 1)                         # the half window is not sent
        self.assertAlmostEqual(z.sent[0]["pituner.tuner.level[tuner1]"],
                               20 * math.log10(20000 * 0.7071 / 32768), delta=0.3)

    def test_dbfs_helper(self):
        self.assertEqual(tuner.dbfs_from_sumsq(0, 10), -90.0)
        self.assertEqual(tuner.dbfs_from_sumsq(10, 0), -90.0)
        self.assertEqual(tuner.dbfs_from_sumsq(32768 ** 2 * 4, 4), 0.0)


class AsyncSendTests(unittest.TestCase):
    def test_send_async_never_blocks_on_a_hung_server(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)                  # accepts into the backlog, never answers
        z = tuner.ZabbixSender({"enabled": "true", "server": "127.0.0.1",
                                "port": str(srv.getsockname()[1])})
        with mock.patch.object(tuner, "log"):
            start = time.time()
            for i in range(50):
                z.send_async([("k", i)])
            self.assertLess(time.time() - start, 0.5)
            srv.close()            # let the queued sends fail fast instead of lingering
            z.flush(15)

    def test_send_async_delivers_and_flush_waits(self):
        zbx = FakeZabbix()
        self.addCleanup(zbx.close)
        z = tuner.ZabbixSender({"enabled": "true", "server": "127.0.0.1", "port": str(zbx.port)})
        z.send_async([("a", 1), ("b", 2)])
        z.flush(5)
        self.assertEqual(zbx.all_items(), {"a": "1", "b": "2"})

    def test_disabled_sender_does_nothing(self):
        z = tuner.ZabbixSender({"enabled": "false"})
        z.send_async([("a", 1)])
        self.assertIsNone(z._worker)


class RbdsToZabbixTests(unittest.TestCase):
    def run_helper(self, events, zbx, tid="tuner1"):
        lines = "\n".join(json.dumps(d) for _, d in events)
        times = iter([events[0][0]] + [t for t, _ in events])   # one call before the loop
        with tempfile.TemporaryDirectory() as d:
            tuner.run_rbds_meta(d, "/tuner1", io.StringIO(lines), lambda *a: True,
                                clock=lambda: next(times), zbx=zbx, tid=tid)

    def test_sends_on_change_and_refreshes_on_the_interval(self):
        z = Captured(interval=60)
        ev = [(0, {"ps": "WXTB"}),
              (13, {"radiotext": "Artist - Song"}),      # label settled: first send
              (20, {"radiotext": "Artist - Song"}),      # unchanged, not yet due
              (25, {"radiotext": "Other - Tune"}),       # changed
              (90, {"ps": "WXTB"})]                      # 65 s later: refresh
        self.run_helper(ev, z)
        k = tuner.ZabbixSender.tuner_key
        self.assertEqual(z.sent, [
            {k("rbds.rt", "tuner1"): "Artist - Song", k("rbds.ps", "tuner1"): "WXTB"},
            {k("rbds.rt", "tuner1"): "Other - Tune", k("rbds.ps", "tuner1"): "WXTB"},
            {k("rbds.rt", "tuner1"): "Other - Tune", k("rbds.ps", "tuner1"): "WXTB"},
        ])
        self.assertTrue(z.flushed)

    def test_scrolling_ps_is_not_sent_as_fragments(self):
        z = Captured()
        frags = ["Station", "Z93 The", "#1 Hit", "Music"]
        ev = [(i * 3, {"ps": frags[i % 4], "callsign": "WZEE"}) for i in range(12)]
        ev.insert(5, (15, {"radiotext": "Z93 The #1 Hit Music Station"}))
        ev.sort(key=lambda e: e[0])
        self.run_helper(ev, z)
        values = {s[tuner.ZabbixSender.tuner_key("rbds.ps", "tuner1")] for s in z.sent}
        self.assertEqual(values, {"WZEE"})

    def test_nothing_without_zabbix_or_tuner_id(self):
        z = Captured()
        self.run_helper([(0, {"ps": "WXTB"}), (20, {"radiotext": "A - B"})], z, tid="")
        self.assertEqual(z.sent, [])
        self.run_helper([(0, {"ps": "WXTB"}), (20, {"radiotext": "A - B"})], None)   # no zbx at all


class EasPerTunerTests(unittest.TestCase):
    def test_pulses_the_tuner_item_and_the_global_item(self):
        rate = 8000
        pcm = b"".join(struct.pack("<h", int(8000 * (math.sin(2 * math.pi * 853 * i / rate)
                                                      + math.sin(2 * math.pi * 960 * i / rate))))
                       for i in range(rate * 6))
        sent = []
        fake = mock.Mock(enabled=True, key_eas="pituner.eas", key_event="pituner.event")
        fake.tuner_key = tuner.ZabbixSender.tuner_key
        fake.send.side_effect = lambda items, mirror=True: sent.append(dict(items))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(tuner, "ZabbixSender", lambda conf: fake), \
                mock.patch.object(tuner, "log_file"), \
                mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(pcm))):
            tuner.run_eas_detector(d, "WLSU", rate, 1, "tuner3")
        self.assertEqual(sent[0]["pituner.eas"], 1)
        self.assertEqual(sent[0]["pituner.tuner.eas[tuner3]"], 1)
        self.assertIn("EAS attention tone heard on WLSU", sent[0]["pituner.event"])
        self.assertEqual(sent[-1], {"pituner.eas": 0, "pituner.tuner.eas[tuner3]": 0})


class PipelineTests(unittest.TestCase):
    CFG = {"name": "WLSU", "band": "fm", "freq": 88.9, "serial": "1", "gain": "",
           "mount": "/tuner3", "rbds": True, "record": True}
    ICE = {"host": "localhost", "port": "8000", "password": "pw"}

    def test_level_tap_only_when_asked_and_ordered_before_eas(self):
        off = tuner.build_command(self.CFG, self.ICE, 0, eas=True)
        self.assertNotIn(" level --dir ", off)
        on = tuner.build_command(self.CFG, self.ICE, 0, eas=True, level=True)
        self.assertIn(' level --dir "/opt/pituner" --tuner tuner3 "WLSU" 48000 2)', on)
        self.assertLess(on.index("demux"), on.index(" record --dir "))
        self.assertLess(on.index(" record --dir "), on.index(" level --dir "))
        self.assertLess(on.index(" level --dir "), on.index("detect-eas"))
        self.assertIn('detect-eas --dir "/opt/pituner" --tuner tuner3', on)
        wx = tuner.build_command(dict(self.CFG, band="wx"), self.ICE, 0, level=True)
        self.assertIn('"WLSU" 25000 1)', wx.split(" level --dir ")[1])

    def test_rbds_helper_is_told_its_tuner_id(self):
        self.assertIn("rbds-meta --dir \"/opt/pituner\" --tuner tuner3 --log-name \"WLSU\" \"/tuner3\"",
                      tuner.build_command(self.CFG, self.ICE, 0, rbds=True))

    def test_level_monitor_follows_zabbix_and_the_flag(self):
        with tempfile.TemporaryDirectory() as d:
            for text, expected in (("ENABLED=true\nSERVER=h\n", True),
                                   ("ENABLED=true\nSERVER=h\nLEVEL_MONITOR=false\n", False),
                                   ("ENABLED=false\nSERVER=h\n", False)):
                write(os.path.join(d, "zabbix.conf"), text)
                t = tuner.Tuner(d)
                t.load_config()
                self.assertEqual(t.level_enabled, expected, text)

    def test_level_subcommand_runs_end_to_end(self):
        zbx = FakeZabbix()
        self.addCleanup(zbx.close)
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "zabbix.conf"),
                  f"ENABLED=true\nSERVER=127.0.0.1\nPORT={zbx.port}\n")
            pcm = b"\x00\x00" * 8000 * 11                 # 11 s of silence at 8 kHz mono
            with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(pcm))):
                self.assertEqual(tuner._main_level(["--dir", d, "--tuner", "tuner1",
                                                    "WLSU", "8000", "1"]), 0)
        self.assertEqual(zbx.all_items(), {"pituner.tuner.level[tuner1]": "-90.0"})


class DeviationMeterTests(unittest.TestCase):
    FM_RATE, WX_RATE = 192000, 25000

    @staticmethod
    def pcm(rate, seconds, amp, dc=0, freq=440):
        n = int(rate * seconds)
        return array.array("h", [int(amp * math.sin(2 * math.pi * freq * i / rate)) + dc
                                 for i in range(n)]).tobytes()

    def run_meter(self, pcm, rate, full_khz, interval=1):
        z = Captured()
        tuner.run_deviation_meter("/nowhere", "tuner1", "WLSU", rate, full_khz,
                                  stream=io.BytesIO(pcm), interval=interval, zbx=z)
        return z

    def values(self, z, i=0):
        k = tuner.ZabbixSender.tuner_key
        return z.sent[i][k("deviation", "tuner1")], z.sent[i][k("modulation", "tuner1")]

    def test_conversion_constants(self):
        self.assertAlmostEqual(tuner.khz_per_count(192000), 0.0058594, places=6)
        self.assertAlmostEqual(tuner.khz_per_count(25000) * 6554, 5.0, delta=0.01)
        self.assertAlmostEqual(12800 * tuner.khz_per_count(192000), 75.0, delta=0.01)

    def test_fm_75_khz_is_100_percent(self):
        z = self.run_meter(self.pcm(self.FM_RATE, 2, 12800), self.FM_RATE, 75.0)
        self.assertEqual(len(z.sent), 2)
        khz, pct = self.values(z)
        self.assertAlmostEqual(khz, 75.0, delta=0.3)
        self.assertAlmostEqual(pct, 100.0, delta=0.4)
        self.assertTrue(z.flushed)

    def test_half_amplitude_is_half_modulation(self):
        z = self.run_meter(self.pcm(self.FM_RATE, 1, 6400), self.FM_RATE, 75.0)
        khz, pct = self.values(z)
        self.assertAlmostEqual(khz, 37.5, delta=0.2)
        self.assertAlmostEqual(pct, 50.0, delta=0.3)

    def test_overmodulation_reads_above_100(self):
        z = self.run_meter(self.pcm(self.FM_RATE, 1, 14000), self.FM_RATE, 75.0)
        self.assertGreater(self.values(z)[1], 105)

    def test_carrier_frequency_error_is_removed(self):
        # a +4 kHz carrier offset shows up as a constant of about 683 counts
        offset = int(4 / tuner.khz_per_count(self.FM_RATE))
        plain = self.values(self.run_meter(self.pcm(self.FM_RATE, 2, 9000), self.FM_RATE, 75.0))
        shifted = self.values(self.run_meter(self.pcm(self.FM_RATE, 2, 9000, dc=offset),
                                             self.FM_RATE, 75.0))
        self.assertAlmostEqual(plain[0], shifted[0], delta=0.5)

    def test_wx_uses_its_own_scale_and_reference(self):
        z = self.run_meter(self.pcm(self.WX_RATE, 2, 6554), self.WX_RATE, 5.0)
        khz, pct = self.values(z)
        self.assertAlmostEqual(khz, 5.0, delta=0.05)
        self.assertAlmostEqual(pct, 100.0, delta=1.0)

    def test_silence_reads_zero(self):
        z = self.run_meter(b"\x00\x00" * self.FM_RATE, self.FM_RATE, 75.0)
        self.assertEqual(self.values(z), (0.0, 0.0))

    def test_a_single_noise_click_is_ignored_but_sustained_peaks_are_not(self):
        rate = self.FM_RATE
        block = int(rate * tuner.DEVIATION_BLOCK_SECS)
        base = array.array("h", [int(6400 * math.sin(2 * math.pi * 440 * i / rate))
                                 for i in range(rate)])
        spike = array.array("h", base)
        for i in range(block * 4, block * 5):
            spike[i] = 16000                       # one 0.1 s burst (a noise click)
        click = self.values(self.run_meter(spike.tobytes(), rate, 75.0))
        self.assertLess(click[0], 45.0)            # not the ~89 kHz the click would read
        self.assertGreater(click[0], 35.0)
        sustained = array.array("h", base)
        for i in range(block * 3, block * 8):
            sustained[i] = 12000                   # five blocks: real program peaks
        self.assertGreater(self.values(self.run_meter(sustained.tobytes(), rate, 75.0))[0], 65.0)

    def test_window_peak_helper(self):
        self.assertEqual(tuner.window_peak([]), 0.0)
        self.assertEqual(tuner.window_peak([5.0]), 5.0)
        self.assertEqual(tuner.window_peak([10, 9, 8]), 10)
        self.assertEqual(tuner.window_peak([100, 10, 9]), 10)      # lone spike dropped
        self.assertEqual(tuner.window_peak([100, 80, 9]), 100)     # sustained: kept

    def test_reaches_zabbix_end_to_end(self):
        zbx = FakeZabbix()
        self.addCleanup(zbx.close)
        z = tuner.ZabbixSender({"enabled": "true", "server": "127.0.0.1", "port": str(zbx.port)})
        tuner.run_deviation_meter("/nowhere", "tuner1", "WLSU", self.FM_RATE, 75.0,
                                  stream=io.BytesIO(self.pcm(self.FM_RATE, 1, 12800)),
                                  interval=1, zbx=z)
        got = zbx.all_items()
        self.assertAlmostEqual(float(got["pituner.tuner.deviation[tuner1]"]), 75.0, delta=0.3)
        self.assertAlmostEqual(float(got["pituner.tuner.modulation[tuner1]"]), 100.0, delta=0.4)

    def test_subcommand_runs(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(b"\x00\x00" * 1000))):
            self.assertEqual(tuner._main_deviation(["--dir", d, "--tuner", "tuner1",
                                                    "--full-khz", "75", "WLSU", "192000"]), 0)


class DeviationPipelineTests(unittest.TestCase):
    CFG = {"name": "WLSU", "band": "fm", "freq": 88.9, "serial": "1", "gain": "",
           "mount": "/tuner3", "rbds": True, "record": True}
    ICE = {"host": "localhost", "port": "8000", "password": "pw"}

    def test_fm_tap_sits_on_the_composite_before_demux_and_rbds(self):
        cmd = tuner.build_command(self.CFG, self.ICE, 0, deviation=True, dev_full_khz=75.0, rbds=True)
        self.assertIn('deviation --dir "/opt/pituner" --tuner tuner3 --full-khz 75 "WLSU" 192000)', cmd)
        self.assertLess(cmd.index("rtl_fm"), cmd.index(" deviation --dir "))
        self.assertLess(cmd.index(" deviation --dir "), cmd.index("redsea"))
        self.assertLess(cmd.index(" deviation --dir "), cmd.index("demux"))

    def test_wx_tap_follows_rtl_fm_with_its_own_reference(self):
        wx = dict(self.CFG, band="wx")
        cmd = tuner.build_command(wx, self.ICE, 0, deviation=True, dev_full_khz=5.0)
        self.assertIn('--full-khz 5 "WLSU" 25000)', cmd)
        self.assertLess(cmd.index(" deviation --dir "), cmd.index(" record --dir "))

    def test_fractional_reference_and_off_by_default(self):
        cmd = tuner.build_command(self.CFG, self.ICE, 0, deviation=True, dev_full_khz=62.5)
        self.assertIn("--full-khz 62.5 ", cmd)
        self.assertNotIn(" deviation --dir ", tuner.build_command(self.CFG, self.ICE, 0))

    def test_config_flags_and_references(self):
        z = tuner.ZabbixSender({})
        self.assertEqual((z.deviation_monitor, z.fm_full_khz, z.wx_full_khz), (True, 75.0, 5.0))
        z = tuner.ZabbixSender({"deviation_monitor": "false", "fm_full_deviation_khz": "70",
                                "wx_full_deviation_khz": "4.5"})
        self.assertEqual((z.deviation_monitor, z.fm_full_khz, z.wx_full_khz), (False, 70.0, 4.5))
        with tempfile.TemporaryDirectory() as d:
            for text, expected in (("ENABLED=true\nSERVER=h\n", True),
                                   ("ENABLED=true\nSERVER=h\nDEVIATION_MONITOR=false\n", False),
                                   ("ENABLED=false\nSERVER=h\n", False)):
                write(os.path.join(d, "zabbix.conf"), text)
                t = tuner.Tuner(d)
                t.load_config()
                self.assertEqual(t.deviation_enabled, expected, text)


if __name__ == "__main__":
    unittest.main()
