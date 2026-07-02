"""
YouTube Downloader API (FastAPI + MongoDB)

Converts the original CLI script into a web API with background jobs.
Job state is persisted in MongoDB instead of an in-memory dict, so jobs
survive server restarts and are visible across multiple worker processes.

Run with:
    uvicorn main:app --reload

Requires a running MongoDB instance. Configure via a .env file in the
working directory (see .env.example), or real environment variables:
    MONGO_URI=mongodb://localhost:27017
    MONGO_DB_NAME=ytdlp_downloader
    FFMPEG_PATH=./ffmpeg-2026-06-29-git-de6bcf5c05-essentials_build/bin

Endpoints:
    POST /download/audio            -> single video, mp3
    POST /download/playlist-audio   -> playlist range, mp3
    POST /download/video            -> single video, mp4
    POST /download/playlist-video   -> playlist range, mp4
    GET  /jobs/{job_id}             -> check status / progress
    GET  /jobs/{job_id}/file        -> download the finished file
    GET  /jobs                      -> list all jobs
"""

import os
import uuid
from datetime import datetime, timezone
from enum import Enum

import yt_dlp
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.errors import PyMongoError

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

app = FastAPI(title="YouTube Downloader API", version="2.0.0")

# ---------- MongoDB setup ----------

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[MONGO_DB_NAME]
jobs_collection = db["jobs"]
jobs_collection.create_index("job_id", unique=True)


@app.on_event("startup")
def check_mongo_connection():
    try:
        mongo_client.admin.command("ping")
    except PyMongoError as e:
        # Fail loudly at startup rather than on the first request.
        raise RuntimeError(f"Could not connect to MongoDB at {MONGO_URI}: {e}")


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------- Request models ----------

class SingleDownloadRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")


class PlaylistDownloadRequest(BaseModel):
    url: str = Field(..., description="YouTube playlist URL")
    start: int = Field(1, ge=1, description="Start index (1-based)")
    end: int = Field(..., ge=1, description="End index (inclusive)")


# ---------- Job helpers (all synchronous — pymongo is a blocking driver,
# which is fine here since yt-dlp itself is blocking and runs in a
# background thread anyway) ----------

def _new_job(job_type: str, url: str) -> str:
    job_id = str(uuid.uuid4())
    jobs_collection.insert_one(
        {
            "job_id": job_id,
            "type": job_type,
            "url": url,
            "status": JobStatus.PENDING,
            "progress": None,
            "error": None,
            "file": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    )
    return job_id


def _update_job(job_id: str, **fields):
    fields["updated_at"] = datetime.now(timezone.utc)
    jobs_collection.update_one({"job_id": job_id}, {"$set": fields})


def _get_job(job_id: str) -> dict | None:
    return jobs_collection.find_one({"job_id": job_id}, {"_id": 0})


def _make_progress_hook(job_id: str):
    def hook(d):
        if d["status"] == "downloading":
            _update_job(
                job_id,
                status=JobStatus.RUNNING,
                progress={
                    "filename": d.get("filename"),
                    "percent": d.get("_percent_str", "").strip(),
                    "speed": d.get("_speed_str", "").strip(),
                    "eta": d.get("_eta_str", "").strip(),
                },
            )
    return hook


def _make_postprocessor_hook(job_id: str):
    # Fires after ffmpeg finishes (e.g. mp4 merge, mp3 extraction), so this
    # gives the *actual* final filename rather than the pre-conversion temp file.
    def hook(d):
        if d["status"] == "finished":
            filepath = d.get("info_dict", {}).get("filepath")
            if filepath:
                _update_job(job_id, file=filepath)
    return hook


def _run_download(job_id: str, ydl_opts: dict, url: str):
    ydl_opts["progress_hooks"] = [_make_progress_hook(job_id)]
    ydl_opts["postprocessor_hooks"] = [_make_postprocessor_hook(job_id)]
    _update_job(job_id, status=JobStatus.RUNNING)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        _update_job(job_id, status=JobStatus.COMPLETED)
    except Exception as e:
        _update_job(job_id, status=JobStatus.FAILED, error=str(e))


# ---------- Endpoints ----------

@app.post("/download/audio")
def download_audio(req: SingleDownloadRequest, background_tasks: BackgroundTasks):
    job_id = _new_job("audio", req.url)
    ydl_opts = {
        **_base_ydl_opts(),
        "format": "bestaudio/best",
        "outtmpl": "Single Audio/%(title)s.%(ext)s",
        "ignoreerrors": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    background_tasks.add_task(_run_download, job_id, ydl_opts, req.url)
    return {"job_id": job_id, "status": JobStatus.PENDING}


@app.post("/download/playlist-audio")
def download_playlist_audio(req: PlaylistDownloadRequest, background_tasks: BackgroundTasks):
    if req.end < req.start:
        raise HTTPException(status_code=400, detail="end must be >= start")

    job_id = _new_job("playlist-audio", req.url)
    ydl_opts = {
        **_base_ydl_opts(),
        "format": "bestaudio/best",
        "outtmpl": "Playlist Audio/%(playlist)s/%(playlist_index)s - %(title)s.%(ext)s",
        "playliststart": req.start,
        "playlistend": req.end,
        "ignoreerrors": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    background_tasks.add_task(_run_download, job_id, ydl_opts, req.url)
    return {"job_id": job_id, "status": JobStatus.PENDING}


@app.post("/download/video")
def download_video(req: SingleDownloadRequest, background_tasks: BackgroundTasks):
    job_id = _new_job("video", req.url)
    ydl_opts = {
        **_base_ydl_opts(),
        "format": "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": "Single Videos/%(title)s.%(ext)s",
        "ignoreerrors": False,
    }
    background_tasks.add_task(_run_download, job_id, ydl_opts, req.url)
    return {"job_id": job_id, "status": JobStatus.PENDING}


@app.post("/download/playlist-video")
def download_playlist_video(req: PlaylistDownloadRequest, background_tasks: BackgroundTasks):
    if req.end < req.start:
        raise HTTPException(status_code=400, detail="end must be >= start")

    job_id = _new_job("playlist-video", req.url)
    ydl_opts = {
        **_base_ydl_opts(),
        "format": "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": "Playlist Videos/%(playlist)s/%(playlist_index)s - %(title)s.%(ext)s",
        "playliststart": req.start,
        "playlistend": req.end,
        "ignoreerrors": False,
    }
    background_tasks.add_task(_run_download, job_id, ydl_opts, req.url)
    return {"job_id": job_id, "status": JobStatus.PENDING}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs/{job_id}/file")
def download_file(job_id: str):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != JobStatus.COMPLETED:
        raise HTTPException(status_code=409, detail=f"Job is {job['status']}, not ready yet")
    filepath = job.get("file")
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(
        path=filepath,
        filename=os.path.basename(filepath),
        media_type="application/octet-stream",
    )


@app.get("/jobs")
def list_jobs():
    return list(jobs_collection.find({}, {"_id": 0}).sort("created_at", -1))