# Spool — Technical Documentation

A self-hosted service for pulling audio and video off YouTube links. FastAPI backend, MongoDB for job persistence, a single-file HTML/Tailwind frontend, deployed as a Docker container.

**Version:** 3.0.0
**Stack:** Python 3.12, FastAPI, yt-dlp, MongoDB, vanilla JS + Tailwind CDN

---

## 1. Architecture

```
┌─────────────────┐        HTTPS/JSON         ┌──────────────────────┐
│  index.html      │ ────────────────────────▶ │  FastAPI (main.py)   │
│  (Vercel, static)│ ◀──────────────────────── │  (Render, Docker)    │
└─────────────────┘        polling / fetch     └──────────┬───────────┘
                                                            │
                                    ┌───────────────────────┼───────────────────────┐
                                    ▼                       ▼                       ▼
                             ┌─────────────┐        ┌──────────────┐        ┌─────────────┐
                             │  MongoDB     │        │  yt-dlp       │        │  ffmpeg /    │
                             │  (Atlas)     │        │  (background  │        │  Deno         │
                             │  job state   │        │  thread)      │        │  (system deps)│
                             └─────────────┘        └──────────────┘        └─────────────┘
```

**Request flow for a download:**

1. Frontend `POST`s a URL to one of four `/download/*` endpoints.
2. The endpoint creates a job document in MongoDB (`status: pending`) and hands off the actual download to a `BackgroundTasks` function, returning a `job_id` immediately.
3. The background function runs `yt_dlp.YoutubeDL(...).extract_info(url, download=True)`, which blocks the background thread (not the request) while ffmpeg/Deno do their work.
4. Progress and postprocessor hooks push live status updates into the same MongoDB document as the download proceeds.
5. The frontend polls `GET /jobs` every 2.5s and diff-renders only the rows that changed.
6. Once `status: completed`, the frontend shows a Download link to `GET /jobs/{id}/file`.
7. That endpoint streams the file (zipping multiple files for playlists) and deletes it from disk immediately after the response finishes sending.

---

## 2. Why these choices

| Decision | Reasoning |
|---|---|
| Background thread, not a task queue (Celery/RQ) | Single-server, low-volume personal tool. A dedicated queue is unnecessary complexity here — would reconsider at multi-worker scale. |
| MongoDB over Postgres | Job documents are naturally schema-flexible (progress dict shape differs by state); no relational joins needed. |
| Files deleted post-serve | Keeps disk usage bounded on ephemeral PaaS storage; nothing about this app needs long-term file retention. |
| `owner_id` via client-generated UUID, not real auth | No login system exists. This solves "don't show me a stranger's job history," not "prevent a determined attacker." See §6. |
| Sync `pymongo`, not `motor` (async) | yt-dlp itself is a blocking library that already runs in a background thread; async Mongo calls would add complexity without a performance win here. |
| Single-file HTML frontend, no build step | Small enough surface area that a framework (React/Vue) would be pure overhead. Deploys as a static file with zero build tooling. |

---

## 3. Data model

Collection: `jobs` (MongoDB, database name via `MONGO_DB_NAME`)

| Field | Type | Notes |
|---|---|---|
| `job_id` | string (UUID4) | Unique index. Primary lookup key. |
| `owner_id` | string | Client-generated UUID from `localStorage`. Indexed. Scopes every read. |
| `type` | string | `audio` \| `playlist-audio` \| `video` \| `playlist-video` |
| `url` | string | Original submitted URL. |
| `title` | string \| null | Resolved from yt-dlp metadata (playlist title for playlist jobs, video title otherwise). Null until yt-dlp resolves it. |
| `status` | string | `pending` \| `running` \| `completed` \| `failed` |
| `progress` | object \| null | `{filename, percent, speed, eta}`, updated on every yt-dlp progress event. ANSI color codes are stripped before storage. |
| `error` | string \| null | Exception message if `status == failed`. |
| `files` | string[] | Absolute paths of every finished file. One entry for single downloads, N for playlists. Populated via `$addToSet` (see §5.1 for why). |
| `created_at` / `updated_at` | datetime (UTC) | |

