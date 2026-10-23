"""Client API Tracker Draupnirr"""

import re
import socket
import time
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from . import __version__
from .config import TrackerConfig


# Réessais sur 429 (limites de débit Draupnirr)
MAX_ATTEMPTS = 6
RETRY_BASE_SECONDS = 2
RETRY_CAP_SECONDS = 120

# GET /api/torrents/match accepte 500 tailles au maximum par appel
MATCH_BATCH_SIZE = 500

# Délais réseau (secondes). Connexion courte, appliquée à chaque adresse du
# domaine : une adresse IPv6 injoignable bascule vite sur l'IPv4 au lieu de
# bloquer en silence. Lecture plus longue pour l'analyse et l'upload.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
UPLOAD_READ_TIMEOUT = 300


def prefer_ipv4():
    """
    Essaie les adresses IPv4 d'un domaine avant ses adresses IPv6.

    Python essaie les adresses une par une (curl, lui, les essaie en parallèle) :
    sur un serveur dont l'IPv6 ne répond pas, chaque adresse IPv6 listée en
    premier coûtait un délai de connexion complet avant d'arriver à l'IPv4.
    L'IPv6 reste en secours pour les machines sans IPv4.
    """
    original = socket.getaddrinfo
    if getattr(original, 'prefers_ipv4', False):
        return

    def getaddrinfo(*args, **kwargs):
        return sorted(original(*args, **kwargs), key=lambda info: info[0] != socket.AF_INET)

    getaddrinfo.prefers_ipv4 = True
    socket.getaddrinfo = getaddrinfo


class TrackerApiError(Exception):
    """Erreur renvoyée par l'API Draupnirr ({"error": "..."})"""

    def __init__(self, status: int, message: str):
        super().__init__(f"Draupnirr {status} : {message}")
        self.status = status
        self.message = message

    @property
    def is_duplicate(self) -> bool:
        """Un 422 de doublon n'est pas un échec : la release est déjà là"""
        msg = self.message.lower()
        return self.status == 422 and ('doublon' in msg or 'duplicate' in msg or 'déjà' in msg)


def _as_dict(value) -> dict:
    """PHP encode un tableau associatif vide en [] : on le lit comme {}"""
    return value if isinstance(value, dict) else {}


def _as_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        return ', '.join(str(v) for v in value)
    return str(value)


