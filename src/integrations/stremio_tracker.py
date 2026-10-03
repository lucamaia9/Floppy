"""Media identity and merge policy for Stremio playback tracking.

`PlaybackProgress` is keyed on `Item`, not on Movie/Episode (Episode is not a
Media subclass), so every observation must resolve to an Item before it can be
stored. Resolution never guesses: an unknown id returns None and the caller
skips the write.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Q

from app.models import Item
from app.models.choices import MediaTypes, Sources

from .stremio_playback import _parse_episode

logger = logging.getLogger(__name__)

# Stremio batches library-event video ids; see LIBRARY_EVENT_VIDEOS_COUNT in
# stremio-core. Only a sanity bound is needed here, not enforcement.
VIDEO_ID_BATCH_LIMIT = 100

# Bounds for a parsed video id's coordinates. A client supplies the video id, so
# an out-of-range number must be rejected here rather than bound into a query.
MAX_EPISODE_COORDINATE = 9999


def _imdb_q(imdb_id):
    """Match an Item by IMDB id, whichever way it stores it.

    A Stremio id is an IMDB tt id. Items sourced directly from IMDB carry it as
    media_id; TMDB-sourced Items carry it in provider_external_ids — and those
    are what the Stremio importer creates.
    """
    return Q(source=Sources.IMDB.value, media_id=imdb_id) | Q(
        provider_external_ids__imdb_id=imdb_id,
    )


def _movie_item(user, imdb_id):
    # Movie.item has no explicit related_name, so the reverse accessor is the
    # default `movie`.
    return (
        Item.objects.filter(
            _imdb_q(imdb_id),
            media_type=MediaTypes.MOVIE.value,
            movie__user=user,
        )
        .distinct()
        .first()
    )


def _series_item(user, imdb_id):
    return (
        Item.objects.filter(
            _imdb_q(imdb_id),
            media_type=MediaTypes.TV.value,
            tv__user=user,
        )
        .distinct()
        .first()
    )


def _episode_item(user, series_imdb_id, season_number, episode_number):
    """Find the episode Item under the resolved series.

    Episode Items are keyed by the *series* media_id plus season_number and
    episode_number — they do not carry a composite tt123:1:2 media_id.
    """
    series_item = _series_item(user, series_imdb_id)
    if series_item is None:
        return None
    return (
        Item.objects.filter(
            media_id=series_item.media_id,
            source=series_item.source,
            media_type=MediaTypes.EPISODE.value,
            season_number=season_number,
            episode_number=episode_number,
            episode__related_season__user=user,
        )
        .distinct()
        .first()
    )


def resolve_media_identity(user, media_type, media_id, video_id=None):
    """Return the Item this observation belongs to, or None.

    For a series with a video_id the episode is the identity, not the series:
    the position describes one episode. Returning the series would collapse
    every episode's progress onto a single item.
    """
    if not media_id:
        return None

    if media_type == "movie":
        return _movie_item(user, media_id)

    if media_type != "series":
        return None

    if not video_id:
        return _series_item(user, media_id)

    parsed = _parse_episode(video_id)
    if parsed is None:
        logger.info("stremio_tracker status=unresolved reason=malformed_video_id")
        return None
    # _parse_episode returns (series_id, season_number, episode_number).
    series_id, season_number, episode_number = parsed

    if not (1 <= season_number <= MAX_EPISODE_COORDINATE) or not (
        1 <= episode_number <= MAX_EPISODE_COORDINATE
    ):
        logger.info(
            "stremio_tracker status=unresolved reason=out_of_range_video_id",
        )
        return None

    return _episode_item(user, series_id, season_number, episode_number)


# A poll observation older than this is treated as stale for position and
# status purposes. The flag may still be applied.
SESSION_GRACE_SECONDS = 600

_TERMINAL_ACTIONS = frozenset({"stop"})
_CLEAR_ACTIONS = frozenset({"unwatched", "libraryRemove"})


@dataclass(frozen=True)
class Observation:
    """One fact about playback, from any source."""

    source: str  # "player" | "library" | "poll" | "subtitles"
    action: str
    position_seconds: int | None
    duration_seconds: int | None
    watched: bool | None
    observed_at: object
    video_id: str | None


@dataclass(frozen=True)
class MergeResult:
    """What the caller should persist after folding in one observation."""

    completed: bool
    position_seconds: int | None
    duration_seconds: int | None
    record_play: bool
    clear_progress: bool
    settled: bool
    last_event_at: object


def new_session(video_id, started_at):
    """Start a fresh session baseline for one media identity."""
    return {
        "video_id": video_id,
        "started_at": started_at,
        "last_event_at": started_at,
        "position_seconds": None,
        "duration_seconds": None,
        "completed": False,
        "play_recorded": False,
        "settled": False,
    }


_REWATCH_START_ACTIONS = frozenset({"start"})


def session_for_observation(session, observation, *, now=None):
    """Return the session this observation belongs to, starting a new one if needed.

    Rule 3: a new session resets the position baseline and clears `completed`, so
    a rewatch is counted as a fresh play instead of being pinned near the end by
    the monotonic-position rule.
    """
    if session is None:
        return new_session(
            video_id=observation.video_id,
            started_at=observation.observed_at,
        )

    if observation.video_id and observation.video_id != session.get("video_id"):
        return new_session(
            video_id=observation.video_id,
            started_at=observation.observed_at,
        )

    if observation.action in _REWATCH_START_ACTIONS and session.get("settled"):
        return new_session(
            video_id=observation.video_id,
            started_at=observation.observed_at,
        )

    last_event_at = session.get("last_event_at")
    if (
        session.get("settled")
        and last_event_at is not None
        and observation.observed_at
        > last_event_at + timedelta(seconds=SESSION_GRACE_SECONDS)
    ):
        return new_session(
            video_id=observation.video_id,
            started_at=observation.observed_at,
        )

    return session


def _is_stale(session, observation):
    """Return whether an observation predates the session's last applied one."""
    last = session.get("last_event_at")
    if last is None:
        return False
    return observation.observed_at < last


