"""
Video / document job processing.

Runs in the dedicated worker process (``manage.py process_jobs``); the web
process only creates PENDING jobs. The Whisper model is loaded lazily so web
workers importing this module never pay for it.
"""

import logging
import time
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .models import ContentKind, JobStatus, VideoJob
from .search_helpers import index_video
from .youtube_utils import download_youtube_video

logger = logging.getLogger(__name__)

_processor = None


def get_processor():
    """Create the CoreVideoProcessor (and load Whisper) on first use."""
    global _processor
    if _processor is None:
        from core_video_processor import CoreVideoProcessor

        _processor = CoreVideoProcessor()
    return _processor


def _fail_job(job, message):
    job.status = JobStatus.FAILED
    job.error_message = message
    job.completed_at = timezone.now()
    job.save()
    logger.error("Job %s failed: %s", job.job_id, message)


def _process_document_job(job):
    """Extract text from a PDF and store it in transcription JSON."""
    from pdf_processor import extract_pdf_content

    start_time = time.time()
    result = extract_pdf_content(job.video_path)

    job.metadata = result.get("metadata")
    job.transcription = result.get("transcription")
    job.processing_errors = result.get("processing_errors", [])
    job.processing_time = time.time() - start_time
    job.completed_at = timezone.now()
    job.content_kind = ContentKind.DOCUMENT

    if result.get("processing_errors"):
        job.status = JobStatus.FAILED
        job.error_message = "; ".join(result["processing_errors"])
        logger.error("Document processing failed: %s", job.error_message)
    else:
        job.status = JobStatus.COMPLETED
        logger.info(
            "Document processing completed in %.2fs (%d words, %d pages)",
            job.processing_time,
            job.word_count,
            (job.metadata or {}).get("page_count", 0),
        )
    job.save()


def _process_media_job(job):
    """Transcribe video/audio with Whisper."""
    start_time = time.time()
    result = get_processor().create_comprehensive_media_summary(job.video_path)

    job.metadata = result.get("metadata")
    job.transcription = result.get("transcription")
    job.processing_errors = result.get("processing_errors", [])
    job.processing_time = time.time() - start_time
    job.completed_at = timezone.now()

    if not job.content_kind or job.content_kind == ContentKind.VIDEO:
        job.content_kind = VideoJob.infer_content_kind(
            job.video_path, youtube_url=job.youtube_url
        )

    if result.get("processing_errors"):
        job.status = JobStatus.FAILED
        job.error_message = "; ".join(result["processing_errors"])
        logger.error("Processing failed: %s", job.error_message)
    else:
        job.status = JobStatus.COMPLETED
        logger.info(
            "Processing completed in %.2fs (%d words, language %s)",
            job.processing_time,
            job.word_count,
            job.language,
        )
    job.save()


def process_video_job(job_id):
    """Download (if YouTube), extract/transcribe, and index a single job."""
    close_old_connections()
    try:
        job = VideoJob.objects.get(job_id=job_id)
        logger.info("Processing content: %s", job.video_name)

        job.status = JobStatus.PROCESSING
        job.started_at = timezone.now()
        job.save()

        if job.youtube_url and not job.video_path:
            media_videos_dir = Path(settings.MEDIA_ROOT) / "videos"
            success, video_path, video_info, error_msg = download_youtube_video(
                job.youtube_url, media_videos_dir
            )
            if not success:
                _fail_job(job, f"Failed to download YouTube video: {error_msg}")
                return
            job.video_path = video_path
            job.video_name = video_info.get("title", job.video_name)
            job.file_size_bytes = Path(video_path).stat().st_size
            job.content_kind = ContentKind.VIDEO
            job.save()

        if not job.video_path:
            _fail_job(job, "No file available for processing")
            return

        if job.is_document or job.content_kind == ContentKind.DOCUMENT:
            _process_document_job(job)
        else:
            _process_media_job(job)

        if job.status == JobStatus.COMPLETED:
            try:
                from semantic_search import search_engine

                windows = index_video(search_engine, job)
                logger.info("Indexed %d windows for %s", windows, job.video_name)
            except Exception as index_error:
                logger.warning(
                    "Failed to index content after processing: %s", index_error
                )

    except Exception as exc:
        logger.exception("Error processing job %s", job_id)
        try:
            job = VideoJob.objects.get(job_id=job_id)
            _fail_job(job, str(exc))
        except Exception:
            pass
    finally:
        close_old_connections()


def requeue_stale_processing_jobs(max_age_minutes=120):
    """
    Reset PROCESSING jobs that were orphaned by a crash/restart back to
    PENDING so the worker retries them. Returns the number requeued.
    """
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(minutes=max_age_minutes)
    stale = VideoJob.objects.filter(status=JobStatus.PROCESSING, updated_at__lt=cutoff)
    count = stale.update(status=JobStatus.PENDING, started_at=None)
    if count:
        logger.warning("Requeued %d stale processing job(s)", count)
    return count
