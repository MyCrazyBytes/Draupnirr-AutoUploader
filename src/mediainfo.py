"""Génération de NFO avec mediainfo"""

import subprocess
from pathlib import Path
from typing import Optional


def check_mediainfo_installed() -> bool:
    try:
        result = subprocess.run(
            ['mediainfo', '--version'],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def get_mediainfo(file_path: str) -> str:
    """
    Exécute mediainfo sur un fichier et retourne le résultat

    Args:
        file_path: Chemin vers le fichier média

    Returns:
        Sortie de mediainfo
    """
    if not check_mediainfo_installed():
        raise RuntimeError(
            "mediainfo n'est pas installé. "
            "Installez-le avec: sudo apt install mediainfo"
        )

    # Lancé depuis le dossier du fichier : « Complete name » affiche le nom du
    # fichier seul, sans le chemin du serveur (./ : un nom en - n'est pas une option)
    path = Path(file_path)
    name = path.name if not path.name.startswith('-') else f"./{path.name}"

    result = subprocess.run(
        ['mediainfo', name],
        capture_output=True,
        text=True,
        cwd=str(path.parent)
    )

    if result.returncode != 0:
        raise RuntimeError(f"Erreur mediainfo: {result.stderr}")

    return result.stdout


def find_largest_video_file(directory: str) -> Optional[str]:
    """
    Trouve le plus gros fichier vidéo dans un dossier

    Args:
        directory: Chemin du dossier

    Returns:
        Chemin du plus gros fichier vidéo ou None
    """
    video_extensions = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov', '.wmv'}
    path = Path(directory)

    largest_file = None
    largest_size = 0

    for file in path.rglob('*'):
        if file.is_file() and file.suffix.lower() in video_extensions:
            size = file.stat().st_size
            if size > largest_size:
                largest_size = size
                largest_file = str(file)

    return largest_file


def find_main_video_file(content_path: str) -> str:
    """
    Trouve le fichier vidéo principal d'un contenu

    Args:
        content_path: Chemin vers le fichier ou dossier du contenu

    Returns:
        Le fichier lui-même, ou le plus gros fichier vidéo du dossier
    """
    path = Path(content_path)

    if path.is_file():
        return str(path)

    if path.is_dir():
        largest_file = find_largest_video_file(str(path))
        if not largest_file:
            raise ValueError(f"Aucun fichier vidéo trouvé dans: {content_path}")
        return largest_file

    raise ValueError(f"Chemin invalide: {content_path}")


def find_all_video_files(directory: str) -> list:
    """
    Trouve tous les fichiers vidéo dans un dossier

    Args:
        directory: Chemin du dossier

    Returns:
        Liste des chemins de fichiers vidéo
    """
    video_extensions = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov', '.wmv'}
    path = Path(directory)

    video_files = []

    for file in path.rglob('*'):
        if file.is_file() and file.suffix.lower() in video_extensions:
            video_files.append(str(file))

    # Trier par nom
    video_files.sort()

    return video_files


def generate_nfo(
    content_path: str,
    signature: str = "",
    is_series: bool = False
) -> str:
    """
    Génère un NFO pour un contenu (film ou série)

    Args:
        content_path: Chemin vers le fichier ou dossier du contenu
        signature: Signature à ajouter à la fin du NFO
        is_series: Si True, génère le NFO pour chaque fichier (série)

    Returns:
        Contenu du NFO
    """
    path = Path(content_path)

    if path.is_file():
        # C'est un fichier unique
        nfo_content = get_mediainfo(str(path))
    elif path.is_dir():
        if is_series:
            # Série: mediainfo sur tous les fichiers
            video_files = find_all_video_files(str(path))
            if not video_files:
                raise ValueError(f"Aucun fichier vidéo trouvé dans: {content_path}")

            nfo_parts = []
            for video_file in video_files:
                file_name = Path(video_file).name
                nfo_parts.append(f"=== {file_name} ===")
                nfo_parts.append(get_mediainfo(video_file))
                nfo_parts.append("")

            nfo_content = "\n".join(nfo_parts)
        else:
            # Film: mediainfo sur le plus gros fichier
            largest_file = find_largest_video_file(str(path))
            if not largest_file:
                raise ValueError(f"Aucun fichier vidéo trouvé dans: {content_path}")

            nfo_content = get_mediainfo(largest_file)
    else:
        raise ValueError(f"Chemin invalide: {content_path}")

    # Ajouter la signature si présente
    if signature:
        nfo_content = f"{nfo_content}\n\n{signature}"

    return nfo_content
