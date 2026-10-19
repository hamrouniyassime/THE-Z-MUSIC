import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlparse

from flask import Flask, abort, jsonify, request, send_file
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
# Set DATA_DIR=/var/data and attach a Render persistent disk there to keep imports across deploys.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR))).resolve()
MUSIC_DIR = DATA_DIR / "music"
COVER_DIR = DATA_DIR / "covers"
TEMP_DIR = DATA_DIR / ".youtube_tmp"
METADATA_FILE = DATA_DIR / "youtube_metadata.json"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus", ".wma", ".aiff", ".aif", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

for directory in (MUSIC_DIR, COVER_DIR, TEMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="youtube-import")
jobs = {}
jobs_lock = threading.RLock()
metadata_lock = threading.RLock()


def read_metadata():
    try:
        with metadata_lock:
            data = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_metadata(data):
    with metadata_lock:
        temporary = METADATA_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(METADATA_FILE)


def get_audio_files():
    files = []
    for folder in (BASE_DIR, MUSIC_DIR):
        try:
            for path in folder.iterdir():
                if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS and not path.name.startswith("."):
                    files.append(path)
        except OSError:
            continue
    return sorted(files, key=lambda p: p.name.casefold())


def is_youtube_url(raw_url):
    if not isinstance(raw_url, str) or len(raw_url) > 2048:
        return False
    try:
        parsed = urlparse(raw_url.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed_host = (
            host == "youtu.be"
            or host.endswith(".youtu.be")
            or host == "youtube.com"
            or host.endswith(".youtube.com")
        )
        return parsed.scheme in {"http", "https"} and allowed_host and bool(parsed.path)
    except (ValueError, AttributeError):
        return False


def update_job(job_id, **fields):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)
            jobs[job_id]["updatedAt"] = time.time()


def progress_hook(job_id, info):
    state = info.get("status")
    if state == "downloading":
        downloaded = info.get("downloaded_bytes") or 0
        total = info.get("total_bytes") or info.get("total_bytes_estimate") or 0
        percent = min(88, int(downloaded * 88 / total)) if total else 5
        update_job(job_id, status="downloading", progress=max(1, percent))
    elif state == "finished":
        update_job(job_id, status="processing", progress=91)


def find_cover(folder, video_id):
    for path in folder.iterdir():
        if path.is_file() and path.stem == video_id and path.suffix.lower() in IMAGE_EXTENSIONS:
            return path
    return None


def import_youtube_video(job_id, raw_url):
    temporary_dir = TEMP_DIR / job_id
    temporary_dir.mkdir(parents=True, exist_ok=True)
    try:
        update_job(job_id, status="downloading", progress=1)
        options = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "outtmpl": {"default": str(temporary_dir / "%(id)s.%(ext)s")},
            "writethumbnail": True,
            "socket_timeout": 25,
            "retries": 2,
            "fragment_retries": 2,
            "file_access_retries": 1,
            "progress_hooks": [lambda data: progress_hook(job_id, data)],
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(raw_url, download=True)
        if not isinstance(info, dict):
            raise RuntimeError("No video metadata returned")
        video_id = str(info.get("id") or "")
        if not YOUTUBE_ID_RE.fullmatch(video_id):
            raise RuntimeError("Invalid video identifier")
        title = str(info.get("title") or "YouTube audio").strip()[:300] or "YouTube audio"
        final_audio = MUSIC_DIR / f"youtube_{video_id}.mp3"
        if not final_audio.exists():
            candidates = [p for p in temporary_dir.glob("*.mp3") if p.is_file()]
            if not candidates:
                raise RuntimeError("MP3 conversion did not produce a file; check that ffmpeg is installed")
            shutil.move(str(candidates[0]), str(final_audio))

        cover_filename = None
        cover_source = find_cover(temporary_dir, video_id)
        if cover_source:
            cover_target = COVER_DIR / f"youtube_{video_id}{cover_source.suffix.lower()}"
            shutil.copy2(cover_source, cover_target)
            cover_filename = cover_target.name

        with metadata_lock:
            metadata = read_metadata()
            metadata[final_audio.name] = {
                "title": title,
                "videoId": video_id,
                "coverFilename": cover_filename,
                "source": "youtube",
            }
            write_metadata(metadata)
        update_job(job_id, status="done", progress=100, trackName=title)
    except Exception as exc:
        # Keep detailed diagnostics in server logs; expose only a generic message to the browser.
        app.logger.warning("YouTube import failed for job %s: %s", job_id, exc)
        update_job(job_id, status="error", progress=0, error="import_failed")
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


