"""Contrats des intégrations sans clés, téléchargement ni publication réels."""
from argparse import Namespace
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import plistlib
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import plugkit
import services
import twitch


class TestTwitch(unittest.TestCase):
    def test_launchagent_resolution_et_arret(self):
        with TemporaryDirectory() as tmp:
            dossier = Path(tmp)
            fichier = dossier / "LaunchAgents" / "twitch.plist"
            with patch.object(twitch, "PLIST", fichier), patch.object(services, "DONNEES", dossier / "etat"), \
                    patch.object(twitch.platform, "system", return_value="Darwin"), \
                    patch.object(twitch, "charger"), patch.object(twitch.subprocess, "run") as run, \
                    redirect_stdout(StringIO()):
                twitch.agent_macos(True, 300, True)
                job = plistlib.loads(fichier.read_bytes())
                self.assertEqual(job["StartInterval"], 300)
                self.assertEqual(job["ProgramArguments"][-2:], ["synchroniser", "--rendre"])
                self.assertEqual(run.call_args.args[0][:2], ["launchctl", "bootstrap"])
                run.return_value.returncode = 0
                twitch.agent_macos(False, 300, False)
                self.assertEqual(run.call_args.args[0][:2], ["launchctl", "bootout"])
                self.assertFalse(fichier.exists())

    def test_pages_et_exclusion_du_live(self):
        client = twitch.Twitch("id", "secret")
        client.helix = Mock(side_effect=[
            {"data": [
                {"id": "20", "stream_id": "live"},
                {"id": "19", "stream_id": "ancien"},
            ], "pagination": {"cursor": "p2"}},
            {"data": [{"id": "18", "stream_id": "ancien-2"}]},
        ])
        self.assertEqual([a["id"] for a in client.archives("77")], ["20", "19", "18"])
        self.assertEqual(client.helix.call_args.kwargs["after"], "p2")
        self.assertEqual(client.helix.call_args.kwargs["type"], "archive")
        client.live = Mock(return_value="live")
        client.archives = Mock(return_value=iter([
            {"id": "20", "stream_id": "live"},
            {"id": "19", "stream_id": "ancien"},
        ]))
        with patch.object(twitch, "preparer") as importer:
            twitch.derniere(client, "77", True)
            importer.assert_called_once_with({"id": "19", "stream_id": "ancien"}, True)

    def test_lecture_garde_les_nouveaux_lives_et_reprend_le_rendu(self):
        now = datetime.now(timezone.utc)
        archives = [
            {"id": "33", "stream_id": "actuel", "published_at": (now + timedelta(seconds=10)).isoformat()},
            {"id": "32", "stream_id": "nouveau", "published_at": (now + timedelta(seconds=10)).isoformat()},
            {"id": "31", "stream_id": "init", "published_at": (now - timedelta(hours=3)).isoformat()},
            {"id": "30", "stream_id": "passe", "published_at": (now - timedelta(days=1)).isoformat()},
        ]
        client = Mock()
        client.live.side_effect = ["init", "actuel", "actuel"]
        client.archives.side_effect = [archives[2:], archives, archives]
        with TemporaryDirectory() as tmp, patch.object(services, "DONNEES", Path(tmp)), \
                patch.object(twitch, "preparer") as importer, \
                patch.object(twitch, "preparer_rendu", side_effect=[None, ValueError("seuil"), None]) as rendre, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            twitch.synchroniser(client, "77")
            importer.assert_not_called()
            with self.assertRaisesRegex(ValueError, "à reprendre"):
                twitch.synchroniser(client, "77", True)
            twitch.synchroniser(client, "77", True)
            etat = services.lire(Path(tmp) / "twitch-77.json")
            self.assertEqual(etat["importees"], ["31", "32"])
            self.assertEqual(etat["rendues"], ["31", "32"])
            self.assertEqual([c.args[0] for c in rendre.call_args_list], ["31", "32", "32"])
            self.assertEqual([c.args[0]["id"] for c in importer.call_args_list], ["31", "32", "32"])

    def test_reauthentification_unique_sur_401(self):
        client = twitch.Twitch("id", "secret")
        rep = {"access_token": "jeton", "expires_in": 600}
        seq = [rep, services.ErreurHTTP("Twitch", 401), rep, {"data": []}]
        with patch.object(services, "appeler", side_effect=seq) as appels:
            self.assertIsNone(client.live("77"))
        self.assertEqual(appels.call_count, 4)


