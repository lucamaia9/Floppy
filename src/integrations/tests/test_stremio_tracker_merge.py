from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from integrations import stremio_tracker as tracker


class MergePolicyTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.session = tracker.new_session(video_id="tt1:1:2", started_at=self.now)

    def _obs(self, **overrides):
        defaults = {
            "source": "player",
            "action": "start",
            "position_seconds": None,
            "duration_seconds": None,
            "watched": None,
            "observed_at": self.now,
            "video_id": "tt1:1:2",
        }
        defaults.update(overrides)
        return tracker.Observation(**defaults)

    def test_playback_start_does_not_record_a_play(self):
        result = tracker.apply_observation(self.session, self._obs())

        self.assertFalse(result.record_play)
        self.assertFalse(result.completed)

    def test_stop_at_seventy_five_percent_completes(self):
        """Stremio flags watched at 70%; a stop at 75% is a real completion.

        is_played() would require the final 30 seconds, so wiring completion to
        it would drop the common 'skipped the credits' case.
        """
        result = tracker.apply_observation(
            self.session,
            self._obs(
                action="stop",
                position_seconds=4500,
                duration_seconds=6000,
                watched=True,
            ),
        )

        self.assertTrue(result.completed)
        self.assertTrue(result.record_play)

    def test_position_is_monotonic_within_a_session(self):
        """A lagging report must not rewind progress."""
        tracker.apply_observation(
            self.session,
            self._obs(action="pause", position_seconds=3000, duration_seconds=6000),
        )
        result = tracker.apply_observation(
            self.session,
            self._obs(action="pause", position_seconds=1000, duration_seconds=6000),
        )

        self.assertEqual(result.position_seconds, 3000)

    def test_late_poll_does_not_rewind_position(self):
        """The 90s push throttle means a poll can carry an older position."""
        tracker.apply_observation(
            self.session,
            self._obs(action="pause", position_seconds=3000, duration_seconds=6000),
        )
        result = tracker.apply_observation(
            self.session,
            self._obs(
                source="poll",
                action="observed",
                position_seconds=2000,
                duration_seconds=6000,
                observed_at=self.now - timedelta(seconds=120),
            ),
        )

        self.assertEqual(result.position_seconds, 3000)

    def test_completion_is_sticky_against_in_progress_observations(self):
        tracker.apply_observation(
            self.session,
            self._obs(
                action="stop",
                position_seconds=4500,
                duration_seconds=6000,
                watched=True,
            ),
        )
        result = tracker.apply_observation(
            self.session,
            self._obs(action="pause", position_seconds=100, duration_seconds=6000),
        )

        self.assertTrue(result.completed)

    def test_play_is_recorded_once_per_session(self):
        first = tracker.apply_observation(
            self.session,
            self._obs(
                action="stop",
                position_seconds=4500,
                duration_seconds=6000,
                watched=True,
            ),
        )
        second = tracker.apply_observation(
            self.session,
            self._obs(
                action="stop",
                position_seconds=4500,
                duration_seconds=6000,
                watched=True,
            ),
        )

        self.assertTrue(first.record_play)
        self.assertFalse(second.record_play)

    def test_rewatch_after_settling_starts_a_new_session(self):
        """Without a reset, monotonic position pins a rewatch near the end."""
        tracker.apply_observation(
            self.session,
            self._obs(
                action="stop",
                position_seconds=5900,
                duration_seconds=6000,
                watched=True,
            ),
        )
        fresh = tracker.new_session(
            video_id="tt1:1:2",
            started_at=self.now + timedelta(hours=1),
        )
        result = tracker.apply_observation(
            fresh,
            self._obs(
                action="pause",
                position_seconds=600,
                duration_seconds=6000,
                observed_at=self.now + timedelta(hours=1),
            ),
        )

        self.assertEqual(result.position_seconds, 600)
        self.assertFalse(result.completed)

    def test_unwatch_clears_state_without_recording_a_play(self):
        result = tracker.apply_observation(
            self.session,
            self._obs(action="unwatched", watched=False),
        )

        self.assertFalse(result.completed)
        self.assertFalse(result.record_play)
        self.assertTrue(result.clear_progress)

    def test_library_remove_clears_progress_only(self):
        result = tracker.apply_observation(
            self.session,
            self._obs(action="libraryRemove"),
        )

        self.assertTrue(result.clear_progress)
        self.assertFalse(result.record_play)

    def test_poll_may_set_watched_state_without_position(self):
        """The flag is authoritative for watched-ness even on a stale poll."""
        result = tracker.apply_observation(
            self.session,
            self._obs(source="poll", action="observed", watched=True),
        )

        self.assertTrue(result.completed)
        self.assertTrue(result.record_play)

    def _settle(self, session=None):
        session = self.session if session is None else session
        tracker.apply_observation(
            session,
            self._obs(
                action="stop",
                position_seconds=4500,
                duration_seconds=6000,
                watched=True,
            ),
        )
        return session

    def test_no_session_starts_one(self):
        session = tracker.session_for_observation(None, self._obs())

        self.assertEqual(session["video_id"], "tt1:1:2")

    def test_start_after_settling_starts_a_new_session(self):
        self._settle()
        session = tracker.session_for_observation(self.session, self._obs())

        self.assertIsNot(session, self.session)
        self.assertFalse(session["completed"])
        self.assertFalse(session["play_recorded"])

    def test_changed_video_id_starts_a_new_session(self):
        session = tracker.session_for_observation(
            self.session,
            self._obs(video_id="tt1:1:3"),
        )

        self.assertIsNot(session, self.session)
        self.assertEqual(session["video_id"], "tt1:1:3")

    def test_same_video_keeps_the_session(self):
        session = tracker.session_for_observation(self.session, self._obs())

        self.assertIs(session, self.session)

    def test_settled_session_past_the_grace_window_starts_a_new_session(self):
        self._settle()
        session = tracker.session_for_observation(
            self.session,
            self._obs(
                action="pause",
                position_seconds=100,
                duration_seconds=6000,
                observed_at=self.now
                + timedelta(seconds=tracker.SESSION_GRACE_SECONDS + 1),
            ),
        )

        self.assertIsNot(session, self.session)

    def test_settled_session_inside_the_grace_window_keeps_the_session(self):
        self._settle()
        session = tracker.session_for_observation(
            self.session,
            self._obs(
                action="pause",
                position_seconds=100,
                duration_seconds=6000,
                observed_at=self.now
                + timedelta(seconds=tracker.SESSION_GRACE_SECONDS - 1),
            ),
        )

        self.assertIs(session, self.session)

    def test_rewatch_after_settling_appends_a_second_play(self):
        """The user-visible requirement: a rewatch counts as a second play."""
        first = tracker.apply_observation(
            self.session,
            self._obs(
                action="stop",
                position_seconds=4500,
                duration_seconds=6000,
                watched=True,
            ),
        )
        session = tracker.session_for_observation(
            self.session,
            self._obs(action="start", observed_at=self.now + timedelta(hours=1)),
        )
        second = tracker.apply_observation(
            session,
            self._obs(
                action="stop",
                position_seconds=4500,
                duration_seconds=6000,
                watched=True,
                observed_at=self.now + timedelta(hours=1),
            ),
        )

        self.assertTrue(first.record_play)
        self.assertTrue(second.record_play)
