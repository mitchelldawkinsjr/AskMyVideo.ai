import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import VideoJob


class AskMyVideoPhase1Tests(TestCase):
    """Test suite for AskMyVideo Phase 1: Foundation features."""

    def setUp(self):
        """Set up test data for each test."""
        self.client = Client()

        # Create test users
        self.user1 = User.objects.create_user(
            username="testuser1", password="testpass123", email="test1@example.com"
        )
        self.user2 = User.objects.create_user(
            username="testuser2", password="testpass123", email="test2@example.com"
        )

        # Create test video jobs
        self.job1 = VideoJob.objects.create(
            user=self.user1,
            video_path="/test/video1.mp4",
            video_name="Test Video 1.mp4",  # This is what gets displayed
            title="Test Video 1",
            status="completed",
            transcript="This is a test transcript for video 1.",
        )

        self.job2 = VideoJob.objects.create(
            user=self.user2,
            video_path="/test/video2.mp4",
            video_name="Test Video 2.mp4",  # This is what gets displayed
            title="Test Video 2",
            status="completed",
            transcript="This is a test transcript for video 2.",
        )

    def test_user_registration(self):
        """Test user registration functionality."""
        response = self.client.get(reverse("register"))
        self.assertEqual(response.status_code, 200)

        # Test successful registration
        response = self.client.post(
            reverse("register"),
            {
                "username": "newuser",
                "password1": "complexpass123",
                "password2": "complexpass123",
            },
        )
        self.assertEqual(response.status_code, 302)  # Redirect after success
        self.assertTrue(User.objects.filter(username="newuser").exists())

    def test_user_login(self):
        """Test user login functionality."""
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)

        # Test successful login
        response = self.client.post(
            reverse("login"), {"username": "testuser1", "password": "testpass123"}
        )
        self.assertEqual(response.status_code, 302)  # Redirect after success

    def test_multi_tenant_video_access(self):
        """Test that users can only access their own videos."""
        # Login as user1
        self.client.login(username="testuser1", password="testpass123")

        # Access library - should only see user1's videos
        response = self.client.get(reverse("video_library"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Video 1.mp4")
        self.assertNotContains(response, "Test Video 2.mp4")

        # Try to access user2's video directly — should redirect away
        response = self.client.get(
            reverse("transcript_editor", kwargs={"job_id": str(self.job2.job_id)})
        )
        self.assertEqual(response.status_code, 302)

    def test_authentication_required(self):
        """Test that authentication is required for protected views."""
        response = self.client.get(reverse("video_library"))
        self.assertEqual(response.status_code, 302)

        response = self.client.get(
            reverse("transcript_editor", kwargs={"job_id": str(self.job1.job_id)})
        )
        self.assertEqual(response.status_code, 302)

    def test_transcript_editor_view(self):
        """Test transcript editor page renders correctly."""
        self.client.login(username="testuser1", password="testpass123")

        response = self.client.get(
            reverse("transcript_editor", kwargs={"job_id": str(self.job1.job_id)})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Transcript Editor")
        self.assertContains(response, str(self.job1.job_id))

    def test_health_check(self):
        """Test health check endpoint for Docker monitoring."""
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.content)
        self.assertEqual(data["status"], "healthy")
        self.assertIn("timestamp", data)

    @patch("video_processor.api.search_engine")
    def test_health_check_with_search_engine(self, mock_search_engine):
        """Test API health check includes search engine status."""
        mock_search_engine.get_stats.return_value = {
            "is_available": True,
            "is_initialized": True,
            "model_name": "test-model",
            "total_segments": 0,
            "index_size": 0,
        }

        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.content)
        self.assertEqual(data["status"], "healthy")
        self.assertIn("components", data)

    def test_video_ownership_isolation(self):
        """Test that video operations respect user ownership."""
        # Create a job for user1
        job = VideoJob.objects.create(
            user=self.user1,
            video_path="/test/owned_video.mp4",
            title="Owned Video",
            status="completed",
        )

        # Login as user2 and try to access user1's video
        self.client.login(username="testuser2", password="testpass123")

        response = self.client.get(
            reverse("transcript_editor", kwargs={"job_id": str(job.job_id)})
        )
        self.assertEqual(response.status_code, 302)

        self.client.login(username="testuser1", password="testpass123")

        response = self.client.get(
            reverse("transcript_editor", kwargs={"job_id": str(job.job_id)})
        )
        self.assertEqual(response.status_code, 200)

    def test_user_specific_video_counts(self):
        """Test that video statistics are user-specific."""
        # Create additional videos for user1
        VideoJob.objects.create(
            user=self.user1,
            video_path="/test/video3.mp4",
            title="User1 Video 2",
            status="processing",
        )
        VideoJob.objects.create(
            user=self.user1,
            video_path="/test/video4.mp4",
            title="User1 Video 3",
            status="failed",
        )

        self.client.login(username="testuser1", password="testpass123")
        response = self.client.get(reverse("video_library"))

        # Should see user1's 3 videos, not user2's videos
        self.assertEqual(response.context["video_count"], 3)
        self.assertEqual(response.context["completed_count"], 1)
        self.assertEqual(response.context["processing_count"], 1)
        self.assertEqual(response.context["failed_count"], 1)


class HomeViewTests(TestCase):
    """Marketing landing vs authenticated search hub routing."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="landinguser", password="testpass123"
        )

    def test_anonymous_home_shows_marketing_landing(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ask your PDFs, audio, and videos")
        self.assertContains(response, "Get started free")
        self.assertNotContains(response, "Ask Your Vault")

    def test_authenticated_home_shows_search_hub(self):
        self.client.login(username="landinguser", password="testpass123")
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ask Your Vault")
        self.assertNotContains(response, "Built for real-world applications")


class TranscriptWindowingTests(TestCase):
    """Segment merging for the semantic index."""

    def test_build_windows_merges_segments_with_real_timestamps(self):
        from semantic_search import build_windows

        segments = [
            {"start": float(i * 2), "end": float(i * 2 + 2), "text": "word " * 10}
            for i in range(12)
        ]
        windows = build_windows(segments)

        self.assertGreater(len(windows), 1)
        # First window covers the first six 10-word segments (60-word target).
        self.assertEqual(windows[0]["start"], 0.0)
        self.assertEqual(windows[0]["end"], 12.0)
        self.assertEqual(len(windows[0]["text"].split()), 60)
        # Second window starts where the first ended.
        self.assertEqual(windows[1]["start"], 12.0)

    def test_build_windows_skips_empty_segments(self):
        from semantic_search import build_windows

        windows = build_windows(
            [
                {"start": 0.0, "end": 1.0, "text": "  "},
                {"start": 1.0, "end": 2.0, "text": "hello world"},
            ]
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["text"], "hello world")
        self.assertEqual(windows[0]["start"], 1.0)


class SearchFunctionalityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="searchuser", password="testpass123"
        )
        self.video = VideoJob.objects.create(
            user=self.user,
            video_path="/test/video.mp4",
            video_name="Budget Planning.mp4",
            status="completed",
            transcription={
                "text": "We discussed the annual budget and planning goals.",
                "text_segments": [
                    {"start": 0.0, "end": 2.0, "text": "We discussed the annual"},
                    {"start": 2.0, "end": 4.0, "text": "budget and planning goals."},
                ],
            },
        )

    def test_keyword_search_matches_cross_segment_queries(self):
        from .search_helpers import perform_keyword_search

        results = perform_keyword_search("annual budget")
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["segments"]), 2)

    def test_semantic_mode_reports_keyword_fallback_when_engine_unavailable(self):
        from unittest.mock import MagicMock

        from .search_helpers import run_search

        mock_engine = MagicMock()
        mock_engine.ensure_ready.return_value = False

        results, mode = run_search("budget", "semantic", mock_engine, user=self.user)
        self.assertEqual(mode, "keyword_fallback")
        self.assertEqual(len(results), 1)

    def test_search_is_scoped_to_user(self):
        from unittest.mock import MagicMock

        from django.contrib.auth.models import User

        from .search_helpers import run_search

        other_user = User.objects.create_user(
            username="otheruser", password="testpass123"
        )
        mock_engine = MagicMock()
        mock_engine.ensure_ready.return_value = False

        results, _ = run_search("budget", "keyword", mock_engine, user=other_user)
        self.assertEqual(results, [])

    def test_api_search_requires_auth_without_username(self):
        import json

        response = self.client.post(
            "/api/search/",
            data=json.dumps({"query": "budget", "search_mode": "keyword"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_api_search_public_username_scope(self):
        import json

        response = self.client.post(
            "/api/search/",
            data=json.dumps(
                {
                    "query": "budget",
                    "search_mode": "keyword",
                    "username": "searchuser",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data["success"])
        self.assertEqual(data["count"], 1)
