"""Importer une VOD Twitch publique à partir de son URL."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import clipping

RACINE = Path(__file__).resolve().parents[1]


def disponible() -> str | None:
    nom = "TwitchDownloaderCLI.exe" if os.name == "nt" else "TwitchDownloaderCLI"
    return shutil.which(nom) or shutil.which(str(RACINE / "bin" / nom))


@contextmanager
def verrou(chemin: Path):
    # Le verrou est libéré à la fermeture du fichier ou à l’arrêt du processus.
    with chemin.open("a+b") as fichier:
        try:
            if os.name == "nt":
                import msvcrt
                fichier.seek(0)
                msvcrt.locking(fichier.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fichier, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Verrou local indisponible : une opération est peut-être déjà en cours.") from exc
        yield


def telecharger(cli: str, mode: str, vod_id: str, destination: Path) -> None:
    subprocess.run(
        [cli, mode, "--id", vod_id, "--output", str(destination), "--collision", "Exit"],
        check=True,
    )
    if not destination.is_file() or not destination.stat().st_size:
        raise ValueError("TwitchDownloaderCLI n’a pas produit de fichier")


def recuperer(url: str, dossier: Path) -> Path:
    match = re.fullmatch(r"https?://(?:www\.)?twitch\.tv/videos/([0-9]+)(?:[/?#].*)?", url.strip())
    if not match:
        raise ValueError("URL attendue : https://www.twitch.tv/videos/ID")
    cli = disponible()
    if not cli:
        raise ValueError("TwitchDownloaderCLI introuvable. Voir le README pour l’install.")
    identifiant = match[1]
    dossier.mkdir(parents=True, exist_ok=True)
    video = dossier / f"VOD-{identifiant}.mp4"
    chat = dossier / f"VOD-{identifiant}_chat.json"
    with verrou(dossier / f".VOD-{identifiant}.lock"), tempfile.TemporaryDirectory(dir=dossier) as tmp:
        stage = Path(tmp)
        if video.exists():
            clipping.sonder(video)
            print(f"VOD locale réutilisée : {video}")
        else:
            partial = stage / video.name
            try:
                telecharger(cli, "videodownload", identifiant, partial)
            except subprocess.CalledProcessError as exc:
                raise ValueError("Échec du téléchargement Twitch : vérifier l’accès à la VOD") from exc
            clipping.sonder(partial)
            partial.replace(video)
        if not chat.exists():
            try:
                partial = stage / chat.name
                telecharger(cli, "chatdownload", identifiant, partial)
                clipping.lire_json(partial)
                partial.replace(chat)
            except (subprocess.CalledProcessError, ValueError) as exc:
                print(f"Chat indisponible, la VOD reste utilisable ({type(exc).__name__}).")
    return video
