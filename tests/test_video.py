"""Unit and integration tests for video processing: tighten, extract, proxy, and timeline export."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from soundlab import projects
from soundlab.editor import (Clip, export_timeline, extract, has_audio_stream,
                             has_video_stream, probe_video_info, tighten)
from soundlab.gui import app
from soundlab.pipeline import probe_duration


def make_test_video(path: Path, duration: float = 6.0, speech_times: list[tuple[float, float]] | None = None) -> None:
    """Generates a test MP4 video (h264 640x360 30fps) with audio tones at specified intervals and silence elsewhere."""
    path.parent.mkdir(parents=True, exist_ok=True)
    speech_times = speech_times or [(0.5, 2.0), (3.5, 5.0)]

    condition = " + ".join(f"between(t,{a},{b})" for a, b in speech_times)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=640x360:rate=30",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-af", f"volume='{condition}':eval=frame",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "128k",
            str(path)
        ],
        capture_output=True, check=True
    )


class TestVideoProcessing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = Path(tempfile.mkdtemp(prefix="soundlab-vidtest-"))
        cls.src_video = cls.tmp_dir / "test_talking_head.mp4"
        # 6s video with speech at 0.5-2.0 and 3.5-5.0 (pause of 1.5s in between)
        make_test_video(cls.src_video, duration=6.0, speech_times=[(0.5, 2.0), (3.5, 5.0)])

        cls.src_audio = cls.tmp_dir / "test_tone.wav"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=2.0",
             "-c:a", "pcm_s16le", str(cls.src_audio)],
            capture_output=True, check=True
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def test_01_stream_detection(self):
        self.assertTrue(has_video_stream(self.src_video))
        self.assertTrue(has_audio_stream(self.src_video))

        self.assertFalse(has_video_stream(self.src_audio))
        self.assertTrue(has_audio_stream(self.src_audio))

        vinfo = probe_video_info(self.src_video)
        self.assertEqual(vinfo["width"], 640)
        self.assertEqual(vinfo["height"], 360)

    def test_02_video_tighten(self):
        out_dir = self.tmp_dir / "tight_out"
        # Tighten with max_pause 0.2s
        data = tighten(self.src_video, 0.0, 6.0, out_dir, max_pause=0.2)
        self.assertIsNotNone(data)
        out_file = Path(data["output"])
        self.assertTrue(out_file.exists())
        self.assertEqual(out_file.suffix, ".mp4")
        self.assertTrue(has_video_stream(out_file))
        self.assertTrue(has_audio_stream(out_file))

        # Check that silence at ends and pause in middle were shortened
        out_dur = probe_duration(out_file)
        self.assertLess(out_dur, 5.5)
        self.assertGreater(out_dur, 2.0)
        self.assertTrue(data.get("is_video"))

    def test_03_video_extract(self):
        out_dir = self.tmp_dir / "extract_out"
        data = extract(self.src_video, 1.0, 4.0, out_dir)
        out_file = Path(data["output"])
        self.assertTrue(out_file.exists())
        self.assertEqual(out_file.suffix, ".mp4")
        self.assertTrue(has_video_stream(out_file))
        self.assertTrue(has_audio_stream(out_file))
        dur = probe_duration(out_file)
        self.assertAlmostEqual(dur, 3.0, delta=0.2)

    def test_04_timeline_export_mixed_video_and_audio(self):
        clips = [
            Clip(path=self.src_video, start=0.5, end=2.0),   # 1.5s video
            Clip(path=self.src_audio, start=0.0, end=1.0),   # 1.0s audio only
            Clip(path=self.src_video, start=3.5, end=5.0),   # 1.5s video
        ]
        # Total duration should be ~4.0s
        data = export_timeline(clips, enhance_it=False)
        out_file = Path(data["output"])
        self.assertTrue(out_file.exists())
        self.assertEqual(out_file.suffix, ".mp4")
        self.assertTrue(has_video_stream(out_file))
        self.assertTrue(has_audio_stream(out_file))
        self.assertAlmostEqual(data["duration_sec"], 4.0, delta=0.2)
        self.assertTrue(data.get("is_video"))

    def test_05_gui_endpoints(self):
        client = app.test_client()

        # Import video
        with open(self.src_video, "rb") as f:
            res = client.post("/api/import", data={"files": (f, "test_upload.mp4")})
        self.assertEqual(res.status_code, 200)
        imported = res.get_json()["imported"]
        self.assertEqual(len(imported), 1)
        rel_path = imported[0]
        self.assertTrue(rel_path.endswith(".mp4"))

        # Audio proxy endpoint for wavesurfer
        res_audio = client.get(f"/api/audio?path={rel_path}")
        self.assertEqual(res_audio.status_code, 200)
        self.assertEqual(res_audio.content_type, "audio/wav")
        self.assertGreater(len(res_audio.data), 1000)

        # Tighten endpoint
        res_tight = client.post("/api/tighten", json={"path": rel_path, "start": 0, "end": 6.0, "max_pause": 0.2})
        self.assertEqual(res_tight.status_code, 200)
        t_data = res_tight.get_json()
        self.assertIn("path", t_data)
        self.assertTrue(t_data["path"].endswith("_tight.mp4"))

        # Clean up imported and tightened files
        from soundlab.pipeline import PROJECT_ROOT
        for rel in [rel_path, t_data["path"]]:
            p = PROJECT_ROOT / rel
            p.unlink(missing_ok=True)
            p.with_suffix(".json").unlink(missing_ok=True)

    def test_06_silent_loudness_handling(self):
        from soundlab.pipeline import (EnhanceOptions, PipelineError,
                                       _loudnorm_filter, enhance,
                                       measure_loudness)

        # 1. Test loudnorm filter doesn't inject -inf / inf
        bad_measured = {
            "input_i": "-inf",
            "input_tp": "-inf",
            "input_lra": "0.00",
            "input_thresh": "-70.00",
            "target_offset": "inf",
        }
        f_str = _loudnorm_filter(EnhanceOptions(), bad_measured)
        self.assertNotIn("-inf", f_str)
        self.assertNotIn(":offset=inf", f_str)
        self.assertNotIn("linear=true", f_str)

        # 2. Test measure_loudness on pure silence
        silent_wav = self.tmp_dir / "pure_silence.wav"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "0.64",
             "-c:a", "pcm_s16le", str(silent_wav)],
            capture_output=True, check=True
        )
        lvl = measure_loudness(silent_wav)
        self.assertEqual(lvl["lufs"], -99.0)
        self.assertEqual(lvl["true_peak"], -99.0)

        # 3. Test enhance on silence raises a clean PipelineError instead of FFmpeg crash
        with self.assertRaises(PipelineError) as ctx:
            enhance(silent_wav, EnhanceOptions())
        self.assertIn("empty or too quiet", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