def apply_observation(session, observation):
    """Fold one observation into a session and report the effects.

    Pure: mutates `session` in place and returns what the caller should write.
    Rules, in precedence order, are documented in the spec §5.
    """
    stale = _is_stale(session, observation)

    # Rule 1: a stale observation may still carry watched state, because the
    # flag is authoritative for watched-ness, but it must not move position or
    # status. A poll is the usual source of a stale observation.
    watched = observation.watched
    if watched is not None:
        session["completed"] = bool(watched)

    if observation.action in _CLEAR_ACTIONS:
        session["completed"] = False
        session["position_seconds"] = None
        session["duration_seconds"] = None
        session["settled"] = True
        session["last_event_at"] = max(
            session.get("last_event_at") or observation.observed_at,
            observation.observed_at,
        )
        return MergeResult(
            completed=False,
            position_seconds=None,
            duration_seconds=None,
            record_play=False,
            clear_progress=True,
            settled=True,
            last_event_at=session["last_event_at"],
        )

    if not stale:
        # Rule 2: position is monotonic within a session.
        if observation.position_seconds is not None:
            current = session.get("position_seconds")
            if current is None or observation.position_seconds > current:
                session["position_seconds"] = observation.position_seconds
        if observation.duration_seconds is not None:
            session["duration_seconds"] = observation.duration_seconds
        if observation.action in _TERMINAL_ACTIONS:
            session["settled"] = True
        if session.get("last_event_at") is None or (
            observation.observed_at > session["last_event_at"]
        ):
            session["last_event_at"] = observation.observed_at

    # Rule 4: completion is sticky — `watched` above may only ever raise it.
    completed = bool(session.get("completed"))

    # Rule 5: a play is appended only on the false->true transition.
    record_play = completed and not session.get("play_recorded")
    if record_play:
        session["play_recorded"] = True

    return MergeResult(
        completed=completed,
        position_seconds=session.get("position_seconds"),
        duration_seconds=session.get("duration_seconds"),
        record_play=record_play,
        clear_progress=False,
        settled=bool(session.get("settled")),
        last_event_at=session.get("last_event_at"),
    )
