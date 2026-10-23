"""Résultats TMDB (recherche via Draupnirr) et client TMDB optionnel pour les genres"""

import requests
from typing import List, Optional
from dataclasses import dataclass

from .config import TmdbConfig


GENRE_ANIMATION = 16
GENRE_DOCUMENTARY = 99
GENRE_MUSIC = 10402


def parse_year(value) -> Optional[int]:
    """Année depuis un entier, un flottant ou une date 'AAAA-MM-JJ'"""
    if value is None or value == '':
        return None
    try:
        return int(str(value)[:4])
    except ValueError:
        return None


@dataclass
class TmdbResult:
    """Œuvre TMDB retenue pour une release"""
    tmdb_id: int
    title: str
    original_title: str
    year: Optional[int]
    media_type: str  # 'movie' ou 'tv'
    overview: str
    poster_url: str
    is_anime: bool = False  # True = animation (genre 16)
    is_documentary: bool = False  # True = documentaire (genre 99)
    is_music: bool = False  # True = musique/concert (genre 10402)

    @classmethod
    def from_draupnirr(cls, data: dict, media_type: str) -> 'TmdbResult':
        """Crée un TmdbResult depuis un résultat de GET /api/tmdb"""
        year = parse_year(data.get('year'))
        if year is None:
            year = parse_year(data.get('release_date') or data.get('first_air_date'))

        return cls(
            tmdb_id=int(data['id']),
            title=data.get('title') or data.get('name') or '',
            original_title=data.get('original_title') or data.get('original_name') or '',
            year=year,
            media_type=media_type,
            overview=data.get('overview') or '',
            poster_url=data.get('poster_url') or ''
        )

    def apply_genres(self, genre_ids: List[int]):
        """Renseigne les types détectés depuis les genres TMDB"""
        self.is_anime = GENRE_ANIMATION in genre_ids
        self.is_documentary = GENRE_DOCUMENTARY in genre_ids
        self.is_music = GENRE_MUSIC in genre_ids


class TmdbClient:
    """Client pour l'API TMDB (clé personnelle, optionnelle)"""

    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self, config: TmdbConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {config.api_key}',
            'Accept': 'application/json'
        })

    def get_genre_ids(self, tmdb_id: int, media_type: str = 'movie') -> List[int]:
        """
        Récupère les genres d'une œuvre TMDB

        Args:
            tmdb_id: ID TMDB
            media_type: 'movie' ou 'tv'

        Returns:
            Liste des IDs de genres
        """
        url = f"{self.BASE_URL}/{media_type}/{tmdb_id}"

        response = self.session.get(url, params={'language': self.config.language})
        response.raise_for_status()

        return [genre['id'] for genre in response.json().get('genres', [])]
