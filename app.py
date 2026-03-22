import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CALIBRE_LIBRARY = os.environ.get("CALIBRE_LIBRARY", "/calibre-library")
DOWNLOAD_DIR    = os.environ.get("DOWNLOAD_DIR", "/downloads")
ANNAS_BASE      = "https://annas-archive.org"
ANNAS_API_KEY   = os.environ.get("ANNAS_ARCHIVE_KEY", "")

# In-memory job tracker
jobs = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

FORMAT_ICONS = {
    "epub": "📖", "pdf": "📄", "mobi": "📱", "azw3": "📱",
    "fb2": "📝", "djvu": "🗂️", "cbz": "🖼️", "cbr": "🖼️",
}


# ── Search (scrape — no auth needed) ─────────────────────────────────────────

def scrape_search(query: str, fmt: str = "") -> list[dict]:
    params = {"q": query, "lang": "", "content": "book_any", "ext": fmt, "sort": ""}
    try:
        resp = requests.get(f"{ANNAS_BASE}/search", params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for item in soup.select("a[href^='/md5/']")[:30]:
        try:
            md5 = item["href"].split("/md5/")[1].rstrip("/")

            title_el = item.select_one(".text-xl, .text-lg, h3, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else item.get_text(" ", strip=True)[:80]

            author_el = item.select_one("[class*='author'], .text-sm.italic, .text-gray")
            author = author_el.get_text(strip=True) if author_el else "Unknown"

            badges = item.select(".bg-\\[\\#0000000f\\], .shrink-0, span")
            fmt_found = size_found = year_found = lang_found = ""
            for b in badges:
                t = b.get_text(strip=True).lower()
                for f in FORMAT_ICONS:
                    if f in t:
                        fmt_found = f
                if re.search(r"\d+(\.\d+)?\s*(mb|kb|gb)", t):
                    size_found = b.get_text(strip=True)
                if re.search(r"\b(19|20)\d{2}\b", t):
                    year_found = re.search(r"\b(19|20)\d{2}\b", t).group()
                if re.search(r"\b(en|fr|de|es|ru|zh|ja|pt|it|nl|pl)\b", t):
                    lang_found = b.get_text(strip=True)

            img_el = item.select_one("img")
            cover = img_el["src"] if img_el and img_el.get("src") else ""

            if not title or not md5:
                continue

            results.append({
                "md5": md5,
                "title": title[:120],
                "author": author[:80],
                "format": fmt_found or "unknown",
                "size": size_found,
                "year": year_found,
                "language": lang_found,
                "cover": cover,
                "icon": FORMAT_ICONS.get(fmt_found, "📚"),
            })
        except Exception:
            continue

    return results


# ── Download (official fast-download API) ────────────────────────────────────

def get_fast_download_url(md5: str) -> str:
    """Call the official members API to get a direct download URL."""
    if not ANNAS_API_KEY:
        raise RuntimeError("ANNAS_ARCHIVE_KEY is not set — add it to your docker-compose env")

    resp = requests.get(
        f"{ANNAS_BASE}/dyn/api/fast_download.json",
        params={"md5": md5, "key": ANNAS_API_KEY},
        headers=HEADERS,
        timeout=20,
    )

    if resp.status_code == 403:
        raise RuntimeError("API key rejected — check ANNAS_ARCHIVE_KEY in your env")
    if resp.status_code == 404:
        raise RuntimeError(f"MD5 not found on Anna's Archive: {md5}")
    resp.raise_for_status()

    data = resp.json()

    # The API returns the download URL — field name may be 'download_url' or 'url'
    url = data.get("download_url") or data.get("url") or data.get("link")
    if not url:
        raise RuntimeError(f"API returned no download URL. Response: {data}")

    return url


def download_file(url: str, dest_dir: str, job_id: str) -> Path:
    """Stream-download a file, updating job progress."""
    Path(dest_dir).mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)

    resp = session.get(url, stream=True, timeout=120, allow_redirects=True)
    resp.raise_for_status()

    # Filename from Content-Disposition or URL
    cd = resp.headers.get("Content-Disposition", "")
    fname_match = re.search(r'filename[^;=\n]*=(["\']?)([^\n"\']+)\1', cd)
    if fname_match:
        filename = fname_match.group(2).strip()
    else:
        filename = urlparse(resp.url).path.split("/")[-1] or f"{job_id}.epub"
        filename = requests.utils.unquote(filename)

    filename = re.sub(r'[^\w\s\-\.]', '', filename).strip() or f"{job_id}.epub"
    dest_path = Path(dest_dir) / filename

    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0

    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded / total * 100)
                    jobs[job_id]["progress"] = pct
                    jobs[job_id]["status"] = f"Downloading... {pct}%"

    return dest_path


def calibre_import(filepath: Path, job_id: str):
    """Add the downloaded file to Calibre library using calibredb."""
    jobs[job_id]["status"] = "Importing into Calibre..."
    try:
        result = subprocess.run(
            ["calibredb", "add", str(filepath), "--library-path", CALIBRE_LIBRARY, "--duplicates"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            jobs[job_id]["status"] = "✅ Done! Added to Calibre."
            jobs[job_id]["progress"] = 100
            jobs[job_id]["done"] = True
        else:
            jobs[job_id]["status"] = f"⚠️ Downloaded but Calibre import failed: {result.stderr[:200]}"
            jobs[job_id]["done"] = True
    except FileNotFoundError:
        jobs[job_id]["status"] = "⚠️ Downloaded (calibredb not found — check container setup)"
        jobs[job_id]["done"] = True
    except Exception as e:
        jobs[job_id]["status"] = f"⚠️ Import error: {str(e)[:200]}"
        jobs[job_id]["done"] = True


def download_and_import(md5: str, job_id: str):
    try:
        jobs[job_id]["status"] = "Requesting download URL..."
        url = get_fast_download_url(md5)

        jobs[job_id]["status"] = "Starting download..."
        filepath = download_file(url, DOWNLOAD_DIR, job_id)

        calibre_import(filepath, job_id)
    except Exception as e:
        jobs[job_id]["status"] = f"❌ {str(e)[:300]}"
        jobs[job_id]["done"] = True


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    fmt   = request.args.get("format", "").strip().lower()
    if not query:
        return jsonify({"results": [], "error": "No query provided"})
    results = scrape_search(query, fmt)
    return jsonify({"results": results})


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json()
    md5  = data.get("md5", "").strip()
    if not md5 or not re.match(r'^[a-fA-F0-9]{32}$', md5):
        return jsonify({"error": "Invalid md5"}), 400

    if not ANNAS_API_KEY:
        return jsonify({"error": "ANNAS_ARCHIVE_KEY not set in environment"}), 500

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"md5": md5, "status": "Queued", "progress": 0, "done": False}

    thread = threading.Thread(target=download_and_import, args=(md5, job_id), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/jobs")
def api_jobs():
    return jsonify({"jobs": jobs})


@app.route("/api/config")
def api_config():
    """Let the frontend know if the API key is configured."""
    return jsonify({"api_key_set": bool(ANNAS_API_KEY)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)