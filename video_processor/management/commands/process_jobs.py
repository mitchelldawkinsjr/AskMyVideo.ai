"""
Dedicated job worker: polls for pending video jobs and processes them
sequentially. Run alongside the web server:

    python manage.py process_jobs            # poll forever
    python manage.py process_jobs --once     # drain the queue and exit
"""

import logging
import time

from django.core.management.base import BaseCommand

from video_processor.jobs import process_video_job, requeue_stale_processing_jobs
from video_processor.models import JobStatus, VideoJob

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process pending video jobs (transcription worker)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process currently pending jobs and exit.",
        )
        parser.add_argument(
            "--poll-interval",
            type=int,
            default=5,
            help="Seconds to sleep when the queue is empty (default: 5).",
        )

    def handle(self, *args, **options):
        # Jobs orphaned as "processing" by a previous crash are ours to retry.
        requeue_stale_processing_jobs(max_age_minutes=0)

        self.stdout.write("Worker started; waiting for pending jobs...")
        while True:
            job = (
                VideoJob.objects.filter(status=JobStatus.PENDING)
                .order_by("created_at")
                .first()
            )
            if job:
                self.stdout.write(f"Processing {job.job_id} ({job.video_name})")
                process_video_job(str(job.job_id))
            elif options["once"]:
                self.stdout.write("Queue empty; exiting.")
                return
            else:
                time.sleep(options["poll_interval"])
