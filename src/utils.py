"""Utilitaires divers"""

import re
from typing import Tuple


# « S06 », « S06E03 », « S06E01-E02 » (aussi écrit S06E01E02 ou S06E01-02)
EPISODE_PATTERN = re.compile(
    r'(?:^|[ ._-])S(\d{1,2})(?:E(\d{1,3})(?:-?E?(\d{1,3}))?)?(?:[ ._-]|$)',
    re.IGNORECASE
)


def format_size(size_bytes: int) -> str:
    """Formate une taille en bytes vers une chaîne lisible (GB, MB, etc.)"""
    if size_bytes < 0:
        return "0 B"
    
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    unit_index = 0
    size = float(size_bytes)
    
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    
    if unit_index >= 2:  # GB ou plus
        return f"{size:.2f} {units[unit_index]}"
    elif unit_index == 1:  # MB
        return f"{size:.1f} {units[unit_index]}"
    else:
        return f"{int(size)} {units[unit_index]}"


def sanitize_filename(filename: str) -> str:
    """Nettoie un nom de fichier pour le rendre valide"""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '.')

    brackets = '()[]'
    for char in brackets:
        filename = filename.replace(char, '.')

    while '..' in filename:
        filename = filename.replace('..', '.')

    return filename.strip('.')


def truncate_string(s: str, max_length: int = 80, suffix: str = "...") -> str:
    """Tronque une chaîne si elle est trop longue"""
    if len(s) <= max_length:
        return s
    return s[:max_length - len(suffix)] + suffix


def parse_season_episode(guessit_info: dict) -> Tuple[int, int]:
    """Extrait la saison et l'épisode depuis les infos guessit"""
    season = guessit_info.get('season')
    episode = guessit_info.get('episode')
    
    if isinstance(season, list):
        season = season[0] if season else None
    if isinstance(episode, list):
        episode = episode[0] if episode else None
    
    return season, episode


def format_episode_tag(name: str, guessit_info: dict) -> str:
    """
    Saison/épisode au format attendu par Draupnirr : S01, S01E03, S01E01-E02

    Lu dans le nom de la release, sinon déduit de guessit. "" si absent.
    """
    match = EPISODE_PATTERN.search(name)
    if match:
        tag = f"S{int(match.group(1)):02d}"
        if match.group(2):
            tag += f"E{int(match.group(2)):02d}"
        if match.group(3):
            tag += f"-E{int(match.group(3)):02d}"
        return tag

    season, episode = parse_season_episode(guessit_info)
    if season is None:
        return ""
    if episode is not None:
        return f"S{season:02d}E{episode:02d}"
    return f"S{season:02d}"
