"""Logique principale de traitement des torrents"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import guessit

from .config import Config
from .qui_client import QuiClient
from .tmdb_client import TmdbClient, TmdbResult
from .tracker_client import TrackerClient, Analysis
from .torrent_file import TorrentMeta, parse_torrent, reseal_torrent
from .mediainfo import generate_nfo, get_mediainfo, find_main_video_file
from .utils import sanitize_filename, format_episode_tag


# Limites de POST /api/upload
NFO_MAX_BYTES = 512 * 1024
DESCRIPTION_MAX_LENGTH = 20000
SYNOPSIS_MAX_LENGTH = 4000
MAX_TAGS = 20
MAX_TAG_LENGTH = 50

# Champs de release qui ne sont pas des facettes de nomenclature
RELEASE_FIELDS = ('work_title', 'year', 'episode', 'episode_title')


@dataclass
class PreparedTorrent:
    """Torrent exporté depuis QUI et re-scellé pour Draupnirr"""
    content: bytes
    meta: TorrentMeta


@dataclass
class WorkInfo:
    """Œuvre identifiée depuis le nom de la release (guessit + TMDB via Draupnirr)"""
    guessit_info: dict
    is_series: bool
    episode: str  # S01, S01E03... ("" pour un film)
    tmdb_result: Optional[TmdbResult]


class TorrentProcessor:
    """Processeur principal pour le traitement et l'upload de torrents"""

    def __init__(self, config: Config):
        self.config = config
        self.qui_client = QuiClient(config.qui)
        self.tracker_client = TrackerClient(config.tracker)
        # Clé TMDB personnelle optionnelle : uniquement pour les genres
        self.tmdb_client = TmdbClient(config.tmdb) if config.tmdb.api_key else None
        # Tag source imposé par Draupnirr (remplacé par celui de GET /api/me)
        self.source_tag = "DRAUPNIRR"

    def analyze_torrent_name(self, name: str) -> dict:
        """Analyse le nom du torrent avec guessit"""
        return guessit.guessit(name)

    def determine_media_type(self, guessit_info: dict) -> str:
        """Détermine le type de média depuis guessit"""
        media_type = guessit_info.get('type', 'movie')

        if media_type == 'episode':
            return 'tv'
        return media_type

    def search_tmdb(
        self,
        guessit_info: dict
    ) -> Optional[TmdbResult]:
        """Recherche l'œuvre sur TMDB via l'API Draupnirr (GET /api/tmdb)"""
        title = guessit_info.get('title', '')
        year = guessit_info.get('year')
        media_type = self.determine_media_type(guessit_info)

        if not title:
            return None

        results = self.tracker_client.search_tmdb(query=str(title), media_type=media_type)
        if not results:
            return None

        candidates = [TmdbResult.from_draupnirr(r, media_type) for r in results]

        # Même année à un an près (les dates de sortie varient selon les pays)
        if year:
            for candidate in candidates:
                if candidate.year and abs(candidate.year - year) <= 1:
                    return candidate

        return candidates[0]

    def identify_work(self, torrent_name: str) -> WorkInfo:
        """
        Identifie l'œuvre d'une release : guessit, saison/épisode, puis TMDB.
        Une série sans saison dans le nom n'est pas cherchée (Draupnirr la refuserait).
        """
        guessit_info = self.analyze_torrent_name(torrent_name)
        is_series = self.determine_media_type(guessit_info) == 'tv'
        episode = format_episode_tag(torrent_name, guessit_info) if is_series else ""

        tmdb_result = None
        if episode or not is_series:
            tmdb_result = self.search_tmdb(guessit_info)

        return WorkInfo(guessit_info=guessit_info, is_series=is_series, episode=episode, tmdb_result=tmdb_result)

    def apply_tmdb_genres(self, tmdb_result: TmdbResult):
        """Détecte documentaire / animation / concert via la clé TMDB personnelle"""
        if not self.tmdb_client:
            return
        tmdb_result.apply_genres(
            self.tmdb_client.get_genre_ids(tmdb_result.tmdb_id, tmdb_result.media_type)
        )

    def prepare_torrent(self, torrent_hash: str) -> PreparedTorrent:
        """
        Exporte le .torrent depuis QUI et le re-scelle pour Draupnirr
        (private=1, tag source, trackers d'origine retirés)
        """
        original = self.qui_client.export_torrent(torrent_hash)
        content = reseal_torrent(original, self.source_tag)
        return PreparedTorrent(content=content, meta=parse_torrent(content))

    def find_layout_problems(self, content_path: str, meta: TorrentMeta) -> List[str]:
        """
        Vérifie que les fichiers du torrent sont là où le cross-seed les cherchera :
        <dossier parent du content_path>/<nom du torrent>[/<fichier>], à la bonne taille.

        Returns:
            Problèmes trouvés (liste vide si tout est en place)
        """
        path = content_path.rstrip('/')
        if os.path.basename(path) != meta.name:
            # qBittorrent donne le dossier de téléchargement lui-même quand les
            # fichiers n'ont pas de dossier propre (disposition « sans sous-dossier »)
            return [f"le contenu est « {content_path} », pas un fichier ou dossier « {meta.name} »"]

        root = os.path.dirname(path)
        if not os.path.isdir(root):
            return [f"dossier « {root} » introuvable depuis ce script (voir qui.content_path_prefix)"]

        problems = []
        for file_path, size in meta.files:
            full_path = path if meta.single_file else os.path.join(path, file_path)
            try:
                actual_size = os.path.getsize(full_path)
            except OSError:
                problems.append(f"{file_path} (absent)")
                continue
            if actual_size != size:
                problems.append(f"{file_path} ({actual_size} octets au lieu de {size})")
        return problems

    def save_torrent_file(self, content: bytes, name: str, subdir: str) -> str:
        """
        Sauvegarde un fichier .torrent dans le dossier temporaire

        Returns:
            Chemin vers le fichier .torrent sauvegardé
        """
        directory = os.path.join(self.config.options.temp_dir, subdir)
        os.makedirs(directory, exist_ok=True)

        filepath = os.path.join(directory, f"{sanitize_filename(name)}.torrent")
        with open(filepath, 'wb') as f:
            f.write(content)

        return filepath

    def generate_nfo_for_torrent(
        self,
        content_path: str,
        is_series: bool
    ) -> str:
        """Génère le NFO pour un torrent"""
        nfo = generate_nfo(
            content_path=content_path,
            signature=self.config.options.nfo_signature,
            is_series=is_series
        )

        # Draupnirr refuse un NFO de plus de 512 Ko : fichier principal seulement
        if is_series and len(nfo.encode('utf-8')) > NFO_MAX_BYTES:
            nfo = generate_nfo(
                content_path=content_path,
                signature=self.config.options.nfo_signature,
                is_series=False
            )

        return nfo

    def get_mediainfo_report(self, content_path: str) -> str:
        """Rapport MediaInfo du fichier vidéo principal (champ mediainfo de l'API)"""
        return get_mediainfo(find_main_video_file(content_path))

    def count_media_files(self, content_path: str) -> int:
        """Compte le nombre de fichiers média dans un dossier"""
        extensions = ['.mkv', '.mp4', '.avi', '.m4v', '.ts']
        path = Path(content_path)

        if path.is_file():
            return 1

        if path.is_dir():
            count = 0
            for ext in extensions:
                count += len(list(path.rglob(f'*{ext}')))
                count += len(list(path.rglob(f'*{ext.upper()}')))
            return count

        return 1

    def format_size_gib(self, size_bytes: int) -> str:
        """Formate une taille en GiB"""
        gib = size_bytes / (1024 ** 3)
        return f"{gib:.2f} GiB"

    def get_category(
        self,
        is_series: bool,
        is_anime: bool = False,
        is_documentary: bool = False,
        is_music: bool = False
    ) -> str:
        """Retourne le slug de catégorie Draupnirr (section categories de la config)"""
        categories = self.config.categories
        if is_documentary:
            return categories.documentary
        if is_music:
            return categories.concert
        if is_anime:
            if is_series:
                return categories.animated_tv
            return categories.animated_movie
        if is_series:
            return categories.tv
        return categories.movie

    def build_release_values(self, tmdb_result: TmdbResult, episode: str) -> Dict[str, str]:
        """Champs de release connus avant l'analyse (les facettes s'y ajoutent ensuite)"""
        values = {
            'work_title': tmdb_result.title,
            'year': str(tmdb_result.year or ''),
        }
        if episode:
            values['episode'] = episode
        return values

    def build_analyze_fields(
        self,
        category: str,
        mediainfo: str,
        tmdb_result: TmdbResult,
        values: Dict[str, str]
    ) -> List[Tuple[str, str]]:
        """Champs de POST /api/upload/analyze"""
        fields = [
            ('category', category),
            ('mediainfo', mediainfo),
            ('tmdb_id', str(tmdb_result.tmdb_id)),
            ('tmdb_type', tmdb_result.media_type),
        ]
        for key, value in values.items():
            fields.append((key if key in RELEASE_FIELDS else f'facets[{key}]', value))
        return fields

    def build_upload_fields(
        self,
        category: str,
        mediainfo: str,
        description: str,
        tmdb_result: TmdbResult,
        values: Dict[str, str],
        tags: List[str]
    ) -> List[Tuple[str, str]]:
        """Champs de POST /api/upload (mêmes valeurs que l'analyse, dans meta[...])"""
        fields = [
            ('category', category),
            ('description', description),
            ('description_format', 'bbcode'),
            ('mediainfo', mediainfo),
            ('meta[tmdb_id]', str(tmdb_result.tmdb_id)),
            ('meta[tmdb_type]', tmdb_result.media_type),
            ('meta[poster_url]', tmdb_result.poster_url),
            ('meta[synopsis]', tmdb_result.overview[:SYNOPSIS_MAX_LENGTH]),
        ]
        for key, value in values.items():
            fields.append((f'meta[{key}]' if key in RELEASE_FIELDS else f'meta[facets][{key}]', value))
        for tag in tags[:MAX_TAGS]:
            fields.append(('meta[tags][]', tag[:MAX_TAG_LENGTH]))
        return fields

    def build_description(
        self,
        tmdb_result: TmdbResult,
        analysis: Analysis,
        values: Dict[str, str],
        torrent_size: int,
        content_path: str
    ) -> str:
        """Génère la description BBCode de la release"""

        def facet(key: str) -> str:
            return values.get(key) or analysis.facet(key)

        def joined(*parts: str, sep: str = ' · ') -> str:
            return sep.join(p for p in parts if p)

        lines = ["[center]"]
        if tmdb_result.poster_url:
            lines.append(f"[img]{tmdb_result.poster_url}[/img]")
        year = f" ({tmdb_result.year})" if tmdb_result.year else ""
        lines.append(f"[size=22][b]{tmdb_result.title}[/b][/size]{year}")
        if values.get('episode'):
            lines.append(f"[b]{values['episode']}[/b]")
        lines.append("[/center]")

        if tmdb_result.overview:
            lines += ["", "[h2]Synopsis[/h2]", f"[quote]{tmdb_result.overview}[/quote]"]

        technical = [
            ("Release", f"[c]{analysis.built_name or analysis.name}[/c]"),
            ("Source", joined(facet('source'), facet('edition'))),
            ("Résolution", joined(facet('resolution'), facet('hdr'))),
            ("Vidéo", joined(facet('video_codec'), facet('bit_depth'), sep=' ')),
            ("Audio", joined(joined(facet('audio_codec'), facet('channels'), sep=' '), facet('languages'), sep=' — ')),
            ("Sous-titres", analysis.media('subtitles')),
            ("Durée", joined(analysis.media('duration'), analysis.media('bitrate'))),
            ("Taille totale", self.format_size_gib(torrent_size)),
            ("Nombre de fichier", str(self.count_media_files(content_path))),
            ("Team", facet('group')),
        ]
        lines += ["", "[h2]Fiche technique[/h2]", "[list]"]
        lines += [f"[*][b]{label}[/b] : {value}" for label, value in technical if value]
        lines.append("[/list]")

        lines += [
            "",
            f"[center][url=https://www.themoviedb.org/{tmdb_result.media_type}/{tmdb_result.tmdb_id}]Fiche TMDB[/url][/center]"
        ]

        return '\n'.join(lines)[:DESCRIPTION_MAX_LENGTH]
