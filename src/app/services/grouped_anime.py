"""Exact, fail-closed classification and promotion for grouped anime.

Grouped anime is stored as TV-shaped records with ``library_media_type`` set to
``anime``.  This module is deliberately provider-agnostic at the call sites:
Stremio imports, Stremio webhooks, and the repair command all use the same
classification and in-place promotion code.

The classifier does not use title matching.  A show must have an exact
Anime-IDs external-ID match and TMDB must identify it as Animation.  This is
intentionally conservative: a false negative leaves a show in TV, while a
false positive would move history into the wrong library.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from app.models import Episode, Item, ItemProviderLink, MediaTypes, Season, Sources
from integrations import anime_mapping

MAPPING_SOURCE = "Kometa Anime-IDs"
ANIMATION_GENRE = "animation"
GROUPED_BUCKET = MediaTypes.ANIME.value
GROUPED_PARENT_TYPES = {Sources.TMDB.value, Sources.TVDB.value}


@dataclass(frozen=True, slots=True)
class GroupedAnimeMatch:
    """The auditable result of one exact grouped-anime classification."""

    decision: str
    reason: str
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None
    mal_ids: tuple[str, ...] = ()
    matched_by: tuple[str, ...] = ()
    mapping_keys: tuple[str, ...] = ()
    mapping_source: str = MAPPING_SOURCE

    @property
    def is_grouped_anime(self) -> bool:
        """Return whether this result is safe to route to grouped anime."""
        return self.decision == "move"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON/report-friendly representation."""
        return {
            "decision": self.decision,
            "reason": self.reason,
            "tmdb_id": self.tmdb_id,
            "tvdb_id": self.tvdb_id,
            "imdb_id": self.imdb_id,
            "mal_ids": list(self.mal_ids),
            "matched_by": list(self.matched_by),
            "mapping_keys": list(self.mapping_keys),
            "mapping_source": self.mapping_source,
        }


def _normalise_ids(value: Any) -> set[str]:
    """Normalize scalar, list, and comma-separated external IDs."""
    if value in (None, ""):
        return set()
    values = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    return {str(entry).strip() for entry in values if str(entry).strip()}


