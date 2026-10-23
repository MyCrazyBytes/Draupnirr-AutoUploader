"""Client API QUI (Autobrr)"""

import requests
from typing import List, Optional, Generator
from dataclasses import dataclass

from .config import QuiConfig


# Configuration par défaut pour la transformation des chemins
DEFAULT_PATH_PREFIX = "/media"
DEFAULT_PATH_REPLACEMENT = "/media/merge-storage"


@dataclass
class TorrentInfo:
    """Informations d'un torrent depuis QUI"""
    name: str
    hash: str
    size: int
    content_path: str  # Chemin lu par le script (après transformation)
    client_content_path: str  # Chemin vu par le client torrent (savePath du cross-seed)
    state: str
    progress: float

    @classmethod
    def from_api_response(
        cls,
        data: dict,
        path_prefix: str = DEFAULT_PATH_PREFIX,
        path_replacement: str = DEFAULT_PATH_REPLACEMENT
    ) -> 'TorrentInfo':
        """
        Crée un TorrentInfo depuis la réponse API.

        Transforme le content_path en remplaçant path_prefix par path_replacement.
        Exemple: /media/downloads/... -> /media/merge-storage/downloads/...
        Le chemin d'origine est gardé dans client_content_path pour le client torrent.
        """
        raw_path = data['content_path']

        # Remplacer le préfixe par le nouveau chemin, seulement s'il couvre un dossier
        # entier : /media ne doit pas transformer /media2/...
        prefix = path_prefix.rstrip('/') if path_prefix else ''
        if path_prefix and path_replacement and (raw_path == prefix or raw_path.startswith(prefix + '/')):
            content_path = path_replacement.rstrip('/') + raw_path[len(prefix):]
        else:
            content_path = raw_path

        return cls(
            name=data['name'],
            hash=data['hash'],
            size=data['size'],
            content_path=content_path,
            client_content_path=raw_path,
            state=data.get('state', 'unknown'),
            progress=data.get('progress', 0)
        )


class QuiClient:
    """Client pour l'API QUI/Autobrr"""

    def __init__(self, config: QuiConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            'X-API-Key': config.api_key,
            'Accept': 'application/json'
        })

    def get_instances(self) -> List[dict]:
        """
        Récupère les instances qBittorrent configurées dans QUI

        Returns:
            Liste des instances (id, name, host, connected, isActive...)
        """
        url = f"{self.config.api_url}/instances"

        response = self.session.get(url)
        response.raise_for_status()

        return response.json()

    def get_torrents_page(
        self,
        page: int = 0,
        limit: int = 10,
        sort: str = 'size',
        order: str = 'desc'
    ) -> tuple[List[TorrentInfo], int]:
        """
        Récupère une page de torrents depuis QUI

        Returns:
            Tuple (liste de torrents, nombre total)
        """
        url = f"{self.config.api_url}/instances/{self.config.instance_id}/torrents"

        params = {
            'page': page,
            'limit': limit,
            'sort': sort,
            'order': order
        }

        response = self.session.get(url, params=params)
        response.raise_for_status()

        data = response.json()
        torrents = [
            TorrentInfo.from_api_response(
                t,
                path_prefix=self.config.content_path_prefix,
                path_replacement=self.config.content_path_replacement
            )
            for t in data.get('torrents', [])
        ]
        total = data.get('total', len(torrents))

        return torrents, total

    def iter_torrents(
        self,
        page_size: int = 10,
        sort: str = 'size',
        order: str = 'desc'
    ) -> Generator[tuple[List[TorrentInfo], int, int], None, None]:
        """
        Itère sur les torrents page par page

        Yields:
            Tuple (liste de torrents de la page, numéro de page, total)
        """
        page = 0
        total = None

        while True:
            torrents, total_count = self.get_torrents_page(
                page=page,
                limit=page_size,
                sort=sort,
                order=order
            )

            if total is None:
                total = total_count

            if not torrents:
                break

            yield torrents, page, total

            page += 1

            # Vérifier si on a atteint la fin
            if (page * page_size) >= total:
                break

    def export_torrent(self, torrent_hash: str) -> bytes:
        """
        Exporte un fichier .torrent depuis QUI

        Args:
            torrent_hash: Hash du torrent à exporter

        Returns:
            Contenu binaire du fichier .torrent
        """
        url = f"{self.config.api_url}/instances/{self.config.instance_id}/torrents/{torrent_hash}/export"

        response = self.session.get(url)
        response.raise_for_status()

        return response.content

    def get_torrent_trackers(self, torrent_hash: str) -> List[str]:
        """
        Récupère la liste des trackers d'un torrent

        Args:
            torrent_hash: Hash du torrent

        Returns:
            Liste des URLs de trackers
        """
        url = f"{self.config.api_url}/instances/{self.config.instance_id}/torrents/{torrent_hash}/trackers"

        response = self.session.get(url)
        response.raise_for_status()

        data = response.json()
        # Extraire les URLs des trackers
        return [tracker.get('url', '') for tracker in data if tracker.get('url')]

    def add_torrent(
        self,
        torrent_content: bytes,
        torrent_filename: str,
        save_path: str = "",
        tags: str = "Auto_UP_Draupnirr",
        category: str = ""
    ) -> dict:
        """
        Ajoute un torrent au client via QUI

        Args:
            torrent_content: Contenu binaire du fichier .torrent
            torrent_filename: Nom du fichier .torrent
            save_path: Chemin de sauvegarde (pour cross-seed)
            tags: Tags à ajouter (défaut: Auto_UP_Draupnirr)
            category: Catégorie du torrent

        Returns:
            Réponse de l'API
        """
        url = f"{self.config.api_url}/instances/{self.config.instance_id}/torrents"

        # Multipart form-data, noms de champs lus par QUI (savepath en minuscules).
        # Démarré et disposition d'origine imposés : sinon les réglages par défaut
        # de qBittorrent s'appliquent (torrent arrêté, fichiers cherchés dans un
        # sous-dossier qui n'existe pas)
        files = {
            'category': (None, category),
            'savepath': (None, save_path),
            'paused': (None, 'false'),
            'skip_checking': (None, 'true'),
            'contentLayout': (None, 'Original'),
            'tags': (None, tags),
            'torrent': (torrent_filename, torrent_content, 'application/x-bittorrent'),
            'urls': (None, '')
        }

        # Utiliser la session pour garder les headers d'authentification
        response = self.session.post(url, files=files)

        # Debug: afficher l'erreur si échec
        if not response.ok:
            print(f"[DEBUG] QUI API Error: {response.status_code} - {response.text}")

        response.raise_for_status()

        # La réponse peut être vide ou JSON
        if response.text:
            try:
                return response.json()
            except:
                return {'status': 'ok'}
        return {'status': 'ok'}
