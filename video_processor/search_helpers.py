"""Search execution, result formatting, and index maintenance helpers."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from django.contrib.auth.models import User

from .models import JobStatus, VideoJob

logger = logging.getLogger(__name__)

# Rank constant for reciprocal rank fusion; 60 is the standard from the RRF paper.
RRF_K = 60


def clean_video_name(video_name: str | None) -> str:
    if not video_name:
        return "Unknown"
    name_without_ext = video_name.rsplit(".", 1)[0] if "." in video_name else video_name
    cleaned_name = re.sub(r"_[a-zA-Z0-9_-]{11}$", "", name_without_ext)
    return cleaned_name.strip() or "Content"


def format_timestamp(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def format_locator(result: dict[str, Any]) -> str:
    """Human-readable locator for a hit (page N or mm:ss)."""
    if result.get("page") is not None or result.get("content_kind") == "document":
        page = result.get("page") or 1
        return f"page {page}"
    return format_timestamp(result.get("start_time") or 0)


def resolve_target_user(username: str | None):
    """Return the User for a public search, None when absent, False when unknown."""
    if not username:
        return None
    try:
        return User.objects.get(username=username)
    except User.DoesNotExist:
        return False


# ---------------------------------------------------------------------------
# Keyword search (single implementation: VideoJob.search_segments)
# ---------------------------------------------------------------------------


def perform_keyword_search(query: str, user=None) -> list[dict[str, Any]]:
    """Scan transcripts for keyword matches, grouped per content item."""
    filters: dict[str, Any] = {
        "status": JobStatus.COMPLETED,
        "transcription__isnull": False,
    }
    if user is not None:
        filters["user"] = user
    search_results = []
    for video in VideoJob.objects.filter(**filters):
        matching_segments = video.search_segments(query)
        if matching_segments:
            search_results.append({"video": video, "segments": matching_segments})
    return search_results


def _flatten_keyword_results(keyword_results) -> list[dict[str, Any]]:
    flat = []
    for item in keyword_results:
        video = item["video"]
        for seg in item["segments"]:
            entry = {
                "job_id": str(video.job_id),
                "video_name": video.video_name,
                "start_time": seg["start_time"],
                "end_time": seg["end_time"],
                "text": seg["text"],
                "score": seg.get("relevance_score", 1.0),
                "search_type": "keyword",
                "content_kind": seg.get("content_kind") or video.content_kind,
            }
            if seg.get("page") is not None:
                entry["page"] = seg["page"]
            flat.append(entry)
    flat.sort(key=lambda r: r["score"], reverse=True)
    return flat


def _semantic_to_flat(semantic_results) -> list[dict[str, Any]]:
    flat = []
    for result in semantic_results:
        entry = {
            "job_id": result.job_id,
            "video_name": result.video_name,
            "start_time": result.start_time,
            "end_time": result.end_time,
            "text": result.text,
            "score": result.score,
            "search_type": result.search_type,
            "content_kind": getattr(result, "content_kind", None) or "video",
        }
        if getattr(result, "page", None) is not None:
            entry["page"] = result.page
        flat.append(entry)
    return flat


def _result_key(result: dict) -> tuple:
    page = result.get("page")
    if page is not None:
        return (result["job_id"], "page", int(page))
    return (result["job_id"], "time", round(result["start_time"], 1))


def _rrf_fuse(
    semantic_flat: list[dict], keyword_flat: list[dict], top_k: int
) -> list[dict[str, Any]]:
    """Combine two ranked lists with reciprocal rank fusion."""
    fused: dict[tuple, dict] = {}
    for ranked_list in (semantic_flat, keyword_flat):
        for rank, result in enumerate(ranked_list):
            key = _result_key(result)
            entry = fused.setdefault(
                key, {**result, "score": 0.0, "search_type": "hybrid"}
            )
            entry["score"] += 1.0 / (RRF_K + rank + 1)
    results = sorted(fused.values(), key=lambda r: r["score"], reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# Unified search entry point
# ---------------------------------------------------------------------------


def run_search(
    query: str,
    mode: str,
    search_engine,
    user: Optional[User] = None,
    top_k: int = 50,
) -> tuple[list[dict[str, Any]], str]:
    """
    Execute a search in ``keyword``, ``semantic``, or ``hybrid`` mode, always
    scoped to ``user`` when given. Returns (api_results, effective_mode).
    """
    user_id = user.id if user is not None else None
    try:
        engine_ready = bool(search_engine and search_engine.ensure_ready())

        if mode == "semantic" and engine_ready:
            semantic = search_engine.semantic_search(
                query, top_k=top_k, user_id=user_id
            )
            if semantic:
                return to_api_format(_semantic_to_flat(semantic)), "semantic"
            return (
                to_api_format(
                    _flatten_keyword_results(perform_keyword_search(query, user))
                ),
                "keyword_fallback",
            )

        if mode == "hybrid" and engine_ready:
            semantic = search_engine.semantic_search(
                query, top_k=top_k, user_id=user_id
            )
            keyword = _flatten_keyword_results(perform_keyword_search(query, user))
            fused = _rrf_fuse(_semantic_to_flat(semantic), keyword, top_k)
            return to_api_format(fused), "hybrid"

        effective = "keyword" if mode == "keyword" else "keyword_fallback"
        return (
            to_api_format(
                _flatten_keyword_results(perform_keyword_search(query, user))
            ),
            effective,
        )
    except Exception as exc:
        logger.error("Search error (%s): %s", mode, exc)
        return (
            to_api_format(
                _flatten_keyword_results(perform_keyword_search(query, user))
            ),
            "keyword_fallback",
        )


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


def to_api_format(flat_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flat result dicts -> JSON API shape used by the search frontends."""
    if not flat_results:
        return []
    max_score = max(result["score"] for result in flat_results) or 1.0
    api_results = []
    for result in flat_results:
        content_kind = result.get("content_kind") or "video"
        locator = "page" if content_kind == "document" else "time"
        entry = {
            "video_id": result["job_id"],
            "job_id": result["job_id"],
            "video_name": clean_video_name(result["video_name"]),
            "title": clean_video_name(result["video_name"]),
            "start_time": result["start_time"],
            "end_time": result["end_time"],
            "text": result["text"],
            "relevance_score": result["score"] / max_score,
            "search_type": result["search_type"],
            "timestamp_formatted": format_timestamp(result["start_time"]),
            "content_kind": content_kind,
            "locator": locator,
            "locator_label": format_locator(result),
        }
        if result.get("page") is not None:
            entry["page"] = result["page"]
        api_results.append(entry)
    return api_results


