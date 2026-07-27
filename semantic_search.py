"""
Semantic search engine for content vault transcripts and documents.

FAISS inner-product index over sentence-transformer embeddings of text
windows. Whisper segments (or PDF page chunks) are merged into ~60-word
windows before embedding, which gives much better retrieval than embedding
5-10 second sentence fragments individually.

Design notes:
    - Per-user filtering: every indexed window stores ``user_id`` so results can be
      scoped to one owner before top-k is returned.
    - Incremental updates: ``add_video`` encodes and indexes a single item;
      full ``rebuild_index`` is only needed after deletes or edits.
    - Multi-process safety: the index is persisted to disk after every change and
      reloaded automatically when another process has written a newer version.
    - Metadata is stored as JSON (not pickle) so the cache is inspectable and safe.
    - Locators: media windows use start/end times; document windows use ``page``.
"""

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

try:
    import faiss
    from sentence_transformers import SentenceTransformer

    SEMANTIC_SEARCH_AVAILABLE = True
except ImportError:
    SEMANTIC_SEARCH_AVAILABLE = False

logger = logging.getLogger(__name__)

METADATA_VERSION = 3

# Windows below this cosine similarity are never returned; top-k semantic hits
# with near-zero similarity are noise, not results.
DEFAULT_MIN_SCORE = 0.35

# Window sizing (in words) for merging Whisper segments before embedding.
TARGET_WINDOW_WORDS = 60
MAX_WINDOW_WORDS = 90


@dataclass
class SearchResult:
    """A single matching transcript or document window."""

    job_id: str
    video_name: str
    user_id: Optional[int]
    start_time: float
    end_time: float
    text: str
    score: float
    search_type: str  # 'keyword', 'semantic', 'hybrid'
    page: Optional[int] = None
    content_kind: str = "video"


def build_windows(segments: List[Dict]) -> List[Dict]:
    """
    Merge raw Whisper segments into ~60-word windows with real timestamps.

    Each window keeps the start time of its first segment and the end time of
    its last, so seeking stays exact.
    """
    windows = []
    current_texts: List[str] = []
    current_words = 0
    current_start = 0.0
    current_end = 0.0

    def flush():
        nonlocal current_texts, current_words
        if current_texts:
            windows.append(
                {
                    "start": current_start,
                    "end": current_end,
                    "text": " ".join(current_texts),
                }
            )
            current_texts = []
            current_words = 0

    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        words = len(text.split())
        if not current_texts:
            current_start = segment.get("start", 0)
        current_texts.append(text)
        current_words += words
        current_end = segment.get("end", segment.get("start", 0))
        # Flush at the target size, or force a flush if the window grew huge.
        if current_words >= TARGET_WINDOW_WORDS or current_words >= MAX_WINDOW_WORDS:
            flush()

    flush()
    return windows


def build_document_windows(segments: List[Dict]) -> List[Dict]:
    """
    Chunk document page text into ~60-word windows, preserving page numbers.

    Pages are never merged across page boundaries so citations stay accurate.
    """
    windows = []
    for segment in segments:
        page = segment.get("page")
        text = (segment.get("text") or "").strip()
        if not text or page is None:
            continue
        words = text.split()
        if not words:
            continue
        index = 0
        while index < len(words):
            end = min(index + TARGET_WINDOW_WORDS, len(words))
            # Prefer slightly larger windows up to MAX when near the end of a page.
            if end < len(words) and (end - index) < MAX_WINDOW_WORDS:
                remaining = len(words) - end
                if remaining < (MAX_WINDOW_WORDS - TARGET_WINDOW_WORDS):
                    end = len(words)
            chunk = words[index:end]
            windows.append(
                {
                    "start": 0,
                    "end": 0,
                    "page": int(page),
                    "text": " ".join(chunk),
                }
            )
            index = end
    return windows


