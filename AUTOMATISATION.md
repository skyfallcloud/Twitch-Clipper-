# Automatiser Twitch Clipper

Le parcours enchaîne la détection d’une rediffusion Twitch, le téléchargement local, le rendu éventuel des aperçus et la programmation via PlugKit après validation. Les deux services peuvent être configurés indépendamment. Les réglages de style et le nombre de clips restent ceux du profil créé par `scripts/clipper.py configurer`.

## Twitch, de la chaîne au premier aperçu

1. Dans le tableau de bord Twitch, active le stockage des diffusions précédentes et publie les rediffusions que tu veux importer. Si tu souhaites toutes les publier, active la publication automatique des VOD. [Réglages Twitch](https://help.twitch.tv/s/article/video-on-demand?language=fr)
2. Dans la console développeur Twitch, crée une application depuis un compte avec la double authentification. Note son **Client ID** et génère son **Client Secret**. Si le formulaire exige une URL de redirection, utilise `http://localhost:3000`. Le clipper utilise un jeton d’application. [Console développeur](https://dev.twitch.tv/console/apps) · [Documentation Twitch](https://dev.twitch.tv/docs/authentication/register-app/)
3. Depuis un terminal local à la racine du dépôt, configure la chaîne et essaie d’abord l’import à la demande. Le secret est saisi en masqué et enregistré dans `.clipper/twitch.json`, hors de Git. Exécute le diagnostic du [README](README.md) et installe TwitchDownloaderCLI si nécessaire.

```bash
python3 scripts/twitch.py connecter @ma_chaine --client-id TON_CLIENT_ID
python3 scripts/twitch.py derniere
```

Le chemin du MP4 s’affiche après l’import. Lancer deux fois la même commande permet de vérifier que le MP4 est réutilisé. Le chat est facultatif. La détection utilise [Get Users](https://dev.twitch.tv/docs/api/reference/users/#get-users), [Get Streams](https://dev.twitch.tv/docs/api/reference/streams/#get-streams) et [Get Videos](https://dev.twitch.tv/docs/api/reference/videos/#get-videos).

4. Pour préparer aussi les aperçus, ajoute `--rendre`. Utilise le Python de ton environnement virtuel si faster-whisper y est installé. Le modèle doit être présent dans le cache pour les rendus parlés. Le premier chargement se fait avec la commande `clipper.py lancer … --telecharger-modele` du README.

```bash
.venv/bin/python scripts/twitch.py derniere --rendre
```

Les aperçus sont sous `exports/automatique/VOD-ID/apercus/`. Le repérage automatique suit le nombre de clips, la durée et le style du profil. Pour appliquer un choix éditorial de passages et de titres, utilise le parcours guidé du README avec les fichiers locaux.

### Surveillance après chaque live

Sur macOS, lance la surveillance permanente avec un LaunchAgent utilisateur. Il tourne lorsque la session du Mac est disponible, avec une vérification toutes les cinq minutes par défaut.

```bash
.venv/bin/python scripts/twitch.py activer --rendre
python3 scripts/twitch.py arreter
```

Utilise `python3` si tu veux simplement importer, en retirant `--rendre`. Les journaux d’exécution se trouvent dans `.clipper/`. Pour tester une exécution planifiée en premier plan, utilise `python3 scripts/twitch.py synchroniser`. Sur macOS ou Linux, `python3 scripts/twitch.py surveiller` propose le même contrôle dans un terminal, jusqu’à `Ctrl+C`. L’option `--intervalle 300` s’exprime en secondes.

La première vérification sert de point de départ. Pour les rediffusions plus anciennes, utilise `derniere` ou leur URL. Après une interruption, la surveillance rattrape les archives encore disponibles depuis ce point de départ. Les sources déjà importées et les rendus terminés sont suivis par identifiant Twitch.

## PlugKit, du clip validé au calendrier

1. Connecte tes réseaux dans le [tableau de bord PlugKit](https://plugkit.co/). Crée une clé API dans **API Keys → New Key**. Ce service dépend de ton propre plan. [Démarrage PlugKit](https://docs.plugkit.co/quickstart)
2. Depuis le dépôt, saisis la clé dans le terminal. Elle est enregistrée localement dans `.clipper/plugkit.json`. Relève l’identifiant du profil puis celui du compte que tu veux tester.

```bash
python3 scripts/plugkit.py connecter
python3 scripts/plugkit.py profils
python3 scripts/plugkit.py comptes --profil IDENTIFIANT_PROFIL
```

3. Après avoir regardé le clip vertical, prépare le titre, la légende, les comptes et le créneau. Cette commande affiche la demande localement.

```bash
python3 scripts/plugkit.py programmer "/chemin/vers/clip.mp4" \
  --profil IDENTIFIANT_PROFIL --compte IDENTIFIANT_COMPTE \
  --titre "Titre du Short" --legende "Légende du clip" \
  --a "2027-01-10T18:00:00+01:00"
```

4. Relance la même commande avec `--confirmer` quand le MP4, la légende et le créneau sont validés. Le script charge le MP4 puis demande sa programmation. Un `--compte` par destination permet de programmer le même clip sur plusieurs comptes. Pour commencer sur un compte sans le publier, remplace `--a ...` par `--brouillon` et confirme. Ouvre le brouillon dans PlugKit pour relire le résultat.
5. Après un envoi, relève `post_id` et lance `python3 scripts/plugkit.py statut IDENTIFIANT_POST`. Le statut est affiché pour chaque compte. Une relance du même clip aux mêmes destinations et à la même heure réutilise le résultat enregistré. Si PlugKit n’a pas répondu, le clipper reprend avec la même clé de requête pendant la fenêtre disponible. Passé ce délai, vérifie le tableau de bord avant de reprogrammer.
6. Pour un MP4 qui dépasse la limite d’envoi direct, charge-le dans la médiathèque PlugKit puis passe son URL directe avec `--media-url`. Le MP4 et les sous-titres restent dans le dossier d’export pour une diffusion manuelle. [Programmation et médias PlugKit](https://docs.plugkit.co/guides/publishing) · [Règles de reprise PlugKit](https://docs.plugkit.co/concepts/api-conventions#idempotency)

## Niveau de validation

Le pilote d’origine a programmé 25 vidéos sur trois réseaux, soit 75 programmations, d’après le bilan du projet. La version publique couvre le filtrage des archives, la reprise après interruption, l’envoi et la programmation PlugKit par des tests simulés. Le premier essai de bout en bout de cette version avec les identifiants du créateur reste à réaliser.
