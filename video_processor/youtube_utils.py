"""YouTube URL validation and download helpers."""

import logging
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

logger = logging.getLogger(__name__)

# Matches the video ID in watch/short/embed URLs.
_YOUTUBE_URL_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)"
    r"([a-zA-Z0-9_-]{11})"
)
# Downloads are saved as "<id>_<title>.<ext>" (see download_youtube_video outtmpl).
_FILENAME_ID_RE = re.compile(r"^([a-zA-Z0-9_-]{11})_")


def extract_youtube_id(url: str | None) -> str | None:
    """Extract the 11-character video ID from a YouTube URL."""
    if not url:
        return None
    match = _YOUTUBE_URL_ID_RE.search(url)
    return match.group(1) if match else None


def extract_youtube_id_from_filename(filename: str | None) -> str | None:
    """Extract the video ID from a yt-dlp download filename (``<id>_<title>``)."""
    if not filename:
        return None
    match = _FILENAME_ID_RE.match(Path(filename).name)
    return match.group(1) if match else None


MAX_PLAYLIST_VIDEOS = 10
MAX_CONCURRENT_JOBS = 2
MAX_VIDEO_DURATION_SECONDS = 3600

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi"}

DEFAULT_COOKIES_PATH = Path("/app/secrets/youtube_cookies.txt")

DEFAULT_YT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)

# android skips cookie auth; web + EJS solver works with cookies on VPS.
PLAYER_CLIENT_CHAINS_WITH_COOKIES = (
    ["web"],
    ["mweb", "web"],
)
PLAYER_CLIENT_CHAINS_WITHOUT_COOKIES = (
    ["android", "web"],
    ["tv_embedded", "web"],
    ["ios", "web"],
    ["mweb", "web"],
)

WRITABLE_COOKIES_PATH = Path("/tmp/youtube_cookies_active.txt")


def resolve_cookies_path():
    """Return configured or default Netscape cookies file when present."""
    configured = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(DEFAULT_COOKIES_PATH)

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def youtube_cookies_configured():
    return resolve_cookies_path() is not None


def is_bot_check_error(message):
    lowered = (message or "").lower()
    return "sign in to confirm" in lowered or "not a bot" in lowered


def format_download_error(exc):
    """Return a concise, actionable error for UI/logs."""
    message = str(exc)
    if is_bot_check_error(message):
        if youtube_cookies_configured():
            return (
                "YouTube blocked the download even with cookies configured. "
                "Re-export fresh browser cookies to secrets/youtube_cookies.txt."
            )
        return (
            "YouTube blocked download from the server (bot check). "
            "Admin must configure browser cookies — see deploy/setup-youtube-cookies.sh."
        )
    return message


def _deno_runtime_path():
    return os.environ.get("YTDLP_DENO_PATH", "/usr/local/bin/deno")


def _cookiefile_for_ytdlp(source: Path) -> str:
    """yt-dlp writes cookie updates back; copy read-only secrets to /tmp."""
    if os.access(source, os.W_OK):
        return str(source)
    shutil.copy2(source, WRITABLE_COOKIES_PATH)
    return str(WRITABLE_COOKIES_PATH)


def build_ytdlp_opts(*, player_clients=None, **overrides):
    """Shared yt-dlp options for prod VPS downloads (bot checks, retries, cookies)."""
    has_cookies = youtube_cookies_configured()
    default_clients = (
        PLAYER_CLIENT_CHAINS_WITH_COOKIES[0]
        if has_cookies
        else PLAYER_CLIENT_CHAINS_WITHOUT_COOKIES[0]
    )
    clients = player_clients or default_clients
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "http_headers": {"User-Agent": DEFAULT_YT_USER_AGENT},
        "extractor_args": {"youtube": {"player_client": clients}},
        "remote_components": {"ejs:github"},
    }

    deno_path = _deno_runtime_path()
    if Path(deno_path).is_file():
        opts["js_runtimes"] = {"deno": {"path": deno_path}}

    cookie_path = resolve_cookies_path()
    if cookie_path:
        opts["cookiefile"] = _cookiefile_for_ytdlp(cookie_path)
        logger.info("Using YouTube cookies from %s", cookie_path)
    elif os.environ.get("YOUTUBE_COOKIES_FILE", "").strip():
        logger.warning(
            "YOUTUBE_COOKIES_FILE set but not found: %s",
            os.environ.get("YOUTUBE_COOKIES_FILE"),
        )

    extra_extractor = overrides.pop("extractor_args", None)
    if extra_extractor:
        youtube_args = opts.setdefault("extractor_args", {}).setdefault("youtube", {})
        youtube_args.update(extra_extractor.get("youtube", {}))

    opts.update(overrides)
    return opts


