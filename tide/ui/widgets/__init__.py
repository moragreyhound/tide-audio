"""Widgets used by the main screen."""

from tide.ui.widgets.downloadspanel import DownloadsPanel
from tide.ui.widgets.header import Header
from tide.ui.widgets.nowplaying import NowPlaying
from tide.ui.widgets.rail import Rail
from tide.ui.widgets.statusbar import StatusBar
from tide.ui.widgets.tracklist import ListRow, TrackList
from tide.ui.widgets.visualizer import VisualizerWidget

__all__ = [
    "DownloadsPanel",
    "Header",
    "ListRow",
    "NowPlaying",
    "Rail",
    "StatusBar",
    "TrackList",
    "VisualizerWidget",
]
