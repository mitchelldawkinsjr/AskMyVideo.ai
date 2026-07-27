"""JSON API endpoints. All POST endpoints are CSRF-protected; the pages that
call them set the CSRF cookie and send X-CSRFToken."""

import json
import logging
import os

import requests
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from semantic_search import search_engine

from .models import JobStatus, VideoJob, VideoSearchQuery
from .search_helpers import (
    format_timestamp,
    rebuild_semantic_search_index,
    resolve_target_user,
    run_search,
)
from .views import get_media_content_type

logger = logging.getLogger(__name__)


def _search_scope(request, data):
    """
    Resolve which user's videos a search request may see.

    Returns (user, error_response). Public requests must name a user via
    ``username``; authenticated requests without one search their own videos.
    """
    username = (data.get("username") or "").strip()
    target_user = resolve_target_user(username)
    if target_user is False:
        return None, JsonResponse({"error": "User not found"}, status=404)
    if target_user is not None:
        return target_user, None
    if not request.user.is_authenticated:
        return None, JsonResponse({"error": "Authentication required"}, status=401)
    return request.user, None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@require_http_methods(["POST"])
def api_search(request):
    """Search one user's video transcripts (keyword / semantic / hybrid)."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    query = (data.get("query") or "").strip()
    if not query:
        return JsonResponse({"error": "Query is required"}, status=400)

    scope_user, error = _search_scope(request, data)
    if error:
        return error

    search_mode = data.get("search_mode", data.get("mode", "hybrid"))
    results, search_mode = run_search(query, search_mode, search_engine, scope_user)

    VideoSearchQuery.objects.create(query=query, results_count=len(results))

    return JsonResponse(
        {
            "success": True,
            "results": results,
            "query": query,
            "search_mode": search_mode,
            "mode": search_mode,
            "total_results": len(results),
            "count": len(results),
        }
    )


# ---------------------------------------------------------------------------
# Ask (RAG question answering)
# ---------------------------------------------------------------------------

ASK_SYSTEM_PROMPT = (
    "You answer questions about the user's content vault using ONLY the "
    "excerpts provided. Each excerpt is numbered like [1] with its title and "
    "locator (timestamp or page). Cite the excerpts you used inline, e.g. [2]. "
    "If the excerpts don't contain the answer, say so plainly. Be concise."
)


def _call_openai(messages):
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "Ask is not configured (OPENAI_API_KEY is not set)."
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages, "temperature": 0.2},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"], None
    except requests.RequestException as exc:
        logger.error("OpenAI request failed: %s", exc)
        return None, "The answer service is temporarily unavailable."


@require_http_methods(["POST"])
def api_ask(request):
    """Answer a question from the user's vault, citing sources with locators."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    question = (data.get("question") or data.get("query") or "").strip()
    if not question:
        return JsonResponse({"error": "Question is required"}, status=400)

    scope_user, error = _search_scope(request, data)
    if error:
        return error

    if not search_engine.ensure_ready():
        return JsonResponse(
            {"error": "Search index is not ready yet. Process some content first."},
            status=503,
        )

    hits = search_engine.semantic_search(question, top_k=8, user_id=scope_user.id)
    if not hits:
        return JsonResponse(
            {
                "success": True,
                "answer": "I couldn't find anything in your content related to that question.",
                "sources": [],
            }
        )

    excerpt_lines = []
    sources = []
    for i, hit in enumerate(hits, start=1):
        content_kind = getattr(hit, "content_kind", None) or "video"
        page = getattr(hit, "page", None)
        if content_kind == "document" or page is not None:
            locator = f"page {page or 1}"
            start_time = None
        else:
            locator = format_timestamp(hit.start_time)
            start_time = hit.start_time
        excerpt_lines.append(f'[{i}] "{hit.video_name}" at {locator}: {hit.text}')
        source = {
            "ref": i,
            "video_id": hit.job_id,
            "job_id": hit.job_id,
            "video_name": hit.video_name,
            "title": hit.video_name,
            "content_kind": content_kind,
            "page": page,
            "start_time": start_time,
            "timestamp": locator,
            "locator_label": locator,
            "text": hit.text,
        }
        sources.append(source)

    answer, error_message = _call_openai(
        [
            {"role": "system", "content": ASK_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Excerpts:\n"
                + "\n\n".join(excerpt_lines)
                + f"\n\nQuestion: {question}",
            },
        ]
    )
    if answer is None:
        return JsonResponse({"error": error_message}, status=503)

    return JsonResponse({"success": True, "answer": answer, "sources": sources})