class Analysis:
    """Réponse de POST /api/upload/analyze (l'assistant d'upload)"""

    def __init__(self, data: dict):
        self.data = data
        self.nomenclature = _as_dict(data.get('nomenclature'))

    @property
    def name(self) -> str:
        """Nom du .torrent"""
        return self.data.get('name') or ''

    @property
    def built_name(self) -> str:
        """Nom canonique calculé par Draupnirr ("" sans work_title)"""
        return self.nomenclature.get('built_name') or ''

    @property
    def duplicate(self) -> bool:
        return bool(self.data.get('duplicate'))

    @property
    def warnings(self) -> List[str]:
        """Avertissements (doublon, private, source)"""
        return [
            w.get('message', str(w)) if isinstance(w, dict) else str(w)
            for w in self.data.get('warnings') or []
        ]

    @property
    def bot_issues(self) -> List[dict]:
        """Verdicts de Ratatosk : [{"level": "block|warn|info", "code": ..., "message": ...}]"""
        bot = self.data.get('bot')
        return [issue for issue in bot if isinstance(issue, dict)] if isinstance(bot, list) else []

    @property
    def missing(self) -> List[str]:
        """Facettes manquantes pour le nom canonique"""
        return [str(m) for m in self.nomenclature.get('missing') or []]

    @property
    def suggested_tags(self) -> List[str]:
        return [str(t) for t in self.data.get('suggested_tags') or []]

    @property
    def slot(self) -> dict:
        """Slot visé par la release (code, label, state, rank, reason, occupant, against, free...)"""
        return _as_dict(self.data.get('slot'))

    @property
    def slot_taken(self) -> bool:
        """
        Le slot est déjà tenu par une autre release, que celle-ci ne remplacerait pas.
        state « occupant » = la release prendrait le slot (libre, ou à la place de l'actuelle).
        """
        return bool(self.slot.get('occupant')) and self.slot.get('state') != 'occupant'

    @property
    def blocking_reasons(self) -> List[str]:
        """Ce qui fera refuser l'upload (422)"""
        reasons = []
        if self.data.get('ok') is False:
            reasons.append("analyse refusée par Draupnirr")
        if self.data.get('executable'):
            reasons.append("contenu exécutable : rapport VirusTotal requis, à uploader à la main")
        if self.data.get('mediainfo_missing'):
            reasons.append("MediaInfo illisible par Draupnirr")
        for issue in self.bot_issues:
            if issue.get('level') == 'block':
                reasons.append(f"Ratatosk : {issue.get('message', issue.get('code', ''))}")
        return reasons

    def facet(self, key: str) -> str:
        """Valeur d'une facette retenue par Draupnirr ("" si absente)"""
        facet = _as_dict(self.nomenclature.get('facets')).get(key)
        if isinstance(facet, dict):
            return _as_text(facet.get('value'))
        return _as_text(facet)

    def facets(self) -> Dict[str, str]:
        """Toutes les facettes retenues, non vides"""
        facets = _as_dict(self.nomenclature.get('facets'))
        return {key: self.facet(key) for key in facets if self.facet(key)}

    def name_facets(self) -> Dict[str, str]:
        """Facettes déclarées par le nom de la release"""
        facets = _as_dict(self.nomenclature.get('name_facets'))
        return {key: _as_text(value) for key, value in facets.items() if value}

    def media(self, key: str) -> str:
        """Information lue dans le média (duration, bitrate, subtitles...)"""
        return _as_text(_as_dict(self.nomenclature.get('media')).get(key))

    def vocabulary(self, key: str) -> List[str]:
        """Valeurs admises pour une facette (si Draupnirr les fournit)"""
        values = _as_dict(self.nomenclature.get('vocabulary')).get(key)
        if isinstance(values, dict):
            return [str(v) for v in values]
        if isinstance(values, list):
            return [str(v) for v in values]
        return []


# Paliers de qualité Draupnirr, dans l'ordre d'affichage
TIER_ORDER = ('720p', '1080p-light', '1080p-web', '1080p', '2160p-light', '2160p-web', '2160p', 'remux')


def tier_sort_key(tier: str) -> int:
    return TIER_ORDER.index(tier) if tier in TIER_ORDER else len(TIER_ORDER)


