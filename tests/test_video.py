"""Video support tests: stream probing, Tighten / extract of a video, timeline export (sync, progress,
cancel), the GUI's import / waveform proxy / Tighten / Trash endpoints, and silent / short input.

Safe to run on a machine with real projects: everything happens in a projects folder of its own
(SOUNDLAB_PROJECTS, a temporary folder inside the repo, removed at the end) with SOUNDLAB_TRASH and
SOUNDLAB_REVEAL_CMD pointing at test folders. The user's projects/, projects/.current and Trash are
never read or written; no microphone is used (all media is generated with ffmpeg).

    .venv/bin/python tests/test_video.py
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Must be set before soundlab is imported: projects / gui read them at import time. The projects folder
# has to be inside the repo (the GUI's API paths are relative to it), but it is not projects/.
WORK = Path(tempfile.mkdtemp(prefix="soundlab-videotest-"))
TEST_PROJECTS = Path(tempfile.mkdtemp(prefix=".soundlab-test-projects-", dir=ROOT))
atexit.register(shutil.rmtree, TEST_PROJECTS, ignore_errors=True)  # also if setup fails halfway
atexit.register(shutil.rmtree, WORK, ignore_errors=True)
os.environ["SOUNDLAB_PROJECTS"] = str(TEST_PROJECTS)
os.environ["SOUNDLAB_TRASH"] = str(WORK / "trash")
os.environ["SOUNDLAB_REVEAL_CMD"] = "true"
sys.path.insert(0, str(ROOT))

from soundlab import editor, projects  # noqa: E402
from soundlab.editor import (Clip, export_timeline, extract, has_audio_stream, has_video_stream,  # noqa: E402
                             keep_segments, probe_video_info, tighten)
from soundlab.gui import app  # noqa: E402
from soundlab.pipeline import (CODECS, Cancelled, EnhanceOptions, PipelineError, _loudnorm_filter,  # noqa: E402
                               enhance, measure_loudness, probe_duration)

USER_PROJECTS = ROOT / "projects"
FPS = 30
RATE = 48000


def _run(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", *args], check=True)


def make_sync_video(path: Path, duration: float, bursts: list[tuple[float, float]], extra: tuple[str, ...] = ()) -> None:
    """A 30 fps video that is white exactly while a 440 Hz tone sounds and black otherwise: after any
    cut, picture and sound must still switch together."""
    cond = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in bursts)
    _run(*extra,
         "-f", "lavfi", "-i", f"color=c=black:s=64x36:r={FPS}:d={duration}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}:sample_rate={RATE}",
         "-filter_complex", f"[0:v]geq=lum='255*gt({cond.replace('t,', 'T,')},0)':cb=128:cr=128[v];"
                            f"[1:a]volume='gt({cond},0)':eval=frame[a]",
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", str(path))


def edges(values: list[bool], step: float) -> list[float]:
    return [i * step for i in range(1, len(values)) if values[i] != values[i - 1]]


def sync_offsets(path: Path) -> tuple[list[float], list[float]]:
    """Times where the picture switches black/white and where the tone starts/stops."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-vf", "scale=8:8",
                          "-pix_fmt", "gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    frames = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    picture = [sum(f) / len(f) > 128 for f in frames]
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-ac", "1", "-ar", str(RATE),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    win = RATE // 1000  # 1 ms windows
    import array
    samples = array.array("h", pcm)
    sound = [max(abs(x) for x in samples[i:i + win]) > 3000 for i in range(0, len(samples) - win, win)]
    return edges(picture, 1 / FPS), edges(sound, 0.001)