---

## 4. API reference

All endpoints except none require a client id, sent as either:
- Header: `X-Client-Id: <uuid>`
- Query param: `?client_id=<uuid>` (used by the frontend's `<a href>` download link, since anchor tags can't set custom headers)

Missing client id → `400`.

### `POST /download/audio`
Single video → mp3.
```json
// Request
{ "url": "https://youtu.be/..." }
// Response
{ "job_id": "...", "status": "pending" }
```

### `POST /download/playlist-audio`
Playlist range → mp3 per track, zipped on download if more than one file.
```json
{ "url": "https://youtube.com/playlist?list=...", "start": 1, "end": 10 }
```
`400` if `end < start`.

### `POST /download/video`
Single video → mp4 (best available `avc1` video + `mp4a` audio, merged).

### `POST /download/playlist-video`
Playlist range → mp4 per track.

### `GET /jobs`
Returns this client's jobs, newest first.

### `GET /jobs/{job_id}`
Single job status/progress. `404` if it doesn't exist *or* belongs to a different `owner_id` (indistinguishable on purpose — no existence leakage).

### `GET /jobs/{job_id}/file`
Streams the finished file. `409` if the job isn't `completed` yet. Deletes the file(s) from disk after the response is fully sent (see §5.2).

---

## 5. Key implementation details

### 5.1 Why `files` is a list, appended via `$addToSet`

Early versions stored a single `file` field, overwritten on every yt-dlp `postprocessor_hooks` "finished" event. For playlists, that meant only the *last* track's path survived — every earlier track quietly vanished from the job record even though it downloaded fine. Switching to an append-only list fixed this; `$addToSet` also makes the operation idempotent if a hook somehow fires twice for the same file.

### 5.2 Post-serve cleanup

```python
def _cleanup_paths(*paths: str):
    for path in paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
                # also remove the parent dir if now empty
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass  # never let cleanup crash an already-successful response
```

Attached via Starlette's `background=BackgroundTask(...)` parameter on `FileResponse`, which guarantees the cleanup only runs *after* the HTTP response body has finished streaming — never before or during. Playlist downloads get zipped into a `tempfile.mkdtemp()` directory first; cleanup removes the zip's temp dir, every source file, and the now-empty per-job playlist folder that `outtmpl` created.

### 5.3 ANSI escape code stripping

yt-dlp's `_percent_str` / `_speed_str` / `_eta_str` fields are formatted with terminal color codes (`\x1b[0;94m...\x1b[0m`) intended for a TTY. Left unstripped, browsers render the raw escape bytes as garbled glyphs. A regex (`_clean()`) strips these before anything is written to MongoDB or shown in the UI.

### 5.4 Title resolution

Job titles are resolved from yt-dlp's own metadata rather than derived from the URL:
- Playlist jobs: `info.get("playlist_title") or info.get("playlist")`
- Single-video jobs: `info.get("title")`

Captured opportunistically the first time it appears in either the progress hook or postprocessor hook (`title_state` dict tracks whether it's already been set, to avoid overwriting a playlist title with an individual track's title on a later event), with a fallback read from `extract_info()`'s return value at job completion in case neither hook caught it.

### 5.5 YouTube anti-bot measures

Two separate mechanisms yt-dlp needs as of 2026, both configured via `_base_ydl_opts()`:

- **Cookies** (`YTDLP_COOKIES_FILE`): fixes `Sign in to confirm you're not a bot`. Must be a genuine Netscape-format `cookies.txt`; browser extension exports vary in reliability. If not needed, this can be safely left unset.
- **Remote components** (`YTDLP_REMOTE_COMPONENTS`, default `ejs:github`): lets yt-dlp fetch its JS challenge-solver scripts to solve YouTube's "n challenge" signature puzzles. Requires a JS runtime (Deno) actually installed and on `PATH` — the option only grants *permission to download* the solver scripts, it doesn't install a runtime.

### 5.6 Frontend rendering strategy

The queue list uses a keyed diff renderer rather than rebuilding the DOM on every poll:

