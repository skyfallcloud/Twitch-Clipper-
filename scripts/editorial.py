#!/usr/bin/env python3
"""Préparer une revue éditoriale locale, sans API ni publication."""
from __future__ import annotations

import argparse
from bisect import bisect_left
import json
import math
from pathlib import Path
import re

PROFILS = {
    "general": "Moment autonome, compréhensible sans le live.",
    "gaming": "Action, réaction, surprise ou astuce ; vérifier les spoilers.",
    "podcast": "Une idée complète, une question ou une réponse ; préserver le contexte.",
    "humour": "Mise en place et chute ; éviter les coupes qui dénaturent le propos.",
    "sport": "Action lisible, résultat et contexte ; vérifier les droits de diffusion.",
    "education": "Une explication utile, exacte et autonome.",
    "musique": "Une performance ou une anecdote ; vérifier les droits audio.",
    "interview": "Une réponse autonome, fidèle à la question et à son contexte.",
}
RESEAUX = ("tiktok", "instagram", "youtube_shorts", "x", "threads")


def lire(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def texte(value):
    return " ".join(str(value or "").split())


def jetons(value):
    return set(re.findall(r"[^\W_]+", value.casefold()))


def temps_chat(path):
    """TwitchDownloader : offsets en secondes ; aucun texte du chat exporté."""
    if path is None:
        return None
    data = lire(path)
    comments = data.get("comments", data.get("Comments")) if isinstance(data, dict) else None
    if not isinstance(comments, list):
        raise ValueError("Format de chat non reconnu")
    offsets = []
    for item in comments:
        if isinstance(item, dict):
            val = item.get("content_offset_seconds", item.get("ContentOffsetSeconds"))
            if type(val) in (int, float) and math.isfinite(val) and val >= 0:
                offsets.append(float(val))
    if comments and not offsets:
        raise ValueError("Offsets du chat indisponibles")
    return sorted(offsets)


def compter(offsets, debut, fin):
    return bisect_left(offsets, fin) - bisect_left(offsets, debut)


def brouillons(clip, credit="", source_url=""):
    titre = texte(clip.get("titre")) or "Extrait à titrer"
    description = texte(clip.get("description"))
    tags = " ".join(dict.fromkeys(texte(t) for t in clip.get("hashtags", []) if texte(t)))
    provenance = " · ".join(x for x in (credit, source_url) if x)
    corps = description or titre
    base = "\n".join(x for x in (corps, provenance) if x)
    avec_tags = "\n".join(x for x in (base, tags) if x)
    return {
        "tiktok": {"legende": avec_tags},
        "instagram": {"legende": avec_tags},
        "youtube_shorts": {"titre": titre[:90], "legende": avec_tags},
        "x": {"legende": "\n".join(x for x in (titre, provenance) if x)},
        "threads": {"legende": base},
    }


def generer(lot, *, categorie="general", credit="", droit="a_verifier",
            spoiler="a_verifier", chat=None, source_url=""):
    if categorie not in PROFILS or droit not in ("a_verifier", "autorise", "interdit"):
        raise ValueError("Profil ou statut des droits invalide")
    if spoiler not in ("a_verifier", "sans_spoiler", "spoiler"):
        raise ValueError("Statut spoiler invalide")
    lot = Path(lot)
    choix = lire(lot / "choix-utilise.json")
    rapport = lire(lot / "rapport.json")
    analyse = lire(lot / "analyse.json")
    mots = lire(lot / "transcription.json")["mots"]
    clips = sorted(choix["clips"], key=lambda c: c["debut"])
    if len(clips) != len(rapport["clips"]):
        raise ValueError("Rapport et choix incohérents")
    offsets, avertissement = None, None
    if chat is not None:
        try:
            offsets = temps_chat(chat)
        except (OSError, ValueError) as e:
            avertissement = "Chat inutilisable ; signal ignoré : " + str(e)
    source_duree = float(analyse["media"]["duree"])
    moyenne_chat = len(offsets) / source_duree if offsets is not None and source_duree > 0 else 0
    candidats = {(round(c["debut"], 3), round(c["fin"], 3)): c for c in analyse["candidats"]}
    resultat = {
        "version": 1, "categorie": categorie, "consigne": PROFILS[categorie],
        "source_url": source_url, "credit": credit, "droits_declares": droit,
        "spoiler_declare": spoiler, "publication_autorisee": False,
        "avertissement_chat": avertissement,
        "note": "Brouillons non validés. Aucun score ne prédit la viralité. Ne pas publier sans visionnage, droits et accord humain.",
        "clips": [],
    }
    tokens_precedents = []
    for i, (clip, rendu) in enumerate(zip(clips, rapport["clips"]), 1):
        debut, fin = clip["debut"], clip["fin"]
        paroles = " ".join(m["mot"] if "mot" in m else m.get("w", "") for m in mots if debut <= m["s"] < fin)
        tokens = jetons(paroles)
        similaires = [j for j, t in tokens_precedents if len(tokens) >= 8 and len(t) >= 8
                      and len(tokens & t) / len(tokens | t) >= .75]
        tokens_precedents.append((i, tokens))
        premiers_mots = [m for m in mots if debut <= m["s"] < min(fin, debut + 2)]
        candidat = candidats.get((round(debut, 3), round(fin, 3)), {})
        messages = compter(offsets, debut, fin) if offsets is not None else None
        densite = messages / (fin - debut) if messages is not None and fin > debut else None
        signal_chat = round(densite / moyenne_chat, 2) if densite is not None and moyenne_chat else None
        nom = rendu["fichier"]
        couverture = Path(nom).stem + "-milieu.jpg"
        resultat["clips"].append({
            "fichier": nom, "debut": debut, "fin": fin,
            "score_technique": candidat.get("score_technique"),
            "mots_2_premieres_secondes": len(premiers_mots),
            "messages_chat": messages, "activite_chat_relative": signal_chat,
            "transcriptions_similaires": similaires,
            "image_couverture_candidate": couverture if (lot / couverture).is_file() else None,
            "sous_titres_a_verifier": rendu.get("sous_titres_a_verifier", False),
            "brouillons": brouillons(clip, credit, source_url),
            "a_verifier": ["Accroche dès le début, sans couper le contexte",
                          "Fin naturelle, cadrage et zones sûres sur mobile",
                          "Titre et légendes fidèles, sans promesse inventée",
                          "Droits, musique, crédits et éventuels spoilers"],
        })
    path = lot / "editorial.json"
    path.write_text(json.dumps(resultat, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md = ["# Revue éditoriale", "", resultat["note"], "", PROFILS[categorie], "",
          f"Droits : {droit} · Spoilers : {spoiler}", ""]
    if avertissement:
        md += [avertissement, ""]
    for item in resultat["clips"]:
        md += [f"## {item['fichier']}", "",
               f"Score technique : {item['score_technique']} · Mots dans les 2 premières secondes : {item['mots_2_premieres_secondes']}",
               f"Chat (messages / densité relative) : {item['messages_chat']} / {item['activite_chat_relative']}",
               f"Transcriptions similaires : {item['transcriptions_similaires']}",
               f"Image candidate : {item['image_couverture_candidate']}", ""]
        if item["sous_titres_a_verifier"]:
            md += ["Sous-titres absents : vérifier la parole.", ""]
        md += ["- [ ] " + x for x in item["a_verifier"]] + [""]
        for reseau, brouillon in item["brouillons"].items():
            md += [f"### {reseau}", "", brouillon.get("titre", ""), brouillon["legende"], ""]
    (lot / "editorial.md").write_text("\n".join(md), encoding="utf-8")
    return resultat


def auto(lot, source):
    source = Path(source)
    match = re.fullmatch(r"VOD-(\d+)", source.stem)
    chat = source.with_name(source.stem + "_chat.json")
    return generer(lot, chat=chat if chat.is_file() else None,
                   source_url="https://www.twitch.tv/videos/" + match[1] if match else "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lot", type=Path)
    parser.add_argument("--categorie", choices=PROFILS, default="general")
    parser.add_argument("--credit", default="")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--droits", choices=("a_verifier", "autorise", "interdit"), default="a_verifier")
    parser.add_argument("--spoiler", choices=("a_verifier", "sans_spoiler", "spoiler"), default="a_verifier")
    parser.add_argument("--chat", type=Path)
    args = parser.parse_args()
    try:
        generer(args.lot, categorie=args.categorie, credit=args.credit, droit=args.droits,
                spoiler=args.spoiler, chat=args.chat, source_url=args.source_url)
    except (OSError, ValueError, KeyError, TypeError) as e:
        parser.exit(1, f"Erreur : {e}\n")
    print(args.lot / "editorial.md")


if __name__ == "__main__":
    main()
