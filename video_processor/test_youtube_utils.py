from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import yt_dlp
from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, TestCase

from .youtube_utils import (
    MAX_PLAYLIST_VIDEOS,
    build_ytdlp_opts,
    download_youtube_video,
    format_download_error,
    is_bot_check_error,
    is_youtube_playlist_url,
    is_youtube_video_url,
    normalize_duration,
    pick_downloaded_video_file,
    resolve_cookies_path,
    youtube_cookies_configured,
)


class YouTubeUrlValidationTests(SimpleTestCase):
    def test_accepts_standard_watch_url(self):
        self.assertTrue(
            is_youtube_video_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        )

    def test_accepts_short_url(self):
        self.assertTrue(is_youtube_video_url("https://youtu.be/dQw4w9WgXcQ"))

    def test_rejects_playlist_url(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLtest"
        self.assertTrue(is_youtube_playlist_url(url))
        self.assertFalse(is_youtube_video_url(url))

    def test_rejects_non_youtube_url(self):
        self.assertFalse(is_youtube_video_url("https://example.com/video.mp4"))
        self.assertFalse(is_youtube_video_url("file:///etc/passwd"))

    def test_rejects_non_http_scheme(self):
        self.assertFalse(is_youtube_video_url("ftp://youtube.com/watch?v=abc"))


class BuildYtdlpOptsTests(SimpleTestCase):
    def test_includes_android_web_player_clients(self):
        opts = build_ytdlp_opts()
        self.assertIn("android", opts["extractor_args"]["youtube"]["player_client"])

    def test_merges_extractor_args(self):
        opts = build_ytdlp_opts(
            extract_flat=True,
            extractor_args={"youtube": {"skip": ["dash", "hls"]}},
        )
        self.assertTrue(opts["extract_flat"])
        self.assertIn("player_client", opts["extractor_args"]["youtube"])
        self.assertEqual(opts["extractor_args"]["youtube"]["skip"], ["dash", "hls"])

    def test_uses_cookies_file_when_present(self):
        with TemporaryDirectory() as tmpdir:
            cookies = Path(tmpdir) / "cookies.txt"
            cookies.write_text("# Netscape HTTP Cookie File\n")
            with patch.dict("os.environ", {"YOUTUBE_COOKIES_FILE": str(cookies)}):
                opts = build_ytdlp_opts()
            self.assertEqual(opts["cookiefile"], str(cookies))

    def test_uses_custom_player_clients(self):
        opts = build_ytdlp_opts(player_clients=["ios", "web"])
        self.assertEqual(
            opts["extractor_args"]["youtube"]["player_client"], ["ios", "web"]
        )


class CookieConfigTests(SimpleTestCase):
    def test_resolve_cookies_path_from_env(self):
        with TemporaryDirectory() as tmpdir:
            cookies = Path(tmpdir) / "cookies.txt"
            cookies.write_text("# Netscape HTTP Cookie File\n")
            with patch.dict(
                "os.environ", {"YOUTUBE_COOKIES_FILE": str(cookies)}, clear=False
            ):
                self.assertEqual(resolve_cookies_path(), cookies)
                self.assertTrue(youtube_cookies_configured())

    def test_resolve_cookies_path_missing(self):
        with patch.dict(
            "os.environ",
            {"YOUTUBE_COOKIES_FILE": "/nonexistent/cookies.txt"},
            clear=False,
        ):
            with patch(
                "video_processor.youtube_utils.DEFAULT_COOKIES_PATH",
                Path("/also/missing.txt"),
            ):
                self.assertIsNone(resolve_cookies_path())
                self.assertFalse(youtube_cookies_configured())


class BotCheckErrorTests(SimpleTestCase):
    def test_detects_bot_check_message(self):
        self.assertTrue(is_bot_check_error("Sign in to confirm you're not a bot"))

    def test_format_download_error_without_cookies(self):
        with patch(
            "video_processor.youtube_utils.youtube_cookies_configured",
            return_value=False,
        ):
            message = format_download_error(
                Exception("Sign in to confirm you're not a bot")
            )
        self.assertIn("setup-youtube-cookies.sh", message)


class DurationNormalizationTests(SimpleTestCase):
    def test_none_becomes_zero(self):
        self.assertEqual(normalize_duration(None), 0)

    def test_invalid_becomes_zero(self):
        self.assertEqual(normalize_duration("not-a-number"), 0)


class PickDownloadedVideoFileTests(SimpleTestCase):
    def test_picks_video_extension_and_ignores_sidecar(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sidecar = root / "abc123_info.json"
            video = root / "abc123_title.mp4"
            sidecar.write_text("{}")
            video.write_bytes(b"video")

            picked = pick_downloaded_video_file(root, "abc123")
            self.assertEqual(picked, video)


class DownloadYouTubeVideoTests(SimpleTestCase):
    def test_rejects_invalid_url_before_yt_dlp(self):
        success, path, info, error = download_youtube_video(
            "https://example.com/evil", "/tmp"
        )
        self.assertFalse(success)
        self.assertIn("Invalid", error)

    @patch("video_processor.youtube_utils._download_with_opts")
    @patch(
        "video_processor.youtube_utils.youtube_cookies_configured", return_value=False
    )
    def test_retries_next_client_on_bot_check(self, _configured, mock_download):
        mock_download.side_effect = [
            yt_dlp.utils.DownloadError("Sign in to confirm you're not a bot"),
            ("video.mp4", {"title": "Test", "duration": 10, "video_id": "abc"}),
        ]
        with TemporaryDirectory() as tmpdir:
            success, path, info, error = download_youtube_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ", tmpdir
            )
        self.assertTrue(success)
        self.assertEqual(path, "video.mp4")
        self.assertEqual(mock_download.call_count, 2)


class PlaylistHandlerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="playlist-user", password="testpass123"
        )
        self.factory = RequestFactory()

    @patch("video_processor.views.extract_playlist_videos")
    def test_playlist_handler_respects_video_limit(self, mock_extract):
        from . import views
        from .models import VideoJob

        urls = [f"https://youtu.be/id{i}" for i in range(15)]
        mock_extract.return_value = (urls, "Test Playlist")

        request = self.factory.post("/upload/")
        request.user = self.user

        with patch("video_processor.views.messages"):
            response = views.handle_youtube_playlist(
                request, "https://www.youtube.com/playlist?list=PLtest"
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            VideoJob.objects.filter(user=self.user).count(), MAX_PLAYLIST_VIDEOS
        )


class YouTubeUploadValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="youtube-user", password="testpass123"
        )
        self.factory = RequestFactory()

    def test_rejects_invalid_youtube_url(self):
        from . import views
        from .models import VideoJob

        request = self.factory.post("/upload/")
        request.user = self.user

        with patch("video_processor.views.messages") as mock_messages:
            response = views.handle_youtube_upload(
                request, "https://example.com/not-youtube"
            )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(VideoJob.objects.filter(user=self.user).exists())
        mock_messages.error.assert_called_once()
