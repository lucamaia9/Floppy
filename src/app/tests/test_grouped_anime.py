from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import TV, Episode, Item, ItemProviderLink, MediaTypes, Season, Sources
from app.services import grouped_anime


class GroupedAnimeClassifierTests(TestCase):
    """Verify exact classification and in-place grouped promotion."""

    def setUp(self):
        self.mapping = {
            "anime-show": {
                "mal_id": "12345",
                "tmdb_show_id": "9001",
                "tvdb_id": "7001",
                "imdb_id": "tt9001001",
            },
        }

    def test_classifier_requires_exact_id_and_animation(self):
        """Animation alone or a title match must not move a show."""
        animation = {
            "media_id": "9001",
            "genres": ["Animation"],
            "provider_external_ids": {"tvdb_id": "7001"},
        }
        result = grouped_anime.classify_tv_metadata(
            animation,
            mapping_data=self.mapping,
        )
        self.assertTrue(result.is_grouped_anime)
        self.assertEqual(result.mal_ids, ("12345",))

        not_animation = {**animation, "genres": ["Drama"]}
        self.assertFalse(
            grouped_anime.classify_tv_metadata(
                not_animation,
                mapping_data=self.mapping,
            ).is_grouped_anime,
        )

        title_only = {
            "media_id": "9999",
            "title": "Anime Show",
            "genres": ["Animation"],
        }
        self.assertFalse(
            grouped_anime.classify_tv_metadata(
                title_only,
                mapping_data=self.mapping,
            ).is_grouped_anime,
        )

    def test_promotion_preserves_tree_ids_and_history_shape(self):
        """Promotion changes buckets without creating replacement rows."""
        user = get_user_model().objects.create_user(
            username="grouped-anime",
            password="password",
        )
        tv_item = Item.objects.create(
            media_id="9001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.TV.value,
            title="Anime Show",
            image="https://example.com/show.jpg",
        )
        tv = TV.objects.create(item=tv_item, user=user)
        season_item = Item.objects.create(
            media_id="9001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            library_media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Anime Show Season 1",
            image="https://example.com/season.jpg",
        )
        season = Season.objects.create(item=season_item, user=user, related_tv=tv)
        episode_item = Item.objects.create(
            media_id="9001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Episode 1",
            image="https://example.com/episode.jpg",
        )
        episode = Episode.objects.create(item=episode_item, related_season=season)
        ids = (tv_item.pk, season_item.pk, episode_item.pk, episode.pk)

        match = grouped_anime.classify_tv_metadata(
            {
                "media_id": "9001",
                "genres": ["Animation"],
                "provider_external_ids": {
                    "tvdb_id": "7001",
                    "imdb_id": "tt9001001",
                },
            },
            mapping_data=self.mapping,
        )
        self.assertTrue(grouped_anime.promote_grouped_anime(tv_item, match))

        self.assertEqual(
            (tv_item.pk, season_item.pk, episode_item.pk, episode.pk),
            ids,
        )
        self.assertTrue(
            Item.objects.filter(
                pk__in=[tv_item.pk, season_item.pk, episode_item.pk],
                library_media_type=MediaTypes.ANIME.value,
            ).count()
            == 3,
        )
        self.assertTrue(
            ItemProviderLink.objects.filter(
                item=tv_item,
                provider=Sources.TVDB.value,
                provider_media_id="7001",
                provider_media_type=MediaTypes.TV.value,
            ).exists(),
        )
        self.assertTrue(
            ItemProviderLink.objects.filter(
                item=tv_item,
                provider=Sources.MAL.value,
                provider_media_id="12345",
                provider_media_type=MediaTypes.TV.value,
            ).exists(),
        )