# ---------------------------------------------------------------------------
# Content details / playback
# ---------------------------------------------------------------------------


def api_video_details(request, job_id):
    """Playback/preview metadata for a vault item (player + PDF viewer)."""
    try:
        video = VideoJob.objects.get(job_id=job_id)
    except (VideoJob.DoesNotExist, ValueError):
        # ValueError covers malformed UUIDs in the URL.
        return JsonResponse({"error": "Content not found"}, status=404)

    youtube_id = video.get_youtube_video_id()
    if video.is_document:
        media_type = "document"
    elif video.is_audio_file:
        media_type = "audio"
    else:
        media_type = "video"

    video_data = {
        "job_id": str(video.job_id),
        "video_name": video.video_name,
        "title": video.title or video.video_name,
        "status": video.status,
        "content_kind": video.content_kind,
        "media_type": media_type,
        "content_type": (
            get_media_content_type(video.video_path) if video.video_path else None
        ),
        "duration_seconds": video.duration_seconds,
        "page_count": (video.metadata or {}).get("page_count"),
        "file_url": f"/video-file/{video.job_id}/" if video.file_available else None,
        "is_youtube": youtube_id is not None,
        "youtube_video_id": youtube_id,
    }
    if youtube_id:
        video_data["youtube_url"] = f"https://www.youtube.com/watch?v={youtube_id}"
        video_data["youtube_watch_url"] = video_data["youtube_url"]
        video_data["youtube_embed_url"] = f"https://www.youtube.com/embed/{youtube_id}"
    return JsonResponse(video_data)


def video_file_serve(request, job_id):
    """Stream the media file, or explain how to play it if it was cleaned up."""
    video_job = get_object_or_404(VideoJob, job_id=job_id)

    if not video_job.file_available or not os.path.exists(video_job.video_path):
        youtube_id = video_job.get_youtube_video_id()
        response_data = {
            "error": "Video file has been cleaned up to save storage",
            "video_name": video_job.video_name,
            "duration_seconds": video_job.duration_seconds,
            "cleaned_up": True,
            "is_youtube": youtube_id is not None,
            "youtube_video_id": youtube_id,
        }
        if youtube_id:
            response_data["youtube_url"] = (
                f"https://www.youtube.com/watch?v={youtube_id}"
            )
            response_data["youtube_watch_url"] = response_data["youtube_url"]
            response_data["youtube_embed_url"] = (
                f"https://www.youtube.com/embed/{youtube_id}"
            )
        return JsonResponse(response_data, status=404)

    response = FileResponse(
        open(video_job.video_path, "rb"),
        content_type=get_media_content_type(video_job.video_path),
    )
    response["Accept-Ranges"] = "bytes"
    response["Content-Length"] = os.path.getsize(video_job.video_path)
    return response


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def health_check(request):
    """Health check endpoint for load balancers and monitoring."""
    try:
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse(
            {
                "status": "healthy",
                "timestamp": timezone.now().isoformat(),
                "version": "1.0.0",
            }
        )
    except Exception as exc:
        return JsonResponse(
            {
                "status": "unhealthy",
                "error": str(exc),
                "timestamp": timezone.now().isoformat(),
            },
            status=503,
        )