class TestPlugkit(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.mp4 = self.root / "clip.mp4"
        self.mp4.write_bytes(b"media simulate")
        params = {"mp4": self.mp4, "titre": "Le titre", "legende": "Une légende", "profil": "profil",
                  "compte": ["compte-2", "compte-1", "compte-1"], "brouillon": False,
                  "a": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                  "confirmer": False, "media_url": None}
        self.args = Namespace(**params)
        self.journal = self.root / "publication.json"

    def test_apercu_sans_effet(self):
        with patch.object(plugkit, "appel") as appel, patch.object(plugkit, "envoyer") as envoyer, \
                redirect_stdout(StringIO()) as log:
            plugkit.planifier(self.args)
        appel.assert_not_called()
        envoyer.assert_not_called()
        self.assertIn("aperçu local", log.getvalue())
        self.assertFalse(self.journal.exists())

    def test_relance_apres_reponse_perdue_et_pas_de_doublon(self):
        self.args.confirmer = True
        res = {"post": {"_id": "pub-1", "status": "scheduled",
                        "platforms": [{"platform": "youtube", "accountId": "compte-1", "status": "pending"}]}}
        with patch.object(services, "DONNEES", self.root), patch.object(plugkit, "JOURNAL", self.journal), \
                patch.object(plugkit.clipping, "sonder", return_value={"largeur": 1080, "hauteur": 1920}), \
                patch.object(plugkit, "cle", return_value="cle-factice"), \
                patch.object(plugkit, "lister", return_value=[{"_id": "compte-1"}, {"_id": "compte-2"}]), \
                patch.object(plugkit, "envoyer", return_value="https://example.org/clip.mp4") as envoyer, \
                patch.object(plugkit, "appel", side_effect=[ValueError("timeout"), res]) as post, \
                redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ValueError, "timeout"):
                plugkit.planifier(self.args)
            plugkit.planifier(self.args)
            plugkit.planifier(self.args)
        envoyer.assert_called_once()
        self.assertEqual(post.call_count, 2)
        premier, second = post.call_args_list
        self.assertEqual(premier, second)
        self.assertEqual(premier.args[1:3], ("/posts", "POST"))
        self.assertEqual(premier.args[3]["accountIds"], ["compte-1", "compte-2"])
        self.assertEqual(next(iter(services.lire(self.journal).values()))["resultat"]["post_id"], "pub-1")

    def test_ticket_upload_sans_transmettre_la_cle(self):
        ticket = {"uploadUrl": "https://storage.example/secret-ticket", "mediaUrl": "https://cdn.example/clip",
                  "uploadMethod": "PUT", "maxBytes": 100, "singlePutMaxBytes": 30}
        retour = Mock()
        retour.__enter__ = Mock()
        retour.__exit__ = Mock()
        with patch.object(plugkit, "appel", return_value=ticket) as appel, \
                patch.object(services, "ouvrir", return_value=retour) as ouvrir:
            url = plugkit.envoyer("clé", self.mp4)
        self.assertEqual(url, ticket["mediaUrl"])
        appel.assert_called_once_with("clé", "/media/upload-url", "POST", {"filename": "clip.mp4"})
        requete = ouvrir.call_args.args[1]
        self.assertEqual(requete.get_method(), "PUT")
        self.assertNotIn("Authorization", requete.headers)
        self.assertEqual(int(requete.get_header("Content-length")), self.mp4.stat().st_size)

    def test_fuseau_exige(self):
        with self.assertRaisesRegex(ValueError, "fuseau"):
            plugkit.horaire("2027-01-11T18:00")
        self.assertEqual(plugkit.horaire("2027-01-11T18:00:00+01:00"), "2027-01-11T17:00:00+00:00")
