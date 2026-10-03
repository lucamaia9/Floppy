from django.contrib.auth import get_user_model
from django.test import TestCase

from app.mixins import disable_fetch_releases
from app.models import TV, Episode, Item, Movie, Season
from app.models.choices import MediaTypes, Sources, Status
from integrations import stremio_tracker

User = get_user_model()


class ResolveMediaIdentityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="identity", password="x")

    def test_movie_resolves_directly(self):
        with disable_fetch_releases():
            item = Item.objects.create(
                media_id="tt100",
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title="A Movie",
                image="",
            )
            Movie.objects.create(
                item=item,
                user=self.user,
                status=Status.PLANNING.value,
            )

        resolved = stremio_tracker.resolve_media_identity(
            self.user,
            "movie",
            "tt100",
        )

        self.assertEqual(resolved, item)

    def test_unknown_movie_returns_none(self):
        """An id Floppy does not know is skipped, never guessed."""
        self.assertIsNone(
            stremio_tracker.resolve_media_identity(self.user, "movie", "tt999"),
        )

    def test_series_without_video_id_resolves_the_series(self):
        with disable_fetch_releases():
            item = Item.objects.create(
                media_id="tt200",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title="A Series",
                image="",
            )
            TV.objects.create(
                item=item,
                user=self.user,
                status=Status.PLANNING.value,
            )

        resolved = stremio_tracker.resolve_media_identity(
            self.user,
            "series",
            "tt200",
        )

        self.assertEqual(resolved, item)

    def test_series_with_video_id_does_not_resolve_to_the_series(self):
        """The position belongs to the episode, not the series.

        Returning the series here would attribute every episode's position to
        one item — the most likely identity bug in the feature.
        """
        with disable_fetch_releases():
            item = Item.objects.create(
                media_id="tt300",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title="Another Series",
                image="",
            )
            TV.objects.create(
                item=item,
                user=self.user,
                status=Status.PLANNING.value,
            )

        resolved = stremio_tracker.resolve_media_identity(
            self.user,
            "series",
            "tt300",
            video_id="tt300:1:2",
        )

        self.assertNotEqual(resolved, item)

    def test_malformed_video_id_returns_none(self):
        self.assertIsNone(
            stremio_tracker.resolve_media_identity(
                self.user,
                "series",
                "tt300",
                video_id="not-an-episode-id",
            ),
        )

    def test_series_with_video_id_resolves_the_episode_item(self):
        """Episode Items are keyed by series media_id + season/episode fields."""
        with disable_fetch_releases():
            series_item = Item.objects.create(
                media_id="tt400",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title="Episode Series",
                image="",
            )
            tv = TV.objects.create(
                item=series_item,
                user=self.user,
                status=Status.PLANNING.value,
            )
            season_item = Item.objects.create(
                media_id="tt400",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                season_number=1,
                title="Episode Series",
                image="",
            )
            season = Season.objects.create(
                item=season_item,
                related_tv=tv,
                user=self.user,
                status=Status.PLANNING.value,
            )
            episode_item = Item.objects.create(
                media_id="tt400",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=2,
                title="An Episode",
                image="",
            )
            # Seed the play row directly: Episode.save() would ask the provider
            # for season metadata, which the test network guard blocks.
            Episode.objects.bulk_create(
                [
                    Episode(
                        item=episode_item,
                        related_season=season,
                        end_date=None,
                    ),
                ],
            )

        resolved = stremio_tracker.resolve_media_identity(
            self.user,
            "series",
            "tt400",
            video_id="tt400:1:2",
        )

        self.assertEqual(resolved, episode_item)