def group_api_results_for_display(api_results) -> list[dict[str, Any]]:
    """JSON API shape -> per-item grouping used by library.html."""
    video_groups: dict[str, dict] = {}
    for result in api_results:
        job_id = result["video_id"]
        if job_id not in video_groups:
            try:
                video = VideoJob.objects.get(job_id=job_id)
            except VideoJob.DoesNotExist:
                continue
            video_groups[job_id] = {"video": video, "segments": []}
        segment = {
            "start_time": result["start_time"],
            "end_time": result["end_time"],
            "text": result["text"],
            "relevance_score": result["relevance_score"],
            "search_type": result["search_type"],
            "content_kind": result.get("content_kind"),
            "locator_label": result.get("locator_label"),
        }
        if result.get("page") is not None:
            segment["page"] = result["page"]
        video_groups[job_id]["segments"].append(segment)
    return list(video_groups.values())


# ---------------------------------------------------------------------------
# Index maintenance
# ---------------------------------------------------------------------------


def video_index_payload(video: VideoJob) -> dict[str, Any]:
    return {
        "job_id": str(video.job_id),
        "video_name": video.video_name,
        "user_id": video.user_id,
        "segments": video.text_segments,
        "content_kind": video.content_kind,
    }


def index_video(search_engine, video: VideoJob) -> int:
    """Incrementally index one completed content item."""
    if not search_engine:
        return 0
    return search_engine.add_video(video_index_payload(video))


def rebuild_semantic_search_index(search_engine) -> int:
    """Full rebuild from every completed item (delete/edit/admin path)."""
    videos = [
        video_index_payload(video)
        for video in VideoJob.objects.filter(
            status=JobStatus.COMPLETED, transcription__isnull=False
        )
        if video.text_segments
    ]
    return search_engine.rebuild_index(videos)
