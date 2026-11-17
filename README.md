# Twitch Clipper

Transforme une rediffusion Twitch ou une vidéo locale en aperçus verticaux pour les réseaux sociaux. Tu choisis le nombre de clips par VOD, le style du titre et les sous-titres avant le rendu. La surveillance Twitch peut importer les nouvelles VOD et lancer le rendu. PlugKit programme ensuite les clips validés. [Configurer l’automatisation](AUTOMATISATION.md).

## Ouvrir avec Claude Code ou Codex

Envoie **le lien de ce dépôt** à ton assistant de code, depuis une session qui peut cloner le dépôt, accéder à tes fichiers et exécuter des commandes sur ta machine. L’assistant lit `AGENTS.md` ou `CLAUDE.md`, lance le diagnostic et te pose les questions de départ en un seul message. Après tes réponses, il enregistre le profil puis lance un aperçu. Tu peux répondre **« utilise les réglages proposés »** au lieu de tout choisir.

Si l’assistant te demande une consigne, écris **« Suis le démarrage guidé de Twitch Clipper. »** Une conversation sans accès au terminal peut te donner les commandes à copier ci-dessous. Une session distante doit avoir accès à la vidéo et à FFmpeg dans son propre environnement.

## Premier aperçu sans compte

Télécharge ou clone le dépôt, puis ouvre un terminal à sa racine. Il te faut Python 3.10 ou plus récent et FFmpeg. Sur Windows, remplace `python3` par `py -3` dans les commandes de base.

```bash
python3 scripts/clipper.py demo
```

La commande vérifie FFmpeg, ffprobe, les filtres vidéo et les encodeurs. Elle génère ensuite un MP4 de 1080 × 1920 avec titre, parole synthétique et sous-titres fournis. Ouvre le chemin affiché, sous `exports/demo/apercus/lot-…/short-01.mp4`. Le fichier SRT est dans le même dossier. La démo prend ses sous-titres dans un fichier d’exemple et fonctionne hors ligne après téléchargement du dépôt.

<img src="examples/apercu.jpg" alt="Exemple vertical avec titre en bas et sous-titres jaunes" width="240">

Essaye une palette et une position différentes.

```bash
python3 scripts/clipper.py demo --nombre-clips 2 --duree 12 --position-titre bas \
  --couleur-titre '#FFFFFF' --fond-titre '#003D5C' --couleur-sous-titres '#FFDD66'
```

Chaque rendu crée un nouvel aperçu. Pour afficher le diagnostic avec le système, Python et l’espace disque, lance `python3 scripts/clipper.py diagnostic`. Voici comment préparer FFmpeg si le diagnostic indique qu’il manque.

