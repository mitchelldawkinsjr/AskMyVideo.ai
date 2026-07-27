from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from .models import JobStatus, VideoJob


class ProcessVideoJobTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="worker-user", password="testpass123"
        )
        self.job = VideoJob.objects.create(
            user=self.user,
            video_path="/tmp/test.mp4",
            video_name="test.mp4",
            status=JobStatus.PENDING,
        )

    @patch("video_processor.jobs.get_processor")
    def test_marks_job_completed_on_success(self, mock_get_processor):
        from . import jobs

        mock_processor = MagicMock()
        mock_get_processor.return_value = mock_processor
        mock_processor.create_comprehensive_media_summary.return_value = {
            "metadata": {"duration": 10},
            "transcription": {"text": "hello"},
            "processing_errors": [],
        }

        jobs.process_video_job(str(self.job.job_id))

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.COMPLETED)
        self.assertIsNotNone(self.job.completed_at)

    @patch("video_processor.jobs.get_processor")
    def test_marks_job_failed_on_processor_errors(self, mock_get_processor):
        from . import jobs

        mock_processor = MagicMock()
        mock_get_processor.return_value = mock_processor
        mock_processor.create_comprehensive_media_summary.return_value = {
            "metadata": {},
            "transcription": {},
            "processing_errors": ["ffmpeg failed"],
        }

        jobs.process_video_job(str(self.job.job_id))

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.FAILED)
        self.assertIn("ffmpeg failed", self.job.error_message)

    def test_requeue_stale_processing_jobs(self):
        from . import jobs

        self.job.status = JobStatus.PROCESSING
        self.job.save()

        requeued = jobs.requeue_stale_processing_jobs(max_age_minutes=0)

        self.assertEqual(requeued, 1)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.PENDING)
