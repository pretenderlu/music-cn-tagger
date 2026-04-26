"""Music CN Tagger — Flask web UI.

Run:
    python app.py
    # opens http://127.0.0.1:5174 in your default browser

Environment overrides (optional, for headless / LAN-server use):
    HOST          (default 127.0.0.1) — bind address; 0.0.0.0 to expose on LAN
    PORT          (default 5174)
    MUSIC_ROOT    (default empty)     — restrict the directory picker to this subtree
    OPEN_BROWSER  (default 1)         — auto-open the system browser on start
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

import tagger as tg

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

JOBS: dict[str, dict] = {}

MUSIC_ROOT = os.environ.get("MUSIC_ROOT", "").strip()
_SKIP_DIR_NAMES = {".", "..", "$RECYCLE.BIN", "System Volume Information",
                   "@eaDir", "#recycle", ".DS_Store"}


# ---------- pages ---------- #

@app.route("/")
def index():
    return render_template("index.html")


# ---------- directory browser (in-browser, container-safe) ---------- #

def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _default_browse_path() -> Path:
    if MUSIC_ROOT:
        return Path(MUSIC_ROOT)
    return Path.home()


@app.get("/api/browse")
def browse():
    """List entries (dirs + audio file counts) under the requested path,
    constrained to MUSIC_ROOT if it's set. Used by the frontend's directory
    picker modal — replaces a native OS dialog so it works in a container."""
    path_str = (request.args.get("path") or "").strip()
    path = Path(path_str).resolve() if path_str else _default_browse_path().resolve()

    # Constrain to MUSIC_ROOT subtree when configured
    root = Path(MUSIC_ROOT).resolve() if MUSIC_ROOT else None
    if root and not _is_within(path, root):
        path = root

    if not path.exists() or not path.is_dir():
        # Invalid path → fall back to default
        fallback = _default_browse_path().resolve()
        if path != fallback and fallback.is_dir():
            path = fallback
        else:
            return jsonify({"error": "目录不存在", "path": str(path)}), 404

    entries = []
    current_count = 0
    try:
        for entry in path.iterdir():
            if entry.name.startswith(".") or entry.name in _SKIP_DIR_NAMES:
                continue
            if entry.is_file():
                if entry.suffix.lower() in tg.MUSIC_EXTS:
                    current_count += 1
                continue
            if entry.is_dir():
                count = 0
                try:
                    for sub in entry.iterdir():
                        if sub.is_file() and sub.suffix.lower() in tg.MUSIC_EXTS:
                            count += 1
                except (PermissionError, OSError):
                    pass
                entries.append({
                    "name": entry.name,
                    "path": str(entry),
                    "music_count": count,
                })
    except PermissionError:
        return jsonify({"error": "权限不足"}), 403

    entries.sort(key=lambda x: x["name"].lower())

    parent: str | None = None
    if path.parent != path:
        if not root or _is_within(path.parent, root):
            parent = str(path.parent)

    return jsonify({
        "path": str(path),
        "parent": parent,
        "entries": entries,
        "current_count": current_count,
        "root": str(root) if root else None,
    })


# ---------- scan job ---------- #

@app.post("/api/scan")
def start_scan():
    data = request.get_json(silent=True) or {}
    directory = (data.get("directory") or "").strip()
    if not directory or not Path(directory).is_dir():
        return jsonify({"error": "无效的目录"}), 400

    sources_raw = data.get("sources") or ["netease", "itunes"]
    if isinstance(sources_raw, str):
        sources_raw = [s.strip() for s in sources_raw.split(",") if s.strip()]
    opts = tg.ScanOptions(
        limit=int(data.get("limit", 10)),
        threshold=float(data.get("threshold", 0.6)),
        delay=float(data.get("delay", 0.3)),
        vote_n=int(data.get("vote_n", 4)),
        per_track=bool(data.get("per_track", False)),
        country=str(data.get("country") or "tw"),
        simplified=bool(data.get("simplified", True)),
        sources=tuple(sources_raw),
    )

    job_id = f"j{int(time.time() * 1000)}"
    q: "queue.Queue[dict | None]" = queue.Queue()
    JOBS[job_id] = {"queue": q, "rows": [], "status": "running", "error": None}

    def runner():
        try:
            def on_event(event: dict):
                q.put(event)
                if event.get("type") == "row":
                    JOBS[job_id]["rows"].append(event["row"])

            tg.scan_directory(Path(directory), opts, on_event=on_event)
            JOBS[job_id]["status"] = "done"
        except Exception as e:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(None)  # sentinel

    threading.Thread(target=runner, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/scan/<job_id>/events")
def stream(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    q = job["queue"]

    def gen():
        # Replay any rows that were already collected if this is a reconnect.
        # (Simple impl: just stream new events.)
        while True:
            try:
                event = q.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if event is None:
                yield "event: end\ndata: {}\n\n"
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(gen(), mimetype="text/event-stream", headers=headers)


# ---------- preview / confirm flow ---------- #

@app.post("/api/itunes-preview")
def itunes_preview():
    """Search iTunes for albums matching the user-supplied album/artist names.
    Returns top candidates with full track listings, so the user can visually
    confirm a match before applying. Pure iTunes — no translation step."""
    data = request.get_json(silent=True) or {}
    album = (data.get("album") or "").strip()
    artist = (data.get("artist") or "").strip()
    folder = (data.get("folder") or "").strip()

    if not album and not artist:
        return jsonify({"error": "至少填一个：专辑名 或 艺人名"}), 400

    opts = tg.ScanOptions(
        country=str(data.get("country") or "tw"),
        simplified=bool(data.get("simplified", True)),
        limit=int(data.get("limit", 10)),
    )

    target_count = 0
    if folder and Path(folder).is_dir():
        target_count = sum(
            1 for p in Path(folder).iterdir()
            if p.is_file() and p.suffix.lower() in tg.MUSIC_EXTS
        )

    candidates: list[dict] = []
    seen: set[str] = set()

    def make_card(a, phase, score):
        return {
            "album_id": str(a.get("collectionId", "")),
            "name": tg.to_zh_cn(a.get("collectionName", ""), opts),
            "artist": tg.to_zh_cn(a.get("artistName", ""), opts),
            "track_count": a.get("trackCount"),
            "artwork": (a.get("artworkUrl100") or "").replace("100x100", "300x300"),
            "release_date": (a.get("releaseDate") or "")[:10],
            "phase": phase,
            "_score": round(score, 3),
        }

    # Phase A: direct combined search. Score by (rank, track-count match, name match).
    parts = [p for p in (artist, album) if p]
    if parts:
        direct = tg.itunes_search_albums(" ".join(parts), opts)
        for idx, a in enumerate(direct):
            if not (tg.has_cjk(a.get("collectionName", "")) or tg.has_cjk(a.get("artistName", ""))):
                continue
            aid = str(a.get("collectionId", ""))
            if not aid or aid in seen:
                continue
            seen.add(aid)
            rank_score = max(0.0, 1.0 - idx * 0.10)
            tc = a.get("trackCount") or 0
            if target_count:
                tc_diff = abs(tc - target_count)
                tc_score = 1.0 if tc_diff == 0 else (0.85 if tc_diff <= 1 else 0.5)
            else:
                tc_score = 0.6
            ns = tg.name_match_score(album, a.get("collectionName", "")) if album else 0.5
            score = rank_score * 0.3 + tc_score * 0.3 + ns * 0.4
            candidates.append(make_card(a, "direct", score))

    # Phase B: artist→albums + name/count match. Phase A search is often noisy,
    # so we always merge in the artist-discography candidates and let scoring
    # decide. Pinyin matching rescues cases like "Dan Dan You Qing" → 淡淡幽情.
    if artist:
        for a in tg._itunes_lookup_artist_albums(artist, opts):
            aid = str(a.get("collectionId", ""))
            if not aid or aid in seen:
                continue
            ns = tg.name_match_score(album, a.get("collectionName", "")) if album else 0.4
            tc = a.get("trackCount") or 0
            if target_count and abs(tc - target_count) <= 2:
                ns += 0.15
            if ns < 0.35:
                continue
            seen.add(aid)
            candidates.append(make_card(a, "artist-list", ns + 0.05))

    # Sort by score, take top 6 (phase B wins on name match even if phase A
    # produced 6 noisy direct results)
    candidates.sort(key=lambda c: c["_score"], reverse=True)
    candidates = candidates[:6]
    for c in candidates:
        c.pop("_score", None)
    for c in candidates:
        _, tracks = tg.itunes_album_detail(c["album_id"], opts)
        c["tracks"] = [
            {"no": t.get("no"), "cd": t.get("cd"),
             "name": t.get("name", ""), "artist": t.get("artist_name", "")}
            for t in tracks
        ]

    return jsonify({
        "candidates": candidates,
        "local_count": target_count,
        "query": " ".join(parts),
    })


@app.post("/api/match-folder")
def match_folder():
    """Given a confirmed (source, album_id) and a folder of audio files,
    run file-to-track matching and return rows. Used after the user picks
    a candidate from /api/itunes-preview."""
    data = request.get_json(silent=True) or {}
    folder_str = (data.get("folder") or "").strip()
    source = data.get("source") or "itunes"
    album_id = (data.get("album_id") or "").strip()

    folder = Path(folder_str) if folder_str else None
    if not folder or not folder.is_dir():
        return jsonify({"error": "无效的文件夹"}), 400
    if not album_id:
        return jsonify({"error": "缺少 album_id"}), 400

    opts = tg.ScanOptions(
        threshold=float(data.get("threshold", 0.6)),
        country=str(data.get("country") or "tw"),
        simplified=bool(data.get("simplified", True)),
    )

    files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in tg.MUSIC_EXTS
    )
    if not files:
        return jsonify({"error": "目录中没有音频文件"}), 400

    album, ne_tracks = tg.album_detail(source, album_id, opts)
    if not album or not ne_tracks:
        return jsonify({"error": "无法获取专辑曲目数据"}), 500

    tagged = [(f, tg.read_tags(f) or {}) for f in files]
    events: list[dict] = []
    def emit(ev): events.append(ev)
    rows = tg._build_rows_for_album(tagged, source, album, ne_tracks, 0.95, opts, emit)

    return jsonify({
        "rows": rows or [],
        "events": events,
        "folder": str(folder),
        "album": {
            "id": album_id, "source": source,
            "name": album.get("name"), "artist": album.get("artist_name"),
            "track_count": len(ne_tracks),
        },
    })


# ---------- apply ---------- #

@app.post("/api/apply")
def apply_changes():
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    rename = bool(data.get("rename", False))
    results = []
    ok_count = fail_count = skip_count = renamed_count = 0
    for row in rows:
        path = Path(row.get("file", ""))
        if not path.exists():
            results.append({"file": str(path), "ok": False, "error": "file missing"})
            fail_count += 1
            continue
        new_tags = {}
        for f in tg.TAG_FIELDS:
            v = (row.get(f"new_{f}") or "").strip()
            if v:
                new_tags[f] = v
        if not new_tags:
            results.append({"file": str(path), "ok": True, "skipped": True})
            skip_count += 1
            continue
        try:
            new_path = tg.write_tags(path, new_tags, rename=rename)
            entry = {"file": str(path), "ok": True}
            if rename and new_path != path:
                entry["new_file"] = str(new_path)
                renamed_count += 1
            results.append(entry)
            ok_count += 1
        except Exception as e:
            results.append({"file": str(path), "ok": False, "error": str(e)})
            fail_count += 1

    return jsonify({
        "results": results,
        "ok_count": ok_count,
        "fail_count": fail_count,
        "skip_count": skip_count,
        "renamed_count": renamed_count,
    })


# ---------- main ---------- #

def open_browser_when_ready(url: str):
    time.sleep(0.6)
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    HOST = os.environ.get("HOST", "127.0.0.1")
    PORT = int(os.environ.get("PORT", "5174"))
    OPEN_BROWSER = os.environ.get("OPEN_BROWSER", "1").lower() in ("1", "true", "yes")

    display_host = "localhost" if HOST in ("0.0.0.0", "::") else HOST
    URL = f"http://{display_host}:{PORT}"
    print(f"Music CN Tagger UI → {URL}")
    if MUSIC_ROOT:
        print(f"  (browse restricted to {MUSIC_ROOT})")
    if OPEN_BROWSER:
        threading.Thread(target=open_browser_when_ready, args=(URL,), daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
