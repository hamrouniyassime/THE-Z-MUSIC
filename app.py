import os
import mimetypes
from pathlib import Path
from flask import Flask, jsonify, send_file, abort, request

BASE_DIR = Path(__file__).resolve().parent
# Put music files in the same directory as app.py, index.html, Dockerfile and requirements.txt.
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus", ".wma", ".aiff", ".aif", ".webm"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GB request limit, if uploads are added later.


def get_audio_files():
    files = []
    for path in BASE_DIR.iterdir():
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS and not path.name.startswith("."):
            files.append(path)
    return sorted(files, key=lambda p: p.name.casefold())


@app.get("/")
def index():
    return send_file(BASE_DIR / "index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/tracks")
def tracks():
    result = []
    for path in get_audio_files():
        # URLs are generated from the filename; Flask route safely resolves the path below.
        from urllib.parse import quote
        encoded = quote(path.name)
        result.append({
            "name": path.stem,
            "filename": path.name,
            "ext": path.suffix.lstrip(".").lower(),
            "streamUrl": f"/music/{encoded}",
            "downloadUrl": f"/download/{encoded}",
        })
    return jsonify({"tracks": result, "count": len(result)})


def resolve_audio(filename):
    # Only allow a direct filename and supported audio extensions; block path traversal.
    if not filename or Path(filename).name != filename:
        abort(404)
    path = BASE_DIR / filename
    if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
        abort(404)
    return path


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


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
