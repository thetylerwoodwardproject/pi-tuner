import base64
import io
import math
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tuner  # noqa: E402

HAVE_OPENSSL = shutil.which("openssl") is not None


class FakeSMTP:
    """Tiny SMTP server for tests: none / starttls / ssl, optional AUTH PLAIN."""

    def __init__(self, mode="none", cert=None, user="u", password="p",
                 require_auth=False, hang=False):
        self.mode, self.cert = mode, cert
        self.user, self.password, self.require_auth = user, password, require_auth
        self.hang = hang
        self.messages = []
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

    def _context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*self.cert)
        return ctx

    def _loop(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            self._live = conn
            try:
                self._handle(conn)
            except Exception:  # noqa: BLE001 - a test client may drop the link
                pass
            finally:
                try:
                    self._live.close()
                except OSError:
                    pass

    def _handle(self, conn):
        conn.settimeout(5)
        if self.hang:
            time.sleep(3)
            return
        if self.mode == "ssl":
            conn = self._live = self._context().wrap_socket(conn, server_side=True)
        f = conn.makefile("rwb", buffering=0)
        authed, tls = False, self.mode == "ssl"
        mail_from, rcpts = "", []

        def say(line):
            f.write((line + "\r\n").encode())

        say("220 fake ESMTP")
        while True:
            line = f.readline().decode().rstrip("\r\n")
            if not line:
                return
            cmd = line.upper()
            if cmd.startswith("EHLO"):
                say("250-fake")
                if self.mode == "starttls" and not tls:
                    say("250-STARTTLS")
                say("250-AUTH PLAIN")
                say("250 OK")
            elif cmd == "STARTTLS":
                say("220 go ahead")
                conn = self._live = self._context().wrap_socket(conn, server_side=True)
                f = conn.makefile("rwb", buffering=0)
                tls = True
            elif cmd.startswith("AUTH PLAIN"):
                token = line.split(" ", 2)[2] if line.count(" ") >= 2 else ""
                _, user, password = base64.b64decode(token).decode().split("\0")
                if (user, password) == (self.user, self.password):
                    authed = True
                    say("235 ok")
                else:
                    say("535 bad credentials")
            elif cmd.startswith("MAIL FROM"):
                if self.require_auth and not authed:
                    say("530 auth required")
                else:
                    mail_from, rcpts = line[10:].strip("<> "), []
                    say("250 ok")
            elif cmd.startswith("RCPT TO"):
                rcpts.append(line[8:].strip("<> "))
                say("250 ok")
            elif cmd == "DATA":
                say("354 go")
                data = b""
                while not data.endswith(b"\r\n.\r\n"):
                    data += f.readline()
                self.messages.append({"from": mail_from, "rcpts": rcpts,
                                      "data": data.decode(errors="replace")})
                say("250 queued")
            elif cmd == "QUIT":
                say("221 bye")
                return
            else:
                say("250 ok")


def make_cert(d):
    key, crt = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                    "-out", crt, "-days", "1", "-subj", "/CN=localhost"],
                   check=True, capture_output=True)
    return crt, key


def conf(port, **kw):
    c = {"enabled": "true", "host": "127.0.0.1", "port": str(port), "security": "none",
         "from": "pituner@example.com", "to": "a@example.com, b@example.com", "timeout": "3"}
    c.update(kw)
    return c


