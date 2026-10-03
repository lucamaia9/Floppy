from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import Item, Movie, PlaybackProgress
from app.models.choices import MediaTypes, Sources
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
