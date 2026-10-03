"""Media identity and merge policy for Stremio playback tracking.

`PlaybackProgress` is keyed on `Item`, not on Movie/Episode (Episode is not a
Media subclass), so every observation must resolve to an Item before it can be
stored. Resolution never guesses: an unknown id returns None and the caller
skips the write.
"""

import logging

from app.models import Item
from app.models.choices import MediaTypes, Sources

from .stremio_playback import _parse_episode

logger = logging.getLogger(__name__)

# Stremio batches library-event video ids; see LIBRARY_EVENT_VIDEOS_COUNT in
# stremio-core. Only a sanity bound is needed here, not enforcement.
VIDEO_ID_BATCH_LIMIT = 100


def _movie_item(user, media_id):
    # Movie.item has no explicit related_name, so the reverse accessor is the
    # default `movie`.
    return (
        Item.objects.filter(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            movie__user=user,
        )
        .distinct()
        .first()
    )


def _series_item(user, media_id):
    return (
        Item.objects.filter(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            tv__user=user,
        )
        .distinct()
        .first()
    )


def _episode_item(user, series_id, season_number, episode_number):
    """Find the episode Item by its coordinate fields.

    Episode Items are keyed by the *series* media_id plus season_number and
    episode_number — they do not carry a composite `tt123:1:2` media_id.
    """
    return (
        Item.objects.filter(
            media_id=series_id,
            source=Sources.TMDB.value,
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

    return _episode_item(user, series_id, season_number, episode_number)
