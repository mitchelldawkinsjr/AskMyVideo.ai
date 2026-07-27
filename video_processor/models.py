import uuid
from pathlib import Path

from django.contrib.auth.models import User
from django.db import models

from .youtube_utils import extract_youtube_id, extract_youtube_id_from_filename


class JobStatus(models.TextChoices):
    """Job status choices."""

    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class ContentKind(models.TextChoices):
    """Kind of vault content stored on a VideoJob row."""

    VIDEO = "video", "Video"
    AUDIO = "audio", "Audio"
    DOCUMENT = "document", "Document"


class VideoJob(models.Model):
    """A content vault item: video, audio, or document with searchable text."""

    AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac"}
    DOCUMENT_EXTENSIONS = {".pdf"}

    job_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)

    video_path = models.CharField(max_length=500, help_text="Path to the video file")
    video_name = models.CharField(max_length=500, help_text="Original video filename")
    youtube_url = models.URLField(
        null=True,
        blank=True,
        help_text="Original YouTube URL if this was a YouTube video",
    )
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    file_removed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the media file was deleted to reclaim storage",
    )
    content_kind = models.CharField(
        max_length=20,
        choices=ContentKind.choices,
        default=ContentKind.VIDEO,
        help_text="video, audio, or document",
    )

    status = models.CharField(
        max_length=20, choices=JobStatus.choices, default=JobStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    processing_time = models.FloatField(
        null=True, blank=True, help_text="Processing time in seconds"
    )

    error_message = models.TextField(null=True, blank=True)

    metadata = models.JSONField(null=True, blank=True, help_text="Video metadata")
    transcription = models.JSONField(
        null=True, blank=True, help_text="Transcription results"
    )
    processing_errors = models.JSONField(default=list, blank=True)

    title = models.CharField(max_length=200, blank=True)
    transcript = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.title or self.video_path}"

    @classmethod
    def infer_content_kind(cls, file_path: str = "", youtube_url: str = None) -> str:
        """Infer content_kind from a path extension or YouTube URL."""
        if youtube_url:
            return ContentKind.VIDEO
        extension = Path(file_path or "").suffix.lower()
        if extension in cls.DOCUMENT_EXTENSIONS:
            return ContentKind.DOCUMENT
        if extension in cls.AUDIO_EXTENSIONS:
            return ContentKind.AUDIO
        return ContentKind.VIDEO

    @property
    def is_audio_file(self):
        """Return True if this job references an audio file."""
        if self.content_kind == ContentKind.AUDIO:
            return True
        if not self.video_path:
            return False
        return Path(self.video_path).suffix.lower() in self.AUDIO_EXTENSIONS

    @property
    def is_document(self):
        """Return True if this job is a document (e.g. PDF)."""
        if self.content_kind == ContentKind.DOCUMENT:
            return True
        if not self.video_path:
            return False
        return Path(self.video_path).suffix.lower() in self.DOCUMENT_EXTENSIONS

    @property
    def locator_type(self):
        """How citations locate a passage: 'page' for documents, else 'time'."""
        return "page" if self.is_document else "time"

    @property
    def file_available(self):
        """Return True when the media file is still expected to be on disk."""
        return bool(self.video_path) and self.file_removed_at is None

    @property
    def duration_seconds(self):
        if self.metadata and "duration_seconds" in self.metadata:
            return self.metadata["duration_seconds"]
        return 0

    @property
    def resolution(self):
        if self.metadata:
            width = self.metadata.get("width_pixels", 0)
            height = self.metadata.get("height_pixels", 0)
            return f"{width}x{height}"
        return "Unknown"

    @property
    def transcription_text(self):
        if self.transcription and "text" in self.transcription:
            return self.transcription["text"]
        return ""

    @property
    def text_segments(self):
        if self.transcription and "text_segments" in self.transcription:
            return self.transcription["text_segments"]
        return []

    @property
    def word_count(self):
        if self.transcription and "word_count" in self.transcription:
            return self.transcription["word_count"]
        return 0

    @property
    def language(self):
        if self.transcription and "language" in self.transcription:
            return self.transcription["language"]
        return "unknown"

    def get_youtube_video_id(self):
        """The YouTube video ID, from the stored URL or the download filename."""
        return extract_youtube_id(self.youtube_url) or extract_youtube_id_from_filename(
            self.video_path
        )

    def is_youtube_video(self):
        return self.get_youtube_video_id() is not None

    def search_segments(self, query):
        """
        Score transcript segments against a keyword query.

        Returns segments sorted by relevance: exact-phrase matches score
        highest, then segments containing all query tokens, then (only when
        the full transcript contains all tokens) segments with partial hits.
        """
        if not self.text_segments or not query:
            return []

        matching_segments = []
        query_lower = query.lower().strip()
        tokens = [token for token in query_lower.split() if token]

        for segment in self.text_segments:
            text = segment.get("text", "").strip()
            text_lower = text.lower()
            if not text_lower:
                continue

            if query_lower in text_lower:
                count = text_lower.count(query_lower)
                position_score = 1.0 if text_lower.startswith(query_lower) else 0.5
                score = count * position_score * 2.0
            elif tokens and all(token in text_lower for token in tokens):
                score = float(sum(text_lower.count(token) for token in tokens))
            else:
                continue

            match = {
                "start_time": segment.get("start", 0),
                "end_time": segment.get("end", 0),
                "text": text,
                "relevance_score": score,
                "content_kind": self.content_kind,
            }
            if "page" in segment:
                match["page"] = segment["page"]
            matching_segments.append(match)

        if not matching_segments and len(tokens) > 1:
            full_text = self.transcription_text.lower()
            if all(token in full_text for token in tokens):
                for segment in self.text_segments:
                    text = segment.get("text", "").strip()
                    text_lower = text.lower()
                    if not text_lower:
                        continue
                    hit_tokens = [token for token in tokens if token in text_lower]
                    if not hit_tokens:
                        continue
                    score = float(sum(text_lower.count(token) for token in hit_tokens))
                    match = {
                        "start_time": segment.get("start", 0),
                        "end_time": segment.get("end", 0),
                        "text": text,
                        "relevance_score": score,
                        "content_kind": self.content_kind,
                    }
                    if "page" in segment:
                        match["page"] = segment["page"]
                    matching_segments.append(match)

        matching_segments.sort(key=lambda item: item["relevance_score"], reverse=True)
        return matching_segments


class VideoSearchQuery(models.Model):
    """Search query log for analytics."""

    query = models.CharField(max_length=255)
    results_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"'{self.query}' ({self.results_count} results)"
