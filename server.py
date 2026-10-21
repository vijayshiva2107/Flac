import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
MANIFEST = {
    "id": "bitchord-internet-archive-flac",
    "name": "Internet Archive FLAC",
    "version": "1.0.0",
    "resources": ["search", "stream"],
    "settings": [{
        "key": "quality",
        "type": "select",
        "default": "lossless",
        "options": [
            {"label": "Lossless", "value": "lossless"},
            {"label": "High", "value": "high"},
            {"label": "Low", "value": "low"}
        ]
    }]
}
session = requests.Session()
session.headers.update({"User-Agent": "BitChord-Internet-Archive-FLAC/1.0"})
IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/{}"

def numeric(value):
    if value in (None, ""):
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    n = float(match.group(0))
    return int(n) if n.is_integer() else n

def flac_info(url):
    try:
        with session.get(url, headers={"Range": "bytes=0-63"}, timeout=(5, 10), stream=True) as r:
            r.raise_for_status()
            data = r.raw.read(64)
        if len(data) < 26 or data[:4] != b"fLaC":
            return None, None
        if (data[4] & 0x7f) != 0:
            return None, None
        packed = int.from_bytes(data[18:26], "big")
        sample_rate = packed >> 44
        bit_depth = ((packed >> 36) & 0x1f) + 1
        return (sample_rate if 1000 <= sample_rate <= 768000 else None,
                bit_depth if 4 <= bit_depth <= 32 else None)
    except requests.RequestException:
        return None, None

def get_flac(identifier):
    r = session.get(IA_METADATA.format(quote(identifier, safe="")), timeout=(5, 20))
    r.raise_for_status()
    candidates = [
        f for f in r.json().get("files", [])
        if str(f.get("name", "")).lower().endswith(".flac")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda f: numeric(f.get("size")) or 0, reverse=True)
    for item in candidates[:5]:
        name = str(item.get("name", ""))
        url = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(name, safe='')}"
        sr = numeric(item.get("sample_rate") or item.get("samplerate"))
        bd = numeric(item.get("bit_depth") or item.get("bitdepth"))
        if sr is None or bd is None:
            detected_sr, detected_bd = flac_info(url)
            sr = sr if sr is not None else detected_sr
            bd = bd if bd is not None else detected_bd
        return {
            "url": url, "filename": name, "length": numeric(item.get("length")),
            "bitrate": numeric(item.get("bitrate")), "sampleRate": sr, "bitDepth": bd
        }
    return None

def quality_label(info):
    details = []
    if info.get("bitDepth"):
        details.append(f"{info['bitDepth']}-bit")
    if info.get("sampleRate"):
        details.append(f"{info['sampleRate'] / 1000:g} kHz")
    hi_res = (info.get("bitDepth") or 0) > 16 or (info.get("sampleRate") or 0) > 48000
    return ("Hi-Res Lossless" if hi_res else "Lossless") + (f" · {' / '.join(details)}" if details else "")

def track_for(doc, info):
    identifier = str(doc["identifier"])
    creator = doc.get("creator") or "Internet Archive"
    if isinstance(creator, list):
        creator = creator[0] if creator else "Internet Archive"
    return {
        "id": identifier,
        "title": str(doc.get("title") or info["filename"]),
        "artist": str(creator),
        "album": str(doc.get("album") or ""),
        "duration": info.get("length"),
        "artworkURL": f"https://archive.org/services/img/{quote(identifier, safe='')}",
        "format": "flac",
        "audioQuality": "LOSSLESS",
        "quality": quality_label(info),
        "sampleRate": info.get("sampleRate"),
        "bitDepth": info.get("bitDepth"),
        "bitrate": info.get("bitrate")
    }

@app.get("/")
@app.get("/manifest.json")
def manifest():
    return jsonify(MANIFEST)

@app.get("/health")
def health():
    return jsonify({"ok": True, "name": MANIFEST["name"], "version": MANIFEST["version"]})

@app.get("/search")
def search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"tracks": []})
    try:
        limit = min(max(int(request.args.get("limit", "20")), 1), 20)
    except ValueError:
        limit = 20
    try:
        r = session.get(IA_SEARCH, params={
            "q": f"(mediatype:audio) AND ({query})",
            "fl[]": ["identifier", "title", "creator", "album"],
            "rows": min(limit * 4, 60), "page": 1, "output": "json"
        }, timeout=(5, 20))
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
    except (requests.RequestException, ValueError):
        return jsonify({"tracks": [], "error": "Internet Archive search is temporarily unavailable"}), 502

    tracks = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(get_flac, str(doc["identifier"])): doc
                   for doc in docs if doc.get("identifier")}
        for future in as_completed(futures):
            try:
                info = future.result()
                if info:
                    tracks.append(track_for(futures[future], info))
                if len(tracks) >= limit:
                    break
            except Exception:
                continue
    return jsonify({"tracks": tracks})

def stream_response(identifier):
    if not identifier:
        return jsonify({"error": "missing id"}), 400
    try:
        info = get_flac(identifier)
    except requests.RequestException:
        return jsonify({"error": "Internet Archive is temporarily unavailable"}), 502
    if not info:
        return jsonify({"error": "No FLAC file found"}), 404
    return jsonify({
        "url": info["url"], "format": "flac", "quality": quality_label(info),
        "audioQuality": "LOSSLESS", "codec": "flac", "container": "flac",
        "manifest": "none", "encrypted": False, "sampleRate": info.get("sampleRate"),
        "bitDepth": info.get("bitDepth"), "bitrate": info.get("bitrate"),
        "audioMode": "stereo"
    })

@app.get("/stream/<path:identifier>")
def stream_path(identifier):
    return stream_response(identifier)

@app.get("/stream")
def stream_query():
    return stream_response(request.args.get("id"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
