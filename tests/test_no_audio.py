import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tuner  # noqa: E402

CFG = {"name": "W300BM", "band": "fm", "freq": 107.9, "mount": "/tuner3",
       "serial": "00001039"}


def fake_proc(root, pid, ppid, comm, wchar=None):
    d = os.path.join(root, str(pid))
    os.makedirs(d)
    with open(os.path.join(d, "stat"), "w") as f:
        f.write(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560\n")
    if wchar is not None:
        with open(os.path.join(d, "io"), "w") as f:
            f.write(f"rchar: 1\nwchar: {wchar}\nsyscr: 1\n")


class BytesWrittenTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_finds_rtl_fm_child_of_the_station_shell(self):
        fake_proc(self.root, 100, 1, "bash")
        fake_proc(self.root, 101, 100, "rtl_fm", wchar=4096)
        fake_proc(self.root, 102, 100, "ffmpeg", wchar=9)
        fake_proc(self.root, 201, 200, "rtl_fm", wchar=777)        # another station's
        self.assertEqual(tuner.rtl_fm_bytes_written(100, self.root), 4096)

    def test_comm_with_spaces_and_parens_is_parsed(self):
        fake_proc(self.root, 100, 1, "bash")
        fake_proc(self.root, 101, 100, "(sd-pam) x", wchar=1)
        fake_proc(self.root, 102, 100, "rtl_fm", wchar=5)
        self.assertEqual(tuner.rtl_fm_bytes_written(100, self.root), 5)

    def test_rtl_fm_that_is_the_shell_itself(self):
        fake_proc(self.root, 100, 1, "rtl_fm", wchar=33)
        self.assertEqual(tuner.rtl_fm_bytes_written(100, self.root), 33)

    def test_unknown_when_no_rtl_fm_or_io_unreadable(self):
        fake_proc(self.root, 100, 1, "bash")
        self.assertIsNone(tuner.rtl_fm_bytes_written(100, self.root))
        fake_proc(self.root, 101, 100, "rtl_fm", wchar=None)      # no io file
        self.assertIsNone(tuner.rtl_fm_bytes_written(100, self.root))
        self.assertIsNone(tuner.rtl_fm_bytes_written(100, os.path.join(self.root, "nope")))

    @unittest.skipUnless(os.path.isdir("/proc/self"), "needs /proc")
    def test_reads_a_real_process(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        fake = os.path.join(d, "rtl_fm")
        shutil.copy(shutil.which("sleep"), fake)
        p = subprocess.Popen(["bash", "-c", f"{fake} 5; true"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        got = None
        for _ in range(50):
            got = tuner.rtl_fm_bytes_written(p.pid)
            if got is not None:
                break
            time.sleep(0.1)
        self.assertIsInstance(got, int)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.t = tuner.Tuner(self.d)
        self.st = tuner.Station(dict(CFG))
        self.st.proc = mock.Mock(pid=4242)
        self.st.status = "streaming"
        self.st.audio_at = 1000.0
        self.logs = []
        p = mock.patch.object(tuner, "log", lambda m, err=False: self.logs.append(m))
        p.start()
        self.addCleanup(p.stop)

    def check(self, written, now):
        with mock.patch.object(tuner, "rtl_fm_bytes_written", return_value=written):
            self.t.check_audio(self.st, now)

    def test_flowing_audio_stays_streaming(self):
        for i, b in enumerate((100, 200, 300, 400)):
            self.check(b, 1000.0 + i * 30)
        self.assertEqual(self.st.status, "streaming")

    def test_silent_dongle_is_flagged_after_the_window(self):
        self.check(0, 1000.0)                     # first reading
        self.check(0, 1000.0 + tuner.NO_AUDIO_SECS - 1)
        self.assertEqual(self.st.status, "streaming")
        self.check(0, 1000.0 + tuner.NO_AUDIO_SECS)
        self.assertEqual(self.st.status, "no_audio")
        self.assertTrue(any("ERROR no audio" in m for m in self.logs))

    def test_flag_is_raised_once(self):
        self.check(0, 1000.0)
        self.check(0, 1030.0)
        self.check(0, 1060.0)
        self.assertEqual(sum("ERROR no audio" in m for m in self.logs), 1)

    def test_recovers_when_audio_returns(self):
        self.check(0, 1000.0)
        self.check(0, 1030.0)
        self.assertEqual(self.st.status, "no_audio")
        self.check(5000, 1040.0)
        self.assertEqual(self.st.status, "streaming")

    def test_unreadable_counter_never_counts_as_silence(self):
        with mock.patch.object(tuner, "rtl_fm_pid", return_value=4243):
            self.check(None, 1000.0)
            self.check(None, 9999.0)
        self.assertEqual(self.st.status, "streaming")

    def test_vanished_rtl_fm_restarts_the_pipeline(self):
        # Dongle pulled: rtl_fm is gone but the shell pipeline is still alive.
        with mock.patch.object(tuner, "rtl_fm_pid", return_value=None), \
                mock.patch.object(self.t, "stop_station") as stop:
            self.check(None, 1000.0)
            self.check(None, 1000.0 + tuner.RTL_GONE_SECS - 1)
            self.assertEqual(self.st.status, "streaming")
            stop.assert_not_called()
            self.check(None, 1000.0 + tuner.RTL_GONE_SECS)
            stop.assert_called_once_with(self.st)
        self.assertEqual(self.st.status, "down")
        self.assertEqual(self.st.restarts, 1)

    def test_restart_attempt_while_it_stays_silent(self):
        self.check(0, 1000.0)
        self.check(0, 1030.0)
        self.assertEqual(self.st.status, "no_audio")
        with mock.patch.object(self.t, "stop_station") as stop:
            self.check(0, 1000.0 + tuner.NO_AUDIO_RESTART_SECS)
        stop.assert_called_once_with(self.st)
        self.assertEqual(self.st.retry_at, 1000.0 + tuner.NO_AUDIO_RESTART_SECS)

    def test_restarting_a_silent_station_does_not_report_streaming(self):
        self.st.status = "no_audio"
        self.st.cfg.update(rbds=False)
        self.t.devices = [("0", "00001039")]
        with mock.patch.object(tuner.subprocess, "Popen", return_value=mock.Mock(pid=1)), \
                mock.patch.object(self.t, "resolve_serial", return_value=0):
            self.t.start_station(self.st)
        self.assertEqual(self.st.status, "no_audio")
        self.assertIsNone(self.st.audio_bytes)

    def test_no_audio_is_not_up_in_zabbix(self):
        items = dict(self.t.zbx.state_items(
            {"id": "tuner3", "state": "no_audio"}))
        self.assertEqual(items["pituner.tuner.up[tuner3]"], 0)

    def test_poll_checks_running_stations_only(self):
        self.t.stations = [self.st]
        self.st.proc = mock.Mock(pid=4242, poll=mock.Mock(return_value=None))
        with mock.patch.object(self.t, "check_audio") as chk:
            self.t.poll()
        chk.assert_called_once()
        self.st.proc.poll.return_value = 1
        with mock.patch.object(self.t, "check_audio") as chk:
            self.t.poll()
        chk.assert_not_called()
        self.assertEqual(self.st.status, "down")


class AlertTests(unittest.TestCase):
    def test_no_audio_sends_a_down_email_that_names_the_cause(self):
        sent = []

        class FakeMailer:
            enabled, alert_station, down_delay = True, True, 60

            def send(self, subject, body):
                sent.append((subject, body))

        am = tuner.AlertManager(FakeMailer())
        am.station_status("W300BM", "no_audio", tuner.NO_AUDIO_DETAIL, 0)
        am.tick(30)
        self.assertEqual(sent, [])
        am.tick(61)
        self.assertEqual(sent[0][0], "W300BM is DOWN")
        self.assertIn("not delivering any audio", sent[0][1])
        am.station_status("W300BM", "streaming", "", 120)
        self.assertEqual(sent[1][0], "W300BM is back up")


if __name__ == "__main__":
    unittest.main()


class EnumerateDevicesTests(unittest.TestCase):
    def fake_rtl_test(self, script):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, "rtl_test")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + script + "\n")
        os.chmod(path, 0o755)
        p = mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ["PATH"]})
        p.start()
        self.addCleanup(p.stop)

    def test_lists_devices(self):
        self.fake_rtl_test("echo 'Found 1 device(s):'; "
                           "echo '  0:  Realtek, RTL2838UHIDIR, SN: 00001039'; sleep 30")
        self.assertEqual(tuner.enumerate_devices(timeout=5), [("0", "00001039")])

    def test_detect_subcommand_prints_index_and_serial(self):
        self.fake_rtl_test("echo 'Found 2 device(s):'; "
                           "echo '  0:  Realtek, RTL2838UHIDIR, SN: 00001015'; "
                           "echo '  1:  Realtek, RTL2838UHIDIR, SN: 00001016'; sleep 30")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(tuner.main(["detect"]), 0)
        self.assertEqual(out.getvalue(), "0\t00001015\n1\t00001016\n")

    def test_hung_rtl_test_cannot_block_the_supervisor(self):
        self.fake_rtl_test("sleep 30")            # silent, like a wedged libusb
        t0 = time.time()
        self.assertEqual(tuner.enumerate_devices(timeout=1), [])
        self.assertLess(time.time() - t0, 6)