def assert_in_sync(test: unittest.TestCase, path: Path, expected_edges: int) -> None:
    pic, snd = sync_offsets(path)
    test.assertEqual(len(pic), expected_edges, f"picture edges {pic}")
    test.assertEqual(len(snd), expected_edges, f"sound edges {snd}")
    diffs = [p - s for p, s in zip(pic, snd)]
    # The source's own edges sit up to a frame off the tone (a frame shows 33 ms), each cut lands on a
    # whole frame (± half a frame): up to 2 frames per edge, but no drift building up over the cuts
    # (the unfixed branch was 0.3 s off after 40 cuts)
    test.assertLess(max(abs(d) for d in diffs), 2 / FPS, f"picture vs sound edges: {list(zip(pic, snd))}")
    n = len(diffs) // 3
    test.assertLess(abs(sum(diffs[-n:]) / n - sum(diffs[:n]) / n), 0.5 / FPS, "picture drifts from the sound")


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict[str, float]:
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.stat().st_mtime for p in root.rglob("*")}


class VideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user_before = snapshot(USER_PROJECTS)
        current = USER_PROJECTS / ".current"
        cls.current_before = current.read_text() if current.exists() else None
        projects.create("video-test")
        projects.set_current("video-test")
        cls.project = projects.current()
        cls.out_root = cls.project.outputs
        cls.media = WORK / "media"
        cls.media.mkdir()
        # 20 tone bursts with 0.6 s pauses: Tighten makes 20 cuts
        cls.bursts = [(0.4 + i * 0.95, 0.75 + i * 0.95) for i in range(20)]
        cls.video = cls.media / "talking_head.mp4"
        make_sync_video(cls.video, 20.0, cls.bursts)
        cls.tone = cls.media / "tone.wav"
        _run("-f", "lavfi", "-i", f"sine=frequency=880:duration=2.0:sample_rate={RATE}", "-c:a", "pcm_s16le",
             str(cls.tone))
        cls.sources = {p: digest(p) for p in (cls.video, cls.tone)}

    @classmethod
    def tearDownClass(cls):
        # the sources were only read
        for p, d in cls.sources.items():
            assert digest(p) == d, f"source changed: {p}"
        shutil.rmtree(TEST_PROJECTS, ignore_errors=True)  # this test's own projects folder
        shutil.rmtree(WORK, ignore_errors=True)
        current = USER_PROJECTS / ".current"
        assert (current.read_text() if current.exists() else None) == cls.current_before, "projects/.current changed"
        assert snapshot(USER_PROJECTS) == cls.user_before, "the user's projects/ changed"

    def test_01_stream_detection(self):
        self.assertTrue(has_video_stream(self.video))
        self.assertTrue(has_audio_stream(self.video))
        self.assertFalse(has_video_stream(self.tone))
        self.assertTrue(has_audio_stream(self.tone))
        self.assertEqual(probe_video_info(self.video), {"width": 64, "height": 36, "fps": "30/1"})
        # a phone's portrait video: stored landscape with a rotation, displayed (and exported) portrait
        rotated = self.media / "portrait.mp4"
        _run("-i", str(self.video), "-c", "copy", "-metadata:s:v:0", "rotate=90", str(rotated))
        if subprocess.run(["ffprobe", "-v", "error", "-show_streams", str(rotated)], capture_output=True,
                          text=True).stdout.find("rotation") == -1:
            _run("-display_rotation:v:0", "90", "-i", str(self.video), "-c", "copy", str(rotated))
        info = probe_video_info(rotated)
        self.assertEqual((info["width"], info["height"]), (36, 64))

    def test_02_formats(self):
        # video is never an audio format choice (CLI --format, GUI format menu)
        self.assertEqual(sorted(CODECS), ["m4a", "wav"])

    def test_03_video_tighten_stays_in_sync(self):
        data = tighten(self.video, 0.0, 20.0, WORK / "tight", max_pause=0.1)
        self.assertIsNotNone(data)
        out = Path(data["output"])
        self.assertEqual(out.suffix, ".mp4")
        self.assertTrue(data["is_video"])
        self.assertTrue(has_video_stream(out) and has_audio_stream(out))
        self.assertGreaterEqual(data["pauses_shortened"], 19)
        self.assertLess(probe_duration(out), 10.0)
        assert_in_sync(self, out, 2 * len(self.bursts))
        # a range that starts mid-file: the picture is cut at the same source times as the sound
        data = tighten(self.video, 3.0, 14.5, WORK / "tight", max_pause=0.1)
        inside = [b for b in self.bursts if b[0] >= 3.0 and b[1] <= 14.5]
        assert_in_sync(self, Path(data["output"]), 2 * len(inside))

    def test_04_video_extract(self):
        data = extract(self.video, 1.0, 4.0, WORK / "extract")
        out = Path(data["output"])
        self.assertEqual(out.suffix, ".mp4")
        self.assertTrue(has_video_stream(out) and has_audio_stream(out))
        self.assertAlmostEqual(probe_duration(out), 3.0, delta=0.1)

    def test_05_timeline_export_mixed_and_sped_up(self):
        # sped-up video clips around an audio-only clip (black picture): atempo's real lengths decide
        # each clip's frame count, so the picture stays on the sound across all clips
        clips = [Clip(self.video, 0.2 + i * 1.9, 0.2 + i * 1.9 + 1.7, 1.37) for i in range(5)]
        clips.insert(2, Clip(self.tone, 0.0, 1.0))
        seen = []
        data = export_timeline(clips, enhance_it=False, opts=EnhanceOptions(outputs_root=self.out_root),
                               log=lambda _: None, progress=seen.append)
        out = Path(data["output"])
        self.assertEqual(out.suffix, ".mp4")
        self.assertTrue(out.is_relative_to(self.out_root))
        self.assertTrue(data["is_video"])
        self.assertTrue(out.with_suffix(".json").is_file())
        self.assertAlmostEqual(data["duration_sec"], 5 * 1.7 / 1.37 + 1.0, delta=0.1)
        self.assertTrue(seen and seen == sorted(seen) and 0 <= seen[0] and seen[-1] <= 1)
        a = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
                                       "-of", "json", str(out)], capture_output=True, text=True).stdout)
        durs = {s["codec_type"]: float(s["duration"]) for s in a["streams"]}
        self.assertLess(abs(durs["video"] - durs["audio"]), 1 / FPS)
        pic, snd = sync_offsets(out)
        # the tone clip in the middle is white-less (black) but sounds: compare only the video clips' edges
        self.assertTrue(pic)
        for p in pic:
            self.assertLess(min(abs(p - s) for s in snd), 2 / FPS, f"picture edge {p} has no sound edge")

    def test_06_enhanced_video_export_and_cancel(self):
        clips = [Clip(self.video, 0.0, 6.0)]
        seen = []
        data = export_timeline(clips, enhance_it=True, opts=EnhanceOptions(outputs_root=self.out_root),
                               log=lambda _: None, progress=seen.append)
        out = Path(data["output"])
        self.assertEqual(out.suffix, ".mp4")
        self.assertEqual(data["normalization_type"], "linear")
        self.assertAlmostEqual(measure_loudness(out)["lufs"], -16.0, delta=0.6)
        self.assertGreater(max(seen), editor.ENHANCE_SHARE)  # the encode reports progress too
        # cancel during the video encode: Cancelled propagates, ffmpeg stops, no output is left
        before = set(self.out_root.rglob("*"))

        def cancel_in_encode(f: float) -> None:
            if f > editor.ENHANCE_SHARE:
                raise Cancelled("Cancelled")

        with self.assertRaises(Cancelled):
            export_timeline(clips, enhance_it=True, opts=EnhanceOptions(outputs_root=self.out_root),
                            log=lambda _: None, progress=cancel_in_encode)
        self.assertEqual({p for p in set(self.out_root.rglob("*")) - before if p.is_file()}, set())

    def test_07_gui_endpoints(self):
        client = app.test_client()
        with open(self.video, "rb") as f:
            res = client.post("/api/import", data={"files": (f, "upload.mp4")})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        rel = res.get_json()["imported"][0]
        self.assertTrue((ROOT / rel).is_relative_to(TEST_PROJECTS))

        # a video without sound is refused
        silent_video = self.media / "no_audio.mp4"
        _run("-f", "lavfi", "-i", "color=c=black:s=64x36:r=30:d=1", "-c:v", "libx264", str(silent_video))
        with open(silent_video, "rb") as f:
            res = client.post("/api/import", data={"files": (f, "mute.mp4")})
        self.assertEqual(res.status_code, 400)

        media = client.get("/api/library").get_json()["groups"][0]["files"]
        self.assertEqual([f["is_video"] for f in media if f["path"] == rel], [True])

        res = client.get(f"/api/audio?path={rel}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content_type, "audio/wav")
        self.assertGreater(len(res.data), RATE)

        res = client.post("/api/tighten", json={"path": rel, "start": 0, "end": 20.0, "max_pause": 0.2})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        tight = res.get_json()["path"]
        self.assertTrue(tight.endswith("_tight.mp4"))

        # export as a background job, polled like the browser does
        res = client.post("/api/export", json={"clips": [{"path": rel, "start": 0, "end": 3, "speed": 1}],
                                               "enhance": False, "format": "m4a"})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        job = res.get_json()["job"]
        for _ in range(600):
            st = client.get(f"/api/jobs/{job}").get_json()
            if st["status"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(st["status"], "done", st)
        self.assertTrue(st["result"]["path"].endswith(".mp4"))

        # Delete moves to SOUNDLAB_TRASH
        for path in (rel, tight):
            res = client.post("/api/trash", json={"path": path})
            self.assertEqual(res.status_code, 200)
            self.assertFalse((ROOT / path).exists())
            self.assertTrue((WORK / "trash" / Path(path).name).is_file())

    def test_08_silent_and_short_input(self):
        bad = {"input_i": "-inf", "input_tp": "-inf", "input_lra": "0.00", "input_thresh": "-70.00",
               "target_offset": "inf"}
        f = _loudnorm_filter(EnhanceOptions(), bad)
        self.assertNotIn("inf", f)
        self.assertNotIn("linear=true", f)

        silent = self.media / "silence.wav"
        _run("-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=mono", "-t", "3", "-c:a", "pcm_s16le", str(silent))
        lvl = measure_loudness(silent)
        self.assertEqual((lvl["lufs"], lvl["true_peak"]), (-99.0, -99.0))
        for src in (silent, self.media / "short.wav"):
            if src.name == "short.wav":  # shorter than loudnorm's 400 ms gating block
                _run("-f", "lavfi", "-i", f"sine=frequency=300:duration=0.2:sample_rate={RATE}",
                     "-c:a", "pcm_s16le", str(src))
            with self.assertRaises(PipelineError) as ctx:
                enhance(src, EnhanceOptions(outputs_root=self.out_root), log=lambda _: None)
            self.assertIn("empty or too quiet", str(ctx.exception))

    def test_09_keep_segments_chunks_render_identically(self):
        keep = [[round(i * 0.019, 3), round(i * 0.019 + 0.012, 3)] for i in range(100)]
        chunked, whole = WORK / "chunked.wav", WORK / "whole.wav"
        keep_segments(self.tone, keep, chunked, ["-c:a", "pcm_s24le"])
        saved = editor.KEEP_CHUNK
        editor.KEEP_CHUNK = 1000
        try:
            keep_segments(self.tone, keep, whole, ["-c:a", "pcm_s24le"])
        finally:
            editor.KEEP_CHUNK = saved
        pcm = lambda p: subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "s32le", "-"],  # noqa: E731
                                       capture_output=True, check=True).stdout
        self.assertEqual(pcm(chunked), pcm(whole))


if __name__ == "__main__":
    unittest.main(verbosity=2)
