"""CLI entry point: play/search/download/playlist/doctor.

Subcommands are wired in ``__main__.py``.
"""

from __future__ import annotations

import os
import sys
import argparse
import traceback

# Tide UI app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tide.ui.app import TideApp

from tide.config import load_config
from tide.paths import get_paths, Paths
from tide.media.search import search as run_search


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="tide",
        description="Tide - terminal music player",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="Tide 0.1.0",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ----------------- play -----------------
    p_play = sub.add_parser("play", help="Play a URL or local file immediately")
    p_play.add_argument("url", help="YouTube URL or local file path")
    p_play.add_argument(
        "--art",
        action="store_true",
        help="Show cover art in visualizer",
    )

    # ----------------- search -----------------
    p_search = sub.add_parser("search", help="Search YouTube")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )

    # ----------------- download -----------------
    p_dl = sub.add_parser("download", help="Download a track/playlist")
    p_dl.add_argument("url", help="YouTube URL or local file path")
    p_dl.add_argument(
        "--art",
        action="store_true",
        help="Embed cover art in download",
    )

    # ----------------- playlist -----------------
    p_pl = sub.add_parser("playlist", help="Playlist management")
    pl_sub = p_pl.add_subparsers(dest="pl_action", required=True)

    pl_sub.add_parser("list", help="List saved playlists")
    pl_ne = pl_sub.add_parser("new", help="Create a new playlist")
    pl_ne.add_argument("name", help="Playlist name")
    pl_ie = pl_sub.add_parser("import", help="Import from .m3u")
    pl_ie.add_argument("path", help="Path to .m3u file")
    pl_ex = pl_sub.add_parser("export", help="Export to .m3u")
    pl_ex.add_argument("name", help="Playlist name to export")
    pl_de = pl_sub.add_parser("delete", help="Delete a playlist")
    pl_de.add_argument("name", help="Playlist name to delete")

    # ----------------- doctor -----------------
    p_doctor = sub.add_parser("doctor", help="Environment checklist")
    p_doctor.add_argument(
        "--check",
        action="store_true",
        help="Run doctor and exit",
    )

    return parser


def cmd_play(args: argparse.Namespace) -> int:
    """Handle the ``play`` subcommand."""
    url = args.url
    print(f"Would play: {url}")
    if args.art:
        print("  (cover art enabled)")
    try:
        paths = get_paths()
        app = TideApp(start_url=url)
        try:
            app.run()
        finally:
            app.shutdown()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Handle the ``search`` subcommand."""
    query = args.query
    config = load_config(paths=get_paths()).config
    try:
        results = run_search(query, count=config.search_count, config=config)
        if args.json:
            import json
            print(json.dumps([{
                "title": r.title,
                "source": r.source,
                "duration": r.duration,
            } for r in results], ensure_ascii=False, indent=2))
        else:
            for i, r in enumerate(results, 1):
                dur = f"({r.duration}s)" if r.duration else ""
                print(f"  {i}. {r.title} {dur} - {r.source[:60]}")
    except Exception as exc:
        print(f"Search failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    """Handle the ``download`` subcommand."""
    url = args.url
    print(f"Would download: {url}")
    if args.art:
        print("  (cover art enabled)")
    return 0


def cmd_playlist(args: argparse.Namespace) -> int:
    """Handle the ``playlist`` subcommand."""
    action = args.pl_action
    print(f"Playlist action: {action}")
    if action == "list":
        print("Listing playlists...")
    elif action == "new":
        print(f"Creating playlist: {args.name}")
    elif action == "import":
        print(f"Importing from: {args.path}")
    elif action == "export":
        print(f"Exporting playlist: {args.name}")
    elif action == "delete":
        print(f"Deleting playlist: {args.name}")
    return 0


def _doctor_check() -> list[str]:
    """Run checks and return results as a list of strings."""
    output = []
    # Check Python version
    import sys
    output.append(f"Python: {sys.version.split()[0]}")
    # Check tide can be imported
    try:
        from tide.cli import main
        output.append("tide CLI: OK")
    except Exception as e:
        output.append(f"tide CLI: ERROR - {e}")
    # Check config
    try:
        from tide.config import load_config
        from tide.paths import get_paths
        config = load_config(paths=get_paths())
        output.append(f"config: OK (music_dir={config.config.music_dir})")
    except Exception as e:
        output.append(f"config: ERROR - {e}")
    return output


def cmd_doctor(args: argparse.Namespace) -> int:
    """Handle the ``doctor`` subcommand."""
    results = _doctor_check()
    for line in results:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``tide`` CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    cmd_map: dict[str, callable] = {
        "play": lambda: cmd_play(args),
        "search": lambda: cmd_search(args),
        "download": lambda: cmd_download(args),
        "playlist": lambda: cmd_playlist(args),
        "doctor": lambda: cmd_doctor(args),
    }

    if args.command not in cmd_map:
        parser.print_usage()
        return 1

    return cmd_map[args.command]()


if __name__ == "__main__":
    sys.exit(main())