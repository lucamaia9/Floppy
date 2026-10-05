"""Parsing for Stremio addon `player` and `library` event extras.

Stremio sends these as a percent-encoded `key=value` sequence joined by `&`,
placed in the URL path segment after the media id — not as a query string, so
``request.GET`` is empty. Every value is read by name; a missing or reordered
key yields None rather than a wrong number.
"""

from dataclasses import dataclass
from urllib.parse import unquote

PLAYER_ACTIONS = frozenset({"start", "pause", "stop"})
LIBRARY_ACTIONS = frozenset({"libraryAdd", "libraryRemove", "watched", "unwatched"})

# Stremio reports positions and durations in milliseconds; Floppy's API and
# PlaybackProgress are in seconds. Omitting this conversion stores every
# position 1000x too large.
_MS_PER_SECOND = 1000


@dataclass(frozen=True)
class PlayerEvent:
    """One player event: a transition and the position it happened at."""

    action: str
    position_seconds: int
    duration_seconds: int | None


@dataclass(frozen=True)
class LibraryEvent:
    """One library event. `video_ids` is empty for item-level actions."""

    action: str
    video_ids: tuple[str, ...]


def _parse_pairs(extra):
    """Decode `a=1&b=2` into a dict, unquoting both sides."""
    pairs = {}
    if not extra:
        return pairs
    for chunk in str(extra).split("&"):
        if not chunk:
            continue
        name, separator, value = chunk.partition("=")
        if not separator:
            continue
        pairs[unquote(name)] = unquote(value)
    return pairs


def _to_int(value):
    """Return a non-negative int, or None when the value is unusable."""
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _to_seconds(value_ms):
    if value_ms is None:
        return None
    return value_ms // _MS_PER_SECOND


def parse_player_extra(extra):
    """Parse a `player` extra into a PlayerEvent, or None when unusable."""
    pairs = _parse_pairs(extra)
    action = pairs.get("action")
    if action not in PLAYER_ACTIONS:
        return None
    position_ms = _to_int(pairs.get("currentTime"))
    if position_ms is None:
        return None
    duration_ms = _to_int(pairs.get("duration"))
    return PlayerEvent(
        action=action,
        position_seconds=_to_seconds(position_ms),
        duration_seconds=_to_seconds(duration_ms),
    )


def parse_library_extra(extra):
    """Parse a `library` extra into a LibraryEvent, or None when unusable."""
    pairs = _parse_pairs(extra)
    action = pairs.get("action")
    if action not in LIBRARY_ACTIONS:
        return None
    raw_video_id = pairs.get("videoId") or ""
    video_ids = tuple(
        part for part in (chunk.strip() for chunk in raw_video_id.split(",")) if part
    )
    return LibraryEvent(action=action, video_ids=video_ids)

# Stremio appends these to the `subtitles` path when the selected stream
# carries them — from the player's `VideoParams` or the stream's
# `behavior_hints`. See `subtitles_update` in stremio-core's `models/player.rs`.
SUBTITLES_VIDEO_EXTRAS = ("videoHash", "videoSize", "filename")

def parse_subtitles_video_extras(extra):
    """Return which video extras arrived on a `subtitles` request.

    Floppy serves subtitles itself, so this extra has always been discarded by
    the route. It is the only release-level attribution Stremio offers, so
    presence is reported before anything is built on it — a client that omits
    all three sends the route no extra at all, which is itself the answer.
    """
    pairs = _parse_pairs(extra)
    return {name: bool(pairs.get(name)) for name in SUBTITLES_VIDEO_EXTRAS}
