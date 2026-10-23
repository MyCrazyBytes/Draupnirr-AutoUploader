#!/usr/bin/env python3
"""
Draupnirr Uploader - Outil d'upload de torrents vers le tracker Draupnirr

Usage:
    python -m src.main [--config CONFIG_PATH]
"""

import os
import sys
import json
import argparse
from typing import Dict, List, Optional, Tuple

import requests

try:
    from rich.console import Console
    from rich.markup import escape
    from rich.prompt import Confirm
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from . import __version__
from .config import load_config, Config
from .qui_client import TorrentInfo
from .torrent_processor import TorrentProcessor, PreparedTorrent, WorkInfo, RELEASE_FIELDS
from .tracker_client import Analysis, TrackerApiError, WorkSlots, prefer_ipv4, tier_sort_key
from .utils import format_size, truncate_string, sanitize_filename
from .torrent_namer import extract_group_from_name


# Libellés des facettes demandées à l'utilisateur
FACET_LABELS = {
    'group': 'Team',
    'source': 'Source (Bluray, WEB.DL, HDTV...)',
    'edition': 'Édition (EXTENDED, IMAX...)',
    'work_title': "Titre de l'œuvre",
    'year': 'Année',
    'episode': 'Saison/épisode (S01, S01E01)',
    'episode_title': "Titre de l'épisode",
}

# Nombre maximum d'analyses par release (30 analyses/min côté Draupnirr)
MAX_ANALYZE_ROUNDS = 5


def occupant_name(value) -> str:
    """Nom d'une release renvoyée par l'API (objet ou texte)"""
    if isinstance(value, dict):
        return value.get('name') or value.get('id') or ''
    return str(value) if value else ''


