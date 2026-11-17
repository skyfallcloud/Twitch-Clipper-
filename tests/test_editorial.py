import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import editorial


class EditorialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lot = Path(self.temp.name)
        clips = [{"debut": 0, "fin": 10, "titre": "Action", "description": "Contexte", "hashtags": ["#gaming"]},
                 {"debut": 20, "fin": 30, "titre": "Réaction"}]
        self.put("choix-utilise.json", {"source": {"chemin": "VOD-123.mp4"}, "clips": clips})
        self.put("rapport.json", {"clips": [{"fichier": "short-01.mp4"}, {"fichier": "short-02.mp4", "sous_titres_a_verifier": True}]})
        self.put("analyse.json", {"media": {"duree": 40}, "candidats": [{"debut": 0, "fin": 10, "score_technique": .7}]})
        words = "un deux trois quatre cinq six sept huit neuf dix".split()
        self.put("transcription.json", {"mots": [{"w": w, "s": start + i} for start in (0, 20) for i, w in enumerate(words)]})
        (self.lot / "short-01-milieu.jpg").touch()

    def put(self, name, data):
        path = self.lot / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_drafts_and_signals(self):
        chat = self.put("chat.json", {"Comments": [{"ContentOffsetSeconds": t} for t in (0, 1, 2, 10)]})
        result = editorial.generer(self.lot, categorie="gaming", credit="@createur", droit="autorise", chat=chat)
        first, second = result["clips"]
        self.assertFalse(result["publication_autorisee"])
        self.assertEqual(first["messages_chat"], 3)
        self.assertEqual(first["activite_chat_relative"], 3.0)
        self.assertEqual(first["mots_2_premieres_secondes"], 2)
        self.assertEqual(first["score_technique"], .7)
        self.assertEqual(first["image_couverture_candidate"], "short-01-milieu.jpg")
        self.assertEqual(second["transcriptions_similaires"], [1])
        self.assertTrue(second["sous_titres_a_verifier"])
        self.assertIn("@createur", first["brouillons"]["tiktok"]["legende"])
        self.assertNotEqual(first["brouillons"]["instagram"], first["brouillons"]["tiktok"])
        self.assertTrue((self.lot / "editorial.md").exists())

    def test_missing_or_invalid_chat(self):
        absent = editorial.generer(self.lot)
        self.assertIsNone(absent["clips"][0]["messages_chat"])
        self.assertEqual(absent["droits_declares"], "a_verifier")
        invalid = editorial.generer(self.lot, chat=self.put("chat.json", {"unknown": []}))
        self.assertIsNone(invalid["clips"][0]["messages_chat"])
        self.assertTrue(invalid["avertissement_chat"])

    def test_chat_ranking_is_bounded_and_optional(self):
        a = {"debut": 0, "fin": 10, "score_technique": .7}
        b = {"debut": 20, "fin": 30, "score_technique": .6}
        candidates = [a, b]
        self.assertEqual(editorial.classer(candidates, None, 40), candidates)
        self.assertEqual(editorial.classer(candidates, [21] * 19, 40), candidates)
        self.assertEqual(editorial.classer(candidates, [21] * 20, 40), [b, a])
        a["score_technique"] = .9
        self.assertEqual(editorial.classer(candidates, [21] * 20, 40), candidates)

    def test_auto_provenance(self):
        source = self.lot / "VOD-123.mp4"
        result = editorial.auto(self.lot, source)
        self.assertEqual(result["source_url"], "https://www.twitch.tv/videos/123")


if __name__ == "__main__":
    unittest.main()
