from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.mixins import disable_fetch_releases
from app.models import TV, Episode, Item, Movie, PlaybackProgress, Season
from app.models.choices import MediaTypes, Sources, Status
from integrations import stremio_tracker as tracker

User = get_user_model()


class PersistMergeResultTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="persist", password="x")
        self.item = Item.objects.create(
            media_id="tt500",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Persisted",
            image="",
        )
        self.movie = Movie.objects.create(item=self.item, user=self.user)

    def _result(self, **overrides):
        defaults = {
            "completed": False,
            "position_seconds": 120,
            "duration_seconds": 600,
            "record_play": False,
            "clear_progress": False,
            "settled": False,
            "last_event_at": None,
        }
        defaults.update(overrides)
        return tracker.MergeResult(**defaults)

    def test_position_is_stored(self):
        tracker.persist_merge_result(
            self.user,
            self.item,
            self._result(),
            media_id="tt500",
            video_id=None,
            session_started_at=None,
            ended_at=None,
        )

        progress = PlaybackProgress.objects.get(user=self.user, item=self.item)
        self.assertEqual(progress.position_seconds, 120)
        self.assertEqual(progress.duration_seconds, 600)
        self.assertFalse(progress.completed)

    def test_clear_progress_removes_the_row(self):
        PlaybackProgress.objects.create(
            user=self.user,
            item=self.item,
            position_seconds=300,
        )

        tracker.persist_merge_result(
            self.user,
            self.item,
            self._result(clear_progress=True, position_seconds=None),
            media_id="tt500",
            video_id=None,
            session_started_at=None,
            ended_at=None,
        )

        self.assertFalse(
            PlaybackProgress.objects.filter(user=self.user, item=self.item).exists(),
        )

    def test_completion_appends_one_history_play(self):
        recorded = tracker.persist_merge_result(
            self.user,
            self.item,
            self._result(completed=True, record_play=True, position_seconds=590),
            media_id="tt500",
            video_id=None,
            session_started_at=timezone.now(),
            ended_at=timezone.now(),
        )

        self.assertTrue(recorded)
        self.assertEqual(self.movie.plays.count(), 1)

    def test_replaying_the_same_session_does_not_append_twice(self):
        """The external_id makes the append idempotent at the database level."""
        started = timezone.now()
        for _ in range(2):
            tracker.persist_merge_result(
                self.user,
                self.item,
                self._result(completed=True, record_play=True),
                media_id="tt500",
                video_id=None,
                session_started_at=started,
                ended_at=timezone.now(),
            )

        self.assertEqual(self.movie.plays.count(), 1)

    def test_a_second_session_appends_a_second_play(self):
        first = timezone.now()
        second = first + timedelta(hours=3)
        for started in (first, second):
            tracker.persist_merge_result(
                self.user,
                self.item,
                self._result(completed=True, record_play=True),
                media_id="tt500",
                video_id=None,
                session_started_at=started,
                ended_at=started,
            )

        self.assertEqual(self.movie.plays.count(), 2)


class PersistMergeResultEpisodeTests(TestCase):
    """The episode branch: `Season.watch` through `episode.related_season`.

    Episodes are the high-volume path for Stremio playback, and their append
    goes through a different API than the movie branch — a `Season.watch`
    returning an `EpisodeWatchResult` instead of `Movie.watch`'s tuple.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="persist-episode", password="x")
        # Watching an episode resolves the episode Item and syncs the season
        # through provider metadata; the test network guard blocks the real
        # call, so stub the season payload the way the model tests do.
        metadata = patch(
            "app.models.providers.services.get_media_metadata",
            return_value={
                "season/1": {
                    "episodes": [{"episode_number": number} for number in (1, 2, 3)],
                },
                "related": {"seasons": [{"season_number": 1}]},
                # The credits backfill runs off Item saves; a real payload
                # carries these, and without them every save logs a warning.
                "cast": [],
                "crew": [],
            },
        )
        metadata.start()
        self.addCleanup(metadata.stop)
        with disable_fetch_releases():
            series_item = Item.objects.create(
                media_id="1399",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                provider_external_ids={"imdb_id": "tt900"},
                title="Episode Series",
                image="",
            )
            self.tv = TV.objects.create(
                item=series_item,
                user=self.user,
                status=Status.PLANNING.value,
            )
            season_item = Item.objects.create(
                media_id="1399",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                season_number=1,
                title="Episode Series",
                image="",
            )
            self.season = Season.objects.create(
                item=season_item,
                related_tv=self.tv,
                user=self.user,
                status=Status.PLANNING.value,
            )
            self.episode_item = Item.objects.create(
                media_id="1399",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=2,
                title="An Episode",
                image="",
            )
            # Seed the row `_append_play` uses to reach the season: without it
            # the episode item has no related season to watch into. It is
            # fixture plumbing, so `_plays` counts it out.
            Episode.objects.bulk_create(
                [
                    Episode(
                        item=self.episode_item,
                        related_season=self.season,
                        end_date=None,
                    ),
                ],
            )
            self.seeded_plays = Episode.objects.filter(
                item=self.episode_item,
            ).count()

    def _plays(self):
        """Count the plays appended for the episode, ignoring the seeded row."""
        return (
            Episode.objects.filter(item=self.episode_item).count() - self.seeded_plays
        )

    def _result(self, **overrides):
        defaults = {
            "completed": True,
            "position_seconds": 590,
            "duration_seconds": 600,
            "record_play": True,
            "clear_progress": False,
            "settled": False,
            "last_event_at": None,
        }
        defaults.update(overrides)
        return tracker.MergeResult(**defaults)

    def _persist(self, session_started_at):
        return tracker.persist_merge_result(
            self.user,
            self.episode_item,
            self._result(),
            media_id="1399",
            video_id="tt900:1:2",
            session_started_at=session_started_at,
            ended_at=session_started_at,
        )

    def test_episode_completion_appends_one_history_play(self):
        recorded = self._persist(timezone.now())

        self.assertTrue(recorded)
        self.assertEqual(self._plays(), 1)

    def test_replaying_the_same_episode_session_does_not_append_twice(self):
        """The external_id makes the episode append idempotent in the database."""
        started = timezone.now()
        for _ in range(2):
            self._persist(started)

        self.assertEqual(self._plays(), 1)

    def test_a_second_episode_session_appends_a_second_play(self):
        first = timezone.now()
        second = first + timedelta(hours=3)
        for started in (first, second):
            self._persist(started)

        self.assertEqual(self._plays(), 2)