```js
const rowElements = new Map();   // job_id -> DOM node
const rowSignatures = new Map(); // job_id -> last-rendered state signature
```

A row's `innerHTML` is only touched if its `status|title|percent|error` signature actually changed since the last poll. This was a deliberate fix — full innerHTML rebuilds every 2.5s were causing visible flicker and restarting the running-job progress animation on rows that hadn't changed at all.

---

## 6. Security model & known limitations

- **`owner_id` is not authentication.** It's a client-generated UUID stored in `localStorage`, sent as a header/query param. It prevents accidental cross-browser visibility of job history; it does not stop someone from forging the header value if they wanted to see another client's jobs. Acceptable for a personal/small-group tool; would need real auth (API keys, OAuth, etc.) before wider exposure.
- **No rate limiting.** Nothing currently stops one client from queueing unlimited jobs.
- **CORS**: controlled via `CORS_ALLOWED_ORIGINS`. Defaults to `*` for local development — **must** be locked to the real frontend origin(s) before public deployment, or any website could trigger requests against the API using a visitor's browser.
- **Cookies file is a credential.** `YTDLP_COOKIES_FILE` contains an authenticated YouTube session. Never commit it to version control; on Render, use Secret Files rather than baking it into the image.
- **Stale jobs on restart.** If the server process restarts mid-download (deploy, crash), that job stays stuck at `status: running` forever in MongoDB — there's no startup sweep to mark orphaned jobs as failed.
- **Legal/ToS surface.** This tool downloads copyrighted material from YouTube. Most hosting providers' terms of service restrict tools whose primary purpose is circumventing a platform's own download restrictions. Treat this as a personal tool, not a public service, unless you've separately confirmed your hosting provider's ToS allows it.

---

## 7. Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MONGO_URI` | Yes | `mongodb://localhost:27017` | MongoDB connection string. |
| `MONGO_DB_NAME` | No | `ytdlp_downloader` | Database name. |
| `FFMPEG_PATH` | No | local ffmpeg build path | Passed to yt-dlp. `/usr/bin` in Docker. |
| `YTDLP_COOKIES_FILE` | No | unset | Path to a Netscape-format cookies file. |
| `YTDLP_REMOTE_COMPONENTS` | No | `ejs:github` | Comma-separated yt-dlp remote component permissions. |
| `CORS_ALLOWED_ORIGINS` | No | `*` | Comma-separated allowed origins. Restrict in production. |

---

## 8. Deployment

**Local:** `docker compose up -d --build` (spins up MongoDB + API together, see `docker-compose.yml`).

**Production (Render + Vercel + Atlas):**
1. MongoDB Atlas free-tier cluster → `MONGO_URI`.
2. Render Docker web service from `Dockerfile` (config in `render.yaml`) → backend URL.
3. Vercel static deploy of `index.html` (config in `vercel.json`) → frontend URL.
4. Set `CORS_ALLOWED_ORIGINS` on Render to the Vercel URL; set `API_BASE` in `index.html` to the Render URL before deploying the frontend.

**Self-hosted VPS:** `docker-compose.yml` + `Caddyfile` for automatic HTTPS via Let's Encrypt.

---

## 9. Project structure

```
.
├── main.py              # FastAPI app — all endpoints, job logic, yt-dlp integration
├── index.html            # Frontend — single file, Tailwind CDN, vanilla JS
├── requirements.txt       # Python dependencies
├── Dockerfile             # Backend image: Python + ffmpeg + Deno
├── docker-compose.yml     # Local dev: API + MongoDB together
├── Caddyfile              # Reverse proxy + auto-HTTPS for self-hosted VPS deploys
├── render.yaml            # Render Blueprint (backend deploy config)
├── vercel.json            # Vercel static deploy config (frontend)
└── .env.example           # Template for local environment variables
```

---

## 10. Possible next steps

- Startup sweep to mark orphaned `running` jobs as `failed` after a server restart.
- Real authentication if this is ever exposed beyond personal/trusted use.
- Rate limiting per `owner_id`.
- A "clear completed jobs" action in the UI, since job history currently only grows.
- TTL index on `created_at` in MongoDB to auto-expire old job records.