- **macOS avec Homebrew**. Lance `brew install ffmpeg-full`, puis `export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"` dans le terminal utilisé pour le clipper. [Formule Homebrew](https://formulae.brew.sh/formula/ffmpeg-full)
- **Debian ou Ubuntu**. Installe le paquet `ffmpeg` depuis le gestionnaire de paquets.
- **Windows**. Prends un build dans les [téléchargements FFmpeg](https://ffmpeg.org/download.html), puis ajoute son dossier `bin` au PATH. Ouvre un nouveau PowerShell et lance `py -3 scripts/clipper.py diagnostic`.

Relance le diagnostic après l’installation. Le rendu est vérifié sous Linux dans l’environnement de développement. Les essais sur de vrais Mac et PC restent à réaliser. Le service d’arrière-plan LaunchAgent est propre à macOS. La commande de surveillance en premier plan est aussi prévue pour Linux et Windows.

## Ta VOD et tes préférences

```bash
python3 scripts/clipper.py configurer --nombre-clips 3 --duree 30 --position-titre haut \
  --cadrage entier --langue fr --priorite "réactions et actions lisibles"
```

Le clipper crée le dossier de réception. Par défaut, les résultats vont dans `exports/`. Tu peux choisir un dossier en dehors du dépôt pour les rendus manuels et automatiques.

```bash
python3 scripts/clipper.py configurer --dossier-clips "$HOME/Movies/Twitch Clipper"
```

Sous Windows, utilise par exemple `py -3 scripts/clipper.py configurer --dossier-clips "$HOME\Videos\Twitch Clipper"`. Le profil `config.local.json` est ignoré par Git. `entier` conserve toute l’image sur fond flou. `recadre` remplit la verticale avec un recadrage centré. Les positions du titre sont `haut`, `bas` et `masque`. Les couleurs du titre, du fond et des sous-titres acceptent le format `#RRGGBB`. Le nombre demandé est une cible, et la durée s’applique au repérage automatique. Ton assistant peut tenir compte de la priorité et fournir des timecodes explicites.

Pour une vidéo avec parole, prépare le moteur de transcription locale dans un environnement isolé.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-transcription.txt
.venv/bin/python scripts/clipper.py lancer "/chemin/vers/video.mp4" --telecharger-modele
```

Sous Windows PowerShell, crée l’environnement avec `py -3 -m venv .venv`, puis utilise `.\.venv\Scripts\python.exe` à la place de `.venv/bin/python`.

`--telecharger-modele` autorise la récupération initiale du modèle Whisper small. Aux prochains rendus, lance avec le même Python et sans cette option pour réutiliser le cache. `--transcription` permet de fournir un JSON au [format de l’exemple](examples/demo.json). Pour une capture sans parole, ajoute `--sans-parole` après l’avoir écoutée. Pour des vidéos parlées où tu veux volontairement masquer les sous-titres, utilise `--sous-titres sans`.

Le moteur propose des fenêtres distinctes selon des signaux audio et vidéo. Pour rendre les passages et titres que tu as choisis avec ton assistant, donne les timecodes et les titres associés dans le même ordre. Chaque lot contient trois titres simples par extrait dans `clips.md`. Les propositions s’appuient sur les mots transcrits lorsqu’ils sont disponibles et sur des repères comme le timecode. Dans le parcours guidé, demande à l’assistant trois propositions qui décrivent le passage visionné.

Si un passage ne reçoit aucune ligne de transcription, le lot sort quand même. `clips.md` et `rapport.json` signalent l’extrait à vérifier avant diffusion.

Pour changer le titre d’un seul clip, donne le dossier du lot affiché à la fin du rendu. Garde le fichier source à son chemin d’origine.

```bash
python3 scripts/clipper.py retitrer "/chemin/vers/apercus/lot-…" --clip 1 --choix 2
```

Remplace `--choix 2` par `--titre "Mon titre"` pour écrire le tien. Le clip produit s’ajoute dans un nouveau dossier de lot.

```bash
.venv/bin/python scripts/clipper.py lancer "/chemin/vers/video.mp4" --nombre-clips 1 \
  --passage 14:38 --titre "Le titre que tu as choisi"
```

Pour une VOD publique que tu as le droit d’utiliser, installe [TwitchDownloaderCLI](https://github.com/lay295/TwitchDownloader/releases), mets-le dans ton PATH ou dans `bin/`, puis remplace le chemin du MP4 par son URL `https://www.twitch.tv/videos/ID`. Le MP4 peut être réutilisé et le chat est téléchargé s’il est accessible. Pour retrouver la dernière VOD d’une chaîne ou surveiller les prochains lives, suis le [guide d’automatisation](AUTOMATISATION.md#twitch-de-la-chaîne-au-premier-aperçu).

## Après le montage

Valide le MP4 et sa légende avant de programmer une publication. Le [guide PlugKit](AUTOMATISATION.md#plugkit-du-clip-validé-au-calendrier) explique comment créer un brouillon, programmer sur plusieurs comptes et vérifier le statut par destination. Les connexions à Twitch et PlugKit se configurent avec tes propres comptes.

[Provenance du moteur et du média d’exemple](third_party/NOTICE.md)
