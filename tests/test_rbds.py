import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tuner  # noqa: E402

ICE = {"host": "localhost", "port": "8000", "password": "pw"}


def station(**kw):
    cfg = {"name": "TEST", "band": "fm", "freq": 98.1, "serial": "1",
           "gain": "", "mount": "/tuner1", "rbds": True}
    cfg.update(kw)
    return cfg


class BuildCommandTests(unittest.TestCase):
    def test_rbds_tee_before_demux(self):
        cmd = tuner.build_command(station(), ICE, 0, rbds=True)
        self.assertIn("redsea", cmd)
        self.assertLess(cmd.index("redsea"), cmd.index("demux"))
        self.assertIn('rbds-meta --dir "/opt/pituner" --tuner tuner1 "/tuner1"', cmd)

    def test_no_rbds_when_disabled_or_unavailable(self):
        self.assertNotIn("redsea", tuner.build_command(station(rbds=False), ICE, 0, rbds=True))
        self.assertNotIn("redsea", tuner.build_command(station(), ICE, 0, rbds=False))

    def test_no_rbds_on_wx(self):
        cmd = tuner.build_command(station(band="wx"), ICE, 0, rbds=True)
        self.assertNotIn("redsea", cmd)


class GenreTests(unittest.TestCase):
    def test_wx_always_weather(self):
        for rbds in (True, False):
            cmd = tuner.build_command(station(band="wx"), ICE, 0, rbds=rbds, genre="Country")
            self.assertIn('-ice_genre "Weather"', cmd)
            self.assertNotIn("Country", cmd)

    def test_fm_genre_falls_back_to_radio(self):
        for kw in ({"rbds": True}, {"rbds": False}, {"rbds": True, "genre": ""}):
            self.assertIn('-ice_genre "Radio"', tuner.build_command(station(), ICE, 0, **kw))
        cmd = tuner.build_command(station(), ICE, 0, rbds=True, genre="Classic rock")
        self.assertIn('-ice_genre "Classic rock"', cmd)

    def test_genre_sanitized(self):
        cmd = tuner.build_command(station(), ICE, 0, genre='Rock"; rm -rf $HOME')
        self.assertNotIn('"; rm', cmd)
        self.assertIn('-ice_genre "Rock rm -rf HOME"', cmd)
        self.assertNotIn("$", cmd.split("-ice_genre")[1].split("-content_type")[0])

    def test_rbds_flag_on_decoder(self):
        self.assertIn("redsea -u -r 192000",
                      tuner.build_command(station(), ICE, 0, rbds=True))

    def test_pty_parsing(self):
        f = tuner.pty_from_json_line
        self.assertEqual(f('{"group":"0A","prog_type":"Country"}'), "Country")
        self.assertEqual(f('{"prog_type":"No PTY"}'), "")
        self.assertEqual(f('{"prog_type":""}'), "")
        self.assertEqual(f('{"prog_type":"Unknown"}'), "")
        self.assertIsNone(f('{"ps":"KXYZ"}'))
        self.assertIsNone(f("garbage"))

    def _scan(self, script, timeout=3.0):
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("rtl_fm", "sleep 30"), ("redsea", script)):
                path = os.path.join(d, name)
                with open(path, "w") as fh:
                    fh.write("#!/bin/bash\n" + body + "\n")
                os.chmod(path, 0o755)
            old = os.environ["PATH"]
            os.environ["PATH"] = d + os.pathsep + old
            try:
                return tuner.scan_pty(station(), 0, timeout=timeout)
            finally:
                os.environ["PATH"] = old

    def test_scan_finds_pty(self):
        self.assertEqual(self._scan(
            'echo \'{"ps":"KXYZ"}\'; echo \'{"prog_type":"Country"}\'; sleep 30'), "Country")

    def test_scan_timeout_and_no_pty(self):
        self.assertEqual(self._scan("sleep 30", timeout=1.0), "")
        self.assertEqual(self._scan('echo \'{"prog_type":"No PTY"}\'; sleep 30'), "")


class StationConfigTests(unittest.TestCase):
    def test_defaults_and_mounts(self):
        with tempfile.TemporaryDirectory() as d:
            for i, extra in enumerate(["RBDS=true", "", "BAND=wx\nRBDS=true"], 1):
                with open(os.path.join(d, f"s{i}.conf"), "w") as f:
                    f.write(f"NAME=S{i}\nFREQUENCY=100.1\n{extra}\n")
            st = tuner.load_stations(d)
        self.assertEqual([s["mount"] for s in st], ["/tuner1", "/tuner2", "/tuner3"])
        self.assertEqual([s["rbds"] for s in st], [True, False, False])


class SongTests(unittest.TestCase):
    def test_song_format(self):
        self.assertEqual(tuner.rbds_song({"ps": "KXYZ", "radiotext": "A - B"}), "A - B (KXYZ)")
        self.assertEqual(tuner.rbds_song({"ps": "KXYZ"}), "KXYZ")
        self.assertEqual(tuner.rbds_song({"radiotext": "A - B"}), "A - B")
        self.assertEqual(tuner.rbds_song({}), "")

    def test_run_dedupes_and_cleans(self):
        lines = "\n".join([
            '{"ps": "KXYZ    "}',
            "not json",
            '{"radiotext": "Artist - Title  \\r"}',
            '{"radiotext": "Artist - Title"}',
            '{"ps": "KXYZ"}',
            '{"radiotext": "Next - Song"}',
        ])
        sent = []
        t = [0.0]

        def clock():  # 5 s per line, so the PS label has settled by line 3
            t[0] += 5.0
            return t[0]
        with tempfile.TemporaryDirectory() as d:
            tuner.run_rbds_meta(d, "/tuner1", io.StringIO(lines),
                                lambda ice, m, s: sent.append((m, s)) or True,
                                clock=clock)
        self.assertEqual(sent, [
            ("/tuner1", "Artist - Title (KXYZ)"),
            ("/tuner1", "Next - Song (KXYZ)"),
        ])


if __name__ == "__main__":
    unittest.main()
