"""PWA manifest, service worker, and offline shell tests."""

import json

from django.test import Client, TestCase
from django.urls import reverse


class PwaEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_manifest_is_served_at_root(self):
        response = self.client.get(reverse("pwa_manifest"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        payload = json.loads(b"".join(response.streaming_content))
        self.assertEqual(payload["name"], "Recall AI")
        self.assertEqual(payload["short_name"], "Recall")
        self.assertEqual(payload["display"], "standalone")
        self.assertEqual(payload["theme_color"], "#002753")
        self.assertTrue(payload["icons"])

    def test_service_worker_is_served_at_root(self):
        response = self.client.get(reverse("pwa_service_worker"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"])
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertIn(b"recall-ai-v1", b"".join(response.streaming_content))

    def test_offline_page_renders(self):
        response = self.client.get(reverse("offline"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You are offline")

    def test_base_template_includes_pwa_head(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rel="manifest"')
        self.assertContains(response, "theme-color")
        self.assertContains(response, "apple-touch-icon")
        self.assertContains(response, "registerSW.js")
