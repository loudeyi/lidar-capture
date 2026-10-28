import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lidar_capture.monitor import SizeTrack, human_duration, human_rate, human_size  # noqa: E402


class TestVerdicts(unittest.TestCase):
    def test_empty(self):
        track = SizeTrack()
        self.assertEqual(track.verdict(0), "empty")

    def test_growing_needs_several_samples(self):
        track = SizeTrack(window=10)
        track.add(0, 100)
        self.assertEqual(track.verdict(0.1), "stalled")  # 只有一个采样，还不能下结论
        track.add(1, 200)
        track.add(2, 300)
        self.assertEqual(track.verdict(2), "growing")

    def test_flat_is_stalled(self):
        track = SizeTrack(window=10)
        for i in range(4):
            track.add(i, 500)
        self.assertEqual(track.verdict(3), "stalled")

    def test_shrunk(self):
        track = SizeTrack(window=10)
        track.add(0, 900)
        track.add(1, 100)
        self.assertEqual(track.verdict(1), "shrunk")

    def test_missing_and_recover(self):
        track = SizeTrack(window=10)
        track.add(0, 900)
        track.add(1, None)
        self.assertEqual(track.verdict(1), "missing")
        self.assertEqual(track.last_known_size(), 900)

    def test_window_drops_old_samples(self):
        track = SizeTrack(window=2.0)
        track.add(0, 10)
        track.add(10, 20)
        self.assertEqual(track.verdict(10), "stalled")  # 0 已滑出窗口，只剩一个采样


class TestStable(unittest.TestCase):
    def test_stable_for_after_stop(self):
        track = SizeTrack(window=30)
        for i in range(6):
            track.add(i, 1234)
        self.assertTrue(track.stable_for(5, 3.0))
        self.assertFalse(track.stable_for(5, 8.0))  # 采样跨度不够长，不能乱判

    def test_not_stable_when_growing(self):
        track = SizeTrack(window=30)
        for i in range(6):
            track.add(i, 1000 + i * 100)
        self.assertFalse(track.stable_for(5, 3.0))

    def test_not_stable_when_missing(self):
        track = SizeTrack(window=30)
        track.add(0, 100)
        track.add(1, None)
        self.assertFalse(track.stable_for(1, 0.5))


class TestRate(unittest.TestCase):
    def test_rate(self):
        track = SizeTrack(window=60)
        track.add(0, 0)
        track.add(2, 4 * 1024 * 1024)
        self.assertAlmostEqual(track.rate(2), 2 * 1024 * 1024, delta=1)

    def test_rate_none_with_single_sample(self):
        track = SizeTrack()
        track.add(0, 5)
        self.assertIsNone(track.rate(0))


class TestFormat(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(None), "—")
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(2048), "2.00 KB")
        self.assertEqual(human_size(3 * 1024 * 1024), "3.00 MB")

    def test_human_rate(self):
        self.assertEqual(human_rate(None), "—")
        self.assertEqual(human_rate(1024), "1.00 KB/s")

    def test_human_duration(self):
        self.assertEqual(human_duration(5), "00:05")
        self.assertEqual(human_duration(65), "01:05")
        self.assertEqual(human_duration(3661), "01:01:01")


if __name__ == "__main__":
    unittest.main()
