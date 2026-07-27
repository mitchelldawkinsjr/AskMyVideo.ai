from django.urls import include, path

from . import api, views

urlpatterns = [
    # PWA
    path("offline/", views.offline_view, name="offline"),
    path("manifest.webmanifest", views.pwa_manifest, name="pwa_manifest"),
    path("sw.js", views.pwa_service_worker, name="pwa_service_worker"),
    # Pages
    path("", views.home_view, name="home"),
    path("search/", views.search_videos, name="search_videos"),
    path(
        "search/<str:username>/",
        views.public_user_search_interface,
        name="public_user_search",
    ),
    path("library/", views.VideoLibraryView.as_view(), name="video_library"),
    path("upload/", views.upload_video, name="upload_video"),
    path("delete/<str:job_id>/", views.delete_video, name="delete_video"),
    path("transcript/<str:job_id>/", views.transcript_editor, name="transcript_editor"),
    # Authentication
    path("accounts/", include("django.contrib.auth.urls")),
    path("accounts/register/", views.register_view, name="register"),
    # Search + Ask APIs
    path("api/search/", api.api_search, name="api_search"),
    path("api/ask/", api.api_ask, name="api_ask"),
    # Playback
    path("api/video/<str:job_id>/", api.api_video_details, name="api_video_details"),
    path("video-file/<str:job_id>/", api.video_file_serve, name="video_file_serve"),
    # Health checks
    path("health/", api.health_check, name="health_check"),
    path("api/health/", api.api_health_check, name="api_health_check"),
    # Maintenance (owner-scoped)
    path("api/search-status/", api.api_search_status, name="api_search_status"),
    path("api/pending-jobs/", api.api_pending_jobs, name="api_pending_jobs"),
    path("api/detailed-stats/", api.api_detailed_stats, name="api_detailed_stats"),
    path(
        "api/rebuild-search-index/",
        api.api_rebuild_search_index,
        name="api_rebuild_search_index",
    ),
    path("api/cleanup-youtube/", api.api_cleanup_youtube, name="api_cleanup_youtube"),
    path("api/retry-job/", api.api_retry_job, name="api_retry_job"),
    path(
        "api/video/<uuid:job_id>/update-metadata/",
        api.api_update_video_metadata,
        name="api_update_video_metadata",
    ),
]
