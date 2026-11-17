"""Programmer un MP4 déjà validé sur des comptes connectés à PlugKit."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request
from uuid import uuid4

import clipping
import services
import twitch_import

API = "https://api.plugkit.co/v1"
CONFIG = services.DONNEES / "plugkit.json"
JOURNAL = services.DONNEES / "publications.json"


def cle() -> str:
    token = os.environ.get("PLUGKIT_API_KEY") or services.lire(CONFIG, {}).get("api_key")
    if not token:
        raise ValueError("Configurer PlugKit dans un terminal local avec plugkit connecter")
    return token


def appel(token: str, route: str, method: str = "GET", contenu: dict | None = None,
          unique: str | None = None) -> dict:
    headers = {"Authorization": "Bearer " + token}
    if contenu is not None:
        headers["Content-Type"] = "application/json"
    if unique:
        headers["Idempotency-Key"] = unique
    return services.appeler("PlugKit", API + route, method, headers,
                             json.dumps(contenu).encode() if contenu is not None else None)


def lister(token: str, ressource: str, **query):
    page = 1
    while True:
        res = appel(token, f"/{ressource}?" + urlencode(dict(query, page=page)))
        yield from res[ressource]
        if not res.get("pagination", {}).get("hasMore"):
            return
        page += 1


def profil_compte(obj: dict) -> dict:
    champs = ("_id", "name", "displayName", "username", "platform", "status")
    return {champ: obj[champ] for champ in champs if champ in obj}


def connexion() -> None:
    token = services.secret("PLUGKIT_API_KEY", "Clé API PlugKit : ")
    profils = appel(token, "/profiles")["profiles"]
    services.ecrire(CONFIG, {"api_key": token})
    afficher([profil_compte(p) for p in profils])


def afficher(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def empreinte(mp4: Path) -> str:
    h = hashlib.sha256()
    with mp4.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def horaire(texte: str) -> str:
    date = datetime.fromisoformat(texte.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Donner la date avec un fuseau, par exemple 2027-01-10T18:00:00+01:00")
    return date.astimezone(timezone.utc).isoformat(timespec="seconds")


def url_https(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("PlugKit : URL HTTPS d’envoi ou de média invalide")
    return url


def envoyer(token: str, mp4: Path) -> str:
    ticket = appel(token, "/media/upload-url", "POST", {"filename": mp4.name})
    taille = mp4.stat().st_size
    limites = [valeur for k in ("maxBytes", "singlePutMaxBytes")
               if (valeur := ticket.get(k)) is not None]
    if any(taille > limite for limite in limites):
        raise ValueError("Clip trop volumineux pour un envoi direct. Charger le MP4 dans la médiathèque PlugKit "
                         "et fournir son URL avec --media-url.")
    if ticket["uploadMethod"] != "PUT":
        raise ValueError("PlugKit : méthode d’envoi inconnue")
    url = url_https(ticket["uploadUrl"])
    media = url_https(ticket["mediaUrl"])
    with mp4.open("rb") as f:
        req = Request(url, method="PUT", data=f, headers={"Content-Length": str(taille)})
        with services.ouvrir("Envoi du MP4", req, timeout=300):
            pass
    return media


def statut(post: dict) -> dict:
    champs_reseau = ("platform", "accountId", "status", "platformPostUrl", "errorCategory")
    return {
        "post_id": post["_id"],
        "statut": post.get("status"),
        "comptes": [
            {key: dest[key] for key in champs_reseau if key in dest}
            for dest in post.get("platforms", [])
        ],
    }


def planifier(args: argparse.Namespace) -> None:
    mp4 = args.mp4.expanduser().resolve()
    if not mp4.is_file() or mp4.suffix.lower() != ".mp4":
        raise ValueError("Choisir un clip MP4 local finalisé")
    if not args.titre.strip() or len(args.titre) > 100 or not args.legende.strip():
        raise ValueError("Fournir un titre de 1 à 100 caractères et une légende")
    comptes = sorted(set(args.compte))
    contenu = {"profileId": args.profil, "accountIds": comptes, "title": args.titre, "content": args.legende}
    if args.brouillon:
        contenu["isDraft"] = True
    else:
        contenu["scheduledFor"] = horaire(args.a)
    media_url = url_https(args.media_url) if args.media_url else None
    if not args.confirmer:
        afficher({"mode": "aperçu local", "mp4": str(mp4), **contenu})
        print("Après validation des comptes, de la date et du clip, relancer avec --confirmer.")
        return

    meta = clipping.sonder(mp4)
    if meta["largeur"] >= meta["hauteur"]:
        raise ValueError("Choisir la version verticale du clip")
    # Même fichier, mêmes destinations et même créneau : une seule programmation.
    identite = json.dumps([empreinte(mp4), args.profil, comptes, contenu.get("scheduledFor", "brouillon")])
    identifiant = hashlib.sha256(identite.encode()).hexdigest()
    services.DONNEES.mkdir(mode=0o700, exist_ok=True)
    with twitch_import.verrou(JOURNAL.with_suffix(".lock")):
        journal = services.lire(JOURNAL, {})
        entree = journal.get(identifiant)
        if entree and "resultat" in entree:
            print("Programmation déjà enregistrée.")
            afficher(entree["resultat"])
            return
        if entree and (entree["plan"] != contenu or entree.get("media_source") != media_url):
            raise ValueError("Une demande différente est déjà en attente pour ce clip et ce créneau. "
                             "Vérifier d’abord le tableau de bord PlugKit.")
        maintenant = datetime.now(timezone.utc)
        if not args.brouillon and datetime.fromisoformat(contenu["scheduledFor"]) <= maintenant:
            raise ValueError("Ce créneau est passé. Pour une demande déjà envoyée, vérifier le tableau de bord PlugKit.")
        if entree and maintenant - datetime.fromisoformat(entree["debut"]) >= timedelta(hours=23):
            raise ValueError("Résultat de la précédente demande inconnu et fenêtre de relance expirée. "
                             "Vérifier le tableau de bord PlugKit avant toute nouvelle programmation.")

        token = cle()
        if not entree:
            admis = {c["_id"] for c in lister(token, "accounts", profileId=args.profil)}
            if set(comptes) - admis:
                raise ValueError("Au moins un compte est absent du profil indiqué")
            media = media_url or envoyer(token, mp4)
            entree = {"plan": contenu, "media_source": media_url, "media": media,
                      "debut": datetime.now(timezone.utc).isoformat(), "cle": str(uuid4())}
            journal[identifiant] = entree
        deja_tentee = entree.get("tentative_ambigue", False)
        entree["tentative_ambigue"] = True
        services.ecrire(JOURNAL, journal)
        requete = {**contenu, "mediaItems": [{"type": "video", "url": entree["media"]}]}
        try:
            rep = appel(token, "/posts", "POST", requete, entree["cle"])
        except services.ErreurHTTP as exc:
            if exc.status in (400, 401, 402, 403, 404, 422) and exc.code not in (
                "idempotency_conflict", "idempotency_key_reused",
            ):
                # Refus confirmé en amont. Une réponse perdue ou un 5xx conserve la clé.
                # En cas de relance après une réponse ambiguë, garder aussi l'état initial.
                if not deja_tentee:
                    del journal[identifiant]
                    services.ecrire(JOURNAL, journal)
            raise
        # Si PlugKit confirme sans structure exploitable, la prochaine demande garde
        # la même clé de création au lieu de déclencher un nouveau post.
        entree["resultat"] = statut(rep["post"])
        services.ecrire(JOURNAL, journal)
        afficher(entree["resultat"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="action", required=True)
    cmds.add_parser("connecter")
    cmds.add_parser("profils")
    comptes = cmds.add_parser("comptes")
    comptes.add_argument("--profil", required=True)
    resultat = cmds.add_parser("statut")
    resultat.add_argument("post_id")
    action = cmds.add_parser("programmer")
    action.add_argument("mp4", type=Path)
    action.add_argument("--profil", required=True)
    action.add_argument("--compte", required=True, action="append")
    action.add_argument("--titre", required=True)
    action.add_argument("--legende", required=True)
    mode = action.add_mutually_exclusive_group(required=True)
    mode.add_argument("--a", metavar="DATE_ISO_AVEC_FUSEAU")
    mode.add_argument("--brouillon", action="store_true", help="Créer un brouillon dans PlugKit")
    action.add_argument("--media-url", help="URL directe d’un MP4 déjà chargé, par exemple dans la médiathèque PlugKit")
    action.add_argument("--confirmer", action="store_true", help="Autoriser l’envoi et le créneau affichés")
    args = parser.parse_args()
    try:
        if args.action == "connecter":
            connexion()
        elif args.action == "profils":
            afficher([profil_compte(p) for p in lister(cle(), "profiles")])
        elif args.action == "comptes":
            afficher([profil_compte(c) for c in lister(cle(), "accounts", profileId=args.profil)])
        elif args.action == "statut":
            afficher(statut(appel(cle(), "/posts/" + quote(args.post_id, safe=""))["post"]))
        else:
            planifier(args)
        return 0
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