class SemanticSearchEngine:
    """FAISS-backed semantic search over transcript and document windows."""

    def __init__(
        self, model_name: str = "all-MiniLM-L6-v2", cache_dir: str = "search_cache"
    ):
        self.model_name = model_name
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)

        self._model = None
        # Re-entrant: indexing methods hold the lock while _get_model
        # acquires it again on first use.
        self._lock = threading.RLock()
        self.index = None
        self.segments_metadata: List[Dict] = []
        self.is_initialized = False
        self._loaded_mtime: Optional[float] = None

    # ------------------------------------------------------------------
    # Model / persistence
    # ------------------------------------------------------------------

    @property
    def _index_path(self) -> Path:
        return self.cache_dir / "faiss_index.bin"

    @property
    def _metadata_path(self) -> Path:
        return self.cache_dir / "segments_metadata.json"

    def _get_model(self):
        """Load the sentence-transformer lazily, once."""
        if not SEMANTIC_SEARCH_AVAILABLE:
            return None
        if self._model is None:
            with self._lock:
                if self._model is None:
                    logger.info("Loading sentence transformer: %s", self.model_name)
                    self._model = SentenceTransformer(self.model_name)
        return self._model

    def _save_index(self):
        try:
            if self.index is not None:
                faiss.write_index(self.index, str(self._index_path))
            payload = {"version": METADATA_VERSION, "segments": self.segments_metadata}
            self._metadata_path.write_text(json.dumps(payload))
            self._loaded_mtime = self._index_path.stat().st_mtime
            logger.info("Saved search index (%d windows)", len(self.segments_metadata))
        except Exception as exc:
            logger.error("Failed to save search index: %s", exc)

    def _load_index(self) -> bool:
        if not SEMANTIC_SEARCH_AVAILABLE:
            return False
        try:
            if not (self._index_path.exists() and self._metadata_path.exists()):
                return False
            payload = json.loads(self._metadata_path.read_text())
            if payload.get("version") != METADATA_VERSION:
                logger.warning("Search index cache has old format; rebuild required")
                return False
            self.index = faiss.read_index(str(self._index_path))
            self.segments_metadata = payload["segments"]
            self._loaded_mtime = self._index_path.stat().st_mtime
            self.is_initialized = True
            logger.info(
                "Loaded search index with %d windows", len(self.segments_metadata)
            )
            return True
        except Exception as exc:
            logger.error("Failed to load search index: %s", exc)
            return False

    def _maybe_reload(self):
        """Reload the index if another process wrote a newer version."""
        try:
            if not self._index_path.exists():
                return
            mtime = self._index_path.stat().st_mtime
            if self._loaded_mtime is None or mtime > self._loaded_mtime:
                self._load_index()
        except OSError:
            pass

    def ensure_ready(self) -> bool:
        """Return True when the index is loaded and searchable."""
        if not SEMANTIC_SEARCH_AVAILABLE:
            return False
        if not self.is_initialized:
            self._load_index()
        else:
            self._maybe_reload()
        return self.is_initialized

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def _encode(self, texts: List[str]) -> np.ndarray:
        model = self._get_model()
        if model is None:
            return np.array([])
        embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        faiss.normalize_L2(embeddings)
        return embeddings.astype("float32")

    def _windows_for_video(self, video_data: Dict):
        """Return (metadata_entries, texts) for one item's segments."""
        content_kind = video_data.get("content_kind") or "video"
        segments = video_data.get("segments", [])
        if content_kind == "document":
            windows = build_document_windows(segments)
        else:
            windows = build_windows(segments)
        entries = []
        texts = []
        for window in windows:
            entry = {
                "job_id": video_data["job_id"],
                "video_name": video_data["video_name"],
                "user_id": video_data.get("user_id"),
                "start_time": window["start"],
                "end_time": window["end"],
                "text": window["text"],
                "content_kind": content_kind,
            }
            if window.get("page") is not None:
                entry["page"] = window["page"]
            entries.append(entry)
            texts.append(window["text"])
        return entries, texts

    def add_video(self, video_data: Dict) -> int:
        """
        Incrementally index one content item (the common path after processing).

        ``video_data`` needs: job_id, video_name, user_id, segments.
        Optional: content_kind.
        Returns the number of windows added.
        """
        if not SEMANTIC_SEARCH_AVAILABLE:
            return 0
        with self._lock:
            self.ensure_ready()
            entries, texts = self._windows_for_video(video_data)
            if not texts:
                return 0
            # Drop any stale entries for this video before re-adding.
            if any(m["job_id"] == video_data["job_id"] for m in self.segments_metadata):
                self._rebuild_excluding(video_data["job_id"])

            embeddings = self._encode(texts)
            if embeddings.size == 0:
                return 0
            if self.index is None:
                self.index = faiss.IndexFlatIP(embeddings.shape[1])
            self.index.add(embeddings)
            self.segments_metadata.extend(entries)
            self.is_initialized = True
            self._save_index()
            return len(entries)

    def remove_video(self, job_id: str) -> None:
        """Remove one item's windows from the index (used on delete/edit)."""
        if not self.ensure_ready():
            return
        with self._lock:
            if any(m["job_id"] == str(job_id) for m in self.segments_metadata):
                self._rebuild_excluding(str(job_id))
                self._save_index()

    def _rebuild_excluding(self, job_id: str):
        """Rebuild the FAISS index from kept metadata, re-encoding kept texts."""
        kept = [m for m in self.segments_metadata if m["job_id"] != job_id]
        self.segments_metadata = kept
        if not kept:
            self.index = None
            self.is_initialized = False
            return
        embeddings = self._encode([m["text"] for m in kept])
        self.index = faiss.IndexFlatIP(embeddings.shape[1])
        self.index.add(embeddings)

    def rebuild_index(self, videos: List[Dict]) -> int:
        """
        Full rebuild from all content items. Each item needs job_id, video_name,
        user_id, segments. Returns the number of indexed windows.
        """
        if not SEMANTIC_SEARCH_AVAILABLE:
            return 0
        with self._lock:
            all_entries: List[Dict] = []
            all_texts: List[str] = []
            for video_data in videos:
                entries, texts = self._windows_for_video(video_data)
                all_entries.extend(entries)
                all_texts.extend(texts)

            if not all_texts:
                self.index = None
                self.segments_metadata = []
                self.is_initialized = False
                self._save_index()
                return 0

            embeddings = self._encode(all_texts)
            self.index = faiss.IndexFlatIP(embeddings.shape[1])
            self.index.add(embeddings)
            self.segments_metadata = all_entries
            self.is_initialized = True
            self._save_index()
            return len(all_entries)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def semantic_search(
        self,
        query: str,
        top_k: int = 20,
        user_id: Optional[int] = None,
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> List[SearchResult]:
        """
        Search text windows by meaning.

        When ``user_id`` is given, only that user's content is searched
        (candidates are oversampled before filtering so top_k stays full).
        """
        if not self.ensure_ready() or self.index is None:
            return []

        try:
            query_embedding = self._encode([query])
            if query_embedding.size == 0:
                return []

            fetch_k = top_k * 4 if user_id is not None else top_k
            fetch_k = min(max(fetch_k, top_k), self.index.ntotal)
            scores, indices = self.index.search(query_embedding, fetch_k)

            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or idx >= len(self.segments_metadata):
                    continue
                if score < min_score:
                    continue
                meta = self.segments_metadata[idx]
                if user_id is not None and meta.get("user_id") != user_id:
                    continue
                results.append(
                    SearchResult(
                        job_id=meta["job_id"],
                        video_name=meta["video_name"],
                        user_id=meta.get("user_id"),
                        start_time=meta["start_time"],
                        end_time=meta["end_time"],
                        text=meta["text"],
                        score=float(score),
                        search_type="semantic",
                        page=meta.get("page"),
                        content_kind=meta.get("content_kind") or "video",
                    )
                )
                if len(results) >= top_k:
                    break
            return results
        except Exception as exc:
            logger.error("Semantic search error: %s", exc)
            return []

    def get_stats(self) -> Dict:
        return {
            "is_available": SEMANTIC_SEARCH_AVAILABLE,
            "is_initialized": self.is_initialized,
            "model_name": self.model_name,
            "total_segments": len(self.segments_metadata),
            "index_size": self.index.ntotal if self.index else 0,
        }


# Shared engine instance. The model and index load lazily on first use, so
# importing this module is cheap (web workers that never search pay nothing).
search_engine = SemanticSearchEngine()
