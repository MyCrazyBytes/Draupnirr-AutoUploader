"""Lecture et re-scellement des fichiers .torrent (bencode)"""

import hashlib
from dataclasses import dataclass
from typing import List, Tuple


# Clés racines retirées au re-scellement : trackers d'origine (avec leur
# passkey) et liens vers le tracker d'origine. Hors info-dict : sans effet
# sur l'infohash.
ORIGIN_KEYS = (
    b'announce', b'announce-list', b'url-list', b'httpseeds',
    b'comment', b'publisher', b'publisher-url',
)


def bdecode(data: bytes):
    """Décode des données bencodées (les chaînes restent en bytes)"""
    value, _ = _decode(data, 0)
    return value


def _decode(data: bytes, pos: int):
    if pos >= len(data):
        raise ValueError("bencode: fin prématurée")

    char = data[pos:pos + 1]

    if char == b'i':
        end = data.index(b'e', pos)
        return int(data[pos + 1:end]), end + 1

    if char == b'l':
        pos += 1
        items = []
        while data[pos:pos + 1] != b'e':
            item, pos = _decode(data, pos)
            items.append(item)
        return items, pos + 1

    if char == b'd':
        pos += 1
        result = {}
        while data[pos:pos + 1] != b'e':
            key, pos = _decode(data, pos)
            if not isinstance(key, bytes):
                raise ValueError("bencode: clé non textuelle")
            result[key], pos = _decode(data, pos)
        return result, pos + 1

    if char.isdigit():
        colon = data.index(b':', pos)
        length = int(data[pos:colon])
        start = colon + 1
        if start + length > len(data):
            raise ValueError("bencode: longueur de chaîne invalide")
        return data[start:start + length], start + length

    raise ValueError(f"bencode: octet inattendu {char!r} à la position {pos}")


def bencode(value) -> bytes:
    """Encode une valeur en bencode (clés de dictionnaire triées)"""
    if isinstance(value, bool):
        raise TypeError("bencode: booléen non supporté")
    if isinstance(value, int):
        return b'i%de' % value
    if isinstance(value, str):
        value = value.encode('utf-8')
    if isinstance(value, bytes):
        return b'%d:%s' % (len(value), value)
    if isinstance(value, list):
        return b'l' + b''.join(bencode(v) for v in value) + b'e'
    if isinstance(value, dict):
        encoded = {(k.encode('utf-8') if isinstance(k, str) else k): v for k, v in value.items()}
        return b'd' + b''.join(bencode(k) + bencode(encoded[k]) for k in sorted(encoded)) + b'e'
    raise TypeError(f"bencode: type {type(value).__name__} non supporté")


@dataclass
class TorrentMeta:
    """Informations d'un fichier .torrent"""
    name: str
    infohash: str
    total_size: int
    files: List[Tuple[str, int]]  # Chemins relatifs au dossier racine (multi-fichiers)
    single_file: bool
    private: bool
    source: str


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value) if value is not None else ''


def parse_torrent(content: bytes) -> TorrentMeta:
    """Lit un .torrent et calcule son infohash (SHA1 de l'info-dict réencodé)"""
    root = bdecode(content)
    info = root.get(b'info') if isinstance(root, dict) else None
    if not isinstance(info, dict):
        raise ValueError("torrent sans info-dict")

    name = _text(info.get(b'name'))
    files = []
    if b'files' in info:
        for entry in info[b'files']:
            path = '/'.join(_text(p) for p in entry.get(b'path', []))
            files.append((path, entry.get(b'length', 0)))
    else:
        files.append((name, info.get(b'length', 0)))

    return TorrentMeta(
        name=name,
        infohash=hashlib.sha1(bencode(info)).hexdigest(),
        total_size=sum(size for _, size in files),
        files=files,
        single_file=b'files' not in info,
        private=info.get(b'private') == 1,
        source=_text(info.get(b'source'))
    )


def reseal_torrent(content: bytes, source_tag: str) -> bytes:
    """
    Re-scelle un .torrent exporté du client pour Draupnirr :
    private=1, source=<tag>, trackers d'origine retirés.

    Le nouvel infohash est propre à Draupnirr ; le .torrent personnalisé
    (announce avec la clé du membre) est servi par l'API après l'upload.
    """
    root = bdecode(content)
    info = root.get(b'info') if isinstance(root, dict) else None
    if not isinstance(info, dict):
        raise ValueError("torrent sans info-dict")

    info[b'private'] = 1
    info[b'source'] = source_tag.encode('utf-8')

    for key in ORIGIN_KEYS:
        root.pop(key, None)

    return bencode(root)