def api_health_check(request):
    """Detailed health check including search engine status."""
    health_status = {
        "status": "healthy",
        "timestamp": timezone.now().isoformat(),
        "version": "1.0.0",
        "components": {},
    }
    overall_healthy = True

    try:
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        health_status["components"]["database"] = "available"
    except Exception as exc:
        health_status["components"]["database"] = f"unavailable: {exc}"
        overall_healthy = False

    try:
        stats = search_engine.get_stats()
        if not stats["is_available"]:
            health_status["components"]["search_engine"] = "unavailable"
        elif stats["is_initialized"]:
            health_status["components"]["search_engine"] = "available_initialized"
        else:
            health_status["components"]["search_engine"] = "available_not_initialized"
    except Exception as exc:
        health_status["components"]["search_engine"] = f"unavailable: {exc}"

    if not overall_healthy:
        health_status["status"] = "unhealthy"
        return JsonResponse(health_status, status=503)
    return JsonResponse(health_status)


# ---------------------------------------------------------------------------
# Admin / maintenance (owner-scoped)
# ---------------------------------------------------------------------------


@login_required
def api_search_status(request):
    """Search engine status for the library admin panel."""
    try:
        search_engine.ensure_ready()
        stats = search_engine.get_stats()
        return JsonResponse(
            {
                "available": stats["is_available"],
                "initialized": stats["is_initialized"],
                "model_name": stats["model_name"],
                "indexed_segments": stats["total_segments"],
            }
        )
    except Exception as exc:
        logger.error("Error in api_search_status: %s", exc)
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
@require_http_methods(["POST"])
def api_rebuild_search_index(request):
    """Rebuild the semantic search index from all completed videos."""
    try:
        if not VideoJob.objects.filter(status=JobStatus.COMPLETED).exists():
            return JsonResponse(
                {"success": False, "error": "No completed videos to index"}
            )
        segments_indexed = rebuild_semantic_search_index(search_engine)
        return JsonResponse(
            {
                "success": True,
                "message": f"Search index rebuilt successfully with {segments_indexed} segments",
                "segments_indexed": segments_indexed,
                "initialized": search_engine.is_initialized,
            }
        )
    except Exception as exc:
        logger.error("Error rebuilding search index: %s", exc)
        return JsonResponse({"success": False, "error": str(exc)}, status=500)


@login_required
@require_http_methods(["POST"])
def api_cleanup_youtube(request):
    """Delete downloaded YouTube media files (transcripts stay searchable)."""
    try:
        dry_run = request.POST.get("dry_run", "false").lower() == "true"

        candidates = VideoJob.objects.filter(
            user=request.user,
            status=JobStatus.COMPLETED,
            file_removed_at__isnull=True,
        ).exclude(video_path="")

        files_processed = 0
        space_freed_mb = 0.0
        files_info = []
        for video in candidates:
            if not video.is_youtube_video() or not os.path.exists(video.video_path):
                continue
            file_size_mb = round(os.path.getsize(video.video_path) / (1024 * 1024), 2)
            files_info.append(
                {"name": os.path.basename(video.video_path), "size_mb": file_size_mb}
            )
            if not dry_run:
                os.remove(video.video_path)
                video.file_removed_at = timezone.now()
                video.save(update_fields=["file_removed_at", "updated_at"])
            files_processed += 1
            space_freed_mb += file_size_mb

        return JsonResponse(
            {
                "success": True,
                "files_processed": files_processed,
                "space_freed_mb": round(space_freed_mb, 2),
                "files": files_info[:10],
            }
        )
    except Exception as exc:
        logger.error("Error in api_cleanup_youtube: %s", exc)
        return JsonResponse({"success": False, "error": str(exc)}, status=500)


