"""Parcours du clipper sur un lot mêlant extraits sous-titrés et extraits à vérifier."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg requis")
class TestParcours(unittest.TestCase):
    def test_lot_mixte_et_reinitialisation_du_titre_d_un_seul_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            transcription = base / "mots.json"
            transcription.write_text(
                json.dumps({"langue": "en", "mots": [{"w": "Welcome", "s": 1.0, "e": 1.8}]}),
                encoding="utf-8",
            )
            chemins = base / "Mes clips"
            commande = [sys.executable, str(ROOT / "scripts" / "clipper.py")]
            subprocess.run(commande + [
                "lancer", str(ROOT / "examples" / "demo.mp4"), "--transcription", str(transcription),
                "--nombre-clips", "2", "--passage", "0:2", "--passage", "27:29", "--dossier-clips", str(chemins),
            ], check=True, capture_output=True)
            lot, = (chemins / "demo" / "apercus").iterdir()
            rapport = json.loads((lot / "rapport.json").read_text(encoding="utf-8"))
            self.assertEqual([item["sous_titres_a_verifier"] for item in rapport["clips"]], [False, True])
            self.assertEqual(len(list(lot.glob("*.mp4"))), 2)
            choix = json.loads((lot / "choix-utilise.json").read_text(encoding="utf-8"))
            self.assertEqual([len(clip["propositions_titre"]) for clip in choix["clips"]], [3, 3])
            subprocess.run(commande + ["retitrer", str(lot), "--clip", "2", "--choix", "3"],
                           check=True, capture_output=True)
            nouveau, = (p for p in lot.parent.iterdir() if p != lot)
            self.assertEqual(len(list(nouveau.glob("*.mp4"))), 1)
            choix_nouveau = json.loads((nouveau / "choix-utilise.json").read_text(encoding="utf-8"))
            self.assertEqual(choix_nouveau["clips"][0]["titre"], choix["clips"][1]["propositions_titre"][2])
            self.assertEqual(len(list(lot.glob("*.mp4"))), 2)