class WorkSlots:
    """Réponse de GET /api/works/{movie|tv}/{tmdb_id}/slots : la carte des slots d'une œuvre"""

    def __init__(self, data: dict):
        self.data = data

    @property
    def title(self) -> str:
        return _as_dict(self.data.get('work')).get('title') or ''

    def editions(self) -> Dict[str, List[dict]]:
        """Films : slots par édition (theatrical, extended...)"""
        return {
            name: [slot for slot in _as_dict(edition).get('slots') or [] if isinstance(slot, dict)]
            for name, edition in _as_dict(self.data.get('editions')).items()
        }

    @staticmethod
    def is_taken(slot: dict) -> bool:
        return bool(slot.get('occupant')) or slot.get('state') not in (None, 'free')

    @staticmethod
    def season_number(episode: str) -> int:
        """Numéro de saison d'un tag S01, S01E03..."""
        return int(re.match(r'S(\d+)', episode).group(1))

    def season(self, number: int) -> dict:
        """Séries : synthèse d'une saison (episodes_total, tiers {palier: {pack, covered}})"""
        for season in self.data.get('seasons') or []:
            if isinstance(season, dict) and season.get('season') == number:
                return season
        return {}

    def season_coverage(self, number: int) -> Dict[str, str]:
        """Séries : paliers occupés d'une saison → « pack » ou « 4/10 ép. »"""
        season = self.season(number)
        total = season.get('episodes_total') if season.get('episodes_total_known') else None
        coverage = {}
        for tier, state in _as_dict(season.get('tiers')).items():
            state = _as_dict(state)
            if state.get('pack'):
                coverage[tier] = "pack"
            elif state.get('covered'):
                coverage[tier] = f"{state['covered']}/{total} ép." if total else f"{state['covered']} ép."
        return coverage

    def has_release(self, episode: str = "") -> bool:
        """
        L'œuvre a-t-elle déjà une release sur Draupnirr, toutes qualités confondues ?

        Série : la saison doit être couverte (pack, ou tous ses épisodes). Des
        épisodes isolés ne disent pas lesquels : la release reste proposée.
        """
        if not episode:
            return any(self.is_taken(slot) for slots in self.editions().values() for slot in slots)

        season = self.season(self.season_number(episode))
        total = season.get('episodes_total') if season.get('episodes_total_known') else None
        for state in _as_dict(season.get('tiers')).values():
            state = _as_dict(state)
            if state.get('pack') or (total and (state.get('covered') or 0) >= total):
                return True
        return False


