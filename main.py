import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from enum import Enum

import yt_dlp
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from starlette.background import BackgroundTask

load_dotenv()  # reads variables from a .env file in the working directory, if present

FFMPEG_PATH = os.environ.get(
    "FFMPEG_PATH", "./ffmpeg-2026-06-29-git-de6bcf5c05-essentials_build/bin"
)
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "ytdlp_downloader")

# YouTube increasingly requires cookies from a logged-in session to avoid
# "Sign in to confirm you're not a bot" errors. Export cookies from your
# browser (e.g. via the "Get cookies.txt LOCALLY" extension) while logged
# into YouTube, and point to the exported file here. Leave unset to
# download without cookies (works for most videos, but not all).
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE")

# YouTube now requires a JS runtime (Deno) to solve "n challenge" signature
# puzzles; without it, only image formats are returned and downloads fail
# with "Requested format is not available". yt-dlp fetches the required
# solver scripts on demand if allowed to via remote_components. Comma-separated,
# e.g. "ejs:github" (fetches from yt-dlp's GitHub releases) or "ejs:npm"
# (Deno/Bun only, fetches from npm). See https://github.com/yt-dlp/yt-dlp/wiki/EJS
_remote_components_raw = os.environ.get("YTDLP_REMOTE_COMPONENTS", "ejs:github")
YTDLP_REMOTE_COMPONENTS = {c.strip() for c in _remote_components_raw.split(",") if c.strip()}


def _base_ydl_opts() -> dict:
    """Options shared by every download (cookies, ffmpeg path, JS runtime, etc.)."""
    opts = {"ffmpeg_location": FFMPEG_PATH}
    if YTDLP_COOKIES_FILE:
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    if YTDLP_REMOTE_COMPONENTS:
        opts["remote_components"] = YTDLP_REMOTE_COMPONENTS
    return opts


app = FastAPI(title="YouTube Downloader API", version="3.0.0")