def build_mapping_indexes(
    mapping_data: dict[str, Any],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Build exact reverse indexes from the Kometa Anime-IDs payload."""
    fields = (
        "tmdb_show_id",
        "tmdb_id",
        "tmdb_tv_id",
        "tvdb_id",
        "imdb_id",
    )
    indexes: dict[str, dict[str, list[dict[str, Any]]]] = {
        field: defaultdict(list) for field in fields
    }

    for mapping_key, raw_entry in (mapping_data or {}).items():
        if not isinstance(raw_entry, dict):
            continue
        mal_ids = sorted(_normalise_ids(raw_entry.get("mal_id")))
        if not mal_ids:
            continue
        entry = {
            "mapping_key": str(mapping_key),
            "mal_ids": mal_ids,
        }
        for field in fields:
            entry[field] = sorted(_normalise_ids(raw_entry.get(field)))
            for external_id in entry[field]:
                indexes[field][external_id].append(entry)

    return indexes


def _metadata_external_ids(metadata: dict[str, Any]) -> dict[str, str]:
    """Extract the show-level IDs exposed by TMDB-shaped metadata."""
    external_ids = dict(metadata.get("provider_external_ids") or {})
    external_ids.update(metadata.get("external_ids") or {})
    if metadata.get("media_id") not in (None, ""):
        external_ids.setdefault("tmdb_id", str(metadata["media_id"]))
    if metadata.get("tvdb_id") not in (None, ""):
        external_ids.setdefault("tvdb_id", str(metadata["tvdb_id"]))

    return {
        key: str(value).strip()
        for key, value in external_ids.items()
        if value not in (None, "")
    }


def classify_tv_metadata(
    metadata: dict[str, Any] | None,
    *,
    mapping_data: dict[str, Any] | None = None,
) -> GroupedAnimeMatch:
    """Classify one TMDB TV metadata payload using exact external IDs."""
    metadata = metadata or {}
    ids = _metadata_external_ids(metadata)
    indexes = build_mapping_indexes(
        anime_mapping.load_mapping_data() if mapping_data is None else mapping_data,
    )

    lookup_fields = {
        "tmdb": (
            "tmdb_show_id",
            "tmdb_id",
            "tmdb_tv_id",
        ),
        "tvdb": ("tvdb_id",),
        "imdb": ("imdb_id",),
    }
    hits: list[tuple[str, dict[str, Any]]] = []
    for provider, fields in lookup_fields.items():
        for field in fields:
            external_id = ids.get(field)
            if not external_id:
                continue
            hits.extend(
                (provider, entry) for entry in indexes[field].get(external_id, [])
            )

    mal_ids = tuple(
        sorted(
            {
                mal_id
                for _provider, entry in hits
                for mal_id in entry["mal_ids"]
            },
        )
    )
    result_kwargs = {
        "tmdb_id": ids.get("tmdb_id"),
        "tvdb_id": ids.get("tvdb_id"),
        "imdb_id": ids.get("imdb_id"),
        "mal_ids": mal_ids,
        "matched_by": tuple(sorted({provider for provider, _entry in hits})),
        "mapping_keys": tuple(
            sorted({entry["mapping_key"] for _provider, entry in hits})
        ),
    }

    if not hits:
        return GroupedAnimeMatch(
            decision="leave",
            reason="no_exact_anime_ids_external_id_match",
            **result_kwargs,
        )
    if not mal_ids:
        return GroupedAnimeMatch(
            decision="leave",
            reason="anime_ids_match_has_no_mal_identity",
            **result_kwargs,
        )

    genres = metadata.get("genres") or []
    if not any(str(genre).casefold() == ANIMATION_GENRE for genre in genres):
        return GroupedAnimeMatch(
            decision="leave",
            reason="tmdb_metadata_is_not_tagged_animation",
            **result_kwargs,
        )

    reason = "exact_external_id_and_animation_genre"
    if len(mal_ids) > 1:
        reason += "_multiple_mal_seasons"
    return GroupedAnimeMatch(
        decision="move",
        reason=reason,
        **result_kwargs,
    )


def _tree_items(tv_item: Item) -> list[Item]:
    """Return the parent and all season/episode Items attached to it."""
    seasons = list(
        Season.objects.filter(related_tv__item=tv_item).select_related("item"),
    )
    episodes = list(
        Episode.objects.filter(related_season__related_tv__item=tv_item).select_related(
            "item",
        ),
    )
    return [
        tv_item,
        *(season.item for season in seasons),
        *(episode.item for episode in episodes),
    ]


def _target_collision(items: list[Item]) -> str | None:
    """Return a collision reason before changing any library buckets."""
    for item in items:
        identity = {
            "media_id": item.media_id,
            "source": item.source,
            "media_type": item.media_type,
            "library_media_type": GROUPED_BUCKET,
            "season_number": item.season_number,
            "episode_number": item.episode_number,
        }
        if Item.objects.filter(**identity).exclude(pk=item.pk).exists():
            return f"target_bucket_collision_for_item_{item.pk}"
    return None


def _safe_upsert_link(
    item: Item,
    provider: str,
    provider_media_id: str | None,
    metadata: dict[str, Any],
) -> None:
    """Add a show-level link without stealing another item's identity."""
    if not provider_media_id:
        return
    existing = ItemProviderLink.objects.filter(
        item=item,
        provider=provider,
        provider_media_type=MediaTypes.TV.value,
        season_number=None,
    ).first()
    if existing is not None:
        return
    claimed = ItemProviderLink.objects.filter(
        provider=provider,
        provider_media_type=MediaTypes.TV.value,
        provider_media_id=str(provider_media_id),
        season_number=None,
    ).exclude(item=item).exists()
    if claimed:
        return
    ItemProviderLink.objects.create(
        item=item,
        provider=provider,
        provider_media_type=MediaTypes.TV.value,
        provider_media_id=str(provider_media_id),
        metadata=metadata,
    )


@transaction.atomic
def promote_grouped_anime(
    tv_item: Item,
    match: GroupedAnimeMatch,
) -> bool:
    """Promote a TV tree to the anime bucket, preserving all row IDs/history.

    Returns False when the target bucket is occupied by another Item.  No
    partial update is committed in that case.
    """
    if not match.is_grouped_anime:
        return False
    if (
        tv_item.media_type != MediaTypes.TV.value
        or tv_item.source not in GROUPED_PARENT_TYPES
    ):
        return False

    items = _tree_items(tv_item)
    collision = _target_collision(items)
    if collision:
        return False

    parent_external_ids = dict(tv_item.provider_external_ids or {})
    parent_external_ids.update(
        {
            key: value
            for key, value in (
                ("tmdb_id", match.tmdb_id),
                ("tvdb_id", match.tvdb_id),
                ("imdb_id", match.imdb_id),
            )
            if value
        },
    )
    if len(match.mal_ids) == 1:
        parent_external_ids["mal_id"] = match.mal_ids[0]

    for item in items:
        updates = []
        if item.library_media_type != GROUPED_BUCKET:
            item.library_media_type = GROUPED_BUCKET
            updates.append("library_media_type")
        if item is tv_item and item.provider_external_ids != parent_external_ids:
            item.provider_external_ids = parent_external_ids
            updates.append("provider_external_ids")
        if updates:
            item.save(update_fields=updates)

    link_metadata = {
        "migration": "grouped-anime-classifier",
        "mapping_source": match.mapping_source,
        "mapping_keys": list(match.mapping_keys),
    }
    _safe_upsert_link(tv_item, Sources.TMDB.value, match.tmdb_id, link_metadata)
    _safe_upsert_link(tv_item, Sources.TVDB.value, match.tvdb_id, link_metadata)
    if len(match.mal_ids) == 1:
        _safe_upsert_link(tv_item, Sources.MAL.value, match.mal_ids[0], link_metadata)
    return True
