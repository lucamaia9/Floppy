import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from integrations import views

User = get_user_model()


class ManifestDeclaresEventResourcesTests(TestCase):
    def test_player_and_library_are_declared(self):
        self.assertIn("player", views.STREMIO_ADDON_MANIFEST["resources"])
        self.assertIn("library", views.STREMIO_ADDON_MANIFEST["resources"])

    def test_existing_resources_are_retained(self):
        for resource in ("catalog", "meta", "subtitles"):
            self.assertIn(resource, views.STREMIO_ADDON_MANIFEST["resources"])

    def test_version_was_bumped(self):
        """A client may hold the old manifest, so the version must change."""
        self.assertNotEqual(views.STREMIO_ADDON_MANIFEST["version"], "1.2.0")


class PlayerRouteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="routes", password="x")

    def test_player_route_accepts_an_episode_id(self):
        url = reverse(
            "stremio_addon_player",
            kwargs={
                "token": self.user.token,
                "media_type": "series",
                "media_id": "tt1%3A1%3A2",
                "extra": "action=start&currentTime=1000&duration=2000",
            },
        )

        response = self.client.get(url)

        self.assertNotEqual(response.status_code, 404)
        self.assertNotEqual(response.status_code, 500)

    def test_player_route_rejects_an_invalid_token(self):
        url = reverse(
            "stremio_addon_player",
            kwargs={
                "token": "not-a-token",
                "media_type": "movie",
                "media_id": "tt1",
                "extra": "action=start&currentTime=1000",
            },
        )

        self.assertEqual(self.client.get(url).status_code, 401)

    def test_library_route_is_reachable(self):
        url = reverse(
            "stremio_addon_library",
            kwargs={
                "token": self.user.token,
                "media_type": "series",
                "media_id": "tt1",
                "extra": "action=watched&videoId=tt1%3A1%3A2",
            },
        )

        self.assertNotEqual(self.client.get(url).status_code, 404)


class InvalidMediaIdTests(TestCase):
    """A media id that fails the addon pattern never reaches the tracker.

    The id is client-supplied and is bound into queries downstream, so the
    route rejects it up front rather than letting the handler resolve it.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="routes-invalid", password="x")

    def test_player_route_rejects_an_invalid_media_id(self):
        url = reverse(
            "stremio_addon_player",
            kwargs={
                "token": self.user.token,
                "media_type": "movie",
                "media_id": "not-an-id",
                "extra": "action=start&currentTime=1000",
            },
        )

        with patch.object(views.stremio_tracker, "record_player_event") as handler:
            response = self.client.get(url)

        self.assertEqual(response.status_code, 400)
        handler.assert_not_called()

    def test_library_route_rejects_an_invalid_media_id(self):
        # Episode 0 does not exist, so `tt1:1:0` is genuinely malformed.
        url = reverse(
            "stremio_addon_library",
            kwargs={
                "token": self.user.token,
                "media_type": "series",
                "media_id": "tt1%3A1%3A0",
                "extra": "action=watched",
            },
        )

        with patch.object(views.stremio_tracker, "record_library_event") as handler:
            response = self.client.get(url)

        self.assertEqual(response.status_code, 400)
        handler.assert_not_called()

    def test_routes_accept_season_zero_specials(self):
        """Season 0 is Stremio's specials bucket; the guard must not reject it.

        It did, which silently dropped every special before it reached the
        tracker.
        """
        for route, handler_name in (
            ("stremio_addon_library", "record_library_event"),
            ("stremio_addon_player", "record_player_event"),
        ):
            with self.subTest(route=route):
                extra = (
                    "action=watched"
                    if route == "stremio_addon_library"
                    else "action=pause&currentTime=1000&duration=2000"
                )
                url = reverse(
                    route,
                    kwargs={
                        "token": self.user.token,
                        "media_type": "series",
                        "media_id": "tt1%3A0%3A2",
                        "extra": extra,
                    },
                )

                with patch.object(views.stremio_tracker, handler_name) as handler:
                    response = self.client.get(url)

                self.assertEqual(response.status_code, 200)
                handler.assert_called_once()


class PlayerResourceGateTests(TestCase):
    """The player/library resources must be withdrawable without a code change."""

    def setUp(self):
        self.user = User.objects.create_user(username="gate", password="x")

    def test_the_gate_defaults_enabled_and_withdraws_on_a_falsy_value(self):
        url = reverse("stremio_addon_manifest", kwargs={"token": self.user.token})

        self.assertIn("player", self.client.get(url).json()["resources"])

        with patch.dict(os.environ, {"STREMIO_ENABLE_PLAYER_RESOURCE": "0"}):
            resources = self.client.get(url).json()["resources"]

        self.assertNotIn("player", resources)
        self.assertNotIn("library", resources)
        self.assertIn("subtitles", resources)