class CLI:

    def __init__(self, config: Config, dump_analyze: bool = False):
        self.config = config
        self.dump_analyze = dump_analyze  # Sauvegarde les réponses de l'assistant (diagnostic)
        self.processor = TorrentProcessor(config)
        self.qui_client = self.processor.qui_client
        self.tracker_client = self.processor.tracker_client
        self.uploaded_names = set()  # Track uploaded torrents during session
        self.works: Dict[str, WorkInfo] = {}  # Œuvres identifiées, par hash de torrent

        if RICH_AVAILABLE:
            self.console = Console()
        else:
            self.console = None

    def get_normalized_key(self, name: str) -> str:
        """Normalise un nom pour comparaison (évite doublons cross-seed)"""
        key = name.lower()
        # Supprimer extensions
        for ext in ['.torrent', '.mkv', '.avi', '.mp4']:
            if key.endswith(ext):
                key = key[:-len(ext)]
        # Espaces → points
        key = key.replace(' ', '.')
        while '..' in key:
            key = key.replace('..', '.')
        return key.strip('.')

    def print_header(self):
        header = f"""
╔═══════════════════════════════════════════════════════════════╗
║                 DRAUPNIRR UPLOADER v{__version__}                     ║
║           Outil d'upload de torrents vers Draupnirr           ║
╚═══════════════════════════════════════════════════════════════╝
        """
        print(header)

    def print_torrent_info(self, torrent: TorrentInfo, index: int):
        name_display = truncate_string(torrent.name, 70)
        size_display = format_size(torrent.size)
        team = extract_group_from_name(torrent.name)

        if RICH_AVAILABLE:
            self.console.print(f"\n[bold cyan]═══ Torrent #{index + 1} ═══[/bold cyan]")
            self.console.print(f"[bold]Nom:[/bold] {name_display}")
            self.console.print(f"[bold]Taille:[/bold] {size_display}")
            if team:
                self.console.print(f"[bold]Team:[/bold] [green]{team}[/green]")
            else:
                self.console.print(f"[bold]Team:[/bold] [red]Non détectée[/red]")
            self.console.print(f"[dim]Hash: {torrent.hash[:16]}...[/dim]")
        else:
            print(f"\n═══ Torrent #{index + 1} ═══")
            print(f"Nom: {name_display}")
            print(f"Taille: {size_display}")
            if team:
                print(f"Team: {team}")
            else:
                print(f"Team: Non détectée")
            print(f"Hash: {torrent.hash[:16]}...")

    def ask_yes_no(self, question: str, default: bool) -> bool:
        if RICH_AVAILABLE:
            return Confirm.ask(f"[yellow]{question}[/yellow]", default=default)
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            response = input(f"{question} {hint}: ").strip().lower()
            if response == '':
                return default
            if response in ('n', 'no', 'non'):
                return False
            if response in ('y', 'yes', 'oui', 'o'):
                return True
            print("Répondez par y (oui) ou n (non)")

    def ask_upload(self, torrent: TorrentInfo) -> bool:
        return self.ask_yes_no("Voulez-vous uploader vers Draupnirr ?", default=False)

    def ask_input(self, question: str) -> str:
        if RICH_AVAILABLE:
            return self.console.input(f"[yellow]{question}[/yellow]").strip()
        return input(question).strip()

    def ask_continue_page(self) -> bool:
        if self.config.options.auto_next_page:
            self.print_info("Page suivante (auto_next_page activé)")
            return True
        if RICH_AVAILABLE:
            return Confirm.ask("\n[cyan]Voir la page suivante ?[/cyan]", default=True)
        else:
            response = input("\nVoir la page suivante ? [Y/n]: ").strip().lower()
            return response in ('', 'y', 'yes', 'oui', 'o')

    def print_processing(self, message: str):
        if RICH_AVAILABLE:
            self.console.print(f"[blue]⏳ {message}[/blue]")
        else:
            print(f"⏳ {message}")

    def print_success(self, message: str):
        if RICH_AVAILABLE:
            self.console.print(f"[green]✓ {message}[/green]")
        else:
            print(f"✓ {message}")

    def print_error(self, message: str):
        if RICH_AVAILABLE:
            self.console.print(f"[red]✗ {message}[/red]")
        else:
            print(f"✗ ERREUR: {message}")

    def print_warning(self, message: str):
        if RICH_AVAILABLE:
            self.console.print(f"[yellow]⚠ {message}[/yellow]")
        else:
            print(f"⚠ {message}")

    def print_info(self, message: str):
        if RICH_AVAILABLE:
            self.console.print(f"[dim]{message}[/dim]")
        else:
            print(f"  {message}")

    def connect_tracker(self) -> bool:
        """Vérifie la clé API Draupnirr et les slugs de catégories configurés"""
        self.print_processing(f"Connexion à Draupnirr ({self.config.tracker.url})...")
        try:
            me = self.tracker_client.get_me()
        except TrackerApiError as e:
            self.print_error(f"Connexion à Draupnirr refusée : {e}")
            return False
        except requests.Timeout as e:
            self.print_error(f"Draupnirr ne répond pas : {e}")
            self.print_info(
                "Vérifiez tracker.url (adresse du site, pas celle de l'announce), "
                "le pare-feu de ce serveur et son accès IPv4/IPv6 (curl -4 / curl -6)"
            )
            return False
        except requests.RequestException as e:
            self.print_error(f"Draupnirr injoignable : {e}")
            return False

        self.processor.source_tag = me.get('source_tag') or self.processor.source_tag
        ratio = me.get('ratio')
        self.print_success(
            f"Connecté : {me.get('name', '?')} "
            f"(ratio: {ratio if ratio is not None else '∞'}, tag source: {self.processor.source_tag})"
        )

        if not self.processor.tmdb_client:
            self.print_info("Pas de clé TMDB : catégories film/série uniquement (pas de détection documentaire/animation/concert)")

        try:
            categories = self.tracker_client.get_categories()
        except (TrackerApiError, requests.RequestException) as e:
            self.print_warning(f"Catégories non vérifiées ({e})")
            return True

        valid_slugs = {c.get('slug') for c in categories}
        invalid = {key: slug for key, slug in vars(self.config.categories).items() if slug not in valid_slugs}
        if invalid:
            for key, slug in invalid.items():
                self.print_error(f"categories.{key} : slug « {slug} » inconnu sur Draupnirr")
            self.print_info("Slugs valides (GET /api/categories) :")
            for c in categories:
                self.print_info(f"  {c.get('slug')} — {c.get('name')}")
            return False

        return True

    def connect_qui(self) -> bool:
        """Vérifie que l'instance QUI configurée (qui.instance_id) existe et est active"""
        instance_id = self.config.qui.instance_id
        self.print_processing(f"Connexion à QUI ({self.config.qui.api_url})...")
        try:
            instances = self.qui_client.get_instances()
        except requests.RequestException as e:
            self.print_error(f"QUI injoignable : {e}")
            return False

        instance = next((i for i in instances if str(i.get('id')) == str(instance_id)), None)
        if instance is None:
            self.print_error(f"qui.instance_id : instance {instance_id} inconnue dans QUI")
            self.print_info("Instances disponibles :")
            for i in instances:
                self.print_info(f"  {i.get('id')} — {i.get('name')} ({i.get('host')})")
            if not instances:
                self.print_info("  (aucune)")
            return False

        description = f"instance {instance_id} — {instance.get('name')} ({instance.get('host')})"
        if instance.get('isActive') is False:
            self.print_error(f"QUI : {description} désactivée")
            return False

        if instance.get('connected'):
            self.print_success(f"QUI connecté : {description}")
        else:
            self.print_warning(f"QUI ne signale pas qBittorrent comme connecté : {description}")
            if instance.get('hasDecryptionError'):
                self.print_info("  QUI n'arrive pas à déchiffrer le mot de passe qBittorrent : ressaisissez-le dans QUI")
            for error in (instance.get('recentErrors') or [])[:3]:
                self.print_info(f"  {error.get('errorMessage', error)}")

        return True

    def has_draupnirr_tracker(self, torrent: TorrentInfo) -> bool:
        """Vérifie si un torrent a déjà un tracker Draupnirr"""
        tracker_urls = self.config.tracker.tracker_urls
        if not tracker_urls:
            return False

        try:
            torrent_trackers = self.qui_client.get_torrent_trackers(torrent.hash)
            for tracker in torrent_trackers:
                for draupnirr_url in tracker_urls:
                    if draupnirr_url in tracker:
                        return True
        except Exception:
            # En cas d'erreur, on considère qu'il n'a pas le tracker
            pass
        return False

    def is_specific_content_path(self, content_path: str) -> bool:
        """
        Vérifie si le content_path pointe vers un contenu spécifique (fichier ou dossier de release)
        et non vers un répertoire de téléchargement commun.
        """
        media_extensions = {'.mkv', '.avi', '.mp4', '.m4v', '.ts', '.iso'}
        if any(content_path.lower().endswith(ext) for ext in media_extensions):
            return True

        common_dirs = {'downloads', 'download', 'complete', 'completed', 'torrents',
                       'data', 'media', 'tmp', 'temp', 'incoming'}

        basename = os.path.basename(content_path.rstrip('/'))
        if basename.lower() in common_dirs:
            return False

        release_markers = ['1080p', '720p', '2160p', '4k', 'x264', 'x265', 'bluray',
                          'webrip', 'webdl', 'web-dl', 'hdtv', 'remux']
        basename_lower = basename.lower()
        if any(marker in basename_lower for marker in release_markers):
            return True

        if len(basename) < 10:
            return False

        return True

    def filter_and_group_torrents(self, torrents: list) -> dict:
        """
        Filtre les torrents qui ont déjà Draupnirr (ou incomplets) et groupe par content_path (cross-seed)

        Returns:
            Dict avec content_path comme clé et liste de TorrentInfo comme valeur
        """
        groups = {}
        for torrent in torrents:
            if self.is_specific_content_path(torrent.content_path):
                key = torrent.content_path
            else:
                key = torrent.hash

            if key not in groups:
                groups[key] = []
            groups[key].append(torrent)

        filtered_groups = {}
        filtered_count = 0
        incomplete_count = 0

        for content_path, group_torrents in groups.items():
            has_draupnirr = False
            for torrent in group_torrents:
                if self.has_draupnirr_tracker(torrent):
                    has_draupnirr = True
                    break

            if has_draupnirr:
                self.print_info(f"Filtré (déjà sur Draupnirr): {truncate_string(group_torrents[0].name, 50)} ({len(group_torrents)} torrent(s))")
                filtered_count += len(group_torrents)
            elif not any(t.progress >= 1 for t in group_torrents):
                # Ratatosk vérifie le média depuis notre client : il faut tout avoir
                self.print_info(f"Filtré (téléchargement incomplet): {truncate_string(group_torrents[0].name, 50)}")
                incomplete_count += len(group_torrents)
            else:
                filtered_groups[content_path] = group_torrents

        if filtered_count > 0:
            self.print_warning(f"{filtered_count} torrent(s) filtré(s) (déjà sur Draupnirr)")
        if incomplete_count > 0:
            self.print_warning(f"{incomplete_count} torrent(s) filtré(s) (téléchargement incomplet)")

        return filtered_groups

    def filter_by_catalog_size(self, groups: dict) -> dict:
        """
        Filtre les groupes dont la taille exacte existe déjà au catalogue Draupnirr
        (GET /api/torrents/match : même release sous un autre infohash)
        """
        sizes = [t.size for group_torrents in groups.values() for t in group_torrents]
        if not sizes:
            return groups

        try:
            matches = self.tracker_client.match_sizes(sizes)
        except (TrackerApiError, requests.RequestException) as e:
            self.print_warning(f"Recherche par taille sur Draupnirr impossible ({e})")
            return groups

        by_size = {}
        for match in matches:
            by_size.setdefault(match.get('size_bytes'), match)

        filtered_groups = {}
        filtered_count = 0

        for content_path, group_torrents in groups.items():
            match = next((by_size[t.size] for t in group_torrents if t.size in by_size), None)
            if match:
                self.print_info(
                    f"Filtré (même taille sur Draupnirr: {truncate_string(match.get('name', '?'), 50)}): "
                    f"{truncate_string(group_torrents[0].name, 50)}"
                )
                filtered_count += len(group_torrents)
            else:
                filtered_groups[content_path] = group_torrents

        if filtered_count > 0:
            self.print_warning(f"{filtered_count} torrent(s) filtré(s) (même taille déjà au catalogue Draupnirr)")

        return filtered_groups

    def find_on_draupnirr(self, torrent: TorrentInfo) -> Tuple[Optional[PreparedTorrent], Optional[dict]]:
        """
        Re-scelle le torrent et cherche son infohash Draupnirr au catalogue
        (GET /api/torrents/{infohash})

        Returns:
            (torrent re-scellé, fiche Draupnirr ou None si absent)
        """
        prepared = self.processor.prepare_torrent(torrent.hash)
        return prepared, self.tracker_client.get_torrent(prepared.meta.infohash)

    def print_torrent_group(self, torrents: list, group_idx: int):
        if len(torrents) == 1:
            self.print_torrent_info(torrents[0], group_idx)
        else:
            if RICH_AVAILABLE:
                self.console.print(f"\n[bold magenta]═══ Groupe #{group_idx + 1} ({len(torrents)} torrents cross-seed) ═══[/bold magenta]")
            else:
                print(f"\n═══ Groupe #{group_idx + 1} ({len(torrents)} torrents cross-seed) ═══")

            for i, torrent in enumerate(torrents):
                name_display = truncate_string(torrent.name, 70)
                size_display = format_size(torrent.size)
                team = extract_group_from_name(torrent.name)

                if RICH_AVAILABLE:
                    self.console.print(f"  [cyan]#{i + 1}[/cyan] {name_display}")
                    if team:
                        team_display = f"[green]{team}[/green]"
                    else:
                        team_display = "[red]Non détectée[/red]"
                    self.console.print(f"      [dim]Taille: {size_display} | Team: {team_display} | Hash: {torrent.hash[:16]}...[/dim]")
                else:
                    print(f"  #{i + 1} {name_display}")
                    team_display = team if team else "Non détectée"
                    print(f"      Taille: {size_display} | Team: {team_display} | Hash: {torrent.hash[:16]}...")

    def ask_facet(self, key: str, vocabulary: List[str]) -> str:
        """Demande la valeur d'une facette manquante"""
        if key == 'group':
            if RICH_AVAILABLE:
                self.console.print("[bold red]⚠ Pas de team détectée[/bold red]")
            else:
                print("⚠ Pas de team détectée")
            return self.ask_input("Entrez le nom de la team (ou vide pour annuler): ")

        if vocabulary:
            self.print_info(f"  Valeurs admises: {', '.join(vocabulary[:30])}")
        return self.ask_input(f"Facette manquante « {FACET_LABELS.get(key, key)} » (vide = ignorer): ")

    def run_assistant(
        self,
        prepared: PreparedTorrent,
        category: str,
        mediainfo: str,
        tmdb_result,
        values: Dict[str, str]
    ) -> Analysis:
        """
        Assistant d'upload Draupnirr : analyse, reprend les facettes déclarées par
        le nom (source, édition, team), puis demande celles qui manquent.

        `values` est complété en place avec ce qui sera envoyé à l'upload.
        """
        def analyze() -> Analysis:
            fields = self.processor.build_analyze_fields(category, mediainfo, tmdb_result, values)
            analysis = self.tracker_client.analyze(prepared.content, fields)
            if self.dump_analyze:
                self.save_analysis(prepared.meta.name, analysis)
            return analysis

        self.print_processing("Analyse par l'assistant Draupnirr...")
        analysis = analyze()
        asked = set()

        for _ in range(MAX_ANALYZE_ROUNDS):
            if analysis.duplicate:
                break

            # Facettes déclarées par le nom : indevinables par la sonde, à envoyer telles quelles
            new_values = {k: v for k, v in analysis.name_facets().items() if not values.get(k)}

            if not new_values:
                to_ask = [m.strip().lower().replace(' ', '_') for m in analysis.missing]
                if not (values.get('group') or analysis.facet('group')):
                    to_ask.append('group')
                for key in dict.fromkeys(to_ask):
                    if key in asked:
                        continue
                    asked.add(key)
                    value = self.ask_facet(key, analysis.vocabulary(key))
                    if value:
                        new_values[key] = value

            if not new_values:
                break

            values.update(new_values)
            self.print_processing("Nouvelle analyse avec les facettes complétées...")
            analysis = analyze()

        return analysis

    def print_analysis(self, analysis: Analysis, values: Dict[str, str]):
        """Affiche la fiche calculée par l'assistant Draupnirr"""
        if analysis.built_name:
            self.print_success(f"Nom Draupnirr: {analysis.built_name}")
        else:
            self.print_warning(f"Nom canonique non calculé : la release gardera le nom du .torrent ({analysis.name})")

        facets = analysis.facets()
        for key, value in values.items():
            if key not in RELEASE_FIELDS:
                facets.setdefault(key, value)
        if facets:
            self.print_info("  Facettes: " + ", ".join(f"{k}={v}" for k, v in sorted(facets.items())))

        self.print_release_slot(analysis)

        for warning in analysis.warnings:
            self.print_warning(warning)

        for issue in analysis.bot_issues:
            message = f"🐿 Ratatosk ({issue.get('level', '?')}): {issue.get('message', issue.get('code', ''))}"
            if issue.get('level') == 'info':
                self.print_info(f"  {message}")
            else:
                self.print_warning(message)

        if analysis.missing:
            self.print_warning(f"Facettes manquantes : {', '.join(analysis.missing)}")

    def save_analysis(self, name: str, analysis: Analysis):
        """--dump-analyze : sauvegarde la réponse brute de l'assistant (la dernière analyse gagne)"""
        directory = os.path.join(self.config.options.temp_dir, "analyze")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{sanitize_filename(name)}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(analysis.data, f, ensure_ascii=False, indent=2)
        self.print_info(f"  Réponse de l'assistant sauvegardée: {path}")

    def identify_work(self, torrent: TorrentInfo) -> WorkInfo:
        """Œuvre de la release (guessit + TMDB), identifiée une seule fois par torrent"""
        if torrent.hash not in self.works:
            self.works[torrent.hash] = self.processor.identify_work(torrent.name)
        return self.works[torrent.hash]

    def find_work_slots(self, torrent: TorrentInfo) -> Tuple[Optional[WorkInfo], Optional[WorkSlots]]:
        """
        Identifie l'œuvre et récupère ses slots Draupnirr (affichage, filtre only_missing).
        En cas d'échec, le torrent reste proposé, sans carte des slots.
        """
        try:
            work = self.identify_work(torrent)
        except (TrackerApiError, requests.RequestException) as e:
            self.print_warning(f"Œuvre non identifiée ({e})")
            return None, None

        if not work.tmdb_result:
            return work, None

        try:
            return work, self.tracker_client.get_work_slots(work.tmdb_result.media_type, work.tmdb_result.tmdb_id)
        except (TrackerApiError, requests.RequestException) as e:
            self.print_warning(f"Slots Draupnirr indisponibles ({e})")
            return work, None

    def colored(self, text: str, color: str) -> str:
        """Texte coloré avec rich (échappé : les noms de release contiennent des [ ]), brut sinon"""
        if RICH_AVAILABLE:
            return f"[{color}]{escape(text)}[/{color}]"
        return text

    def print_line(self, line: str):
        if RICH_AVAILABLE:
            self.console.print(line)
        else:
            print(line)

    def print_work_slots(self, work: WorkInfo, slots: Optional[WorkSlots]):
        """Slots Draupnirr de l'œuvre : occupés en rouge (avec la release), libres en vert"""
        tmdb = work.tmdb_result
        year = f" ({tmdb.year})" if tmdb.year else ""

        if slots is None:
            self.print_info(f"Draupnirr — {tmdb.title}{year} : slots indisponibles")
            return

        if work.episode:
            self.print_season_slots(work, slots)
            return

        editions = slots.editions()
        all_slots = [slot for edition_slots in editions.values() for slot in edition_slots]
        header = f"Draupnirr — {tmdb.title}{year}"

        if not any(slots.is_taken(slot) for slot in all_slots):
            self.print_line(self.colored(f"○ {header} : aucune release, {len(all_slots)} slot(s) libre(s)", 'green'))
            return

        self.print_line(self.colored(header, 'bold'))
        for edition, edition_slots in editions.items():
            indent = "  "
            if len(editions) > 1:
                self.print_line(f"  {self.colored(edition, 'bold')}")
                indent = "    "

            taken = sorted((s for s in edition_slots if slots.is_taken(s)), key=lambda s: tier_sort_key(s.get('tier', '')))
            for slot in taken:
                occupant = slot.get('occupant') or {}
                seeders = occupant.get('seeders')
                detail = occupant.get('name', '?') + (f" ({seeders} seeders)" if seeders is not None else "")
                self.print_line(indent + self.colored(f"● {slot.get('tier', '?'):<12} {slot.get('code', '?')} : {detail}", 'red'))

            free = {}
            for slot in edition_slots:
                if not slots.is_taken(slot):
                    free[slot.get('tier', '?')] = free.get(slot.get('tier', '?'), 0) + 1
            if free:
                summary = " · ".join(f"{tier} ×{count}" for tier, count in sorted(free.items(), key=lambda i: tier_sort_key(i[0])))
                self.print_line(indent + self.colored(f"○ Libres : {summary}", 'green'))

    def print_season_slots(self, work: WorkInfo, slots: WorkSlots):
        """Séries : paliers de la saison occupés (pack ou épisodes) en rouge, libres en vert"""
        number = WorkSlots.season_number(work.episode)
        season = slots.season(number)
        header = f"Draupnirr — {work.tmdb_result.title}, saison {number}"

        if not season:
            self.print_line(self.colored(f"○ {header} : absente de Draupnirr", 'green'))
            return

        if season.get('episodes_total_known') and season.get('episodes_total'):
            header += f" ({season['episodes_total']} épisodes)"
        self.print_line(self.colored(header, 'bold'))

        coverage = slots.season_coverage(number)
        tiers = season.get('tiers') if isinstance(season.get('tiers'), dict) else {}
        taken = [f"{tier} ({coverage[tier]})" for tier in sorted(coverage, key=tier_sort_key)]
        free = [tier for tier in sorted(tiers, key=tier_sort_key) if tier not in coverage]

        if taken:
            self.print_line("  " + self.colored(f"● Occupés : {' · '.join(taken)}", 'red'))
        if free:
            self.print_line("  " + self.colored(f"○ Libres : {' · '.join(free)}", 'green'))

    def preview_slot(self, torrent: TorrentInfo, prepared: PreparedTorrent, work: WorkInfo) -> Optional[Analysis]:
        """
        Mode slot : analyse Draupnirr avant la question, pour connaître le slot que la
        release viserait. En cas d'échec, le torrent reste proposé.
        """
        tmdb_result = work.tmdb_result
        try:
            self.processor.apply_tmdb_genres(tmdb_result)
        except requests.RequestException:
            pass  # catégorie film/série par défaut, comme à l'upload
        category = self.processor.get_category(
            work.is_series,
            is_anime=tmdb_result.is_anime,
            is_documentary=tmdb_result.is_documentary,
            is_music=tmdb_result.is_music
        )

        self.print_processing(f"Slot Draupnirr de {truncate_string(torrent.name, 50)}...")
        try:
            mediainfo = self.processor.get_mediainfo_report(torrent.content_path)
            values = self.processor.build_release_values(tmdb_result, work.episode)
            fields = self.processor.build_analyze_fields(category, mediainfo, tmdb_result, values)
            analysis = self.tracker_client.analyze(prepared.content, fields)
        except (TrackerApiError, requests.RequestException, RuntimeError, ValueError) as e:
            self.print_warning(f"Slot non vérifié ({e})")
            return None

        if self.dump_analyze:
            self.save_analysis(prepared.meta.name, analysis)
        return analysis

    def print_release_slot(self, analysis: Analysis):
        """Slot visé par la release : vert si elle l'occuperait, rouge s'il est déjà tenu"""
        slot = analysis.slot
        if not slot:
            return

        title = f"Slot {slot.get('code', '?')}"
        if slot.get('label'):
            title += f" ({slot['label']})"
        occupant = occupant_name(slot.get('occupant'))
        reason = f" — {slot['reason']}" if slot.get('reason') else ""

        if analysis.slot_taken:
            rank = f", rang {slot['rank']}" if slot.get('rank') else ""
            self.print_line(self.colored(
                f"● {title} : déjà tenu par {occupant} (votre release : {slot.get('state', '?')}{rank})", 'red'))
        elif occupant:
            self.print_line(self.colored(f"○ {title} : votre release prendrait la place de {occupant}{reason}", 'green'))
        else:
            self.print_line(self.colored(f"○ {title} : libre, votre release l'occuperait{reason}", 'green'))

    def process_and_upload(self, torrent: TorrentInfo, prepared: PreparedTorrent) -> str:
        """
        Returns:
            'uploaded', 'present' (déjà sur Draupnirr), 'cancelled' ou 'error'
        """
        try:
            # guessit + TMDB (déjà faits pour l'affichage des slots, sinon maintenant)
            self.print_processing("Identification de l'œuvre (guessit + TMDB via Draupnirr)...")
            work = self.identify_work(torrent)
            guessit_info, is_series, episode = work.guessit_info, work.is_series, work.episode
            title = guessit_info.get('title', 'Inconnu')
            self.print_info(f"  Titre détecté: {title}")
            self.print_info(f"  Type: {'Série' if is_series else 'Film'}")

            if is_series:
                if not episode:
                    self.print_error("Saison absente du nom (S01, S01E03) : Draupnirr la refuserait")
                    return 'cancelled'
                self.print_info(f"  Saison/épisode: {episode}")

            tmdb_result = work.tmdb_result
            if not tmdb_result:
                self.print_error(f"Média non trouvé sur TMDB pour: {title}")
                return 'error'

            year_display = f" ({tmdb_result.year})" if tmdb_result.year else ""
            self.print_success(f"TMDB: {tmdb_result.title}{year_display} (ID: {tmdb_result.tmdb_id})")
            release_year = guessit_info.get('year')
            if release_year and tmdb_result.year and abs(tmdb_result.year - release_year) > 1:
                self.print_warning(f"Année TMDB ({tmdb_result.year}) différente de la release ({release_year}) : vérifiez l'œuvre")

            try:
                self.processor.apply_tmdb_genres(tmdb_result)
            except requests.RequestException as e:
                self.print_warning(f"Genres TMDB non récupérés ({e}) : catégorie film/série par défaut")

            # type détecté
            if tmdb_result.is_documentary:
                self.print_info("  Détecté comme: Documentaire")
            elif tmdb_result.is_music:
                self.print_info("  Détecté comme: Concert/Musique")
            elif tmdb_result.is_anime:
                self.print_info("  Détecté comme: Animation")

            category = self.processor.get_category(
                is_series,
                is_anime=tmdb_result.is_anime,
                is_documentary=tmdb_result.is_documentary,
                is_music=tmdb_result.is_music
            )
            self.print_info(f"  Catégorie: {category}")

            # NFO + rapport MediaInfo du fichier principal
            self.print_processing("Génération du NFO avec mediainfo...")
            nfo = self.processor.generate_nfo_for_torrent(
                content_path=torrent.content_path,
                is_series=is_series
            )
            mediainfo = self.processor.get_mediainfo_report(torrent.content_path)
            self.print_success("NFO généré")

            # Assistant Draupnirr
            values = self.processor.build_release_values(tmdb_result, episode)
            analysis = self.run_assistant(prepared, category, mediainfo, tmdb_result, values)

            if analysis.duplicate:
                self.print_warning("Doublon détecté par Draupnirr : release déjà présente")
                return 'present'

            self.print_analysis(analysis, values)

            if not (values.get('group') or analysis.facet('group')):
                self.print_warning("Upload annulé (pas de team)")
                return 'cancelled'

            blocking = analysis.blocking_reasons
            if blocking:
                for reason in blocking:
                    self.print_error(reason)
                self.print_warning("Upload annulé (refusé par Draupnirr)")
                return 'cancelled'

            has_warnings = bool(
                analysis.warnings or analysis.missing
                or any(i.get('level') == 'warn' for i in analysis.bot_issues)
            )
            if has_warnings:
                self.print_warning("Avec ces avertissements, la release partira en modération humaine (pending)")

            release_name = analysis.built_name or prepared.meta.name
            confirm_default = not has_warnings and not analysis.slot_taken
            if not self.ask_yes_no(f"Publier « {release_name} » sur Draupnirr ?", default=confirm_default):
                self.print_info("Upload annulé")
                return 'cancelled'

            # Upload
            description = self.processor.build_description(
                tmdb_result=tmdb_result,
                analysis=analysis,
                values=values,
                torrent_size=torrent.size,
                content_path=torrent.content_path
            )
            fields = self.processor.build_upload_fields(
                category=category,
                mediainfo=mediainfo,
                description=description,
                tmdb_result=tmdb_result,
                values=values,
                tags=analysis.suggested_tags
            )

            self.print_processing("Upload vers Draupnirr...")
            try:
                result = self.tracker_client.upload_torrent(
                    torrent_content=prepared.content,
                    torrent_filename=f"{prepared.meta.name}.torrent",
                    fields=fields,
                    nfo=nfo
                )
            except TrackerApiError as e:
                if e.is_duplicate:
                    self.print_warning(f"Déjà présent sur Draupnirr : {e.message}")
                    return 'present'
                raise

            # Tracker le nom pour éviter doublons cross-seed
            self.uploaded_names.add(self.get_normalized_key(torrent.name))

            torrent_id = result['id']
            self.print_success(f"Upload réussi ! {self.tracker_client.torrent_page_url(torrent_id)}")
            if result.get('status') == 'pending':
                self.print_warning("Release en modération humaine (status: pending)")
            if result.get('awaiting_validation'):
                self.print_warning("Quarantaine Ratatosk : vérification du média en cours, la release doit rester en seed")
            if result.get('infohash') and result['infohash'].lower() != prepared.meta.infohash:
                self.print_warning(f"Infohash Draupnirr différent de l'infohash local ({result['infohash']})")

            self.seed_uploaded_torrent(torrent, torrent_id, release_name)
            return 'uploaded'

        except Exception as e:
            self.print_error(f"Erreur lors du traitement: {str(e)}")
            return 'error'

    def seed_uploaded_torrent(self, torrent: TorrentInfo, torrent_id: str, release_name: str):
        """Télécharge le .torrent personnalisé et l'ajoute à QUI sur les mêmes données"""
        try:
            self.print_processing("Téléchargement du torrent depuis Draupnirr...")
            tracker_torrent_content = self.tracker_client.download_torrent(torrent_id)
            self.print_success("Torrent téléchargé depuis Draupnirr")

            tmp_torrent_path = self.processor.save_torrent_file(
                tracker_torrent_content, release_name, "torrents_draupnirr"
            )
            self.print_info(f"Torrent sauvegardé: {tmp_torrent_path}")

            self.print_processing("Envoi du torrent au client QUI...")
            # Cross-Seed : chemin vu par le client torrent, pas celui lu par le script
            save_path = os.path.dirname(torrent.client_content_path)
            self.print_info(f"  Save path: {save_path}")
            self.qui_client.add_torrent(
                torrent_content=tracker_torrent_content,
                torrent_filename=os.path.basename(tmp_torrent_path),
                save_path=save_path,
                tags=self.config.qui.upload_tag
            )
            self.print_success("Torrent ajouté au client, en seed pour la vérification Ratatosk !")
        except Exception as e:
            self.print_error(f"Release uploadée mais pas remise en seed : {e}")
            self.print_warning(
                f"Ajoutez-la à la main depuis {self.tracker_client.torrent_page_url(torrent_id)} : "
                "sans seeder, Ratatosk ne peut pas vérifier le média (relance à 6 h, puis strike H&R)"
            )

    def run(self) -> bool:
        self.print_header()

        if not self.connect_tracker():
            return False

        if not self.connect_qui():
            return False

        print(f"\nRécupération des torrents depuis QUI (triés par taille, décroissant)...")
        print(f"Page size: {self.config.options.page_size} torrents")
        if self.config.tracker.tracker_urls:
            print(f"Filtrage des torrents déjà sur Draupnirr: {self.config.tracker.tracker_urls}\n")
        else:
            print()

        uploaded_count = 0
        present_count = 0
        skipped_count = 0
        error_count = 0
        total_groups = 0

        try:
            for torrents, page_num, total in self.qui_client.iter_torrents(
                page_size=self.config.options.page_size
            ):
                if RICH_AVAILABLE:
                    self.console.print(f"\n[bold]═══ Page {page_num + 1} ({len(torrents)} torrents sur {total} total) ═══[/bold]")
                else:
                    print(f"\n═══ Page {page_num + 1} ({len(torrents)} torrents sur {total} total) ═══")

                self.print_processing("Analyse des trackers et regroupement...")
                groups = self.filter_and_group_torrents(torrents)

                if groups:
                    self.print_processing("Recherche des tailles identiques au catalogue Draupnirr...")
                    groups = self.filter_by_catalog_size(groups)

                if not groups:
                    self.print_info("Aucun torrent à traiter sur cette page (tous filtrés)")
                    if (page_num + 1) * self.config.options.page_size < total:
                        if not self.ask_continue_page():
                            break
                    continue

                self.print_success(f"{len(groups)} groupe(s) de torrents à traiter")

                for group_idx, (content_path, group_torrents) in enumerate(groups.items()):
                    # Copie complète du groupe (Ratatosk vérifie depuis notre seed)
                    torrent = next(t for t in group_torrents if t.progress >= 1)

                    normalized_key = self.get_normalized_key(torrent.name)
                    if normalized_key in self.uploaded_names:
                        self.print_info(f"Skipped (déjà uploadé): {truncate_string(torrent.name, 50)}")
                        skipped_count += 1
                        continue

                    if self.config.options.skip_no_team_prompt:
                        team = extract_group_from_name(torrent.name)
                        if not team:
                            self.print_torrent_group(group_torrents, total_groups + group_idx)
                            self.print_warning("Skipped (pas de team détectée)")
                            skipped_count += 1
                            continue

                    # Check Draupnirr par infohash, après le check interne QUI
                    try:
                        prepared, existing = self.find_on_draupnirr(torrent)
                    except Exception as e:
                        self.print_error(f"Vérification Draupnirr impossible pour {truncate_string(torrent.name, 50)}: {e}")
                        error_count += 1
                        continue

                    if existing:
                        self.print_info(
                            f"Filtré (déjà sur Draupnirr, id {existing.get('id', '?')}): "
                            f"{truncate_string(torrent.name, 50)}"
                        )
                        present_count += 1
                        continue

                    # Les fichiers doivent être là où le cross-seed les cherchera
                    problems = self.processor.find_layout_problems(torrent.content_path, prepared.meta)
                    if problems:
                        self.print_warning(f"Ignoré (fichiers introuvables pour le cross-seed): {truncate_string(torrent.name, 50)}")
                        for problem in problems[:3]:
                            self.print_info(f"  {problem}")
                        if len(problems) > 3:
                            self.print_info(f"  ... et {len(problems) - 3} autre(s)")
                        skipped_count += 1
                        continue

                    work, slots = self.find_work_slots(torrent)

                    if self.config.options.only_missing == 'work' and slots and slots.has_release(work.episode):
                        self.print_info(f"Filtré (œuvre déjà sur Draupnirr): {truncate_string(torrent.name, 50)}")
                        present_count += 1
                        continue

                    slot_preview = None
                    if self.config.options.only_missing == 'slot' and work and work.tmdb_result:
                        slot_preview = self.preview_slot(torrent, prepared, work)
                        if slot_preview and slot_preview.slot_taken:
                            slot = slot_preview.slot
                            self.print_info(
                                f"Filtré (slot {slot.get('code', '?')} déjà tenu par "
                                f"{truncate_string(occupant_name(slot.get('occupant')), 50)}): {truncate_string(torrent.name, 50)}"
                            )
                            present_count += 1
                            continue

                    self.print_torrent_group(group_torrents, total_groups + group_idx)
                    if work and work.tmdb_result:
                        self.print_work_slots(work, slots)
                    if slot_preview:
                        self.print_release_slot(slot_preview)

                    if self.ask_upload(torrent):
                        status = self.process_and_upload(torrent, prepared)
                        if status == 'uploaded':
                            uploaded_count += 1
                        elif status == 'present':
                            present_count += 1
                        elif status == 'cancelled':
                            skipped_count += 1
                        else:
                            error_count += 1
                    else:
                        self.print_info("Groupe ignoré")
                        skipped_count += 1

                total_groups += len(groups)

                if (page_num + 1) * self.config.options.page_size < total:
                    if not self.ask_continue_page():
                        break

        except KeyboardInterrupt:
            print("\n\nInterruption par l'utilisateur.")

        print("\n" + "=" * 50)
        print("RÉSUMÉ")
        print("=" * 50)
        print(f"Groupes traités: {total_groups}")
        print(f"Uploadés avec succès: {uploaded_count}")
        print(f"Déjà présents sur Draupnirr: {present_count}")
        print(f"Ignorés: {skipped_count}")
        print(f"Erreurs: {error_count}")

        return True


def main():
    """Point d'entrée principal"""
    parser = argparse.ArgumentParser(
        description="Draupnirr Uploader - Upload de torrents vers le tracker Draupnirr"
    )
    parser.add_argument(
        '--config', '-c',
        type=str,
        default=None,
        help="Chemin vers le fichier de configuration (défaut: config.yaml)"
    )
    parser.add_argument(
        '--dump-analyze',
        action='store_true',
        help="Sauvegarde les réponses de l'assistant Draupnirr en JSON (diagnostic)"
    )
    parser.add_argument(
        '--version', '-v',
        action='store_true',
        help="Affiche la version"
    )

    args = parser.parse_args()

    if args.version:
        print(f"Draupnirr Uploader v{__version__}")
        sys.exit(0)

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"ERREUR: {e}")
        print("Copiez config.example.yaml vers config.yaml et configurez vos API keys.")
        sys.exit(1)
    except Exception as e:
        print(f"ERREUR de configuration: {e}")
        sys.exit(1)

    prefer_ipv4()

    cli = CLI(config, dump_analyze=args.dump_analyze)
    if not cli.run():
        sys.exit(1)


if __name__ == '__main__':
    main()
