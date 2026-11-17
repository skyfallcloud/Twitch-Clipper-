"""Trouver et importer les archives Twitch d'une chaîne configurée."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import sys
import time
from urllib.parse import urlencode

import services
import twitch_import

CONFIG = services.DONNEES / "twitch.json"
API = "https://api.twitch.tv/helix/"
OAUTH = "https://id.twitch.tv/oauth2/token"
VOD_URL = "https://www.twitch.tv/videos/"
LABEL = "ia.skyfall.twitch-clipper." + hashlib.sha256(os.fsencode(services.RACINE)).hexdigest()[:12]
PLIST = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def horodatage(texte: str) -> datetime:
    moment = datetime.fromisoformat(texte.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("Twitch : heure d’archive sans fuseau")
    return moment


class Twitch:
    def __init__(self, client_id: str, secret: str) -> None:
        self.client_id = client_id
        self.secret = secret
        self.jeton = ""
        self.expire = 0.0

    def renouveler(self) -> None:
        token = services.appeler(
            "Twitch OAuth", OAUTH, "POST", {"Content-Type": "application/x-www-form-urlencoded"},
            urlencode({"client_id": self.client_id, "client_secret": self.secret,
                       "grant_type": "client_credentials"}).encode(),
        )
        self.jeton = token["access_token"]
        self.expire = time.monotonic() + max(0, int(token["expires_in"]) - 60)

    def helix(self, route: str, **query) -> dict:
        if time.monotonic() >= self.expire:
            self.renouveler()
        url = API + route + "?" + urlencode(query)
        try:
            return services.appeler("Twitch", url, headers={"Client-Id": self.client_id,
                                                           "Authorization": "Bearer " + self.jeton})
        except services.ErreurHTTP as exc:
            if exc.status != 401:
                raise
        self.renouveler()
        return services.appeler("Twitch", url, headers={"Client-Id": self.client_id,
                                                       "Authorization": "Bearer " + self.jeton})

    def utilisateur(self, login: str) -> dict:
        users = self.helix("users", login=login)["data"]
        if not users:
            raise ValueError(f"Chaîne Twitch introuvable : {login}")
        return users[0]

    def live(self, user_id: str) -> str | None:
        streams = self.helix("streams", user_id=user_id)["data"]
        return streams[0]["id"] if streams else None

    def archives(self, user_id: str):
        filtres = {"type": "archive", "user_id": user_id, "first": 100, "sort": "time"}
        cursors = set()
        while True:
            page = self.helix("videos", **filtres)
            yield from page["data"]
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor:
                return
            if cursor in cursors:
                raise ValueError("Twitch : curseur de pagination répété")
            cursors.add(cursor)
            filtres["after"] = cursor


def connecter(chaine: str, client_id: str) -> None:
    login = chaine.lower().strip().removeprefix("@")
    if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
        raise ValueError("Indiquer le pseudo Twitch, avec ou sans @")
    if not client_id.strip():
        raise ValueError("Indiquer le Client ID Twitch")
    secret = services.secret("TWITCH_CLIENT_SECRET", "Client Secret Twitch : ")
    compte = Twitch(client_id, secret).utilisateur(login)
    services.ecrire(CONFIG, {"login": compte["login"], "id": compte["id"],
                             "client_id": client_id, "secret": secret})
    print(f"Chaîne connectée : @{compte['login']}")


def charger() -> tuple[Twitch, str]:
    if not CONFIG.exists():
        raise ValueError("Configurer la chaîne avec twitch connecter")
    config = services.lire(CONFIG)
    return Twitch(config["client_id"], config["secret"]), config["id"]


def preparer(archive: dict, rendre: bool) -> None:
    path = twitch_import.recuperer(VOD_URL + archive["id"], services.RACINE / "media")
    print(path, flush=True)
    if rendre:
        preparer_rendu(archive["id"])


def derniere(twitch: Twitch, user_id: str, rendre: bool) -> None:
    live = twitch.live(user_id)
    for archive in twitch.archives(user_id):
        if not live or archive.get("stream_id") != live:
            preparer(archive, rendre)
            return
    print("Aucune VOD publique d’un live terminé.")


def synchroniser(twitch: Twitch, user_id: str, rendre: bool = False) -> None:
    if not re.fullmatch(r"[0-9]+", user_id):
        raise ValueError("Identifiant de chaîne invalide")
    services.DONNEES.mkdir(mode=0o700, exist_ok=True)
    chemin = services.DONNEES / f"twitch-{user_id}.json"
    with twitch_import.verrou(chemin.with_suffix(".lock")):
        maintenant = datetime.now(timezone.utc)
        active = twitch.live(user_id)
        etat = services.lire(chemin, {})
        if not etat:
            etat = {"depuis": maintenant.isoformat(), "live_initial": active, "importees": [],
                    "rendues": []}
            services.ecrire(chemin, etat)
            print("Surveillance initialisée. Utiliser twitch derniere pour importer une archive plus ancienne.")
        depuis = horodatage(etat["depuis"])
        terminees = set(etat["rendues"] if rendre else etat["importees"])
        eligibles = {}
        for archive in twitch.archives(user_id):
            stream = archive.get("stream_id")
            if (active and stream == active) or archive["id"] in terminees:
                continue
            publication = horodatage(archive.get("published_at") or archive["created_at"])
            if publication >= depuis or (stream and stream == etat["live_initial"]):
                eligibles[archive["id"]] = archive
        echecs = []
        for archive in reversed(list(eligibles.values())):
            identifiant = archive["id"]
            try:
                preparer(archive, False)
                if identifiant not in etat["importees"]:
                    etat["importees"].append(identifiant)
                    services.ecrire(chemin, etat)
                if rendre:
                    preparer_rendu(identifiant)
                    etat["rendues"].append(identifiant)
                    services.ecrire(chemin, etat)
            except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                print(f"VOD {identifiant} : {exc}", file=sys.stderr, flush=True)
                echecs.append(identifiant)
        if echecs:
            raise ValueError(f"{len(echecs)} VOD à reprendre au prochain passage.")
        print(f"{len(eligibles)} VOD traitée(s).", flush=True)


def preparer_rendu(identifiant: str) -> None:
    path = services.RACINE / "media" / f"VOD-{identifiant}.mp4"
    dossier = services.RACINE / "exports" / "automatique" / path.stem
    if not any(dossier.glob("apercus/*/rapport.json")):
        subprocess.run([sys.executable, str(services.RACINE / "scripts" / "clipper.py"),
                        "lancer", str(path), "--sortie", str(dossier)], check=True)


def agent_macos(activer: bool, intervalle: int, rendre: bool) -> None:
    if platform.system() != "Darwin":
        raise ValueError("Le LaunchAgent s’active sous macOS. Utiliser twitch surveiller sur les autres systèmes.")
    domaine = f"gui/{os.getuid()}"
    if not activer:
        if PLIST.exists():
            etat = subprocess.run(["launchctl", "print", f"{domaine}/{LABEL}"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if etat.returncode == 0:
                subprocess.run(["launchctl", "bootout", domaine, str(PLIST)], check=True)
            PLIST.unlink()
        print("Surveillance arrêtée.")
        return
    charger()
    if PLIST.exists():
        raise ValueError("Surveillance déjà configurée. Utiliser twitch arreter pour modifier son intervalle.")
    services.DONNEES.mkdir(mode=0o700, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, str(Path(__file__).resolve()), "synchroniser"]
    if rendre:
        args.append("--rendre")
    bloc = {
        "Label": LABEL, "ProgramArguments": args, "WorkingDirectory": str(services.RACINE),
        "RunAtLoad": True, "StartInterval": intervalle,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", os.defpath), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(services.DONNEES / "twitch.log"),
        "StandardErrorPath": str(services.DONNEES / "twitch-erreurs.log"),
    }
    with PLIST.open("xb") as f:
        plistlib.dump(bloc, f)
    try:
        subprocess.run(["launchctl", "bootstrap", domaine, str(PLIST)], check=True)
    except (OSError, subprocess.CalledProcessError):
        PLIST.unlink(missing_ok=True)
        raise
    print(f"Surveillance activée. Vérification toutes les {intervalle} secondes. Journaux dans .clipper/.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="action", required=True)
    setup = cmds.add_parser("connecter")
    setup.add_argument("chaine")
    setup.add_argument("--client-id", required=True)
    cmds.add_parser("arreter")
    for action in ("derniere", "synchroniser", "surveiller", "activer"):
        sub = cmds.add_parser(action)
        sub.add_argument("--rendre", action="store_true")
        if action in ("surveiller", "activer"):
            sub.add_argument("--intervalle", type=int, default=300, metavar="SECONDES")
    args = parser.parse_args()
    try:
        if args.action == "connecter":
            connecter(args.chaine, args.client_id)
        elif args.action in ("activer", "arreter"):
            intervalle = getattr(args, "intervalle", 300)
            if intervalle < 30:
                raise ValueError("Intervalle minimal : 30 secondes")
            agent_macos(args.action == "activer", intervalle, getattr(args, "rendre", False))
        else:
            twitch, uid = charger()
            if args.action == "derniere":
                derniere(twitch, uid, args.rendre)
            elif args.action == "synchroniser":
                synchroniser(twitch, uid, args.rendre)
            else:
                if args.intervalle < 30:
                    raise ValueError("Intervalle minimal : 30 secondes")
                while True:
                    try:
                        synchroniser(twitch, uid, args.rendre)
                    except (ValueError, RuntimeError, OSError, KeyError) as exc:
                        print(f"Surveillance Twitch : {exc}", file=sys.stderr, flush=True)
                    time.sleep(args.intervalle)
        return 0
    except KeyboardInterrupt:
        print("\nSurveillance arrêtée.")
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
