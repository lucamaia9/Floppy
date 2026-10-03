"""Media identity and merge policy for Stremio playback tracking.

`PlaybackProgress` is keyed on `Item`, not on Movie/Episode (Episode is not a
Media subclass), so every observation must resolve to an Item before it can be
stored. Resolution never guesses: an unknown id returns None and the caller
skips the write.
"""

import logging

from django.db.models import Q

from app.models import Item
from app.models.choices import MediaTypes, Sources

from .stremio_playback import _parse_episode

logger = logging.getLogger(__name__)

# Stremio batches library-event video ids; see LIBRARY_EVENT_VIDEOS_COUNT in
# stremio-core. Only a sanity bound is needed here, not enforcement.
VIDEO_ID_BATCH_LIMIT = 100


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

    return _episode_item(user, series_id, season_number, episode_number)
