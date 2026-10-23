# Draupnirr-AutoUploader

Outil automatisé pour uploader vos torrents vers le tracker Draupnirr depuis votre client Autobrr/QUI
(anciennement T9-AutoUploader).

## Fonctionnalités

- Liste et filtre les torrents depuis votre client QUI/Autobrr
- Détection des torrents déjà présents sur Draupnirr :
  - via les trackers des torrents dans QUI (`tracker_urls`)
  - via le catalogue Draupnirr : taille identique (`GET /api/torrents/match`) puis infohash (`GET /api/torrents/{infohash}`)
- Re-scellement du `.torrent` exporté depuis QUI (privé, tag source Draupnirr, trackers d'origine retirés) : pas de re-hachage
- Génération automatique du NFO et du rapport MediaInfo
- Recherche TMDB via l'API Draupnirr (clé TMDB personnelle optionnelle pour les genres)
- Assistant d'upload Draupnirr (`POST /api/upload/analyze`) : nom canonique, facettes, avertissements et verdicts de Ratatosk avant publication
- Génération de la description BBCode
- Upload vers Draupnirr puis remise en seed automatique dans le client (cross-seed), indispensable à la vérification Ratatosk

## Catégories prises en compte

| Type | Détection | Clé `categories` | Slug par défaut |
|------|-----------|------------------|-----------------|
| **Documentaire** | TMDB genre 99 | `documentary` | `films-film` |
| **Concert/Musique** | TMDB genre 10402 | `concert` | `films-film` |
| **Film d'animation** | TMDB genre 16 + film | `animated_movie` | `films-film` |
| **Série animée** | TMDB genre 16 + série | `animated_tv` | `series-serie-tv` |
| **Série TV** | Guessit type=episode | `tv` | `series-serie-tv` |
| **Film** | Guessit type=movie | `movie` | `films-film` |

La détection par genre TMDB nécessite une clé TMDB personnelle (`tmdb.api_key`). Sans clé, seuls Film et Série TV sont détectés.

Les slugs sont vérifiés au démarrage contre `GET /api/categories` : si un slug est inconnu, la liste des slugs valides est affichée.

### Non pris en compte

Les catégories suivantes ne sont **pas détectées automatiquement** :

- VO
- Spectacle
- Sport
- Vidéo-clips

## Prérequis

- **Python 3.9+**
- **Linux**
- **mediainfo** installé (`sudo apt install mediainfo`)
- **Autobrr/QUI** installé sur votre instance
- **Compte Draupnirr** avec un jeton API (ou votre passkey)
- **Compte TMDB** (optionnel) pour la détection des genres

## Installation

```bash
git clone https://github.com/votre-username/t9-uploader.git
cd t9-uploader

# Créer un environnement virtuel (recommandé)
python3 -m venv venv
source venv/bin/activate

# Installer les dépendances
pip install -r requirements.txt

# Installer mediainfo si nécessaire
sudo apt install mediainfo
```

## Configuration

Copiez le fichier `config.example.yaml` en `config.yaml` :
```bash
cp config.example.yaml config.yaml
```

### Créer un jeton API Draupnirr

1. Sur Draupnirr, allez dans **Profil** → **Sécurité** → **Jetons API** (mot de passe demandé)
2. Créez un jeton : il commence par `drp_` et n'est affiché qu'une seule fois
3. Copiez-le dans `tracker.api_key`

Le jeton est envoyé dans l'en-tête `X-Api-Key`. Votre passkey fonctionne aussi, mais un jeton est préférable :
il se révoque seul, sans toucher à vos clients torrent, et n'ouvre ni l'announce ni Torznab.

### Adresse du site

`tracker.url` est l'adresse du site Draupnirr (ex : `https://draupnirr.example`). Les routes de l'API sont sous `/api/`.

`tracker.tracker_urls` liste les domaines d'announce Draupnirr : un torrent de QUI dont un tracker contient
l'un d'eux est considéré comme déjà présent sur Draupnirr.

### Créer un compte TMDB et obtenir une clé API (optionnel)

1. Créez un compte sur [TMDB](https://www.themoviedb.org/signup)
2. Allez dans **Settings** → **API** : [https://www.themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
3. Cliquez sur **Create** → **Developer**
4. Remplissez le formulaire et copiez votre **API Read Access Token**

### Récupérer la clé API QUI/Autobrr

1. Ouvrez l'interface de **QUI/Autobrr**
2. Allez dans **Settings** → **API Keys**
3. Créez une nouvelle clé API et copiez-la

### Fichier config.yaml complet

```yaml
# API Tracker Draupnirr
tracker:
  # Domaines d'announce Draupnirr pour détecter les torrents déjà présents
  tracker_urls: ["draupnirr.example"]
  # Adresse du site Draupnirr
  url: "https://draupnirr.example"
  # Jeton API (drp_...) ou passkey
  api_key: "drp_votre_jeton"

# API QUI (Autobrr)
qui:
  # URL de votre instance QUI
  api_url: "http://votre-ip:7476/api"
  # Clé API de QUI (Settings → API Keys)
  api_key: "votre_api_key"
  # ID de l'instance (généralement 1)
  instance_id: 1
  # Transformation des chemins : chemin vu par qBittorrent -> chemin lu par ce script
  content_path_prefix: ""
  content_path_replacement: ""
  # Tag des torrents remis en seed après upload
  upload_tag: "Auto_UP_Draupnirr"

# API TMDB (optionnel, pour les genres)
tmdb:
  api_key: ""
  language: "fr-FR"

# Slugs de catégorie Draupnirr
categories:
  movie: "films-film"
  tv: "series-serie-tv"
  documentary: "films-film"
  concert: "films-film"
  animated_movie: "films-film"
  animated_tv: "series-serie-tv"

# Options
options:
  page_size: 100
  nfo_signature: "NFO automatically Generated by: Draupnirr-AutoUploader"
  temp_dir: "./tmp/draupnirr-uploader"
  skip_no_team_prompt: false
  auto_next_page: false
  only_missing: false
```

### Transformation des chemins

Si le script ne voit pas les fichiers au même chemin que qBittorrent (docker, mergerfs...), renseignez
`content_path_prefix` (préfixe vu par qBittorrent) et `content_path_replacement` (préfixe lu par le script).
Le chemin transformé sert uniquement à lire les fichiers (mediainfo, NFO) ; le `savePath` du cross-seed
envoyé à QUI reste le chemin vu par qBittorrent.

### Options disponibles

| Option | Description |
|--------|-------------|
| `page_size` | Nombre de torrents récupérés en même temps depuis QUI |
| `nfo_signature` | Signature appliquée au NFO généré (vide = pas de signature) |
| `temp_dir` | Dossier temporaire pour stocker les fichiers .torrent |
| `skip_no_team_prompt` | Si `true`, les torrents sans team détectée sont affichés mais skippés automatiquement (pas de demande d'upload) |
| `auto_next_page` | Si `true`, passe automatiquement à la page suivante sans demander |
| `only_missing` | N'afficher que ce qui manque sur Draupnirr. `false` : tout afficher. `"work"` : seulement les œuvres absentes de Draupnirr, toutes qualités confondues (série : la saison ne doit avoir ni pack ni tous ses épisodes ; des épisodes isolés ne masquent rien). `"slot"` : seulement les torrents dont le slot visé est libre, ou qu'ils prendraient à la release actuelle (une analyse Draupnirr par torrent avant la question, limite 30/min) |

## Utilisation

```bash
# Lancer l'outil
python -m src.main

# Avec un fichier de config spécifique
python -m src.main --config /chemin/vers/config.yaml

# Diagnostic : sauvegarde les réponses de l'assistant Draupnirr en JSON (dans temp_dir/analyze)
python -m src.main --dump-analyze
```

## Workflow

1. Connexion à Draupnirr (`GET /api/me`) : vérification de la clé et du tag source, puis des slugs de catégories
2. L'outil récupère la liste des torrents depuis QUI, triés par taille décroissante
3. Filtre les torrents déjà présents sur Draupnirr (via les tracker URLs) et les téléchargements incomplets
4. Filtre les releases dont la taille exacte existe déjà au catalogue Draupnirr
5. Pour chaque torrent : export depuis QUI, re-scellement, puis recherche de son infohash sur Draupnirr
6. Affiche : Nom, Taille, Team détectée, les slots Draupnirr de l'œuvre (occupés en rouge avec la release qui les tient, libres en vert ; par saison pour une série), et demande si vous voulez l'uploader [Y/N]
7. Si "Oui" :
   - Analyse du nom avec `guessit`
   - Recherche TMDB via Draupnirr
   - Génération du NFO et du rapport MediaInfo avec `mediainfo`
   - Assistant Draupnirr : nom canonique, facettes (source, édition, team reprises du nom), slot visé (vert : libre ou repris ; rouge : déjà tenu, confirmation à Non par défaut), avertissements et verdicts de Ratatosk ; les facettes manquantes sont demandées
   - Confirmation avant publication
   - Upload vers Draupnirr
   - Téléchargement du `.torrent` personnalisé et ajout au client pour le cross-seed
8. Si "Non" : passage au torrent suivant
9. À la fin de chaque page, possibilité de continuer ou arrêter

### Vérification Ratatosk

Après l'upload d'un film ou d'une série, la release reste en quarantaine jusqu'à ce que Ratatosk ait vérifié
le média : il le télécharge **depuis votre client**. La release est donc remise en seed automatiquement ;
ne la retirez pas du client avant le verdict (sans seeder : relance au bout de 6 h, puis strike hit-and-run).

## Structure du projet

```
Draupnirr-AutoUploader/
├── config.example.yaml    # Configuration exemple
├── config.yaml            # Votre configuration (à créer)
├── requirements.txt       # Dépendances Python
├── README.md
└── src/
    ├── __init__.py
    ├── main.py              # Point d'entrée CLI
    ├── config.py            # Gestion de la configuration
    ├── qui_client.py        # Client API QUI/Autobrr
    ├── tmdb_client.py       # Résultats TMDB et client TMDB (genres)
    ├── tracker_client.py    # Client API Draupnirr
    ├── torrent_file.py      # Lecture et re-scellement des .torrent
    ├── mediainfo.py         # Génération NFO
    ├── torrent_processor.py # Logique principale
    ├── torrent_namer.py     # Détection de la team
    └── utils.py             # Utilitaires
```

## Dépannage

### "mediainfo n'est pas installé"
```bash
sudo apt install mediainfo
```

### "Fichier de configuration non trouvé"
Créez `config.yaml` à partir de `config.example.yaml`

### "tracker.api_key manquant"
L'ancienne configuration T9 (`api_url` + `bearer_token`) n'est plus utilisée : renseignez `tracker.url` et `tracker.api_key`.

### "Draupnirr 401 : invalid api key"
Jeton révoqué ou mal copié : créez-en un nouveau dans Profil › Sécurité › Jetons API.

### Slug de catégorie inconnu
Corrigez la section `categories` avec l'un des slugs listés au démarrage.

### "Draupnirr 422 : ..."
Upload refusé par Draupnirr : le motif est affiché (doublon, torrent non privé, refus de Ratatosk...).
Un doublon n'est pas une erreur : la release est déjà sur Draupnirr.

### Limite de débit (429)
L'outil attend automatiquement la fin de la fenêtre indiquée par Draupnirr puis réessaie.

### Erreur de connexion QUI
Vérifiez l'URL et la clé API de votre instance QUI/Autobrr

### Erreur 400 lors de l'ajout du torrent
Vérifiez que le `savePath` est correct pour le cross-seed

## Licence

MIT
