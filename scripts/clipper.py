#!/usr/bin/env python3
"""Configurer le clipper et produire des aperçus verticaux."""
from __future__ import annotations

import argparse
import json
import math
import platform
from pathlib import Path
import shutil
import sys
import tempfile

import clipping
import twitch_import

RACINE = Path(__file__).resolve().parents[1]
CONFIGURATION = RACINE / "config.local.json"
DEMO = RACINE / "examples" / "demo.mp4"
DEFAUTS = {
    "nombre_clips": 1,
    "duree": 25.0,
    "position_titre": "haut",
    "couleur_titre": "#FFFFFF",
    "fond_titre": "#17243A",
    "couleur_sous_titres": "#FFFFFF",
    "cadrage": "entier",
    "langue": "fr",
    "sous_titres": "automatiques",
    "priorite": "moments compréhensibles hors du live",
}


def options_style(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--nombre-clips", type=int)
    parser.add_argument("--duree", type=float, help="Durée cible d’un clip en secondes")
    parser.add_argument("--position-titre", choices=("haut", "bas", "masque"))
    parser.add_argument("--couleur-titre", metavar="#RRGGBB")
    parser.add_argument("--fond-titre", metavar="#RRGGBB")
    parser.add_argument("--couleur-sous-titres", metavar="#RRGGBB")
    parser.add_argument("--cadrage", choices=("entier", "recadre"))
    parser.add_argument("--langue", choices=("fr", "en", "auto"))
    parser.add_argument("--sous-titres", choices=("automatiques", "sans"))
    parser.add_argument("--priorite", help="Types de moments à privilégier lors de la sélection")


def profil(args: argparse.Namespace) -> dict:
    regles = clipping.lire_json(CONFIGURATION) if CONFIGURATION.exists() else {}
    if not isinstance(regles, dict) or regles.keys() - DEFAUTS.keys():
        raise ValueError("Configuration locale invalide")
    options = {key: getattr(args, key, None) for key in DEFAUTS}
    valeurs = {**DEFAUTS, **regles, **{key: val for key, val in options.items() if val is not None}}
    nombre = valeurs["nombre_clips"]
    if type(nombre) is not int or not 1 <= nombre <= 50:
        raise ValueError("Choisir de 1 à 50 clips par VOD")
    if not 3 <= clipping.nombre(valeurs["duree"], "Durée du clip") <= 180:
        raise ValueError("Choisir une durée entre 3 et 180 secondes")
    for key in ("couleur_titre", "fond_titre", "couleur_sous_titres"):
        clipping.couleur_ass(valeurs[key])
    for key, choices in (
        ("position_titre", ("haut", "bas", "masque")),
        ("cadrage", ("entier", "recadre")),
        ("langue", ("fr", "en", "auto")),
        ("sous_titres", ("automatiques", "sans")),
    ):
        if valeurs[key] not in choices:
            raise ValueError(f"Valeur invalide pour {key}")
    if not isinstance(valeurs["priorite"], str):
        raise ValueError("La priorité doit être une consigne écrite")
    return valeurs


def outils() -> dict:
    checks = clipping.diagnostic()
    if checks["ffmpeg"]:
        encoders = clipping.ff("-encoders")
        checks["encodeur_libx264"] = any(" libx264 " in line for line in encoders.splitlines())
        checks["encodeur_aac"] = any(" aac " in line for line in encoders.splitlines())
    checks["twitch_downloader"] = twitch_import.disponible()
    return checks


def verifier(mesures: dict) -> None:
    requis = ("ffmpeg", "ffprobe", "filtre_ass", "filtre_loudnorm",
              "filtre_scdet", "encodeur_libx264", "encodeur_aac")
    manquants = [cle for cle in requis if not mesures.get(cle)]
    if manquants:
        raise ValueError("Prérequis manquants : " + ", ".join(manquants) + ". Voir le README.")


def lire_passage(raw: str) -> tuple[float, float]:
    try:
        start, end = map(float, raw.split(":"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Indiquer DEBUT:FIN en secondes, par exemple 12.5:39") from exc
    if not all(math.isfinite(t) for t in (start, end)) or not 0 <= start < end:
        raise argparse.ArgumentTypeError("Timecodes invalides")
    return start, end


def traiter(args: argparse.Namespace, styles: dict) -> None:
    verifier(outils())
    demo = args.source == "demo"
    if demo:
        source = DEMO
    elif args.source.startswith(("http://", "https://")):
        source = twitch_import.recuperer(args.source, RACINE / "media")
    else:
        source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"MP4 introuvable : {source}")

    cible = styles["nombre_clips"]
    if len(args.passage) > cible or len(args.titre) > (len(args.passage) or cible):
        raise ValueError("Les passages ou titres fournis dépassent le nombre de clips demandé")
    if any(not titre.strip() or len(titre) > 90 for titre in args.titre):
        raise ValueError("Chaque titre doit contenir entre 1 et 90 caractères")

    meta = clipping.sonder(source)
    duree = styles["duree"]
    if demo and not args.passage:
        duree = min(duree, max(3, math.floor(meta["duree"] / cible)))
    dossier_parent = args.sortie.resolve() if args.sortie else RACINE / "exports" / ("demo" if demo else source.stem)
    dossier_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".analyse-", dir=dossier_parent) as temp:
        travail = Path(temp) / "travail"
        transcription = args.transcription or (DEMO.with_suffix(".json") if demo else None)
        sans_sous_titres = args.sans_parole or styles["sous_titres"] == "sans"
        clipping.analyser(
            source, travail, cible, duree, transcription if not sans_sous_titres else None,
            sans_sous_titres, args.telecharger_modele, args.piste_audio,
            langue=styles["langue"], assurer_nombre=not args.passage,
        )
        analyse = clipping.lire_json(travail / "analyse.json")
        candidats = [(item["debut"], item["fin"]) for item in analyse["candidats"]]
        passages = args.passage or candidats[:cible]
        if len(args.titre) > len(passages):
            raise ValueError("Il y a plus de titres que de passages disponibles")
        premiere = []
        for i, (start, end) in enumerate(passages):
            titre = args.titre[i] if i < len(args.titre) else ("À toi de jouer" if demo else f"Extrait {i + 1}")
            premiere.append({
                "debut": start, "fin": end, "titre": titre, "bandeau": titre,
                "sans_parole_confirme": sans_sous_titres or not meta["audio"],
                **{key: styles[key] for key in ("cadrage", "position_titre", "couleur_titre",
                                                "fond_titre", "couleur_sous_titres")},
            })
        if not premiere:
            raise ValueError("Aucun passage exploitable, fournir des timecodes ou réduire la durée cible")
        if sans_sous_titres:
            for clip in premiere:
                clip["sous_titres"] = []
        choix = travail / "choix.json"
        clipping.ecrire_json(choix, {"source": analyse["source"], "clips": premiere})
        rapport = clipping.rendre(source, travail, choix, "auto", False, False)
        base = dossier_parent / "apercus"
        base.mkdir(exist_ok=True)
        destination = base / f"lot-{Path(temp).name.removeprefix('.analyse-')}"
        (travail / "apercu").rename(destination)
        if len(premiere) < cible:
            print(f"{len(premiere)} clip(s) sur {cible} : passages distincts disponibles insuffisants "
                  "avec les timecodes ou la durée retenus.")
        for item in rapport["clips"]:
            print(destination / item["fichier"])


def main() -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 ou plus récent est nécessaire. Voir le README.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    commandes = parser.add_subparsers(dest="action", required=True)
    commandes.add_parser("diagnostic")
    options_style(commandes.add_parser("configurer", help="Enregistrer un profil réutilisable"))
    options_style(commandes.add_parser("demo", help="Produire un premier clip sans compte"))
    lancer = commandes.add_parser("lancer", help="Traiter un MP4 ou une URL de VOD publique")
    lancer.add_argument("source")
    lancer.add_argument("--passage", type=lire_passage, action="append", default=[])
    lancer.add_argument("--titre", action="append", default=[], help="Un titre par passage, dans le même ordre")
    lancer.add_argument("--sortie", type=Path)
    lancer.add_argument("--piste-audio", type=int, default=0)
    sous_titres = lancer.add_mutually_exclusive_group()
    sous_titres.add_argument("--transcription", type=Path)
    sous_titres.add_argument("--telecharger-modele", action="store_true")
    sous_titres.add_argument("--sans-parole", action="store_true", help="Confirmer l’absence de parole après écoute")
    options_style(lancer)
    args = parser.parse_args()
    try:
        if args.action == "diagnostic":
            mesures = outils()
            mesures.update(systeme=platform.system(), architecture=platform.machine(), python=platform.python_version())
            mesures["stockage_libre_Gio"] = round(shutil.disk_usage(RACINE).free / 1024**3, 1)
            print(json.dumps(mesures, indent=2, ensure_ascii=False))
            verifier(mesures)
        elif args.action == "configurer":
            clipping.ecrire_json(CONFIGURATION, profil(args))
            print(f"Profil enregistré dans {CONFIGURATION}")
        else:
            if args.action == "demo":
                args.source, args.passage, args.titre = "demo", [], []
                args.sortie = args.transcription = None
                args.piste_audio, args.telecharger_modele, args.sans_parole = 0, False, False
            if args.piste_audio < 0:
                raise ValueError("L’index de la piste audio doit être positif ou nul")
            traiter(args, profil(args))
        return 0
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
