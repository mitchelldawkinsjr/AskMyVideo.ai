"""Tests for PDF content vault ingest, search locators, and Ask citations."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from pdf_processor import extract_pdf_content
from semantic_search import SearchResult, build_document_windows
from video_processor.jobs import process_video_job
from video_processor.models import ContentKind, JobStatus, VideoJob
from video_processor.search_helpers import to_api_format
from video_processor.views import get_media_content_type


class ContentKindModelTests(TestCase):
    def test_infer_content_kind_from_extension(self):
        self.assertEqual(VideoJob.infer_content_kind("notes.pdf"), ContentKind.DOCUMENT)
        self.assertEqual(VideoJob.infer_content_kind("talk.mp3"), ContentKind.AUDIO)
        self.assertEqual(VideoJob.infer_content_kind("clip.mp4"), ContentKind.VIDEO)
        self.assertEqual(
            VideoJob.infer_content_kind("", youtube_url="https://youtu.be/abc"),
            ContentKind.VIDEO,
        )

    def test_document_helpers(self):
        user = User.objects.create_user(username="vault-user", password="testpass123")
        doc = VideoJob.objects.create(
            user=user,
            video_path="/media/documents/notes.pdf",
            video_name="notes.pdf",
            content_kind=ContentKind.DOCUMENT,
        )
        self.assertTrue(doc.is_document)
        self.assertEqual(doc.locator_type, "page")
        self.assertFalse(doc.is_audio_file)


class PdfProcessorTests(TestCase):
    def test_extract_pdf_content_with_pages(self):
        mock_page_1 = MagicMock()
        mock_page_1.extract_text.return_value = "Chapter one about leadership habits."
        mock_page_2 = MagicMock()
        mock_page_2.extract_text.return_value = "Chapter two about coaching teams."
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page_1, mock_page_2]

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
            temp_file.write(b"%PDF-1.4 fake")
            temp_path = temp_file.name

        try:
            with patch("pypdf.PdfReader", return_value=mock_reader):
                result = extract_pdf_content(temp_path)
        finally:
            Path(temp_path).unlink(missing_ok=True)

        self.assertEqual(result["processing_errors"], [])
        self.assertEqual(result["transcription"]["page_count"], 2)
        self.assertEqual(len(result["transcription"]["text_segments"]), 2)
        self.assertEqual(result["transcription"]["text_segments"][0]["page"], 1)
        self.assertIn("leadership", result["transcription"]["text"])

    def test_extract_pdf_content_empty_text_fails_clearly(self):
        mock_page = MagicMock()
        mock_page.extract_text.return_value = ""
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
            temp_file.write(b"%PDF-1.4 fake")
            temp_path = temp_file.name

        try:
            with patch("pypdf.PdfReader", return_value=mock_reader):
                result = extract_pdf_content(temp_path)
        finally:
            Path(temp_path).unlink(missing_ok=True)

        self.assertTrue(result["processing_errors"])
        self.assertIsNone(result["transcription"])
        self.assertIn("Scanned", result["processing_errors"][0])


class DocumentWindowTests(TestCase):
    def test_build_document_windows_preserves_page(self):
        words = " ".join(f"word{i}" for i in range(80))
        segments = [
            {"page": 2, "text": words, "start": 0, "end": 0},
            {"page": 3, "text": "short page text here", "start": 0, "end": 0},
        ]
        windows = build_document_windows(segments)
        self.assertTrue(windows)
        self.assertTrue(all("page" in window for window in windows))
        self.assertEqual(windows[0]["page"], 2)
        self.assertTrue(any(window["page"] == 3 for window in windows))


class PdfUploadViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="pdf-user", password="testpass123"
        )
        self.client.login(username="pdf-user", password="testpass123")

    def test_upload_accepts_pdf_file(self):
        upload = SimpleUploadedFile(
            "notes.pdf", b"%PDF-1.4 fake content", content_type="application/pdf"
        )
        response = self.client.post(reverse("upload_video"), {"video": upload})

        self.assertEqual(response.status_code, 302)
        job = VideoJob.objects.get(user=self.user, video_name="notes.pdf")
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.content_kind, ContentKind.DOCUMENT)
        self.assertIn("documents", job.video_path)

    def test_get_media_content_type_for_pdf(self):
        self.assertEqual(get_media_content_type("notes.pdf"), "application/pdf")


class PdfJobProcessingTests(TestCase):
    def test_process_document_job_stores_page_segments(self):
        user = User.objects.create_user(username="job-user", password="testpass123")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
            temp_file.write(b"%PDF-1.4 fake")
            temp_path = temp_file.name

        job = VideoJob.objects.create(
            user=user,
            video_path=temp_path,
            video_name="guide.pdf",
            content_kind=ContentKind.DOCUMENT,
            status=JobStatus.PENDING,
        )

        fake_result = {
            "metadata": {"page_count": 1, "file_size_bytes": 12, "format_extension": "pdf"},
            "transcription": {
                "text": "Leadership is a habit of showing up.",
                "text_segments": [
                    {
                        "page": 1,
                        "text": "Leadership is a habit of showing up.",
                        "start": 0,
                        "end": 0,
                    }
                ],
                "page_count": 1,
                "word_count": 7,
                "source": "pdf",
                "language": "unknown",
            },
            "processing_errors": [],
        }

        try:
            with (
                patch("pdf_processor.extract_pdf_content", return_value=fake_result),
                patch("semantic_search.search_engine") as mock_engine,
                patch("video_processor.jobs.index_video", return_value=1),
            ):
                mock_engine.ensure_ready.return_value = True
                process_video_job(job.job_id)
        finally:
            Path(temp_path).unlink(missing_ok=True)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.content_kind, ContentKind.DOCUMENT)
        self.assertEqual(job.text_segments[0]["page"], 1)


class SearchLocatorApiTests(TestCase):
    def test_to_api_format_includes_page_locator(self):
        flat = [
            {
                "job_id": "abc",
                "video_name": "notes.pdf",
                "start_time": 0,
                "end_time": 0,
                "text": "leadership habits",
                "score": 1.0,
                "search_type": "keyword",
                "content_kind": "document",
                "page": 4,
            }
        ]
        api = to_api_format(flat)
        self.assertEqual(api[0]["page"], 4)
        self.assertEqual(api[0]["content_kind"], "document")
        self.assertEqual(api[0]["locator"], "page")
        self.assertEqual(api[0]["locator_label"], "page 4")


class AskCitationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="ask-user", password="testpass123"
        )
        self.client.login(username="ask-user", password="testpass123")

    def test_api_ask_returns_page_citations_for_documents(self):
        hit = SearchResult(
            job_id="11111111-1111-1111-1111-111111111111",
            video_name="notes.pdf",
            user_id=self.user.id,
            start_time=0,
            end_time=0,
            text="Leadership is a habit.",
            score=0.9,
            search_type="semantic",
            page=3,
            content_kind="document",
        )

        with (
            patch("video_processor.api.search_engine") as mock_engine,
            patch("video_processor.api._call_openai", return_value=("Answer [1]", None)),
        ):
            mock_engine.ensure_ready.return_value = True
            mock_engine.semantic_search.return_value = [hit]
            response = self.client.post(
                reverse("api_ask"),
                data=json.dumps({"question": "What is leadership?"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["sources"][0]["content_kind"], "document")
        self.assertEqual(data["sources"][0]["page"], 3)
        self.assertEqual(data["sources"][0]["locator_label"], "page 3")
        self.assertIsNone(data["sources"][0]["start_time"])

    def test_api_video_details_for_document(self):
        job = VideoJob.objects.create(
            user=self.user,
            video_path="/media/documents/notes.pdf",
            video_name="notes.pdf",
            content_kind=ContentKind.DOCUMENT,
            status=JobStatus.COMPLETED,
            metadata={"page_count": 5},
        )
        response = self.client.get(
            reverse("api_video_details", kwargs={"job_id": str(job.job_id)})
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["media_type"], "document")
        self.assertEqual(data["content_kind"], "document")
        self.assertEqual(data["content_type"], "application/pdf")
        self.assertEqual(data["page_count"], 5)
