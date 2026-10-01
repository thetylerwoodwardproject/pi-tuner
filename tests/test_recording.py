import datetime
import glob
import io
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tuner  # noqa: E402

ICE = {"host": "localhost", "port": "8000", "password": "pw"}


def ts(*args):
    return datetime.datetime(*args).timestamp()


def station(**kw):
    cfg = {"name": "WXYZ-FM", "band": "fm", "freq": 98.1, "serial": "1",
           "gain": "", "mount": "/tuner1", "rbds": False, "record": True}
    cfg.update(kw)
    return cfg


class PathTests(unittest.TestCase):
    def test_recording_path_format(self):
        p = tuner.recording_path("/opt/pituner", "WXYZ-FM", ts(2026, 3, 5, 14, 7, 33))
        self.assertEqual(
            p, "/opt/pituner/recordings/WXYZ-FM/2026/03/05/260305_140733_WXYZ-FM.mp3")

    def test_name_is_filesystem_safe(self):
        p = tuner.recording_path("/b", "My 100%/Station ../x", ts(2026, 1, 2, 3, 4, 5))
        self.assertEqual(p, "/b/recordings/My_100Station_..x/2026/01/02/"
                            "260102_030405_My_100Station_..x.mp3")
        self.assertNotIn("%", os.path.basename(p))
        self.assertEqual(tuner._rec_name("../.."), "station")

    def test_next_boundary(self):
        nb = tuner.next_boundary
        self.assertEqual(nb(ts(2026, 3, 5, 14, 7, 33)), ts(2026, 3, 5, 14, 15))
        self.assertEqual(nb(ts(2026, 3, 5, 14, 15, 0)), ts(2026, 3, 5, 14, 30))
        self.assertEqual(nb(ts(2026, 3, 5, 14, 44, 59)), ts(2026, 3, 5, 14, 45))
        self.assertEqual(nb(ts(2026, 3, 5, 23, 55)), ts(2026, 3, 6, 0, 0))


class CommandTests(unittest.TestCase):
    def test_tee_only_when_enabled(self):
        self.assertIn(" record --dir ", tuner.build_command(station(), ICE, 0))
        self.assertNotIn(" record --dir ",
                         tuner.build_command(station(record=False), ICE, 0))

    def test_fm_and_wx_taps(self):
        fm = tuner.build_command(station(), ICE, 0, eas=True)
        self.assertLess(fm.index("demux"), fm.index(" record --dir "))
        self.assertLess(fm.index(" record --dir "), fm.index("detect-eas"))
        self.assertIn('"WXYZ-FM" 48000 2)', fm)
        wx = tuner.build_command(station(band="wx"), ICE, 0, eas=True)
        self.assertLess(wx.index("rtl_fm"), wx.index(" record --dir "))
        self.assertLess(wx.index(" record --dir "), wx.index("detect-eas"))
        self.assertIn('"WXYZ-FM" 25000 1)', wx)

    def test_config_keys(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.conf"), "w") as f:
                f.write("NAME=A\nFREQUENCY=100.1\nRECORD=true\nRECORD_KEEP_DAYS=14\n")
            with open(os.path.join(d, "b.conf"), "w") as f:
                f.write("NAME=B\nFREQUENCY=100.1\nRECORD_KEEP_DAYS=abc\n")
            a, b = tuner.load_stations(d)
        self.assertEqual((a["record"], a["record_keep_days"]), (True, 14))
        self.assertEqual((b["record"], b["record_keep_days"]), (False, 0))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg/ffprobe not installed")
class RecorderTests(unittest.TestCase):
    RATE = 8000

    def pcm(self, seconds):
        n = int(self.RATE * seconds)
        return b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(n))

    def fake_clock(self, start, step):
        t = [start - step]

        def clock():
            t[0] += step
            return t[0]
        return clock

    def test_rotates_into_dated_folders(self):
        with tempfile.TemporaryDirectory() as d:
            start = ts(2026, 3, 5, 23, 59, 58)  # crosses midnight
            tuner.run_recorder(d, "WXYZ-FM", self.RATE, 1,
                               stream=io.BytesIO(self.pcm(4.0)),
                               clock=self.fake_clock(start, 0.5),
                               boundary=lambda now: now + 1.0)
            files = sorted(glob.glob(os.path.join(d, "recordings", "WXYZ-FM", "*", "*", "*", "*.mp3")))
            rel = [os.path.relpath(f, os.path.join(d, "recordings", "WXYZ-FM")) for f in files]
            self.assertEqual(rel, [
                "2026/03/05/260305_235958_WXYZ-FM.mp3",
                "2026/03/05/260305_235959_WXYZ-FM.mp3",
                "2026/03/06/260306_000000_WXYZ-FM.mp3",
                "2026/03/06/260306_000001_WXYZ-FM.mp3",
            ])
            for f in files:
                out = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,bit_rate",
                     "-of", "default=nw=1", f], capture_output=True, text=True)
                self.assertIn("codec_name=mp3", out.stdout)
                self.assertGreater(os.path.getsize(f), 1000)

    def test_unwritable_dir_never_stops_draining(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "recordings")
            open(blocker, "w").close()  # a file where the folder should be
            stream = io.BytesIO(self.pcm(2.0))
            tuner.run_recorder(d, "X", self.RATE, 1, stream=stream,
                               clock=self.fake_clock(ts(2026, 3, 5, 12), 0.5),
                               boundary=lambda now: now + 1.0)
            self.assertEqual(stream.read(), b"")  # fully consumed


class PruneTests(unittest.TestCase):
    def touch(self, path, when):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        os.utime(path, (when, when))

    def test_prune(self):
        now = ts(2026, 3, 20, 12)
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "recordings", "S")
            old = os.path.join(root, "2026", "03", "01", "old.mp3")
            new = os.path.join(root, "2026", "03", "19", "new.mp3")
            self.touch(old, ts(2026, 3, 1, 12))
            self.touch(new, ts(2026, 3, 19, 12))
            self.assertEqual(tuner.prune_recordings(d, "S", 0, now), 0)
            self.assertTrue(os.path.exists(old))
            self.assertEqual(tuner.prune_recordings(d, "S", 14, now), 1)
            self.assertFalse(os.path.exists(old))
            self.assertFalse(os.path.exists(os.path.dirname(old)))  # empty day removed
            self.assertTrue(os.path.exists(new))
            self.assertTrue(os.path.isdir(root))


if __name__ == "__main__":
    unittest.main()