class MailerTests(unittest.TestCase):
    def test_plain_with_auth_to_multiple_recipients(self):
        srv = FakeSMTP(require_auth=True)
        self.addCleanup(srv.close)
        m = tuner.Mailer(conf(srv.port, username="u", password="p"))
        m.send_sync("WLSU is DOWN", "details here")
        msg = srv.messages[0]
        self.assertEqual(msg["rcpts"], ["a@example.com", "b@example.com"])
        self.assertEqual(msg["from"], "pituner@example.com")
        self.assertIn("Subject: [Pi-Tuner] WLSU is DOWN (", msg["data"])
        self.assertIn("details here", msg["data"])

    def test_wrong_password_raises(self):
        srv = FakeSMTP(require_auth=True)
        self.addCleanup(srv.close)
        m = tuner.Mailer(conf(srv.port, username="u", password="nope"))
        with self.assertRaises(Exception) as cm:
            m.send_sync("x", "y")
        self.assertIn("535", str(cm.exception))

    @unittest.skipUnless(HAVE_OPENSSL, "openssl not installed")
    def test_starttls_and_ssl_modes(self):
        with tempfile.TemporaryDirectory() as d:
            cert = make_cert(d)
            for mode in ("starttls", "ssl"):
                srv = FakeSMTP(mode=mode, cert=cert, require_auth=True)
                self.addCleanup(srv.close)
                m = tuner.Mailer(conf(srv.port, security=mode, verify_tls="false",
                                      username="u", password="p"))
                m.send_sync("hello", "world")
                self.assertEqual(len(srv.messages), 1, mode)

    @unittest.skipUnless(HAVE_OPENSSL, "openssl not installed")
    def test_self_signed_cert_rejected_when_verifying(self):
        with tempfile.TemporaryDirectory() as d:
            srv = FakeSMTP(mode="ssl", cert=make_cert(d))
            self.addCleanup(srv.close)
            m = tuner.Mailer(conf(srv.port, security="ssl", verify_tls="true"))
            with self.assertRaises(ssl.SSLError):
                m.send_sync("x", "y")

    def test_async_send_delivers_and_flush_waits(self):
        srv = FakeSMTP()
        self.addCleanup(srv.close)
        m = tuner.Mailer(conf(srv.port))
        m.send("queued", "body")
        m.flush(5)
        self.assertEqual(len(srv.messages), 1)

    def test_disabled_or_misconfigured_sends_nothing(self):
        for c, why in (({}, "ENABLED"), ({"enabled": "true"}, "HOST"),
                       ({"enabled": "true", "host": "h"}, "TO")):
            m = tuner.Mailer(c)
            self.assertFalse(m.enabled)
            self.assertIn(why, m.problem())
            m.send("x", "y")          # no-op, no thread, no error
            self.assertIsNone(m._worker)

    def test_failure_is_logged_not_raised_and_does_not_block(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                      # nothing listens here
        m = tuner.Mailer(conf(port))
        m.ATTEMPT_DELAYS = (0.0, 0.0)
        with mock.patch.object(tuner, "log_file") as lf, mock.patch.object(tuner, "log"):
            start = time.time()
            m.send("will fail", "body")
            self.assertLess(time.time() - start, 0.5)
            m.flush(5)
        self.assertTrue(any("FAILED" in c.args[1] for c in lf.call_args_list))

    def test_hung_server_never_blocks_the_caller(self):
        srv = FakeSMTP(hang=True)
        self.addCleanup(srv.close)
        m = tuner.Mailer(conf(srv.port, timeout="1"))
        start = time.time()
        for _ in range(3):
            m.send("x", "y")
        self.assertLess(time.time() - start, 0.5)

    def test_password_is_never_logged(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        m = tuner.Mailer(conf(port, username="u", password="SECRETPW"))
        m.ATTEMPT_DELAYS = (0.0, 0.0)
        with mock.patch.object(tuner, "log_file") as lf, mock.patch.object(tuner, "log") as lg:
            m.send("x", "y")
            m.flush(5)
        text = " ".join(str(c) for c in lf.call_args_list + lg.call_args_list)
        self.assertNotIn("SECRETPW", text)


class FakeMailer:
    def __init__(self, **kw):
        self.enabled = True
        self.alert_station = self.alert_eas = self.alert_disk = self.alert_service = True
        self.down_delay = 120
        self.disk_min_gb = 2.0
        self.__dict__.update(kw)
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))

    def flush(self, timeout=0):
        self.flushed = True


