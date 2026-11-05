import os
import re
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import tuner  # noqa: E402

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def conf(**kw):
    base = {"enabled": "true", "server": "127.0.0.1"}
    base.update({k.lower(): str(v) for k, v in kw.items()})
    return base


class FormatTests(unittest.TestCase):
    def line(self, key, value, **kw):
        return tuner.SyslogSender(conf(hostname="pi", **kw)).format(key, value, now=0).decode()

    def test_metric_is_rfc5424_with_structured_fields(self):
        m = self.line("pituner.tuner.level[tuner1]", -23.4)
        self.assertRegex(m, r"^<134>1 1970-01-01T00:00:00\.000Z pi pituner \d+ metric ")
        self.assertIn('[pituner@32473 key="pituner.tuner.level[tuner1\\]" value="-23.4" '
                      'tuner="tuner1" metric="level"]', m)
        self.assertTrue(m.endswith(" pituner.tuner.level[tuner1]=-23.4\n"))

    def test_global_item_has_no_tuner_fields(self):
        m = self.line("pituner.heartbeat", 1)
        self.assertNotIn("tuner=", m)
        self.assertIn('key="pituner.heartbeat" value="1"', m)

    def test_event_is_the_message_and_severity_follows_the_text(self):
        down = self.line("pituner.event", "station WXYZ down")
        ok = self.line("pituner.event", "station WXYZ streaming")
        self.assertTrue(down.startswith("<132>1 "))      # local0 + warning
        self.assertTrue(ok.startswith("<134>1 "))        # local0 + info
        self.assertTrue(down.endswith("] station WXYZ down\n"))

    def test_facility_and_newlines_and_quotes(self):
        m = self.line("pituner.tuner.rbds.rt[t1]", 'a "b"\nc]', facility="daemon")
        self.assertTrue(m.startswith("<30>1 "))          # daemon(3)*8 + info(6)
        self.assertEqual(m.count("\n"), 1)
        self.assertIn('value="a \\"b\\" c\\]"', m)

    def test_bad_settings_fall_back(self):
        s = tuner.SyslogSender(conf(protocol="carrier-pigeon", facility="nope", port="x"))
        self.assertEqual((s.protocol, s.facility, s.port), ("udp", 16, 514))

    def test_disabled_without_server_or_flag(self):
        self.assertFalse(tuner.SyslogSender({}).available)
        self.assertFalse(tuner.SyslogSender({"enabled": "true"}).available)


class WireTests(unittest.TestCase):
    def test_udp_one_datagram_per_item_and_discovery_skipped(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(3)
        s = tuner.SyslogSender(conf(port=rx.getsockname()[1]))
        s.send([(tuner.ZabbixSender.key_discovery, "{}"), ("pituner.heartbeat", 1),
                ("pituner.event", "hello")])
        got = [rx.recv(4096).decode() for _ in range(2)]
        rx.close()
        s._close()
        self.assertIn("pituner.heartbeat=1", got[0])
        self.assertTrue(got[1].endswith("hello"))
        self.assertFalse(any("discovery" in g for g in got))

    def test_tcp_newline_framed_and_reconnects(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        port = srv.getsockname()[1]
        received = []

        def serve():
            for _ in range(2):          # second connection proves the reconnect
                c, _a = srv.accept()
                with c:
                    buf = b""
                    c.settimeout(3)
                    try:
                        want = 1 if received else 2
                        while buf.count(b"\n") < want:
                            chunk = c.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                    except socket.timeout:
                        pass
                    received.append(buf)

        t = threading.Thread(target=serve)
        t.start()
        s = tuner.SyslogSender(conf(port=port, protocol="tcp"))
        s.send([("pituner.heartbeat", 1), ("pituner.event", "two")])
        s._sock.close()                  # simulate the server dropping us
        s.send([("pituner.event", "three")])
        t.join(10)
        srv.close()
        s._close()
        self.assertEqual(received[0].count(b"\n"), 2)
        self.assertTrue(received[1].decode().rstrip().endswith("three"))

    def test_unreachable_server_never_raises(self):
        s = tuner.SyslogSender(conf(port=1, protocol="tcp"))
        s.send([("pituner.event", "x")])      # connection refused: logged, not raised


class FanOutTests(unittest.TestCase):
    def test_zabbix_send_also_goes_to_syslog_even_with_zabbix_off(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(3)
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "syslog.conf"), "w") as f:
                f.write(f"ENABLED=true\nSERVER=127.0.0.1\nPORT={rx.getsockname()[1]}\n")
            with open(os.path.join(d, "zabbix.conf"), "w") as f:
                f.write("ENABLED=false\n")
            z = tuner.load_sender(d)
            self.assertTrue(z.enabled)
            self.assertFalse(z.zabbix_enabled)
            z.send_async([(z.tuner_key("level", "tuner1"), -20.0)])
            z.flush(3)
            self.assertIn("pituner.tuner.level[tuner1]=-20.0", rx.recv(4096).decode())
            z.syslog._close()
        rx.close()

    def test_nothing_enabled_means_not_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(tuner.load_sender(d).enabled)


class ConfigTests(unittest.TestCase):
    def test_shipped_file_covers_the_table_and_is_disabled(self):
        path = os.path.join(REPO, "syslog.conf")
        lines = tuner._read_lines(path)
        for spec in tuner.CONFIG_KEYS["syslog.conf"]:
            self.assertTrue(tuner._has_key(lines, spec[0]), spec[0])
        self.assertFalse(tuner.SyslogSender(tuner.parse_keyvalue(path)).enabled)

    def test_upgrade_creates_syslog_conf(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(tuner.upgrade_config(d).get("syslog.conf"), ["(new file)"])
            self.assertFalse(tuner.SyslogSender(
                tuner.parse_keyvalue(os.path.join(d, "syslog.conf"))).enabled)


if __name__ == "__main__":
    unittest.main()
