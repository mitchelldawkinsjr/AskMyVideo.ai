# AskMyVideo

**Intelligent Video Search & Recall** — upload videos or YouTube links, search their content with AI, ask questions with cited answers, and jump to the exact moment.

## What it does

- **Transcription** — faster-whisper transcribes every uploaded video/audio file
- **Semantic search** — FAISS + sentence-transformers over ~60-word transcript windows with real timestamps
- **Keyword & hybrid search** — exact matching plus reciprocal-rank-fusion of both modes
- **Ask (RAG)** — ask a question, get an answer synthesized from your transcripts with timestamped citations (requires `OPENAI_API_KEY`)
- **YouTube ingestion** — single videos or playlists via yt-dlp, with optional media cleanup after transcription
- **Multi-tenant** — every user's library is private; per-user public search pages at `/search/<username>/`

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r core_requirements.txt

python manage.py migrate
python manage.py runserver          # web (terminal 1)
python manage.py process_jobs       # transcription worker (terminal 2)
```

Visit http://localhost:8000, register, and upload a video. Uploads are queued as
pending jobs; the `process_jobs` worker picks them up, transcribes them, and
adds them to the search index incrementally.

## Configuration (environment variables)

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Django secret (required in production) |
| `DATABASE_URL` | Postgres URL (SQLite is used when unset) |
| `OPENAI_API_KEY` | Enables the Ask (RAG) endpoint |
| `OPENAI_MODEL` | Ask model (default `gpt-4o-mini`) |
| `DEMO_USERNAME` | Account whose videos power the landing-page demo search |
| `YOUTUBE_COOKIES_FILE` | Cookies for YouTube downloads on datacenter IPs |

## Architecture

- `core_video_processor.py` — validation, ffprobe metadata, ffmpeg audio extraction, Whisper transcription
- `semantic_search.py` — FAISS index over embedded transcript windows (per-user filtering, incremental adds)
- `video_processor/views.py` — HTML pages; `video_processor/api.py` — JSON endpoints; `video_processor/jobs.py` — worker job processing
- `video_processor/management/commands/process_jobs.py` — the transcription worker

## API

| Endpoint | Method | Auth | Purpose |
| --- | --- | --- | --- |
| `/api/search/` | POST | session or `username` param | keyword / semantic / hybrid search |
| `/api/ask/` | POST | session or `username` param | RAG question answering with citations |
| `/api/video/<id>/` | GET | public | playback metadata |
| `/video-file/<id>/` | GET | public | media file streaming |
| `/health/`, `/api/health/` | GET | public | health checks |
| `/api/search-status/`, `/api/rebuild-search-index/`, `/api/pending-jobs/`, `/api/detailed-stats/`, `/api/cleanup-youtube/`, `/api/retry-job/` | — | login | maintenance |

## Deployment

See [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md). Production runs via
`docker-compose.prod.yml`: a gunicorn web container, a `process_jobs` worker
container, and Postgres, sharing `media` and `search_cache` volumes.

## Tests

```bash
python manage.py test video_processor
```
