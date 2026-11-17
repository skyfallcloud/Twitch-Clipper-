"""Régressions utiles et vrais rendus FFmpeg sur sources synthétiques uniquement."""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("clipping", ROOT / "scripts/clipping.py")
clipping = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clipping)


class ValidationTests(unittest.TestCase):
    def test_empty_selection_rejected(self):
        with self.assertRaises(ValueError):
            clipping.valider_choix({"clips": []}, 30)

    def test_timecodes_and_overlap_rejected(self):
        for clips in ([{"debut": -1, "fin": 3}], [{"debut": 5, "fin": 4}],
                      [{"debut": 0, "fin": 31}], [{"debut": 0, "fin": 5}, {"debut": 4, "fin": 6}]):
            with self.subTest(clips=clips), self.assertRaises(ValueError):
                clipping.valider_choix({"clips": clips}, 30)

    def test_nonfinite_and_boolean_times_rejected(self):
        for value in (float("nan"), float("inf"), True, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clipping.valider_choix({"clips": [{"debut": value, "fin": 3}]}, 30)

    def test_bad_crop_rejected(self):
        for extra in ({"cadrage": "tracking"}, {"ancrage_x": 1.2}, {"ancrage_y": float("nan")}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                clipping.valider_choix({"clips": [{"debut": 0, "fin": 2, **extra}]}, 30)

    def test_bad_metadata_types_rejected(self):
        for extra in ({"bandeau": 2}, {"hashtags": "#gaming"}, {"sans_parole_confirme": "oui"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                clipping.valider_choix({"clips": [{"debut": 0, "fin": 2, **extra}]}, 30)

    def test_manual_selection_can_use_silent_moment_and_variable_length(self):
        clips = clipping.valider_choix({"clips": [{"debut": 8, "fin": 9.5}, {"debut": 0, "fin": 3}]}, 30)
        self.assertEqual([c["debut"] for c in clips], [0, 8])

    def test_auto_preserves_fractional_and_60fps(self):
        for rate in ("30000/1001", "60000/1001", "60/1"):
            meta = {"taux": rate, "fps": float(clipping.Fraction(rate))}
            self.assertEqual(clipping.taux_sortie(meta, "auto"), rate)

    def test_no_invented_framerate(self):
        with self.assertRaises(ValueError):
            clipping.taux_sortie({"taux": "30/1", "fps": 30}, "60")

    def test_invalid_transcript_and_timing_rejected(self):
        for words in ([{"w": "bonjour", "s": 2, "e": 1}], [{"w": "", "s": 0, "e": 1}],
                      [{"w": "un", "s": 2, "e": 3}, {"w": "deux", "s": 0, "e": 1}]):
            with self.subTest(words=words), self.assertRaises(ValueError):
                clipping.valider_mots({"mots": words}, 10)

    def test_words_straddling_start_and_end_are_clipped(self):
        entries = clipping.sous_titres({"debut": 1, "fin": 2}, [{"w": "Alex", "s": .8, "e": 1.2}, {"w": "arrive", "s": 1.8, "e": 2.2}])
        self.assertEqual(entries[0]["debut"], 0)
        self.assertEqual(entries[-1]["fin"], 1)

    def test_caption_pause_not_held_across_silence(self):
        entries = clipping.sous_titres({"debut": 0, "fin": 10}, [{"w": "Salut", "s": 0, "e": .4}, {"w": "Alex", "s": 5, "e": 6}])
        self.assertLess(entries[0]["fin"], 1)
        self.assertEqual(len(entries), 2)

    def test_french_elisions_and_hyphens_are_rejoined(self):
        mots = [{"w": w, "s": i, "e": i + .5} for i, w in enumerate(["l", "'exemple", "est", "-ce", "que", "j", "’ai"])]
        self.assertEqual(clipping.joindre(mots), "l'exemple est-ce que j’ai")
        entries = clipping.sous_titres({"debut": 0, "fin": 7}, mots)
        self.assertEqual(" ".join(e["texte"] for e in entries), "l'exemple est-ce que j’ai")

    def test_corrected_subtitles_override_transcription(self):
        corrected = [{"debut": 0, "fin": 1, "texte": "C'est Alex !"}]
        self.assertEqual(clipping.sous_titres({"debut": 10, "fin": 12, "sous_titres": corrected}, []), corrected)

    def test_invalid_corrected_subtitles_rejected(self):
        for entries in ([{"debut": 0, "fin": 4, "texte": "trop long"}],
                        [{"debut": 0, "fin": 1, "texte": "a"}, {"debut": .5, "fin": 2, "texte": "b"}]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                clipping.sous_titres({"debut": 10, "fin": 12, "sous_titres": entries}, [])

    def test_timestamps_carry_over_minute(self):
        self.assertEqual(clipping.temps(59.9999), "00:01:00,000")
        self.assertEqual(clipping.temps(59.9999, True), "0:01:00.00")

    def test_ass_commands_are_neutralized(self):
        escaped = clipping.texte_ass(r"{\pos(0,0)}Salut")
        self.assertNotIn("{", escaped)
        self.assertNotIn("\\pos", escaped)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg requis pour les rendus")
class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clipper-tests-é espace-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def source(self, fps=30, audio=True, portrait=False):
        path = self.root / f"capture {fps} {'son' if audio else 'muette'}.mp4"
        size = "180x320" if portrait else "320x180"
        args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration=2"]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2", "-c:a", "aac"]
        args += ["-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", str(path)]
        subprocess.run(args, check=True, capture_output=True)
        return path

    def analysis(self, video):
        folder = self.root / ("travail " + video.stem)
        supplied = self.root / "transcription fournie.json"
        clipping.ecrire_json(supplied, {"langue": "fr", "mots": [{"w": "Test", "s": .1, "e": .4}, {"w": "Alex", "s": .5, "e": .9}]})
        clipping.analyser(video, folder, 1, 1.5, supplied, False, False, 0)
        self.assertEqual(clipping.lire_json(folder / "choix.json")["clips"], [])
        return folder

    def select(self, folder, **extra):
        path = folder / "choix.json"
        clipping.ecrire_json(path, {"source": clipping.lire_json(folder / "analyse.json")["source"],
            "clips": [{"debut": 0, "fin": 1.2, "titre": "Fixture de test", **extra}]})
        return path

    def test_real_60fps_audio_preview_final_and_preserved_sources(self):
        video = self.source(60)
        original = hashlib.sha256(video.read_bytes()).hexdigest()
        folder = self.analysis(video)
        choice = self.select(folder, bandeau="Test technique")
        report = clipping.rendre(video, folder, choice, "auto", False, False)
        self.assertEqual(report["clips"][0]["fps"], 60)
        self.assertTrue(report["clips"][0]["audio"])
        self.assertTrue((folder / "apercu/short-01.srt").read_text().strip())
        self.assertTrue((folder / "apercu/short-01.ass").exists())
        self.assertEqual(len(list((folder / "apercu").glob("*.jpg"))), 3)
        with self.assertRaises(ValueError):
            clipping.rendre(video, folder, choice, "auto", True, False)
        clipping.rendre(video, folder, choice, "auto", True, True)
        self.assertEqual(hashlib.sha256(video.read_bytes()).hexdigest(), original)
        with self.assertRaises(ValueError):
            clipping.rendre(video, folder, choice, "auto", True, True)

    def test_real_30fps_crop_without_audio_and_revised_preview(self):
        video = self.source(30, audio=False)
        folder = self.analysis(video)
        choice = self.select(folder, cadrage="recadre", ancrage_x=.8)
        report = clipping.rendre(video, folder, choice, "auto", False, False)
        self.assertEqual(report["clips"][0]["fps"], 30)
        self.assertFalse(report["clips"][0]["audio"])
        self.select(folder, cadrage="recadre", ancrage_x=.2)
        with self.assertRaises(ValueError):
            clipping.rendre(video, folder, choice, "auto", True, True)
        clipping.rendre(video, folder, choice, "auto", False, False)
        self.assertTrue((folder / "apercu-002/short-01.mp4").exists())

    def test_source_change_and_missing_captions_block_render(self):
        video = self.source(30)
        folder = self.analysis(video)
        choice = self.select(folder)
        clipping.ecrire_json(folder / "transcription.json", {"mots": []})
        with self.assertRaises(ValueError):
            clipping.rendre(video, folder, choice, "auto", False, False)
        self.select(folder, sans_parole_confirme=True)
        video.write_bytes(video.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            clipping.rendre(video, folder, choice, "auto", False, False)

    def test_short_portrait_source_no_audio_analysis(self):
        video = self.source(60, audio=False, portrait=True)
        folder = self.root / "portrait"
        clipping.analyser(video, folder, 8, 25, None, True, False, 0)
        analysis = clipping.lire_json(folder / "analyse.json")
        self.assertGreaterEqual(len(analysis["candidats"]), 1)
        self.assertLessEqual(analysis["candidats"][0]["fin"], 2)
        choice = self.select(folder, sans_parole_confirme=True)
        report = clipping.rendre(video, folder, choice, "auto", False, False)
        self.assertEqual(report["clips"][0]["sous_titres"], 0)
        with self.assertRaises(ValueError):
            clipping.analyser(video, folder, 8, 25, None, True, False, 0)


if __name__ == "__main__":
    unittest.main()