class TrackerClient:
    """Client pour l'API du tracker Draupnirr"""

    def __init__(self, config: TrackerConfig):
        self.config = config
        self.base_url = config.url
        self.session = requests.Session()
        self.session.headers.update({
            'X-Api-Key': config.api_key,
            'Accept': 'application/json',
            'User-Agent': f'Draupnirr-AutoUploader/{__version__}'
        })

    def torrent_page_url(self, torrent_id: str) -> str:
        """URL de la fiche d'une release sur le site"""
        return f"{self.base_url}/torrents/{torrent_id}"

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        """Pause demandée par un 429 : Retry-After, sinon X-RateLimit-Reset, sinon backoff"""
        retry_after = response.headers.get('Retry-After', '').strip()
        if retry_after:
            if retry_after.isdigit():
                return int(retry_after) + 1
            try:
                return max(parsedate_to_datetime(retry_after).timestamp() - time.time(), 0) + 1
            except (TypeError, ValueError):
                pass

        reset = response.headers.get('X-RateLimit-Reset', '').strip()
        if reset.isdigit():
            return max(int(reset) - time.time(), 0) + 1

        return RETRY_BASE_SECONDS * (2 ** attempt)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Requête vers l'API, avec attente et réessai sur 429"""
        url = f"{self.base_url}{path}"
        timeout = (CONNECT_TIMEOUT, UPLOAD_READ_TIMEOUT if method == 'POST' else READ_TIMEOUT)

        for attempt in range(MAX_ATTEMPTS):
            response = self.session.request(method, url, timeout=timeout, **kwargs)
            if response.status_code != 429 or attempt + 1 >= MAX_ATTEMPTS:
                break
            delay = self._retry_delay(response, attempt)
            if delay > RETRY_CAP_SECONDS:
                break
            print(f"  Limite de débit Draupnirr atteinte, nouvelle tentative dans {delay:.0f}s...")
            time.sleep(delay)

        if response.status_code >= 400:
            try:
                data = response.json()
                message = data.get('error') or data.get('message') or ''
            except ValueError:
                message = ''
            if not message:
                message = response.text.strip()[:200]
            raise TrackerApiError(response.status_code, message)

        return response

    def get_me(self) -> dict:
        """GET /api/me : compte du membre (valide la clé, donne le source_tag)"""
        return self._request('GET', '/api/me').json()

    def get_categories(self) -> List[dict]:
        """GET /api/categories : slugs valides pour l'upload"""
        data = self._request('GET', '/api/categories').json()
        if isinstance(data, dict):
            return data.get('categories', [])
        return data

    def get_torrent(self, id_or_infohash: str) -> Optional[dict]:
        """
        GET /api/torrents/{id|infohash} : détail d'une release

        Returns:
            Fiche de la release, ou None si inconnue au catalogue (404)
        """
        try:
            return self._request('GET', f'/api/torrents/{id_or_infohash}').json()
        except TrackerApiError as e:
            if e.status == 404:
                return None
            raise

    def match_sizes(self, sizes: Sequence[int]) -> List[dict]:
        """GET /api/torrents/match : releases du catalogue de ces tailles exactes"""
        unique_sizes = sorted(set(sizes))
        matches = []
        for i in range(0, len(unique_sizes), MATCH_BATCH_SIZE):
            batch = unique_sizes[i:i + MATCH_BATCH_SIZE]
            response = self._request(
                'GET', '/api/torrents/match',
                params={'sizes': ','.join(str(s) for s in batch)}
            )
            matches.extend(response.json())
        return matches

    def get_work_slots(self, tmdb_type: str, tmdb_id: int) -> Optional[WorkSlots]:
        """
        GET /api/works/{movie|tv}/{tmdb_id}/slots : carte des slots d'une œuvre
        (série : synthèse par saison)

        Returns:
            Carte des slots, ou None si l'œuvre est inconnue (404)
        """
        try:
            return WorkSlots(self._request('GET', f'/api/works/{tmdb_type}/{tmdb_id}/slots').json())
        except TrackerApiError as e:
            if e.status == 404:
                return None
            raise

    def search_tmdb(self, query: str, media_type: str = 'movie') -> List[dict]:
        """GET /api/tmdb : recherche d'œuvre (clé TMDB côté serveur)"""
        data = self._request('GET', '/api/tmdb', params={'q': query, 'type': media_type}).json()
        if isinstance(data, dict):
            return data.get('results', [])
        return data

    def analyze(self, torrent_content: bytes, fields: List[Tuple[str, str]]) -> Analysis:
        """
        POST /api/upload/analyze : l'assistant d'upload

        Args:
            torrent_content: Contenu du .torrent re-scellé
            fields: Champs du formulaire (category, mediainfo, work_title, facets[...]...)

        Returns:
            Fiche calculée par Draupnirr (name, infohash, warnings, bot, nomenclature...)
        """
        files = {'torrent': ('release.torrent', torrent_content, 'application/x-bittorrent')}
        data = [(k, v) for k, v in fields if v not in (None, '')]
        return Analysis(self._request('POST', '/api/upload/analyze', files=files, data=data).json())

    def upload_torrent(
        self,
        torrent_content: bytes,
        torrent_filename: str,
        fields: List[Tuple[str, str]],
        nfo: str = ""
    ) -> dict:
        """
        POST /api/upload : upload d'une release

        Args:
            torrent_content: Contenu du .torrent re-scellé
            torrent_filename: Nom du fichier .torrent envoyé
            fields: Champs du formulaire (category, description, mediainfo, meta[...]...)
            nfo: Contenu du NFO (optionnel)

        Returns:
            {"id": ..., "infohash": ..., "status": ..., "awaiting_validation": ...}
        """
        files = {'torrent': (torrent_filename, torrent_content, 'application/x-bittorrent')}
        if nfo:
            files['nfo'] = ('release.nfo', nfo.encode('utf-8'), 'text/plain')

        data = [(k, v) for k, v in fields if v not in (None, '')]
        result = self._request('POST', '/api/upload', files=files, data=data).json()

        if not result.get('id'):
            raise TrackerApiError(201, f"réponse d'upload sans id : {result}")

        return result

    def download_torrent(self, torrent_id: str) -> bytes:
        """
        GET /api/torrents/{id}/download : .torrent personnalisé avec la clé du membre

        Args:
            torrent_id: ID public (sqid) ou infohash

        Returns:
            Contenu binaire du fichier .torrent
        """
        return self._request('GET', f'/api/torrents/{torrent_id}/download').content
