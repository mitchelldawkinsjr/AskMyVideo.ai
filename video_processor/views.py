"""Page views (HTML). JSON endpoints live in api.py; job processing in jobs.py."""

import json
import logging
import mimetypes
import uuid
from pathlib import Path

import yt_dlp
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.db.models import Count, Sum
from django.http import FileResponse, JsonResponse
from django.contrib.staticfiles import finders
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import ListView

from core_video_processor import VideoFileValidator
from semantic_search import search_engine

from .models import ContentKind, JobStatus, VideoJob, VideoSearchQuery
from .search_helpers import group_api_results_for_display, run_search
from .youtube_utils import (
    MAX_PLAYLIST_VIDEOS,
    build_ytdlp_opts,
    is_youtube_playlist_url,
    is_youtube_video_url,
)

logger = logging.getLogger(__name__)

ALLOWED_UPLOAD_EXTENSIONS = (
    VideoFileValidator.SUPPORTED_MEDIA_FORMATS | VideoJob.DOCUMENT_EXTENSIONS
)

AUDIO_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


def get_media_content_type(file_path: str) -> str:
    """Return the HTTP content type for a media or document file path."""
    extension = Path(file_path).suffix.lower()
    if extension == ".pdf":
        return "application/pdf"
    if extension in AUDIO_CONTENT_TYPES:
        return AUDIO_CONTENT_TYPES[extension]
    guessed_type, _ = mimetypes.guess_type(file_path)
    return guessed_type or "video/mp4"


def user_library_stats(user):
    """Dashboard stats for one user's content, computed in the database."""
    videos = VideoJob.objects.filter(user=user)
    completed = videos.filter(status=JobStatus.COMPLETED)
    aggregates = completed.aggregate(
        total_videos=Count("job_id"),
        total_processing_time=Sum("processing_time"),
    )
    return {
        "total_videos": aggregates["total_videos"] or 0,
        "total_processing_time": aggregates["total_processing_time"] or 0,
        "total_words": sum(job.word_count for job in completed),
        "pending_jobs": videos.filter(status=JobStatus.PENDING).count(),
    }


# ---------------------------------------------------------------------------
# PWA (manifest + service worker at root, matching prod fasted_calendar_pwa)
# ---------------------------------------------------------------------------


def _pwa_static_file(*parts: str) -> Path:
    relative = "/".join(("video_processor", "pwa", *parts))
    found = finders.find(relative)
    if not found:
        raise FileNotFoundError(relative)
    return Path(found)


def pwa_manifest(request):
    """Web app manifest served at /manifest.webmanifest."""
    return FileResponse(
        _pwa_static_file("manifest.webmanifest").open("rb"),
        content_type="application/manifest+json",
    )


def pwa_service_worker(request):
    """Service worker served at /sw.js with root scope."""
    response = FileResponse(
        _pwa_static_file("sw.js").open("rb"),
        content_type="application/javascript",
    )
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