_cors_origins_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "*")
CORS_ALLOWED_ORIGINS = (
    ["*"] if _cors_origins_raw.strip() == "*"
    else [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)



mongo_client = MongoClient(MONGO_URI)
db = mongo_client[MONGO_DB_NAME]
jobs_collection = db["jobs"]
jobs_collection.create_index("job_id", unique=True)
jobs_collection.create_index("owner_id")


@app.on_event("startup")
def check_mongo_connection():
    try:
        mongo_client.admin.command("ping")
    except PyMongoError as e:
        
        raise RuntimeError(f"Could not connect to MongoDB at {MONGO_URI}: {e}")


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"




def get_client_id(
    x_client_id: str | None = Header(None, alias="X-Client-Id"),
    client_id: str | None = Query(None),
) -> str:
    cid = x_client_id or client_id
    if not cid:
        raise HTTPException(
            status_code=400,
            detail="Missing client id. Send it as an X-Client-Id header or client_id query param.",
        )
    return cid




class SingleDownloadRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")


class PlaylistDownloadRequest(BaseModel):
    url: str = Field(..., description="YouTube playlist URL")
    start: int = Field(1, ge=1, description="Start index (1-based)")
    end: int = Field(..., ge=1, description="End index (inclusive)")


# ---------- Job helpers (all synchronous — pymongo is a blocking driver,
# which is fine here since yt-dlp itself is blocking and runs in a
# background thread anyway) ----------

def _new_job(job_type: str, url: str, owner_id: str) -> str:
    job_id = str(uuid.uuid4())
    jobs_collection.insert_one(
        {
            "job_id": job_id,
            "owner_id": owner_id,
            "type": job_type,
            "url": url,
            "title": None,  # set once yt-dlp resolves the real video/playlist title
            "status": JobStatus.PENDING,
            "progress": None,
            "error": None,
            "files": [],  # every finished file path for this job (1 for single, N for playlist)
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    )
    return job_id


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s) -> str:
    """Strip terminal ANSI color codes yt-dlp embeds in its progress strings
    (meant for a terminal, not a browser) so the UI doesn't show garbage
    characters like the escape sequences around '100.0%'."""
    return _ANSI_RE.sub("", s).strip() if s else ""


def _update_job(job_id: str, **fields):
    fields["updated_at"] = datetime.now(timezone.utc)
    jobs_collection.update_one({"job_id": job_id}, {"$set": fields})


def _append_job_file(job_id: str, filepath: str):
    jobs_collection.update_one(
        {"job_id": job_id},
        {"$addToSet": {"files": filepath}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )


def _get_job(job_id: str, owner_id: str) -> dict | None:
    # Scoped by owner_id so one client can't look up another client's job_id.
    return jobs_collection.find_one({"job_id": job_id, "owner_id": owner_id}, {"_id": 0})


def _job_title_from_info(job_type: str, info: dict) -> str | None:
    """Prefer the playlist title for playlist jobs, the video title otherwise."""
    if not info:
        return None
    if job_type.startswith("playlist"):
        return info.get("playlist_title") or info.get("playlist") or info.get("title")
    return info.get("title")


def _make_progress_hook(job_id: str, job_type: str, title_state: dict):
    def hook(d):
        if d["status"] == "downloading":
            fields = {
                "status": JobStatus.RUNNING,
                "progress": {
                    "filename": d.get("filename"),
                    "percent": _clean(d.get("_percent_str", "")),
                    "speed": _clean(d.get("_speed_str", "")),
                    "eta": _clean(d.get("_eta_str", "")),
                },
            }
            # Grab the real title the first time yt-dlp resolves it, instead
            # of leaving the queue showing the raw URL for the whole download.
            if not title_state["set"]:
                title = _job_title_from_info(job_type, d.get("info_dict") or {})
                if title:
                    fields["title"] = title
                    title_state["set"] = True
            _update_job(job_id, **fields)
    return hook


def _make_postprocessor_hook(job_id: str, job_type: str, title_state: dict):
    # Fires after ffmpeg finishes (e.g. mp4 merge, mp3 extraction), so this
    # gives the *actual* final filename rather than the pre-conversion temp
    # file. Appends rather than overwrites, since a playlist job produces
    # one "finished" event per track.
    def hook(d):
        if d["status"] == "finished":
            info = d.get("info_dict", {})
            filepath = info.get("filepath")
            if filepath:
                _append_job_file(job_id, filepath)
            if not title_state["set"]:
                title = _job_title_from_info(job_type, info)
                if title:
                    _update_job(job_id, title=title)
                    title_state["set"] = True
    return hook


def _run_download(job_id: str, job_type: str, ydl_opts: dict, url: str):
    title_state = {"set": False}
    ydl_opts["progress_hooks"] = [_make_progress_hook(job_id, job_type, title_state)]
    ydl_opts["postprocessor_hooks"] = [_make_postprocessor_hook(job_id, job_type, title_state)]
    _update_job(job_id, status=JobStatus.RUNNING)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        fields = {"status": JobStatus.COMPLETED}
        if not title_state["set"]:
            title = _job_title_from_info(job_type, info or {})
            if title:
                fields["title"] = title
        _update_job(job_id, **fields)
    except Exception as e:
        _update_job(job_id, status=JobStatus.FAILED, error=str(e))


def _cleanup_paths(*paths: str):
    """Best-effort delete of files (and their now-possibly-empty parent dirs)."""
    for path in paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
                parent = os.path.dirname(path)
                if parent and os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass  # never let cleanup crash a response that already succeeded


# ---------- Endpoints ----------

@app.post("/download/audio")
def download_audio(
    req: SingleDownloadRequest,
    background_tasks: BackgroundTasks,
    owner_id: str = Depends(get_client_id),
):
    job_id = _new_job("audio", req.url, owner_id)
    ydl_opts = {
        **_base_ydl_opts(),
        "format": "bestaudio/best",
        "outtmpl": "Single Audio/%(title)s.%(ext)s",
        "ignoreerrors": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    background_tasks.add_task(_run_download, job_id, "audio", ydl_opts, req.url)
    return {"job_id": job_id, "status": JobStatus.PENDING}


@app.post("/download/playlist-audio")
def download_playlist_audio(
    req: PlaylistDownloadRequest,
    background_tasks: BackgroundTasks,
    owner_id: str = Depends(get_client_id),
):
    if req.end < req.start:
        raise HTTPException(status_code=400, detail="end must be >= start")

    job_id = _new_job("playlist-audio", req.url, owner_id)
    ydl_opts = {
        **_base_ydl_opts(),
        "format": "bestaudio/best",
        "outtmpl": f"Playlist Audio/{job_id}/%(playlist_index)s - %(title)s.%(ext)s",
        "playliststart": req.start,
        "playlistend": req.end,
        "ignoreerrors": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    background_tasks.add_task(_run_download, job_id, "playlist-audio", ydl_opts, req.url)
    return {"job_id": job_id, "status": JobStatus.PENDING}


@app.post("/download/video")
def download_video(
    req: SingleDownloadRequest,
    background_tasks: BackgroundTasks,
    owner_id: str = Depends(get_client_id),
):
    job_id = _new_job("video", req.url, owner_id)
    ydl_opts = {
        **_base_ydl_opts(),
        "format": "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": "Single Videos/%(title)s.%(ext)s",
        "ignoreerrors": False,
    }
    background_tasks.add_task(_run_download, job_id, "video", ydl_opts, req.url)
    return {"job_id": job_id, "status": JobStatus.PENDING}


@app.post("/download/playlist-video")
def download_playlist_video(
    req: PlaylistDownloadRequest,
    background_tasks: BackgroundTasks,
    owner_id: str = Depends(get_client_id),
):
    if req.end < req.start:
        raise HTTPException(status_code=400, detail="end must be >= start")

    job_id = _new_job("playlist-video", req.url, owner_id)
    ydl_opts = {
        **_base_ydl_opts(),
        "format": "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": f"Playlist Videos/{job_id}/%(playlist_index)s - %(title)s.%(ext)s",
        "playliststart": req.start,
        "playlistend": req.end,
        "ignoreerrors": False,
    }
    background_tasks.add_task(_run_download, job_id, "playlist-video", ydl_opts, req.url)
    return {"job_id": job_id, "status": JobStatus.PENDING}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, owner_id: str = Depends(get_client_id)):
    job = _get_job(job_id, owner_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs/{job_id}/file")
def download_file(job_id: str, owner_id: str = Depends(get_client_id)):
    job = _get_job(job_id, owner_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != JobStatus.COMPLETED:
        raise HTTPException(status_code=409, detail=f"Job is {job['status']}, not ready yet")

    files = [f for f in job.get("files", []) if os.path.exists(f)]
    if not files:
        raise HTTPException(status_code=404, detail="File(s) not found on disk")

    if len(files) == 1:
        # Single file: serve directly, delete it (and its now-possibly-empty
        # folder) once the response has finished sending.
        filepath = files[0]
        return FileResponse(
            path=filepath,
            filename=os.path.basename(filepath),
            media_type="application/octet-stream",
            background=BackgroundTask(_cleanup_paths, filepath),
        )

    # Multiple files (a playlist): zip them into a temp dir, serve the zip,
    # then delete both the zip and every source file once sent.
    tmp_dir = tempfile.mkdtemp(prefix="ytdlp_zip_")
    zip_name = f"{job['type']}-{job_id[:8]}.zip"
    zip_path = os.path.join(tmp_dir, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=os.path.basename(f))

    # Clean up the zip's temp dir, the original downloaded files, and their
    # (now empty) playlist folder.
    playlist_dirs = {os.path.dirname(f) for f in files}
    cleanup_targets = [tmp_dir, *files, *playlist_dirs]

    return FileResponse(
        path=zip_path,
        filename=zip_name,
        media_type="application/zip",
        background=BackgroundTask(_cleanup_paths, *cleanup_targets),
    )


@app.get("/jobs")
def list_jobs(owner_id: str = Depends(get_client_id)):
    return list(
        jobs_collection.find({"owner_id": owner_id}, {"_id": 0}).sort("created_at", -1)
    )