class ServiceReadinessTests(unittest.TestCase):
    def test_ready_is_sent_before_the_slow_station_starts(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        t = tuner.Tuner(d)
        events = []

        def load():
            t.stations = [tuner.Station(dict(CFG)), tuner.Station(dict(CFG, name="B", mount="/tuner4"))]

        with mock.patch.object(t, "load_config", load), \
                mock.patch.object(t, "refresh_devices"), \
                mock.patch.object(t, "start_station", lambda st: events.append(("start", st.name))), \
                mock.patch.object(t, "poll", lambda: t.shutdown()), \
                mock.patch.object(t, "stop_station"), \
                mock.patch.object(t.alerts, "service_started"), \
                mock.patch.object(t.alerts, "service_stopped"), \
                mock.patch.object(tuner, "sd_notify", lambda s: events.append(("notify", s))), \
                mock.patch.object(tuner.time, "sleep"):
            t.run()
        kinds = [e for e in events if e[0] == "start" or e == ("notify", "READY=1")]
        self.assertEqual(kinds[0], ("notify", "READY=1"))       # before any station starts
        self.assertEqual([e for e in events if e[0] == "start"], [("start", "W300BM"), ("start", "B")])
        self.assertGreaterEqual(events.count(("notify", "WATCHDOG=1")), 2)   # fed while stations start


class LogRedactionTests(unittest.TestCase):
    def test_passwords_in_urls_are_masked(self):
        cmd = "rtl_fm -d 0 | ffmpeg icecast://source:hunter2@localhost:8000/tuner1"
        self.assertEqual(tuner.redact_secrets(cmd),
                         "rtl_fm -d 0 | ffmpeg icecast://source:***@localhost:8000/tuner1")
        self.assertEqual(tuner.redact_secrets(tuner.redact_secrets(cmd)), tuner.redact_secrets(cmd))
        self.assertEqual(tuner.redact_secrets("http://host/path no secret"), "http://host/path no secret")

    def test_log_and_log_file_never_write_the_password(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with mock.patch.object(tuner, "LOG_DIR", d), mock.patch("sys.stdout") as out:
            tuner.log("station A: device 0 -> ffmpeg icecast://source:hunter2@localhost/t")
            tuner.log_file("other.log", "icecast://source:hunter2@localhost/t")
        for name in ("tuner.log", "other.log"):
            with open(os.path.join(d, name)) as f:
                text = f.read()
            self.assertNotIn("hunter2", text)
            self.assertIn("source:***@", text)
        printed = "".join(str(c.args[0]) for c in out.write.call_args_list)
        self.assertNotIn("hunter2", printed)
