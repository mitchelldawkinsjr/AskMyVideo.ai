#!/usr/bin/env python3
"""
Core media processing: validation, metadata extraction (ffprobe), audio
extraction (ffmpeg), and transcription (faster-whisper via whisper_compat).
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import whisper_compat as whisper  # faster-whisper shim (4x faster on CPU)

    WHISPER_AVAILABLE = True
except ImportError:
    try:
        import whisper  # fallback to openai-whisper if available

        WHISPER_AVAILABLE = True
    except ImportError:
        WHISPER_AVAILABLE = False
        logger.warning("Whisper not available - transcription will be disabled")


SUPPORTED_VIDEO_FORMATS = frozenset({".mp4", ".avi", ".mov", ".mkv", ".webm"})
SUPPORTED_AUDIO_FORMATS = frozenset({".mp3", ".wav", ".m4a", ".aac"})


class ProcessingStatus(Enum):
    """Enumeration of possible processing statuses."""

    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL_SUCCESS = "partial_success"


def is_audio_file(file_path: str) -> bool:
    """Return True if the file path has a supported audio extension."""
    return Path(file_path).suffix.lower() in SUPPORTED_AUDIO_FORMATS


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


@dataclass
class VideoMetadata:
    """Technical and file-system metadata for a media file."""

    file_path: str
    duration_seconds: float
    width_pixels: int
    height_pixels: int
    frames_per_second: float
    format_extension: str
    file_size_bytes: int
    creation_timestamp: datetime

    def to_dict(self) -> Dict[str, Any]:
        metadata_dict = asdict(self)
        metadata_dict["creation_timestamp"] = self.creation_timestamp.isoformat()
        return metadata_dict

    @property
    def file_size_megabytes(self) -> float:
        return self.file_size_bytes / (1024 * 1024)

    @property
    def resolution_string(self) -> str:
        return f"{self.width_pixels}x{self.height_pixels}"


@dataclass
class ProcessingResult:
    """Outcome of a processing operation (e.g. audio extraction)."""

    status: ProcessingStatus
    output_file_path: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    processing_duration_seconds: Optional[float] = None

    @property
    def is_successful(self) -> bool:
        return self.status == ProcessingStatus.SUCCESS

    def to_dict(self) -> Dict[str, Any]:
        result_dict = asdict(self)
        result_dict["status"] = self.status.value
        return result_dict


@dataclass
class TranscriptionResult:
    """Outcome of a transcription, with text, segments, and timing."""

    status: ProcessingStatus
    transcribed_text: Optional[str] = None
    text_segments: Optional[List[Dict[str, Any]]] = None
    detected_language: Optional[str] = None
    confidence_score: Optional[float] = None
    processing_duration_seconds: Optional[float] = None
    error_message: Optional[str] = None

    @property
    def is_successful(self) -> bool:
        return self.status == ProcessingStatus.SUCCESS

    @property
    def word_count(self) -> int:
        if not self.transcribed_text:
            return 0
        return len(self.transcribed_text.split())


class VideoFileValidator:
    """Validates media files: existence, format, and size limits."""

    SUPPORTED_FORMATS = SUPPORTED_VIDEO_FORMATS
    SUPPORTED_AUDIO_FORMATS = SUPPORTED_AUDIO_FORMATS
    SUPPORTED_MEDIA_FORMATS = SUPPORTED_VIDEO_FORMATS | SUPPORTED_AUDIO_FORMATS
    MAX_FILE_SIZE_BYTES = 1024 * 1024 * 1024  # 1GB default limit

    def __init__(self, max_file_size_bytes: int = MAX_FILE_SIZE_BYTES):
        self.max_file_size_bytes = max_file_size_bytes

    def _validate(
        self, file_path: str, allowed_formats, kind: str
    ) -> Tuple[bool, Optional[str]]:
        path = Path(file_path)
        if not path.exists():
            return False, f"{kind} file does not exist: {file_path}"

        file_extension = path.suffix.lower()
        if file_extension not in allowed_formats:
            supported = ", ".join(sorted(allowed_formats))
            return (
                False,
                f"Unsupported {kind.lower()} format '{file_extension}'. Supported: {supported}",
            )

        file_size_bytes = path.stat().st_size
        if file_size_bytes > self.max_file_size_bytes:
            file_size_mb = file_size_bytes / (1024 * 1024)
            max_size_mb = self.max_file_size_bytes / (1024 * 1024)
            return (
                False,
                f"File too large: {file_size_mb:.1f}MB (maximum: {max_size_mb:.1f}MB)",
            )
        if file_size_bytes == 0:
            return False, f"{kind} file is empty"
        return True, None

    def validate_video_file(self, file_path: str) -> Tuple[bool, Optional[str]]:
        return self._validate(file_path, self.SUPPORTED_FORMATS, "Video")

    def validate_audio_file(self, file_path: str) -> Tuple[bool, Optional[str]]:
        return self._validate(file_path, self.SUPPORTED_AUDIO_FORMATS, "Audio")

    def validate_media_file(self, file_path: str) -> Tuple[bool, Optional[str]]:
        file_extension = Path(file_path).suffix.lower()
        if file_extension in self.SUPPORTED_AUDIO_FORMATS:
            return self.validate_audio_file(file_path)
        if file_extension in self.SUPPORTED_FORMATS:
            return self.validate_video_file(file_path)
        supported = ", ".join(sorted(self.SUPPORTED_MEDIA_FORMATS))
        return (
            False,
            f"Unsupported media format '{file_extension}'. Supported: {supported}",
        )


class VideoMetadataExtractor:
    """Extracts media metadata with ffprobe (basic file info as fallback)."""

    def __init__(self):
        self.ffmpeg_available = _ffmpeg_available()
        if not self.ffmpeg_available:
            logger.warning("ffprobe not available - metadata will be basic file info")

    def _run_ffprobe(self, file_path: str) -> Optional[Dict[str, Any]]:
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    file_path,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout)
        except Exception as ffprobe_error:
            logger.warning("ffprobe failed: %s", ffprobe_error)
            return None

    @staticmethod
    def _basic_metadata(file_path: str) -> Dict[str, Any]:
        path = Path(file_path)
        return {
            "file_path": str(path.absolute()),
            "format_extension": path.suffix.lower().lstrip("."),
            "file_size_bytes": path.stat().st_size,
            "creation_timestamp": datetime.now(),
            "duration_seconds": 0.0,
            "width_pixels": 0,
            "height_pixels": 0,
            "frames_per_second": 0.0,
        }

    def extract_metadata(self, file_path: str) -> Optional[VideoMetadata]:
        """Extract metadata from a video file."""
        try:
            metadata = self._basic_metadata(file_path)
            ffprobe_data = (
                self._run_ffprobe(file_path) if self.ffmpeg_available else None
            )
            if ffprobe_data:
                metadata["duration_seconds"] = float(
                    ffprobe_data.get("format", {}).get("duration", 0)
                )
                for stream in ffprobe_data.get("streams", []):
                    if stream.get("codec_type") != "video":
                        continue
                    metadata["width_pixels"] = int(stream.get("width", 0))
                    metadata["height_pixels"] = int(stream.get("height", 0))
                    frame_rate = stream.get("r_frame_rate", "0/1")
                    try:
                        numerator, denominator = map(int, frame_rate.split("/"))
                        metadata["frames_per_second"] = (
                            numerator / denominator if denominator > 0 else 0.0
                        )
                    except (ValueError, ZeroDivisionError):
                        metadata["frames_per_second"] = 0.0
                    break
            return VideoMetadata(**metadata)
        except Exception as extraction_error:
            logger.error("Failed to extract video metadata: %s", extraction_error)
            return None

    def extract_audio_metadata(self, file_path: str) -> Optional[VideoMetadata]:
        """Extract metadata from an audio-only file."""
        try:
            metadata = self._basic_metadata(file_path)
            ffprobe_data = (
                self._run_ffprobe(file_path) if self.ffmpeg_available else None
            )
            if ffprobe_data:
                metadata["duration_seconds"] = float(
                    ffprobe_data.get("format", {}).get("duration", 0)
                )
                if not metadata["duration_seconds"]:
                    for stream in ffprobe_data.get("streams", []):
                        if stream.get("codec_type") == "audio":
                            metadata["duration_seconds"] = float(
                                stream.get("duration", 0)
                            )
                            break
            return VideoMetadata(**metadata)
        except Exception as extraction_error:
            logger.error("Failed to extract audio metadata: %s", extraction_error)
            return None


class AudioExtractor:
    """Extracts audio tracks from video files with ffmpeg."""

    def __init__(self, temp_directory: Optional[str] = None):
        self.temp_directory = temp_directory or tempfile.gettempdir()
        self.ffmpeg_available = _ffmpeg_available()
        if not self.ffmpeg_available:
            logger.warning("FFmpeg not available - audio extraction will fail")

    def extract_audio_from_video(
        self, video_path: str, output_path: Optional[str] = None
    ) -> ProcessingResult:
        start_time = datetime.now()
        try:
            if not self.ffmpeg_available:
                return ProcessingResult(
                    status=ProcessingStatus.FAILED,
                    error_message="FFmpeg not available for audio extraction",
                )

            if output_path is None:
                video_filename = Path(video_path).stem
                output_path = os.path.join(
                    self.temp_directory, f"{video_filename}_extracted_audio.mp3"
                )
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)

            subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    video_path,
                    "-q:a",
                    "0",  # best audio quality
                    "-map",
                    "a",  # audio streams only
                    "-y",
                    output_path,
                ],
                check=True,
                capture_output=True,
            )

            if not Path(output_path).exists():
                return ProcessingResult(
                    status=ProcessingStatus.FAILED,
                    error_message="Audio extraction completed but output file not found",
                )

            processing_duration = (datetime.now() - start_time).total_seconds()
            return ProcessingResult(
                status=ProcessingStatus.SUCCESS,
                output_file_path=output_path,
                metadata={
                    "input_video_path": video_path,
                    "extraction_timestamp": datetime.now().isoformat(),
                    "extraction_tool": "ffmpeg",
                },
                processing_duration_seconds=processing_duration,
            )
        except subprocess.CalledProcessError as ffmpeg_error:
            error_message = "FFmpeg error: " + (
                ffmpeg_error.stderr.decode()
                if ffmpeg_error.stderr
                else str(ffmpeg_error)
            )
            logger.error(error_message)
            return ProcessingResult(
                status=ProcessingStatus.FAILED,
                error_message=error_message,
                processing_duration_seconds=(
                    datetime.now() - start_time
                ).total_seconds(),
            )
        except Exception as extraction_error:
            error_message = f"Audio extraction failed: {extraction_error}"
            logger.error(error_message)
            return ProcessingResult(
                status=ProcessingStatus.FAILED,
                error_message=error_message,
                processing_duration_seconds=(
                    datetime.now() - start_time
                ).total_seconds(),
            )


class WhisperTranscriptionService:
    """Transcribes audio files with Whisper."""

    def __init__(self, model_name: str = "base"):
        self.model_name = model_name
        self.whisper_model = None
        self.whisper_available = WHISPER_AVAILABLE
        if self.whisper_available:
            self._load_whisper_model()

    def _load_whisper_model(self):
        try:
            logger.info("Loading Whisper model: %s", self.model_name)
            self.whisper_model = whisper.load_model(self.model_name)
        except Exception as model_error:
            logger.error("Failed to load Whisper model: %s", model_error)
            self.whisper_model = None
            self.whisper_available = False

    def transcribe_audio_file(
        self, audio_path: str, language: str = "auto"
    ) -> TranscriptionResult:
        start_time = datetime.now()
        try:
            if not self.whisper_available or not self.whisper_model:
                return TranscriptionResult(
                    status=ProcessingStatus.FAILED,
                    error_message="Whisper model not available for transcription",
                )
            if not Path(audio_path).exists():
                return TranscriptionResult(
                    status=ProcessingStatus.FAILED,
                    error_message=f"Audio file not found: {audio_path}",
                )

            logger.info("Starting transcription of audio file: %s", audio_path)
            transcription_options = {}
            if language != "auto":
                transcription_options["language"] = language
            transcription_result = self.whisper_model.transcribe(
                audio_path, **transcription_options
            )
            processing_duration = (datetime.now() - start_time).total_seconds()

            confidence_score = None
            segments = transcription_result.get("segments") or []
            if segments:
                segment_confidences = [
                    segment.get("confidence", 0) for segment in segments
                ]
                confidence_score = (
                    sum(segment_confidences) / len(segment_confidences)
                    if segment_confidences
                    else None
                )

            logger.info("Transcription completed in %.2f seconds", processing_duration)
            return TranscriptionResult(
                status=ProcessingStatus.SUCCESS,
                transcribed_text=transcription_result.get("text", ""),
                text_segments=segments,
                detected_language=transcription_result.get("language", language),
                confidence_score=confidence_score,
                processing_duration_seconds=processing_duration,
            )
        except Exception as transcription_error:
            error_message = f"Transcription failed: {transcription_error}"
            logger.error(error_message)
            return TranscriptionResult(
                status=ProcessingStatus.FAILED,
                error_message=error_message,
                processing_duration_seconds=(
                    datetime.now() - start_time
                ).total_seconds(),
            )


class CoreVideoProcessor:
    """Coordinates validation, metadata, audio extraction, and transcription."""

    def __init__(
        self,
        temporary_directory: Optional[str] = None,
        whisper_model_name: str = "base",
        max_file_size_bytes: int = 1024 * 1024 * 1024,
        cleanup_temporary_files: bool = True,
    ):
        self.temporary_directory = temporary_directory or tempfile.gettempdir()
        self.cleanup_temporary_files = cleanup_temporary_files
        os.makedirs(self.temporary_directory, exist_ok=True)

        self.video_validator = VideoFileValidator(max_file_size_bytes)
        self.metadata_extractor = VideoMetadataExtractor()
        self.audio_extractor = AudioExtractor(self.temporary_directory)
        self.transcription_service = WhisperTranscriptionService(whisper_model_name)

    def validate_media_file(self, file_path: str) -> Tuple[bool, Optional[str]]:
        return self.video_validator.validate_media_file(file_path)

    def extract_audio_file_metadata(
        self, audio_file_path: str
    ) -> Optional[VideoMetadata]:
        is_valid, validation_error = self.video_validator.validate_audio_file(
            audio_file_path
        )
        if not is_valid:
            logger.error("Audio validation failed: %s", validation_error)
            return None
        return self.metadata_extractor.extract_audio_metadata(audio_file_path)

    def extract_video_metadata(self, video_file_path: str) -> Optional[VideoMetadata]:
        is_valid, validation_error = self.video_validator.validate_video_file(
            video_file_path
        )
        if not is_valid:
            logger.error("Video validation failed: %s", validation_error)
            return None
        return self.metadata_extractor.extract_metadata(video_file_path)

    def extract_audio_from_video(
        self, video_file_path: str, audio_output_path: Optional[str] = None
    ) -> ProcessingResult:
        is_valid, validation_error = self.video_validator.validate_video_file(
            video_file_path
        )
        if not is_valid:
            return ProcessingResult(
                status=ProcessingStatus.FAILED,
                error_message=f"Video validation failed: {validation_error}",
            )
        return self.audio_extractor.extract_audio_from_video(
            video_file_path, audio_output_path
        )

    def transcribe_audio_file(
        self, audio_file_path: str, language_code: str = "auto"
    ) -> TranscriptionResult:
        return self.transcription_service.transcribe_audio_file(
            audio_file_path, language_code
        )

    def transcribe_video_file(
        self, video_file_path: str, language_code: str = "auto"
    ) -> TranscriptionResult:
        """Extract audio from video and transcribe it to text."""
        try:
            audio_extraction_result = self.extract_audio_from_video(video_file_path)
            if not audio_extraction_result.is_successful:
                return TranscriptionResult(
                    status=ProcessingStatus.FAILED,
                    error_message=f"Audio extraction failed: {audio_extraction_result.error_message}",
                )

            transcription_result = self.transcribe_audio_file(
                audio_extraction_result.output_file_path, language_code
            )

            if (
                self.cleanup_temporary_files
                and audio_extraction_result.output_file_path
            ):
                try:
                    os.remove(audio_extraction_result.output_file_path)
                except Exception as cleanup_error:
                    logger.warning(
                        "Could not clean up temporary audio file: %s", cleanup_error
                    )
            return transcription_result
        except Exception as pipeline_error:
            error_message = f"Video transcription pipeline failed: {pipeline_error}"
            logger.error(error_message)
            return TranscriptionResult(
                status=ProcessingStatus.FAILED, error_message=error_message
            )

    def create_comprehensive_media_summary(
        self, file_path: str, language_code: str = "auto"
    ) -> Dict[str, Any]:
        """Full analysis (metadata + transcription) for a video or audio file."""
        analysis_summary = {
            "video_file_path": file_path,
            "analysis_timestamp": datetime.now().isoformat(),
            "metadata": None,
            "transcription": None,
            "processing_errors": [],
        }

        try:
            is_valid, validation_error = self.validate_media_file(file_path)
            if not is_valid:
                analysis_summary["processing_errors"].append(validation_error)
                return analysis_summary

            audio = is_audio_file(file_path)
            metadata = (
                self.extract_audio_file_metadata(file_path)
                if audio
                else self.extract_video_metadata(file_path)
            )
            if metadata:
                analysis_summary["metadata"] = metadata.to_dict()
            elif not audio:
                analysis_summary["processing_errors"].append(
                    "Failed to extract video metadata"
                )

            if self.transcription_service.whisper_available:
                transcription_result = (
                    self.transcribe_audio_file(file_path, language_code)
                    if audio
                    else self.transcribe_video_file(file_path, language_code)
                )
                if transcription_result.is_successful:
                    analysis_summary["transcription"] = {
                        "text": transcription_result.transcribed_text,
                        "text_segments": transcription_result.text_segments,
                        "language": transcription_result.detected_language,
                        "confidence_score": transcription_result.confidence_score,
                        "word_count": transcription_result.word_count,
                        "processing_duration_seconds": transcription_result.processing_duration_seconds,
                    }
                    logger.info(
                        "Transcription completed: %d words in %s",
                        transcription_result.word_count,
                        transcription_result.detected_language,
                    )
                else:
                    analysis_summary["processing_errors"].append(
                        f"Transcription failed: {transcription_result.error_message}"
                    )
            else:
                analysis_summary["processing_errors"].append(
                    "Transcription service not available"
                )
        except Exception as analysis_error:
            error_message = f"Media analysis failed: {analysis_error}"
            logger.error(error_message)
            analysis_summary["processing_errors"].append(error_message)

        return analysis_summary

    # Kept for the CLI below and any external callers.
    def create_comprehensive_video_summary(
        self, video_file_path: str, language_code: str = "auto"
    ) -> Dict[str, Any]:
        return self.create_comprehensive_media_summary(video_file_path, language_code)


def main():
    """Simple CLI for testing: metadata, transcribe, or summary."""
    import argparse

    parser = argparse.ArgumentParser(description="Core media processor CLI")
    parser.add_argument("action", choices=["metadata", "transcribe", "summary"])
    parser.add_argument("video_file", help="Path to the media file to process")
    parser.add_argument("--output", help="Output file path for results (JSON format)")
    parser.add_argument("--language", default="auto")
    parser.add_argument(
        "--whisper-model",
        default="base",
        choices=["tiny", "base", "small", "medium", "large"],
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    processor = CoreVideoProcessor(whisper_model_name=args.whisper_model)

    if args.action == "metadata":
        metadata = processor.extract_video_metadata(args.video_file)
        if not metadata:
            print("Failed to extract video metadata")
            sys.exit(1)
        result_data = metadata.to_dict()
    elif args.action == "transcribe":
        transcription = processor.transcribe_video_file(args.video_file, args.language)
        if not transcription.is_successful:
            print(f"Transcription failed: {transcription.error_message}")
            sys.exit(1)
        result_data = {
            "text": transcription.transcribed_text,
            "language": transcription.detected_language,
            "confidence": transcription.confidence_score,
            "word_count": transcription.word_count,
            "processing_duration": transcription.processing_duration_seconds,
        }
    else:
        result_data = processor.create_comprehensive_media_summary(
            args.video_file, args.language
        )

    print(json.dumps(result_data, indent=2))
    if args.output:
        with open(args.output, "w") as output_file:
            json.dump(result_data, output_file, indent=2)
        print(f"Results saved to: {args.output}")


if __name__ == "__main__":
    main()