@app.get("/")
def index():
    return send_file(BASE_DIR / "index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/tracks")
def tracks():
    result = []
    metadata = read_metadata()
    for path in get_audio_files():
        imported = path.parent == MUSIC_DIR
        encoded = quote(path.name)
        meta = metadata.get(path.name, {})
        cover_filename = meta.get("coverFilename") if isinstance(meta, dict) else None
        cover_url = None
        if isinstance(cover_filename, str) and Path(cover_filename).name == cover_filename:
            cover_path = COVER_DIR / cover_filename
            if cover_path.is_file() and cover_path.suffix.lower() in IMAGE_EXTENSIONS:
                cover_url = f"/covers/{quote(cover_filename)}"
        display_name = meta.get("title") if isinstance(meta, dict) else None
        result.append({
            "name": display_name or path.stem,
            "filename": path.name,
            "ext": path.suffix.lstrip(".").lower(),
            "streamUrl": f"/music/imported/{encoded}" if imported else f"/music/{encoded}",
            "downloadUrl": f"/download/imported/{encoded}" if imported else f"/download/{encoded}",
            "coverUrl": cover_url,
        })
    return jsonify({"tracks": result, "count": len(result)})


def resolve_audio(filename, imported=False):
    if not filename or Path(filename).name != filename:
        abort(404)
    folder = MUSIC_DIR if imported else BASE_DIR
    path = folder / filename
    if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
        abort(404)
    return path


@app.get("/music/imported/<path:filename>")
def stream_imported_music(filename):
    path = resolve_audio(filename, imported=True)
    mime, _ = mimetypes.guess_type(path.name)
    return send_file(path, mimetype=mime or "application/octet-stream", conditional=True, as_attachment=False)


@app.get("/download/imported/<path:filename>")
def download_imported_music(filename):
    path = resolve_audio(filename, imported=True)
    mime, _ = mimetypes.guess_type(path.name)
    return send_file(path, mimetype=mime or "application/octet-stream", as_attachment=True, download_name=path.name, conditional=True)


@app.get("/music/<path:filename>")
def stream_music(filename):
    path = resolve_audio(filename)
    mime, _ = mimetypes.guess_type(path.name)
    return send_file(path, mimetype=mime or "application/octet-stream", conditional=True, as_attachment=False)


@app.get("/download/<path:filename>")
def download_music(filename):
    path = resolve_audio(filename)
    mime, _ = mimetypes.guess_type(path.name)
    return send_file(path, mimetype=mime or "application/octet-stream", as_attachment=True, download_name=path.name, conditional=True)


@app.get("/covers/<path:filename>")
def serve_cover(filename):
    if not filename or Path(filename).name != filename:
        abort(404)
    path = COVER_DIR / filename
    if path.suffix.lower() not in IMAGE_EXTENSIONS or not path.is_file():
        abort(404)
    mime, _ = mimetypes.guess_type(path.name)
    return send_file(path, mimetype=mime or "application/octet-stream", conditional=True, max_age=86400)


@app.post("/api/youtube-import")
def start_youtube_import():
    payload = request.get_json(silent=True) or {}
    url = payload.get("url")
    if not is_youtube_url(url):
        return jsonify({"error": "Enter a valid YouTube video URL.", "code": "invalid_url"}), 400

    now = time.time()
    with jobs_lock:
        # Keep the in-memory status registry bounded.
        expired = [key for key, value in jobs.items() if value.get("status") in {"done", "error"} and now - value.get("updatedAt", now) > 3600]
        for key in expired:
            jobs.pop(key, None)
        active = sum(1 for value in jobs.values() if value.get("status") in {"queued", "downloading", "processing"})
        if active >= 2:
            return jsonify({"error": "The import queue is full.", "code": "busy"}), 429
        job_id = uuid.uuid4().hex
        jobs[job_id] = {"status": "queued", "progress": 0, "updatedAt": now}
    executor.submit(import_youtube_video, job_id, url.strip())
    return jsonify({"jobId": job_id, "status": "queued"}), 202


@app.get("/api/youtube-import/<job_id>")
def youtube_import_status(job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id or ""):
        abort(404)
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Import job not found.", "code": "job_not_found"}), 404
        return jsonify({key: value for key, value in job.items() if key != "updatedAt"})


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
