#!/usr/bin/env python3
"""Clipping multiformat (gaming, émissions, musique, Just Chatting, IRL) : diagnostic,
repérage indicatif, sélection humaine, aperçu et export. Aucune hypothèse de jeu ou de catégorie.

Adapté du clipping d'Achille B (MIT), voir third_party/NOTICE.md.
FFmpeg/ffprobe sont requis. faster-whisper est chargé uniquement pour transcrire.
Aucun téléchargement, installation, publication ou suppression de source implicite.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from fractions import Fraction
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

W, H = 1080, 1920
PAS = 0.5


def executer(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"Échec de {args[0]} : {result.stderr[-1600:]}")
    return result.stdout + result.stderr


def ff(*args: str, cwd: Path | None = None) -> str:
    return executer(["ffmpeg", "-hide_banner", "-nostdin", "-threads", "2", *args], cwd)


def lire_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def ecrire_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def nombre(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} doit être un nombre fini")
    return float(value)


def identite(video: Path) -> dict:
    stat = video.stat()
    return {"chemin": str(video.resolve()), "taille": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def sonder(video: Path) -> dict:
    raw = json.loads(executer(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)]))
    streams = raw.get("streams", [])
    visuels = [s for s in streams if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not visuels:
        raise ValueError("La source ne contient aucune piste vidéo")
    stream = visuels[0]
    duration = float(raw["format"].get("duration", stream.get("duration", 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Durée de la source invalide")
    rate = stream.get("avg_frame_rate", "0/0")
    try:
        fps = float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        fps = 0
    if fps <= 0:
        rate = stream.get("r_frame_rate", "0/0")
        fps = float(Fraction(rate))
    return {"duree": duration, "largeur": stream["width"], "hauteur": stream["height"],
            "fps": fps, "taux": rate, "codec": stream.get("codec_name"),
            "audio": [s for s in streams if s["codec_type"] == "audio"]}


def diagnostic() -> dict:
    checks = {name: shutil.which(name) is not None for name in ("ffmpeg", "ffprobe", "uv")}
    checks["faster_whisper"] = importlib.util.find_spec("faster_whisper") is not None
    if checks["ffmpeg"]:
        filters = ff("-filters")
        for name in ("ass", "loudnorm", "scdet"):
            checks[f"filtre_{name}"] = re.search(rf"\s{name}\s", filters) is not None
    return checks


def volume(video: Path, piste: int) -> list[float]:
    out = ff("-i", str(video), "-map", f"0:a:{piste}", "-vn", "-af",
             f"aresample=44100,asetnsamples=n={int(44100 * PAS)}:p=0,astats=metadata=1:reset=1,"
             "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-")
    return [float(v) if v not in ("-inf", "inf", "nan") else -90.0
            for v in re.findall(r"RMS_level=(\S+)", out)]


def images(video: Path) -> tuple[dict[int, list[float]], list[float]]:
    out = ff("-i", str(video), "-an", "-vf", "fps=10,scale=320:-2,scdet=threshold=12,metadata=print:file=-", "-f", "null", "-")
    action, coupes, t = {}, [], 0.0
    for line in out.splitlines():
        match = re.search(r"pts_time:([\d.]+)", line)
        if match:
            t = float(match[1])
        match = re.search(r"lavfi\.scd\.score=([\d.]+)", line)
        if match:
            action.setdefault(int(t), []).append(float(match[1]))
        match = re.search(r"lavfi\.scd\.time=([\d.]+)", line)
        if match:
            coupes.append(float(match[1]))
    return action, coupes


def normer(values: list[float]) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    low, high = ordered[int(len(values) * .1)], ordered[max(0, int(len(values) * .95) - 1)]
    return [min(1., max(0., (v - low) / (high - low))) if high > low else 0. for v in values]


def joindre(mots: list[dict]) -> str:
    """Recoller les mots : Whisper découpe « l'exemple » en « l » + « 'exemple » et « est-ce » en « est » + « -ce »."""
    texte = ""
    for m in mots:
        w = m["w"]
        texte += w if not texte or w[0] in "'’-" else " " + w
    return texte


def valider_mots(data: dict, duration: float) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("mots"), list):
        raise ValueError("La transcription doit contenir une liste mots")
    result, previous = [], -1.
    for item in data["mots"]:
        if not isinstance(item, dict):
            raise ValueError("Mot invalide")
        start, end = nombre(item.get("s"), "s"), nombre(item.get("e"), "e")
        word = item.get("w")
        if not isinstance(word, str) or not word.strip() or len(word) > 300:
            raise ValueError("Chaque mot doit contenir un texte non vide (300 caractères maximum)")
        if not 0 <= start < end <= duration + .05 or start < previous:
            raise ValueError("Temps des mots invalides ou non chronologiques")
        result.append({"w": word.strip(), "s": start, "e": min(end, duration)})
        previous = start
    return result


def transcrire(video: Path, piste: int, autoriser_telechargement: bool, langue: str = "fr") -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ValueError("faster-whisper absent. Utiliser une transcription JSON fournie, ou préparer explicitement "
                         "l'environnement selon le README. Rien n'a été installé.") from exc
    try:
        model = WhisperModel("small", device="cpu", compute_type="int8", local_files_only=not autoriser_telechargement)
    except Exception as exc:
        raise ValueError("Modèle small indisponible. Vérifier le cache ou autoriser son téléchargement "
                         "avec --telecharger-modele (réseau et espace disque requis).") from exc
    # WAV temporaire sur disque : ne pas charger l'audio de toute une VOD en RAM.
    with tempfile.TemporaryDirectory(prefix="clipper-audio-") as temp:
        wav = Path(temp) / "voix.wav"
        ff("-i", str(video), "-map", f"0:a:{piste}", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav))
        # Lire le WAV 16 kHz nous-mêmes : faster-whisper 1.2.x passe sinon par PyAV,
        # dont la version 19 a retiré l'option metadata_errors (échec à l'ouverture).
        import numpy as np
        import wave
        with wave.open(str(wav), "rb") as handle:
            audio = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = model.transcribe(audio, language=None if langue == "auto" else langue, word_timestamps=True, vad_filter=True)
        words = [{"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)}
                 for segment in segments for w in (segment.words or []) if w.word.strip() and w.end > w.start]
    return {"langue": info.language, "mots": words, "origine": "faster-whisper-small ; à corriger"}


def analyser(video: Path, dossier: Path, n: int, duree: float, transcription: Path | None,
             sans_transcription: bool, telecharger: bool, piste: int, langue: str = "fr",
             assurer_nombre: bool = False) -> None:
    t0 = time.monotonic()
    meta = sonder(video)
    if meta["audio"] and not 0 <= piste < len(meta["audio"]):
        raise ValueError("Piste audio inexistante")
    if dossier.exists():
        raise ValueError("Le dossier de sortie existe déjà ; choisir un autre --sortie pour préserver les corrections")
    if transcription:
        transcript = lire_json(transcription)
    elif sans_transcription or not meta["audio"]:
        transcript = {"langue": "fr", "mots": [], "origine": "non transcrit ; à compléter ou confirmer sans parole"}
    else:
        transcript = transcrire(video, piste, telecharger, langue)
    words = valider_mots(transcript, meta["duree"])
    starts = [m["s"] for m in words]
    print("Analyse du volume et des changements à l'image…", flush=True)
    vol = normer(volume(video, piste)) if meta["audio"] else []
    action_s, cuts = images(video)
    seconds = math.ceil(meta["duree"])
    action = normer([statistics.mean(action_s.get(s, [0.])) for s in range(seconds)])
    length = min(duree, meta["duree"])
    candidates, t = [], 0.
    while t + length <= meta["duree"] + .0001:
        values = vol[int(t / PAS):math.ceil((t + length) / PAS)] or [0.]
        mean, peak = statistics.mean(values), max(values)
        activity = statistics.mean(action[int(t):math.ceil(t + length)] or [0.])
        nb_cuts = sum(t <= c < t + length for c in cuts)
        spoken = words[bisect_left(starts, t):bisect_left(starts, t + length)]
        score = .35 * mean + .2 * peak + .3 * activity + .05 * min(1., nb_cuts / 4) + .1 * min(1., len(spoken) / length / 2.5)
        candidates.append({"debut": round(t, 3), "fin": round(min(t + length, meta["duree"]), 3),
                           "score_technique": round(score, 3), "paroles": joindre(spoken)})
        t += 1.
    selected = []
    for candidate in sorted(candidates, key=lambda c: c["score_technique"], reverse=True):
        if all(candidate["fin"] <= c["debut"] or candidate["debut"] >= c["fin"] for c in selected):
            selected.append(candidate)
        if len(selected) >= 2 * n:
            break
    if assurer_nombre and len(selected) < n:
        # Une fenêtre centrale très bien notée peut bloquer deux fenêtres distinctes.
        # Essayer l’espacement régulier avant de signaler un manque de passages.
        regulieres = candidates[::max(1, math.ceil(length))][:n]
        if len(regulieres) > len(selected):
            selected = sorted(regulieres, key=lambda c: c["score_technique"], reverse=True)
    dossier.mkdir(parents=True)
    transcript["mots"] = words
    source = identite(video)
    ecrire_json(dossier / "transcription.json", transcript)
    ecrire_json(dossier / "analyse.json", {"version": 1, "source": source, "media": meta, "piste_audio": piste,
        "candidats": selected, "avertissement": "Indice technique ; aucune détection de l'humour ou du sens de l'action.",
        "temps_secondes": round(time.monotonic() - t0, 2)})
    ecrire_json(dossier / "choix.json", {"source": source, "clips": []})
    example = {"debut": 0., "fin": min(length, meta["duree"]), "bandeau": "", "titre": "À écrire après visionnage",
               "description": "", "hashtags": [], "cadrage": "entier"}
    ecrire_json(dossier / "choix.exemple.json", {"source": source, "clips": [example]})
    print(f"{len(selected)} propositions dans {dossier / 'analyse.json'}. Remplir choix.json après visionnage ; aucun clip sélectionné automatiquement.")


def valider_choix(data: dict, duration: float) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list) or not data["clips"]:
        raise ValueError("Remplir choix.json avec au moins un clip choisi après visionnage")
    if len(data["clips"]) > 50:
        raise ValueError("Maximum 50 clips par lot")
    clips = []
    for raw in data["clips"]:
        if not isinstance(raw, dict):
            raise ValueError("Clip invalide")
        clip = dict(raw)
        start, end = nombre(clip.get("debut"), "debut"), nombre(clip.get("fin"), "fin")
        if not 0 <= start < end <= duration + .001:
            raise ValueError("Timecodes du clip hors de la source ou inversés")
        if clip.get("cadrage", "entier") not in ("entier", "recadre"):
            raise ValueError("Cadrage attendu : entier ou recadre")
        for key in ("ancrage_x", "ancrage_y"):
            if not 0 <= nombre(clip.get(key, .5), key) <= 1:
                raise ValueError(f"{key} doit être compris entre 0 et 1")
        for key in ("bandeau", "titre", "description"):
            if not isinstance(clip.get(key, ""), str):
                raise ValueError(f"{key} doit être du texte")
        if len(clip.get("bandeau", "")) > 90:
            raise ValueError("Bandeau trop long (90 caractères maximum)")
        tags = clip.get("hashtags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("hashtags doit être une liste de textes")
        for key in ("sans_parole_confirme", "apercu_sans_sous_titres"):
            if key in clip and not isinstance(clip[key], bool):
                raise ValueError(f"{key} doit être un booléen")
        clip["debut"], clip["fin"] = start, min(end, duration)
        clips.append(clip)
    clips.sort(key=lambda c: c["debut"])
    if any(a["fin"] > b["debut"] for a, b in zip(clips, clips[1:])):
        raise ValueError("Les clips se chevauchent ; corriger la sélection")
    return clips


def taux_sortie(meta: dict, fps: str) -> str:
    if fps == "auto":
        return meta["taux"] if meta["fps"] <= 60.01 else "60"
    if float(fps) > meta["fps"] + 1:
        raise ValueError("Le réglage demandé dépasse la fréquence source ; utiliser auto")
    return fps


def sous_titres(clip: dict, mots: list[dict]) -> list[dict]:
    duration = clip["fin"] - clip["debut"]
    if "sous_titres" in clip:
        entries = clip["sous_titres"]
        if not isinstance(entries, list):
            raise ValueError("sous_titres doit être une liste")
        result, previous = [], 0.
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("Sous-titre invalide")
            a, b = nombre(item.get("debut"), "début sous-titre"), nombre(item.get("fin"), "fin sous-titre")
            text = item.get("texte")
            if not isinstance(text, str) or not text.strip() or len(text) > 160:
                raise ValueError("Texte du sous-titre invalide (160 caractères maximum)")
            if not 0 <= a < b <= duration + .001 or a < previous:
                raise ValueError("Sous-titres hors du clip ou se chevauchant")
            result.append({"debut": a, "fin": min(b, duration), "texte": text})
            previous = b
        return result
    words = [m for m in mots if m["e"] > clip["debut"] and m["s"] < clip["fin"]]
    result, i = [], 0
    while i < len(words):
        packet = [words[i]]
        i += 1
        while i < len(words):
            # Ne jamais commencer une ligne par « 'ai » ou « -ce » : ces fragments restent avec le mot précédent.
            rattache = words[i]["w"][0] in "'’-"
            if not rattache and (len(packet) >= 3 or words[i]["s"] - packet[-1]["e"] > .5
                                 or len(joindre(packet + [words[i]])) > 30):
                break
            packet.append(words[i])
            i += 1
        start = max(0., packet[0]["s"] - clip["debut"])
        following = words[i]["s"] - clip["debut"] if i < len(words) else duration
        end = min(duration, following, packet[-1]["e"] - clip["debut"] + .2)
        if end > start:
            result.append({"debut": start, "fin": end, "texte": joindre(packet)})
    return result


def temps(value: float, ass: bool = False) -> str:
    units = round(value * (100 if ass else 1000))
    scale = 100 if ass else 1000
    sec, fraction = divmod(units, scale)
    minutes, sec = divmod(sec, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}.{fraction:02d}" if ass else f"{hours:02d}:{minutes:02d}:{sec:02d},{fraction:03d}"


def texte_ass(text: str) -> str:
    # Neutraliser les balises ASS provenant de titres ou transcriptions.
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r", "").replace("\n", "\\N")


def couleur_ass(value: str, alpha: str = "00") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Couleur attendue au format #RRGGBB")
    return f"&H{alpha}{value[5:7]}{value[3:5]}{value[1:3]}".upper()


def ecrire_sous_titres(base: Path, clip: dict, entries: list[dict]) -> None:
    position = clip.get("position_titre", "haut")
    if position not in ("haut", "bas", "masque"):
        raise ValueError("Position du titre attendue : haut, bas ou masque")
    couleur_titre = couleur_ass(clip.get("couleur_titre", "#FFFFFF"))
    fond_titre = couleur_ass(clip.get("fond_titre", "#000000"), "35")
    couleur_sous_titres = couleur_ass(clip.get("couleur_sous_titres", "#FFFFFF"))
    alignement, marge_titre, marge_sous_titres = (2, 285, 465) if position == "bas" else (8, 200, 470)
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Mots,Arial,64,{couleur_sous_titres},{couleur_sous_titres},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,2,100,180,{marge_sous_titres},1",
        f"Style: Bandeau,Arial,64,{couleur_titre},{couleur_titre},{fond_titre},{fond_titre},-1,0,0,0,100,100,0,0,3,8,0,{alignement},100,180,{marge_titre},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    if clip.get("bandeau") and position != "masque":
        lines.append(f"Dialogue: 1,{temps(0, True)},{temps(clip['fin'] - clip['debut'], True)},Bandeau,,0,0,0,,{texte_ass(clip['bandeau'])}")
    srt = []
    for i, entry in enumerate(entries, 1):
        a, b, text = entry["debut"], entry["fin"], entry["texte"]
        lines.append(f"Dialogue: 0,{temps(a, True)},{temps(b, True)},Mots,,0,0,0,,{texte_ass(text)}")
        srt.append(f"{i}\n{temps(a)} --> {temps(b)}\n{text}\n")
    base.with_suffix(".ass").write_text("\n".join(lines) + "\n", encoding="utf-8")
    base.with_suffix(".srt").write_text("\n".join(srt), encoding="utf-8")


def graphe(clip: dict, rate: str, ass: str) -> str:
    prefix = f"[0:v:0]fps={rate},setsar=1"
    if clip.get("cadrage", "entier") == "entier":
        return (prefix + f",split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=28,eq=brightness=-0.18[fond];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[avant];"
                f"[fond][avant]overlay=(W-w)/2:(H-h)/2,ass={ass},format=yuv420p[v]")
    x, y = clip.get("ancrage_x", .5), clip.get("ancrage_y", .5)
    return prefix + f",scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}:(iw-ow)*{x}:(ih-oh)*{y},ass={ass},format=yuv420p[v]"


def rendre(video: Path, dossier: Path, choix_path: Path, fps: str, final: bool, valide: bool) -> dict:
    t0 = time.monotonic()
    analysis, choices = lire_json(dossier / "analyse.json"), lire_json(choix_path)
    source = identite(video)
    if analysis.get("source") != source or not isinstance(choices, dict) or choices.get("source") != source:
        raise ValueError("La source a changé ou le choix correspond à une autre vidéo ; refaire l'analyse")
    meta = sonder(video)
    clips = valider_choix(choices, meta["duree"])
    transcript = lire_json(dossier / "transcription.json")
    words = valider_mots(transcript, meta["duree"])
    rate = taux_sortie(meta, fps)
    captions = [sous_titres(c, words) for c in clips]
    for clip, entries in zip(clips, captions):
        if not entries and clip.get("sans_parole_confirme") is not True:
            if not final and clip.get("apercu_sans_sous_titres") is True:
                continue
            raise ValueError("Sous-titres absents : compléter la transcription, fournir sous_titres dans le choix, "
                             "ou confirmer sans_parole_confirme=true après écoute")
    signature = hashlib.sha256(json.dumps({"source": source, "clips": clips, "sous_titres": captions, "fps": rate,
        "piste_audio": analysis["piste_audio"]}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if final:
        if not valide:
            raise ValueError("Export final : --valide requis après validation humaine de l'aperçu complet et lecture smartphone")
        previews = sorted(dossier.glob("apercu*/rapport.json"))
        if not any(lire_json(path).get("empreinte_plan") == signature for path in previews):
            raise ValueError("Créer un aperçu avec exactement ce choix, ces sous-titres et cette fréquence avant l'export final")
    destination = dossier / ("final" if final else "apercu")
    if destination.exists():
        if final:
            raise ValueError("Un export final existe déjà ; le conserver et utiliser un autre dossier de travail")
        version = 2
        while (dossier / f"apercu-{version:03d}").exists():
            version += 1
        destination = dossier / f"apercu-{version:03d}"
    report = {"version": 1, "etape": "final" if final else "apercu", "empreinte_plan": signature,
              "validation_humaine_declaree": bool(final and valide), "verification_automatique": "en cours", "clips": []}
    # Construire dans un dossier temporaire puis déplacer seulement un lot terminé.
    with tempfile.TemporaryDirectory(prefix=".rendu-", dir=dossier) as temp:
        stage = Path(temp)
        for i, (clip, entries) in enumerate(zip(clips, captions), 1):
            name = f"short-{i:02d}"
            ecrire_sous_titres(stage / name, clip, entries)
            args = ["-n", "-ss", str(clip["debut"]), "-t", str(clip["fin"] - clip["debut"]), "-i", str(video.resolve()),
                    "-filter_complex_threads", "1", "-filter_complex", graphe(clip, rate, name + ".ass"), "-map", "[v]"]
            if meta["audio"]:
                args += ["-map", f"0:a:{analysis['piste_audio']}", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
                         "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
            args += ["-map_metadata", "-1", "-c:v", "libx264", "-threads", "2", "-preset", "veryfast" if not final else "medium",
                     "-crf", "20", "-movflags", "+faststart", name + ".mp4"]
            ff(*args, cwd=stage)
            output = sonder(stage / (name + ".mp4"))
            if (output["largeur"], output["hauteur"], output["codec"]) != (W, H, "h264"):
                raise ValueError("Métadonnées de sortie non conformes")
            if abs(output["fps"] - float(Fraction(rate))) > .1 or abs(output["duree"] - (clip["fin"] - clip["debut"])) > .25:
                raise ValueError("Fréquence ou durée de sortie non conforme")
            if bool(output["audio"]) != bool(meta["audio"]) or (output["audio"] and output["audio"][0].get("codec_name") != "aac"):
                raise ValueError("Piste audio de sortie non conforme")
            ff("-v", "error", "-xerror", "-i", name + ".mp4", "-f", "null", "-", cwd=stage)
            for label, fraction in (("debut", .05), ("milieu", .5), ("fin", .95)):
                ff("-n", "-ss", str((clip["fin"] - clip["debut"]) * fraction), "-i", name + ".mp4", "-frames:v", "1",
                   "-vf", "scale=360:-2", f"{name}-{label}.jpg", cwd=stage)
            report["clips"].append({"fichier": name + ".mp4", "duree": output["duree"], "fps": output["fps"],
                "audio": bool(output["audio"]), "sous_titres": len(entries),
                "sous_titres_a_verifier": not entries and clip.get("sans_parole_confirme") is not True,
                "decodage_complet": True})
        report["verification_automatique"] = "metadonnees_et_decodage_ok"
        report["temps_secondes"] = round(time.monotonic() - t0, 2)
        ecrire_json(stage / "rapport.json", report)
        ecrire_json(stage / "choix-utilise.json", choices)
        md = ["# Clips verticaux", "", "Valider chaque vidéo complète : narration, sous-titres, cadrage, son et lecture smartphone.", ""]
        for i, clip in enumerate(clips, 1):
            md += [f"## short-{i:02d}.mp4", f"Titre : {clip.get('titre', '')}", f"Description : {clip.get('description', '')}",
                   "Hashtags : " + " ".join(clip.get("hashtags", [])), ""]
            if clip.get("propositions_titre"):
                md += ["Titres proposés", ""] + [
                    f"{numero}. {titre}" for numero, titre in enumerate(clip["propositions_titre"], 1)
                ] + [""]
            if report["clips"][i - 1]["sous_titres_a_verifier"]:
                md += ["Cet aperçu est dépourvu de sous-titres. Vérifier s’il contient de la parole.", ""]
        (stage / "clips.md").write_text("\n".join(md), encoding="utf-8")
        shutil.move(str(stage), str(destination))
    print(f"{len(clips)} clip(s) dans {destination} ; contrôles techniques réussis. Validation éditoriale humaine requise.")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="etape", required=True)
    subs.add_parser("diagnostic", help="Vérifier les outils sans installer ni télécharger")
    analyse = subs.add_parser("analyser", help="Proposer des passages sans les sélectionner")
    render = subs.add_parser("rendre", help="Créer un aperçu ; export final après validation")
    for command in (analyse, render):
        command.add_argument("video", type=Path)
        command.add_argument("--sortie", type=Path, required=True, help="Dossier d'analyse et de travail (hors Git)")
    analyse.add_argument("--n", type=int, default=3, help="Nombre maximal de clips envisagés, sans quota à remplir")
    analyse.add_argument("--duree", type=float, default=25, help="Fenêtre indicative en secondes")
    transcription = analyse.add_mutually_exclusive_group()
    transcription.add_argument("--transcription", type=Path, help="JSON de mots fourni ou corrigé, sans modèle")
    transcription.add_argument("--sans-transcription", action="store_true", help="Repérage seul ; sous-titres à fournir avant le rendu")
    analyse.add_argument("--telecharger-modele", action="store_true", help="Autoriser explicitement le téléchargement du modèle small")
    analyse.add_argument("--piste-audio", type=int, default=0, help="Index de piste audio, 0 par défaut")
    render.add_argument("--choix", type=Path, help="choix.json par défaut dans le dossier de travail")
    render.add_argument("--fps", choices=("auto", "30", "60"), default="auto", help="Conserver la fréquence source jusqu'à 60 i/s")
    render.add_argument("--final", action="store_true")
    render.add_argument("--valide", action="store_true", help="Attester la validation humaine de l'aperçu complet, dont lecture smartphone")
    args = parser.parse_args()
    try:
        if args.etape == "diagnostic":
            print(json.dumps(diagnostic(), ensure_ascii=False, indent=2))
            return 0
        for exe in ("ffmpeg", "ffprobe"):
            if not shutil.which(exe):
                raise ValueError(f"{exe} absent ; préparer l'environnement selon le README")
        video = args.video.resolve()
        if not video.is_file():
            raise ValueError("Fichier vidéo absent ; vérifier le chemin de la source")
        if args.etape == "analyser":
            if not 1 <= args.n <= 50 or not math.isfinite(args.duree) or args.duree <= 0 or args.piste_audio < 0:
                raise ValueError("n doit être compris entre 1 et 50, durée positive et piste audio non négative")
            analyser(video, args.sortie.resolve(), args.n, args.duree, args.transcription, args.sans_transcription,
                     args.telecharger_modele, args.piste_audio)
        else:
            rendre(video, args.sortie.resolve(), args.choix or args.sortie / "choix.json", args.fps, args.final, args.valide)
        return 0
    except (ValueError, OSError, KeyError, ZeroDivisionError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
