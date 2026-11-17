"""Transport HTTP et fichiers locaux des intégrations."""
from __future__ import annotations

from getpass import getpass
import json
from pathlib import Path
import os
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

RACINE = Path(__file__).resolve().parents[1]
DONNEES = RACINE / ".clipper"


class SansRedirection(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ErreurHTTP(ValueError):
    def __init__(self, service: str, status: int, code: str = "") -> None:
        self.status = status
        self.code = code
        super().__init__(f"{service} : HTTP {status}" + (f" ({code})" if code else ""))


def ouvrir(service: str, requete: Request, timeout: int = 30):
    try:
        return build_opener(SansRedirection()).open(requete, timeout=timeout)
    except HTTPError as exc:
        with exc:
            try:
                body = json.loads(exc.read(8192))
                code = str(body.get("code", "")) if isinstance(body, dict) else ""
            except (ValueError, OSError):
                code = ""
        raise ErreurHTTP(service, exc.code, code if re.fullmatch(r"[a-z0-9_]{1,64}", code) else "") from None
    except (URLError, TimeoutError):
        raise ValueError(f"{service} : connexion interrompue ou délai dépassé") from None


def appeler(service: str, url: str, method: str = "GET", headers: dict | None = None,
            contenu: bytes | None = None) -> dict:
    req = Request(url, method=method, headers=headers or {}, data=contenu)
    with ouvrir(service, req) as rep:
        try:
            body = json.load(rep)
        except ValueError:
            raise ValueError(f"{service} : réponse JSON invalide") from None
    if not isinstance(body, dict):
        raise ValueError(f"{service} : réponse inattendue")
    return body


def lire(path: Path, default: dict | None = None) -> dict:
    if default is not None and not path.exists():
        return default
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Fichier local invalide : {path.name}")
    return data


def ecrire(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporaire = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporaire, path)
    finally:
        Path(temporaire).unlink(missing_ok=True)


def secret(variable: str, invite: str) -> str:
    valeur = os.environ.get(variable, "").strip()
    if not valeur:
        if not sys.stdin.isatty():
            raise ValueError(f"Saisir le secret dans un terminal local ou définir {variable}.")
        valeur = getpass(invite).strip()
    if not valeur:
        raise ValueError(f"Valeur manquante pour {variable}")
    return valeur
