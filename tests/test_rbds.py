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
        self.assertIn('rbds-meta --dir "/opt/pituner" "/tuner1"', cmd)

    def test_no_rbds_when_disabled_or_unavailable(self):
        self.assertNotIn("redsea", tuner.build_command(station(rbds=False), ICE, 0, rbds=True))
        self.assertNotIn("redsea", tuner.build_command(station(), ICE, 0, rbds=False))

    def test_no_rbds_on_wx(self):
        cmd = tuner.build_command(station(band="wx"), ICE, 0, rbds=True)
        self.assertNotIn("redsea", cmd)


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
        with tempfile.TemporaryDirectory() as d:
            tuner.run_rbds_meta(d, "/tuner1", io.StringIO(lines),
                                lambda ice, m, s: sent.append((m, s)) or True)
        self.assertEqual(sent, [
            ("/tuner1", "KXYZ"),
            ("/tuner1", "Artist - Title (KXYZ)"),
            ("/tuner1", "Next - Song (KXYZ)"),
        ])


if __name__ == "__main__":
    unittest.main()
