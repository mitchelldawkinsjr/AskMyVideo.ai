import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core_video_processor import (
    CoreVideoProcessor,
    ProcessingStatus,
    TranscriptionResult,
    VideoMetadata,
    is_audio_file,
)

from .models import JobStatus, VideoJob
from .views import get_media_content_type


class AudioFileDetectionTests(TestCase):
    def test_is_audio_file_detects_supported_extensions(self):
        self.assertTrue(is_audio_file("/tmp/recording.mp3"))
        self.assertTrue(is_audio_file("/tmp/recording.WAV"))
        self.assertFalse(is_audio_file("/tmp/video.mp4"))

    def test_video_job_is_audio_file_property(self):
        user = User.objects.create_user(username="audio-user", password="testpass123")
        audio_job = VideoJob.objects.create(
            user=user,
            video_path="/media/videos/test.mp3",
            video_name="test.mp3",
        )
        video_job = VideoJob.objects.create(
            user=user,
            video_path="/media/videos/test.mp4",
            video_name="test.mp4",
        )

        self.assertTrue(audio_job.is_audio_file)
        self.assertFalse(video_job.is_audio_file)


class MediaContentTypeTests(TestCase):
    def test_get_media_content_type_for_audio(self):
        self.assertEqual(get_media_content_type("song.mp3"), "audio/mpeg")
        self.assertEqual(get_media_content_type("clip.wav"), "audio/wav")
        self.assertEqual(get_media_content_type("podcast.m4a"), "audio/mp4")


class AudioProcessorTests(TestCase):
    def test_create_comprehensive_media_summary_transcribes_audio_directly(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_file:
            temp_file.write(b"fake audio content")
            temp_path = temp_file.name

        try:
            processor = CoreVideoProcessor()
            mock_metadata = VideoMetadata(
                file_path=temp_path,
                duration_seconds=12.5,
                width_pixels=0,
                height_pixels=0,
                frames_per_second=0.0,
                format_extension="mp3",
                file_size_bytes=18,
                creation_timestamp=datetime.now(),
            )

            with (
                patch.object(
                    processor.transcription_service,
                    "whisper_available",
                    True,
                ),
                patch.object(
                    processor,
                    "extract_audio_file_metadata",
                    return_value=mock_metadata,
                ),
                patch.object(
                    processor,
                    "transcribe_audio_file",
                    return_value=TranscriptionResult(
                        status=ProcessingStatus.SUCCESS,
                        transcribed_text="hello audio",
                        text_segments=[],
                        detected_language="en",
                    ),
                ) as mock_transcribe,
                patch.object(
                    processor,
                    "transcribe_video_file",
                ) as mock_transcribe_video,
                patch.object(
                    processor,
                    "extract_audio_from_video",
                ) as mock_extract_audio,
            ):
                result = processor.create_comprehensive_media_summary(temp_path)

            self.assertEqual(result["transcription"]["text"], "hello audio")
            mock_transcribe.assert_called_once_with(temp_path, "auto")
            mock_transcribe_video.assert_not_called()
            mock_extract_audio.assert_not_called()
            self.assertEqual(result["processing_errors"], [])
        finally:
            Path(temp_path).unlink(missing_ok=True)


class AudioUploadViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="upload-user", password="testpass123"
        )
        self.client.login(username="upload-user", password="testpass123")

    def test_upload_accepts_mp3_file(self):
        upload = SimpleUploadedFile(
            "podcast.mp3", b"fake mp3 bytes", content_type="audio/mpeg"
        )
        response = self.client.post(
            reverse("upload_video"),
            {"video": upload},
        )

        self.assertEqual(response.status_code, 302)
        job = VideoJob.objects.get(user=self.user, video_name="podcast.mp3")
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.content_kind, "audio")

    def test_upload_rejects_unsupported_extension(self):
        upload = SimpleUploadedFile(
            "notes.txt", b"not media", content_type="text/plain"
        )
        response = self.client.post(
            reverse("upload_video"),
            {"video": upload},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(VideoJob.objects.filter(video_name="notes.txt").exists())


class AudioApiDetailsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="api-user", password="testpass123"
        )
        self.audio_job = VideoJob.objects.create(
            user=self.user,
            video_path="/media/videos/sample.mp3",
            video_name="sample.mp3",
            status=JobStatus.COMPLETED,
        )

    def test_api_video_details_includes_audio_media_type(self):
        response = self.client.get(
            reverse("api_video_details", kwargs={"job_id": str(self.audio_job.job_id)})
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["media_type"], "audio")
        self.assertEqual(data["content_type"], "audio/mpeg")
