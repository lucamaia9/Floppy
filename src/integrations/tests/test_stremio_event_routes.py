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