class AlertManagerTests(unittest.TestCase):
    def mgr(self, **kw):
        fm = FakeMailer(**kw)
        return tuner.AlertManager(fm), fm

    def test_blip_sends_nothing(self):
        am, fm = self.mgr()
        am.station_status("WLSU", "down", "exit code 1", 1000)
        am.tick(1060)
        am.station_status("WLSU", "streaming", "", 1070)
        am.tick(1300)
        self.assertEqual(fm.sent, [])

    def test_down_after_delay_then_recovery_with_duration(self):
        am, fm = self.mgr()
        am.station_status("WLSU", "down", "exit code 1", 1000)
        am.tick(1119)
        self.assertEqual(fm.sent, [])
        am.tick(1120)
        am.tick(1200)                  # only once
        self.assertEqual([s for s, _ in fm.sent], ["WLSU is DOWN"])
        self.assertIn("exit code 1", fm.sent[0][1])
        am.station_status("WLSU", "streaming", "", 1500)
        self.assertEqual([s for s, _ in fm.sent], ["WLSU is DOWN", "WLSU is back up"])
        self.assertIn("8m 20s", fm.sent[1][1])

    def test_serial_not_found_counts_as_down(self):
        am, fm = self.mgr()
        am.station_status("Z93", "serial_not_found", "serial 1 not found", 0)
        am.tick(200)
        self.assertEqual(fm.sent[0][0], "Z93 is DOWN")
        self.assertIn("serial was not found", fm.sent[0][1])

    def test_flapping_sends_one_down_and_one_recovery(self):
        am, fm = self.mgr()
        t = 0
        for _ in range(20):            # down/up every 10-20 s for a while
            am.station_status("S", "down", "", t); t += 10
            am.tick(t)
            am.station_status("S", "streaming", "", t); t += 10
        am.station_status("S", "down", "", t)
        am.tick(t + 130)
        am.station_status("S", "streaming", "", t + 200)
        self.assertEqual([s for s, _ in fm.sent], ["S is DOWN", "S is back up"])

    def test_down_status_change_keeps_original_time(self):
        am, fm = self.mgr()
        am.station_status("S", "down", "", 0)
        am.station_status("S", "serial_not_found", "gone", 60)
        am.tick(125)
        self.assertEqual(len(fm.sent), 1)

    def test_reload_keeps_state_and_drops_removed_stations(self):
        am, fm = self.mgr()
        am.station_status("A", "down", "", 0)
        am.station_status("B", "down", "", 0)
        am.sync_stations({"A"})
        am.tick(500)
        self.assertEqual([s for s, _ in fm.sent], ["A is DOWN"])

    def test_toggles_and_disabled(self):
        am, fm = self.mgr(alert_station=False)
        am.station_status("S", "down", "", 0)
        am.tick(500)
        self.assertEqual(fm.sent, [])
        am, fm = self.mgr(enabled=False)
        am.station_status("S", "down", "", 0)
        am.tick(500)
        am.service_started({"S": "streaming"})
        self.assertEqual(fm.sent, [])
        am, fm = self.mgr(alert_service=False)
        am.service_started({"S": "streaming"})
        am.service_stopped()
        self.assertEqual(fm.sent, [])

    def test_disk_low_alert_with_hysteresis(self):
        free = {"gb": 5.0}

        def usage(path):
            return shutil._ntuple_diskusage(100 * 1024 ** 3, 0, int(free["gb"] * 1024 ** 3))
        fm = FakeMailer()
        am = tuner.AlertManager(fm, disk_usage=usage)
        t = 0
        am.check_disk(t, "/opt/pituner", True)
        self.assertEqual(fm.sent, [])
        free["gb"] = 1.5; t += 301
        am.check_disk(t, "/opt/pituner", True)
        self.assertEqual([s for s, _ in fm.sent], ["Low disk space"])
        free["gb"] = 1.0; t += 301
        am.check_disk(t, "/opt/pituner", True)               # still low: no repeat
        free["gb"] = 2.2; t += 301                           # above 2 GB but below 2.5: wait
        am.check_disk(t, "/opt/pituner", True)
        self.assertEqual(len(fm.sent), 1)
        free["gb"] = 3.0; t += 301
        am.check_disk(t, "/opt/pituner", True)
        self.assertEqual([s for s, _ in fm.sent], ["Low disk space", "Disk space recovered"])

    def test_disk_check_only_when_recording_and_rate_limited(self):
        calls = []
        fm = FakeMailer()
        am = tuner.AlertManager(fm, disk_usage=lambda p: calls.append(p) or shutil._ntuple_diskusage(1, 0, 0))
        am.check_disk(0, "/x", False)
        self.assertEqual(calls, [])
        am.check_disk(1, "/x", True)
        am.check_disk(2, "/x", True)
        self.assertEqual(len(calls), 1)

    def test_started_and_stopped(self):
        am, fm = self.mgr()
        am.service_started({"WLSU": "streaming", "Z93": "serial_not_found"})
        am.service_stopped()
        self.assertEqual([s for s, _ in fm.sent], ["Pi-Tuner started", "Pi-Tuner stopped"])
        self.assertIn("Z93: serial_not_found", fm.sent[0][1])
        self.assertTrue(fm.flushed)