def normalize_duration(raw):
    """Return a numeric duration in seconds; missing/invalid values become 0."""
    if raw is None:
        return 0
    try:
        duration = float(raw)
    except (TypeError, ValueError):
        return 0
    return max(duration, 0)


def _parsed_host(url):
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    return (parsed.netloc or "").lower()


def is_youtube_host(url):
    host = _parsed_host(url)
    return host in YOUTUBE_HOSTS if host else False


def is_youtube_playlist_url(url):
    """Detect YouTube playlist URLs."""
    if not is_youtube_host(url):
        return False
    return "list=" in url


def is_youtube_video_url(url):
    """Allow single YouTube video URLs only (not playlists)."""
    if not url or not isinstance(url, str):
        return False
    if not is_youtube_host(url):
        return False
    if is_youtube_playlist_url(url):
        return False
    lowered = url.lower()
    if "/playlist" in lowered:
        return False
    return "watch?v=" in lowered or "youtu.be/" in lowered or "/shorts/" in lowered


def pick_downloaded_video_file(output_dir, video_id):
    """Pick the most likely video file for a yt-dlp download."""
    output_path = Path(output_dir)
    candidates = list(output_path.glob(f"{video_id}_*"))
    if not candidates:
        return None

    video_candidates = [
        path
        for path in candidates
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not video_candidates:
        return None

    video_candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return video_candidates[0]


def _download_with_opts(url, output_path, ydl_opts):
    probe_opts = dict(ydl_opts)
    probe_opts.pop("format", None)
    probe_opts.pop("outtmpl", None)
    probe_opts["ignore_no_formats_error"] = True

    with yt_dlp.YoutubeDL(probe_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise yt_dlp.utils.DownloadError("Could not extract video information")

        duration = normalize_duration(info.get("duration"))
        if duration > MAX_VIDEO_DURATION_SECONDS:
            raise yt_dlp.utils.DownloadError(
                f"Video too long: {duration // 60:.1f} minutes (max 60 minutes)"
            )

        video_id = info.get("id", "unknown")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

        video_path = pick_downloaded_video_file(output_path, video_id)
        if not video_path:
            raise yt_dlp.utils.DownloadError("Downloaded video file not found")

        return (
            str(video_path),
            {
                "title": info.get("title", "Unknown"),
                "duration": duration,
                "video_id": video_id,
            },
        )


def download_youtube_video(url, output_path):
    """Download a YouTube video using yt-dlp."""
    if not is_youtube_video_url(url):
        return False, None, None, "Invalid YouTube video URL"

    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    base_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": str(output_path / "%(id)s_%(title)s.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
    }

    last_error = None
    client_chains = (
        PLAYER_CLIENT_CHAINS_WITH_COOKIES
        if youtube_cookies_configured()
        else PLAYER_CLIENT_CHAINS_WITHOUT_COOKIES
    )

    for clients in client_chains:
        try:
            ydl_opts = build_ytdlp_opts(player_clients=clients, **base_opts)
            video_path, video_info = _download_with_opts(url, output_path, ydl_opts)
            return True, video_path, video_info, None
        except Exception as exc:
            last_error = exc
            if is_bot_check_error(str(exc)) and clients != client_chains[-1]:
                logger.warning(
                    "YouTube bot check with clients %s, trying next client chain",
                    clients,
                )
                continue
            break

    logger.error("Error downloading YouTube video: %s", last_error)
    return False, None, None, format_download_error(last_error)
