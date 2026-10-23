"""Détection de la team de release depuis le nom du torrent"""

import re


def extract_group_from_name(name: str) -> str:
    """Extrait le groupe de release depuis le nom du torrent"""
    clean_name = name

    for ext in ['.torrent', '.mkv', '.avi', '.mp4']:
        if clean_name.lower().endswith(ext):
            clean_name = clean_name[:-len(ext)]

    clean_name = clean_name.replace(' ', '.')
    while '..' in clean_name:
        clean_name = clean_name.replace('..', '.')

    bracket_match = re.search(r'\[([A-Za-z0-9]+)\]\.?$', clean_name)
    if bracket_match:
        return bracket_match.group(1)

    dash_match = re.search(r'-([A-Za-z0-9]+)(?:\[.*\])?$', clean_name)
    if dash_match:
        return dash_match.group(1)

    technical_words = {'1080p', '720p', '480p', '2160p', '4k', 'x264', 'x265','h265','264','265','hevc', 'avc',
                       'bluray', 'webrip', 'webdl', 'hdtv', 'dvdrip', 'bdrip', 'multi',
                       'vostfr', 'french', 'english', 'truefrench', 'vff', 'vf', 'vo',
                       'hdrip', 'brrip', 'web', 'dl', 'remux', 'proper', 'repack'}

    release_markers = {'1080p', '720p', '480p', '2160p', '4k', 'x264', 'x265', 'hevc',
                       'bluray', 'webrip', 'webdl', 'hdtv', 'dvdrip', 'bdrip', 'hdrip',
                       'brrip', 'remux', 'web-dl', 'web'}

    parts = clean_name.split('.')
    parts_lower = [p.lower() for p in parts]

    has_release_marker = any(marker in parts_lower for marker in release_markers)

    if len(parts) > 4 and has_release_marker:
        last_part = parts[-1]
        if (last_part.lower() not in technical_words and
            re.match(r'^[A-Za-z0-9]{2,15}$', last_part)):
            return last_part

    return ''