def offline_view(request):
    """Offline fallback page precached by the service worker."""
    return render(request, "video_processor/offline.html")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def register_view(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get("username")
            messages.success(request, f"Account created for {username}! Please log in.")
            return redirect("login")
    else:
        form = UserCreationForm()
    return render(request, "registration/register.html", {"form": form})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@ensure_csrf_cookie
def home_view(request):
    """Marketing landing for guests; search hub for authenticated users."""
    if request.user.is_authenticated:
        return render(request, "video_processor/search_interface.html")
    return render(
        request,
        "video_processor/landing.html",
        # The public demo searches this account's videos; without one the
        # demo section is hidden (anonymous search is always user-scoped).
        {"demo_username": getattr(settings, "DEMO_USERNAME", "")},
    )


class VideoLibraryView(LoginRequiredMixin, ListView):
    model = VideoJob
    template_name = "video_processor/library.html"
    context_object_name = "videos"
    login_url = reverse_lazy("login")

    def get_queryset(self):
        return VideoJob.objects.filter(user=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user_videos = self.get_queryset()
        status_counts = dict(
            user_videos.values_list("status").annotate(count=Count("job_id"))
        )
        context.update(
            {
                "video_count": user_videos.count(),
                "completed_count": status_counts.get(JobStatus.COMPLETED, 0),
                "processing_count": status_counts.get(JobStatus.PROCESSING, 0),
                "failed_count": status_counts.get(JobStatus.FAILED, 0),
                "stats": user_library_stats(self.request.user),
            }
        )
        return context


@login_required
def search_videos(request):
    """Form-POST search rendered into the library page, scoped to the user."""
    if request.method != "POST":
        return redirect("video_library")

    query = request.POST.get("query", "").strip()
    search_mode = request.POST.get("search_mode", "hybrid")
    if not query:
        return redirect("video_library")

    api_results, search_mode = run_search(
        query, search_mode, search_engine, user=request.user
    )
    search_results = group_api_results_for_display(api_results)

    VideoSearchQuery.objects.create(query=query, results_count=len(api_results))

    return render(
        request,
        "video_processor/library.html",
        {
            "videos": VideoJob.objects.filter(
                user=request.user, status=JobStatus.COMPLETED
            ),
            "search_results": search_results,
            "query": query,
            "search_mode": search_mode,
            "semantic_available": search_engine.ensure_ready(),
            "stats": user_library_stats(request.user),
        },
    )


@ensure_csrf_cookie
def public_user_search_interface(request, username):
    """Public search page over one user's videos."""
    try:
        target_user = User.objects.get(username=username)
    except User.DoesNotExist:
        return render(
            request, "video_processor/user_not_found.html", {"username": username}
        )
    return render(
        request,
        "video_processor/public_user_search.html",
        {
            "target_user": target_user,
            "username": username,
            "semantic_available": search_engine.ensure_ready(),
        },
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@login_required
def upload_video(request):
    """Handle media upload and processing (files, YouTube URLs, or playlists)."""
    if request.method == "GET":
        return render(
            request,
            "video_processor/upload.html",
            {
                "recent_videos": VideoJob.objects.filter(user=request.user).order_by(
                    "-created_at"
                )[:10],
            },
        )

    if request.method != "POST":
        return redirect("video_library")

    youtube_url = request.POST.get("youtube_url", "").strip()
    playlist_url = request.POST.get("playlist_url", "").strip()

    if youtube_url:
        if is_youtube_playlist_url(youtube_url):
            return handle_youtube_playlist(request, youtube_url)
        return handle_youtube_upload(request, youtube_url)

    if playlist_url:
        return handle_youtube_playlist(request, playlist_url)

    if "video" not in request.FILES:
        messages.error(request, "No media file provided")
        return redirect("video_library")

    video_file = request.FILES["video"]
    if not video_file.name:
        messages.error(request, "Invalid media file")
        return redirect("video_library")

    file_extension = Path(video_file.name).suffix.lower()
    if file_extension not in ALLOWED_UPLOAD_EXTENSIONS:
        supported = ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))
        messages.error(
            request,
            f"Unsupported file type '{file_extension}'. Supported formats: {supported}",
        )
        return redirect("video_library")

    try:
        content_kind = VideoJob.infer_content_kind(video_file.name)
        media_subdir = "documents" if content_kind == ContentKind.DOCUMENT else "videos"
        media_dir = Path(settings.MEDIA_ROOT) / media_subdir
        media_dir.mkdir(parents=True, exist_ok=True)

        unique_id = uuid.uuid4()
        file_path = media_dir / f"{unique_id}_{video_file.name}"
        with open(file_path, "wb+") as destination:
            for chunk in video_file.chunks():
                destination.write(chunk)

        VideoJob.objects.create(
            job_id=unique_id,
            user=request.user,
            video_path=str(file_path),
            video_name=video_file.name,
            file_size_bytes=video_file.size,
            content_kind=content_kind,
            status=JobStatus.PENDING,
        )
        messages.success(
            request, f'"{video_file.name}" uploaded and queued for processing'
        )
    except Exception as exc:
        logger.error("Error uploading media file: %s", exc)
        messages.error(request, f"Error uploading file: {exc}")

    return redirect("video_library")


def handle_youtube_upload(request, youtube_url):
    """Queue a single YouTube URL for download and processing."""
    if not is_youtube_video_url(youtube_url):
        messages.error(request, "Invalid YouTube video URL.")
        return redirect("video_library")

    VideoJob.objects.create(
        user=request.user,
        video_path="",
        video_name="YouTube Video (downloading...)",
        youtube_url=youtube_url,
        content_kind=ContentKind.VIDEO,
        status=JobStatus.PENDING,
    )
    messages.success(request, "YouTube video queued for download and processing.")
    messages.info(
        request,
        "Video will appear in your library as it is processed. This may take several minutes.",
    )
    return redirect("video_library")


def extract_playlist_videos(playlist_url):
    """Return (video_urls, playlist_title) for a YouTube playlist."""
    try:
        ydl_opts = build_ytdlp_opts(
            extract_flat=True,
            extractor_args={"youtube": {"skip": ["dash", "hls"]}},
        )
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            playlist_info = ydl.extract_info(playlist_url, download=False)

        if not playlist_info or "entries" not in playlist_info:
            return [], None

        video_urls = []
        for entry in playlist_info["entries"]:
            if entry and entry.get("id"):
                video_urls.append(f"https://www.youtube.com/watch?v={entry['id']}")
            elif entry and entry.get("url"):
                video_urls.append(entry["url"])
        return video_urls, playlist_info.get("title", "Unknown Playlist")
    except Exception as exc:
        logger.error("Error extracting playlist videos: %s", exc)
        return [], None


def handle_youtube_playlist(request, playlist_url):
    """Queue every video from a YouTube playlist (up to MAX_PLAYLIST_VIDEOS)."""
    if not is_youtube_playlist_url(playlist_url):
        messages.error(
            request,
            "Invalid YouTube playlist URL. Please provide a valid playlist link.",
        )
        return redirect("video_library")

    video_urls, playlist_title = extract_playlist_videos(playlist_url)
    if not video_urls:
        messages.error(
            request, "Could not extract videos from playlist. Please check the URL."
        )
        return redirect("video_library")

    jobs_created = 0
    for video_url in video_urls[:MAX_PLAYLIST_VIDEOS]:
        try:
            VideoJob.objects.create(
                user=request.user,
                video_path="",
                video_name="Playlist Video (downloading...)",
                youtube_url=video_url,
                content_kind=ContentKind.VIDEO,
                status=JobStatus.PENDING,
            )
            jobs_created += 1
        except Exception as exc:
            logger.error("Error creating job for video %s: %s", video_url, exc)

    if jobs_created:
        messages.success(
            request,
            f'YouTube playlist "{playlist_title or "Unknown"}" processed. '
            f"{jobs_created} videos queued for download and processing.",
        )
        messages.info(
            request,
            "Videos will appear in your library as they are processed. This may take several minutes.",
        )
    else:
        messages.error(request, "No videos could be processed from the playlist.")
    return redirect("video_library")


# ---------------------------------------------------------------------------
# Video management
# ---------------------------------------------------------------------------


@login_required
def transcript_editor(request, job_id):
    """View/edit a video's transcript text."""
    try:
        video = VideoJob.objects.get(job_id=job_id, user=request.user)
    except VideoJob.DoesNotExist:
        if (
            request.method != "GET"
            or request.headers.get("Content-Type") == "application/json"
        ):
            return JsonResponse(
                {"status": "error", "message": "Video not found or access denied."},
                status=404,
            )
        messages.error(request, "Video not found or access denied.")
        return redirect("video_library")

    if video.status != JobStatus.COMPLETED:
        if (
            request.method != "GET"
            or request.headers.get("Content-Type") == "application/json"
        ):
            return JsonResponse(
                {"status": "error", "message": "Video processing not completed yet."},
                status=400,
            )
        messages.error(request, "Video processing not completed yet.")
        return redirect("video_library")

    if request.method == "GET" and request.GET.get("format") == "json":
        return JsonResponse(
            {"status": "success", "transcript": video.transcription_text}
        )

    if request.method == "POST":
        try:
            data = json.loads(request.body)
            transcript = data.get("transcript", "")
            video.transcription = {
                **(video.transcription or {}),
                "text": transcript,
                "word_count": len(transcript.split()),
            }
            video.save()
            return JsonResponse({"status": "success"})
        except (json.JSONDecodeError, TypeError) as exc:
            return JsonResponse({"status": "error", "message": str(exc)}, status=400)

    return render(request, "video_processor/transcript_editor.html", {"video": video})


@login_required
def delete_video(request, job_id):
    """Delete a video, its file, and its search index entries."""
    if request.method != "POST":
        messages.error(request, "Invalid request method.")
        return redirect("video_library")

    try:
        video = VideoJob.objects.get(job_id=job_id, user=request.user)
    except VideoJob.DoesNotExist:
        messages.error(request, "Video not found or access denied.")
        return redirect("video_library")

    try:
        video_name = video.video_name
        if video.file_available:
            Path(video.video_path).unlink(missing_ok=True)
        video.delete()

        try:
            search_engine.remove_video(str(job_id))
        except Exception as index_error:
            logger.warning("Failed to remove video from search index: %s", index_error)

        messages.success(
            request, f'Video "{video_name}" has been successfully deleted.'
        )
    except Exception as exc:
        logger.error("Error deleting video %s: %s", job_id, exc)
        messages.error(request, f"Error deleting video: {exc}")

    return redirect("video_library")