@login_required
def api_pending_jobs(request):
    """The requesting user's pending jobs."""
    jobs_data = [
        {
            "job_id": str(job.job_id),
            "video_name": job.video_name,
            "file_size_mb": (
                round(job.file_size_bytes / (1024 * 1024), 2)
                if job.file_size_bytes
                else 0
            ),
            "created_at": job.created_at.isoformat(),
        }
        for job in VideoJob.objects.filter(
            user=request.user, status=JobStatus.PENDING
        ).order_by("-created_at")[:20]
    ]
    return JsonResponse({"count": len(jobs_data), "pending_jobs": jobs_data})


@login_required
@require_http_methods(["POST"])
def api_retry_job(request):
    """Requeue a failed or stuck job so the worker retries it."""
    job_id = request.POST.get("job_id")
    if not job_id:
        return JsonResponse({"success": False, "error": "Job ID required"})
    try:
        job = VideoJob.objects.get(job_id=job_id, user=request.user)
    except (VideoJob.DoesNotExist, ValueError):
        return JsonResponse(
            {"success": False, "error": "Job not found or access denied"}
        )

    if job.status == JobStatus.COMPLETED:
        return JsonResponse({"success": False, "error": "Job already completed"})
    job.status = JobStatus.PENDING
    job.started_at = None
    job.error_message = None
    job.save()
    return JsonResponse(
        {
            "success": True,
            "message": f"'{job.video_name}' requeued for processing",
            "job_id": job_id,
        }
    )


@login_required
def api_detailed_stats(request):
    """Dashboard statistics for the requesting user's videos."""
    try:
        user_videos = VideoJob.objects.filter(user=request.user)
        completed = user_videos.filter(status=JobStatus.COMPLETED)
        status_counts = dict(
            user_videos.values_list("status").annotate(count=Count("job_id"))
        )
        seven_days_ago = timezone.now() - timezone.timedelta(days=7)

        processing_agg = completed.aggregate(total=Sum("processing_time"))
        total_processing_time = processing_agg["total"] or 0
        completed_count = status_counts.get(JobStatus.COMPLETED, 0)

        total_duration_seconds = sum(job.duration_seconds or 0 for job in completed)
        total_words = sum(job.word_count or 0 for job in completed)
        total_file_size_bytes = (
            user_videos.aggregate(total=Sum("file_size_bytes"))["total"] or 0
        )

        search_stats = search_engine.get_stats()

        return JsonResponse(
            {
                "video_stats": {
                    "total_videos": completed_count,
                    "pending_jobs": status_counts.get(JobStatus.PENDING, 0),
                    "processing_jobs": status_counts.get(JobStatus.PROCESSING, 0),
                    "failed_jobs": status_counts.get(JobStatus.FAILED, 0),
                    "recent_jobs_7_days": user_videos.filter(
                        created_at__gte=seven_days_ago
                    ).count(),
                },
                "processing_stats": {
                    "total_processing_time": round(total_processing_time, 1),
                    "average_processing_time": (
                        round(total_processing_time / completed_count, 1)
                        if completed_count
                        else 0
                    ),
                    "total_duration_hours": round(total_duration_seconds / 3600, 1),
                },
                "content_stats": {"total_words": total_words},
                "storage_stats": {
                    "total_file_size_gb": round(total_file_size_bytes / (1024**3), 2)
                },
                "search_stats": {
                    "indexed_segments": search_stats["total_segments"],
                    "is_initialized": search_stats["is_initialized"],
                    "model_name": search_stats["model_name"],
                },
            }
        )
    except Exception as exc:
        logger.error("Error getting detailed stats: %s", exc)
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
@require_http_methods(["POST"])
def api_update_video_metadata(request, job_id):
    """Update a video's title / YouTube URL (owner only)."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    try:
        video = VideoJob.objects.get(job_id=job_id, user=request.user)
    except VideoJob.DoesNotExist:
        return JsonResponse({"error": "Video not found"}, status=404)

    title = (data.get("title") or "").strip()
    youtube_url = (data.get("youtube_url") or "").strip()
    if title:
        video.video_name = title
        video.title = title
    video.youtube_url = youtube_url or None
    video.save()
    return JsonResponse({"success": True})