class TunerWiringTests(unittest.TestCase):
    def write(self, d, name, text):
        with open(os.path.join(d, name), "w") as f:
            f.write(text)

    def test_set_status_feeds_alerts_and_startup_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "smtp.conf", "ENABLED=true\nHOST=h\nTO=a@b.c\n")
            t = tuner.Tuner(d)
            t.load_config()
            fake = FakeMailer()
            t.alerts.mailer = fake
            st = tuner.Station({"name": "WLSU"})
            with mock.patch.object(tuner, "log"):
                t.set_status(st, "streaming")             # stopped -> streaming at startup
                t.set_status(st, "down", "exit code 1")
            t.alerts.tick(time.time() + 500)
            self.assertEqual([s for s, _ in fake.sent], ["WLSU is DOWN"])

    def test_eas_runs_for_email_only_configs(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "smtp.conf", "ENABLED=true\nHOST=h\nTO=a@b.c\n")
            t = tuner.Tuner(d)
            t.load_config()
            self.assertTrue(t.eas_enabled)
            self.write(d, "smtp.conf", "ENABLED=true\nHOST=h\nTO=a@b.c\nALERT_EAS=false\n")
            t.load_config()
            self.assertFalse(t.eas_enabled)
            self.write(d, "smtp.conf", "ENABLED=false\n")
            t.load_config()
            self.assertFalse(t.eas_enabled)


class EasEmailTests(unittest.TestCase):
    def test_tone_sends_one_email(self):
        rate = 8000
        n = rate * 6
        pcm = b"".join(struct.pack("<h", int(8000 * (math.sin(2 * math.pi * 853 * i / rate)
                                                      + math.sin(2 * math.pi * 960 * i / rate))))
                       for i in range(n))
        fake = FakeMailer()
        stdin = mock.Mock(buffer=io.BytesIO(pcm))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(tuner, "Mailer", lambda conf: fake), \
                mock.patch.object(tuner, "log_file"), \
                mock.patch.object(sys, "stdin", stdin):
            tuner.run_eas_detector(d, "WLSU", rate, 1)
        self.assertEqual(len(fake.sent), 1)
        self.assertIn("EAS attention tone heard on WLSU", fake.sent[0][0])
        self.assertTrue(fake.flushed)


class TestEmailCommandTests(unittest.TestCase):
    def run_cmd(self, d, *extra):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = tuner._main_test_email(["--dir", d, *extra])
        return rc, out.getvalue()

    def test_success(self):
        srv = FakeSMTP()
        self.addCleanup(srv.close)
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "smtp.conf"), "w") as f:
                f.write("\n".join(f"{k}={v}" for k, v in conf(srv.port).items()))
            rc, out = self.run_cmd(d)
            self.assertEqual(rc, 0)
            self.assertIn("Test email sent", out)
            rc, _ = self.run_cmd(d, "--to", "only@example.com")
            self.assertEqual(srv.messages[-1]["rcpts"], ["only@example.com"])
        self.assertEqual(len(srv.messages), 2)
        self.assertIn("Subject: [Pi-Tuner] Test email", srv.messages[0]["data"])

    def test_failure_and_not_enabled(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with tempfile.TemporaryDirectory() as d:
            rc, out = self.run_cmd(d)
            self.assertEqual(rc, 1)
            self.assertIn("not enabled", out)
            with open(os.path.join(d, "smtp.conf"), "w") as f:
                f.write("\n".join(f"{k}={v}" for k, v in conf(port).items()))
            rc, out = self.run_cmd(d)
            self.assertEqual(rc, 1)
            self.assertIn("FAILED", out)


if __name__ == "__main__":
    unittest.main()
