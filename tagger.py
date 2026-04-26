"""Music CN Tagger — translate music tags to Chinese via iTunes / NetEase.

Library + CLI. The web UI (app.py) imports `scan_directory`, `write_tags`,
`ScanOptions`, plus the `itunes_*` and `netease_*` helpers directly.

Auto-scan flow is two-stage:
  1. Translate raw artist/album → Chinese names via iTunes (album search,
     with an artist→discography fallback that matches by pinyin so e.g.
     'Dan Dan You Qing' resolves to 淡淡幽情).
  2. Look up the Chinese album in each source from `ScanOptions.sources`
     in order (default: NetEase first, iTunes fallback). NetEase carries
     richer Chinese metadata when it has the album; iTunes covers the
     gaps (notably albums NetEase has lost to licensing).

Storefront defaults to Taiwan ('tw') for catalog completeness; iTunes
results are converted Traditional → Simplified via zhconv when
`simplified=True`.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

import requests
from mutagen import File as MutagenFile

try:
    from zhconv import convert as _zh_convert
    _HAS_ZHCONV = True
except ImportError:
    _HAS_ZHCONV = False

try:
    from pypinyin import lazy_pinyin
    _HAS_PYPINYIN = True
except ImportError:
    _HAS_PYPINYIN = False

MUSIC_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav"}
TAG_FIELDS = ["title", "artist", "album", "albumartist", "genre"]
HELPER_FIELDS = ["tracknumber", "discnumber"]

CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
ITUNES_HEADERS = {"User-Agent": "MusicCNTagger/1.0"}

NETEASE_SEARCH_URL = "https://music.163.com/api/search/get/web"
NETEASE_ALBUM_URL = "https://music.163.com/api/v1/album/{}"
NETEASE_HEADERS = {
    "Referer": "https://music.163.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Cookie": "appver=2.0.2;",
}


@dataclass
class ScanOptions:
    limit: int = 10
    threshold: float = 0.6
    delay: float = 0.3
    vote_n: int = 4
    per_track: bool = False
    country: str = "tw"
    simplified: bool = True
    sources: tuple[str, ...] = ("netease", "itunes")


# ---------- helpers ---------- #

def has_cjk(s: str) -> bool:
    return bool(s and CJK_RE.search(s))


def parse_int(v) -> int | None:
    if v is None:
        return None
    s = str(v).split("/")[0].strip()
    try:
        return int(s)
    except ValueError:
        return None


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower().strip(), (b or "").lower().strip()).ratio()


def to_zh_cn(text: str, opts: ScanOptions) -> str:
    """Convert Traditional → Simplified Chinese when enabled."""
    if not text or not opts.simplified or not _HAS_ZHCONV:
        return text or ""
    return _zh_convert(text, "zh-cn")


def _normalize_text(s: str) -> str:
    """Lowercase and strip non-alphanumeric. Used for transliteration matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _to_pinyin(s: str) -> str:
    """Chinese text → lowercase pinyin string (no separators).
    Empty string if pypinyin missing or input has no Chinese."""
    if not _HAS_PYPINYIN or not s:
        return ""
    try:
        return _normalize_text(" ".join(lazy_pinyin(s)))
    except Exception:
        return ""


def name_match_score(user_query: str, candidate_name: str) -> float:
    """Score similarity between a user query (English / pinyin / Chinese)
    and a candidate name (typically Chinese). Range: 0.0–1.0.

    Compares against the candidate's normalized form AND its pinyin form,
    so 'Dan Dan You Qing' matches '淡淡幽情'."""
    if not user_query or not candidate_name:
        return 0.0
    user_norm = _normalize_text(user_query)
    cand_norm = _normalize_text(candidate_name)
    cand_pinyin = _to_pinyin(candidate_name)

    if user_norm and cand_pinyin:
        if user_norm == cand_pinyin:
            return 1.0
        if user_norm in cand_pinyin or cand_pinyin in user_norm:
            return 0.92
    if user_norm and cand_norm:
        if user_norm == cand_norm:
            return 1.0
        if user_norm in cand_norm or cand_norm in user_norm:
            return 0.85
    if user_norm and cand_pinyin:
        return SequenceMatcher(None, user_norm, cand_pinyin).ratio()
    return 0.0


# ---------- mutagen ---------- #

def read_tags(path: Path) -> dict | None:
    try:
        audio = MutagenFile(str(path), easy=True)
    except Exception:
        return None
    if audio is None:
        return None
    out = {}
    for field_ in TAG_FIELDS + HELPER_FIELDS:
        v = audio.get(field_)
        if v:
            out[field_] = v[0] if isinstance(v, list) else str(v)
        else:
            out[field_] = ""
    return out


_FILENAME_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(s: str) -> str:
    """Strip characters illegal on Windows/POSIX, trim, cap length."""
    s = _FILENAME_ILLEGAL_RE.sub("", s or "")
    s = s.strip().rstrip(".").strip()
    return s[:180]


def build_track_filename(track_no, title: str, ext: str) -> str:
    """`05 - 晴天.mp3`. If no track number, drop the prefix."""
    safe = sanitize_filename(title)
    if not safe:
        return ""
    n = parse_int(track_no)
    if n is not None:
        return f"{n:02d} - {safe}{ext}"
    return f"{safe}{ext}"


def write_tags(path: Path, new_tags: dict, rename: bool = False) -> Path:
    """Write tags to the file. When `rename=True`, also rename the file to
    `<NN> - <new_title>.<ext>` (using the existing tracknumber tag and the
    incoming new title). Returns the path the file ended up at."""
    audio = MutagenFile(str(path), easy=True)
    if audio is None:
        raise RuntimeError(f"unsupported format: {path.suffix}")
    if audio.tags is None:
        audio.add_tags()

    track_no_existing = ""
    v = audio.get("tracknumber")
    if v:
        track_no_existing = v[0] if isinstance(v, list) else str(v)

    for field_, value in new_tags.items():
        if not value:
            continue
        try:
            audio[field_] = value
        except (KeyError, ValueError):
            pass
    audio.save()

    if not rename:
        return path

    new_title = (new_tags.get("title") or "").strip()
    if not new_title:
        return path
    new_name = build_track_filename(track_no_existing, new_title, path.suffix)
    if not new_name:
        return path
    target = path.parent / new_name
    if target == path:
        return path
    if target.exists() and target.resolve() != path.resolve():
        # Collision with a different file — append a counter
        stem = target.stem
        i = 2
        while True:
            target = path.parent / f"{stem} ({i}){path.suffix}"
            if not target.exists():
                break
            i += 1
    path.rename(target)
    return target


# ---------- normalized shapes ---------- #
# song:  {source, id, name, artist_name, album_id, album_name, no, cd, aliases}
# album: {source, id, name, artist_name, track_count}


# ---------- iTunes ---------- #

def itunes_search_songs(query: str, opts: ScanOptions) -> list[dict]:
    if not query.strip():
        return []
    try:
        r = requests.get(
            ITUNES_SEARCH_URL,
            params={
                "term": query, "country": opts.country, "media": "music",
                "entity": "song", "limit": opts.limit,
            },
            headers=ITUNES_HEADERS, timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
    except Exception:
        return []
    return [_itunes_track_to_normalized(t, opts) for t in results]


def itunes_search_albums(query: str, opts: ScanOptions) -> list[dict]:
    """Album search returns much more reliable rankings than song search for
    English queries that target Chinese-language albums. Use this as the
    primary album-resolver for iTunes."""
    if not query.strip():
        return []
    try:
        r = requests.get(
            ITUNES_SEARCH_URL,
            params={
                "term": query, "country": opts.country, "media": "music",
                "entity": "album", "limit": opts.limit,
            },
            headers=ITUNES_HEADERS, timeout=10,
        )
        r.raise_for_status()
        return r.json().get("results") or []
    except Exception:
        return []


def itunes_album_detail(album_id: str, opts: ScanOptions) -> tuple[dict, list[dict]]:
    try:
        r = requests.get(
            ITUNES_LOOKUP_URL,
            params={"id": album_id, "country": opts.country, "entity": "song"},
            headers=ITUNES_HEADERS, timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("results") or []
    except Exception:
        return {}, []
    album_raw = next((x for x in data if x.get("wrapperType") == "collection"), {})
    track_raws = [x for x in data if x.get("wrapperType") == "track"]
    if not album_raw:
        return {}, []
    album = {
        "source": "itunes",
        "id": str(album_raw.get("collectionId", "")),
        "name": to_zh_cn(album_raw.get("collectionName", ""), opts),
        "artist_name": to_zh_cn(album_raw.get("artistName", ""), opts),
        "track_count": album_raw.get("trackCount") or len(track_raws),
    }
    tracks = [_itunes_track_to_normalized(t, opts) for t in track_raws]
    return album, tracks


def _itunes_track_to_normalized(t: dict, opts: ScanOptions) -> dict:
    return {
        "source": "itunes",
        "id": str(t.get("trackId", "")),
        "name": to_zh_cn(t.get("trackName", ""), opts),
        "artist_name": to_zh_cn(t.get("artistName", ""), opts),
        "album_id": str(t.get("collectionId", "")),
        "album_name": to_zh_cn(t.get("collectionName", ""), opts),
        "album_artist_name": to_zh_cn(t.get("artistName", ""), opts),
        "no": t.get("trackNumber"),
        "cd": t.get("discNumber"),
        "aliases": [],
    }


# ---------- NetEase ---------- #

def _netease_search_raw(query: str, limit: int) -> list[dict]:
    if not query.strip():
        return []
    try:
        r = requests.post(
            NETEASE_SEARCH_URL,
            data={"s": query, "type": 1, "limit": limit, "offset": 0},
            headers=NETEASE_HEADERS, timeout=10,
        )
        r.raise_for_status()
        return ((r.json().get("result") or {}).get("songs")) or []
    except Exception:
        return []


def _netease_search_albums_raw(query: str, limit: int) -> list[dict]:
    """NetEase album search (type=10). Reliable for Chinese queries; near-empty for English."""
    if not query.strip():
        return []
    try:
        r = requests.post(
            NETEASE_SEARCH_URL,
            data={"s": query, "type": 10, "limit": limit, "offset": 0},
            headers=NETEASE_HEADERS, timeout=10,
        )
        r.raise_for_status()
        return ((r.json().get("result") or {}).get("albums")) or []
    except Exception:
        return []


def _netease_album_raw(album_id: int) -> tuple[dict, list[dict]]:
    try:
        r = requests.get(NETEASE_ALBUM_URL.format(album_id), headers=NETEASE_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("album") or {}, data.get("songs") or []
    except Exception:
        return {}, []


def netease_search_songs(query: str, opts: ScanOptions) -> list[dict]:
    raw = _netease_search_raw(query, opts.limit)
    return [_netease_song_to_normalized(s) for s in raw]


def netease_album_detail(album_id: str, opts: ScanOptions) -> tuple[dict, list[dict]]:
    try:
        aid = int(album_id)
    except (ValueError, TypeError):
        return {}, []
    album_raw, songs_raw = _netease_album_raw(aid)
    if not album_raw or not songs_raw:
        return {}, []
    artists = album_raw.get("artists") or []
    album = {
        "source": "netease",
        "id": str(album_raw.get("id", "")),
        "name": album_raw.get("name", ""),
        "artist_name": "/".join(a.get("name", "") for a in artists if a.get("name")),
        "track_count": len(songs_raw),
    }
    tracks = [_netease_album_track_to_normalized(s) for s in songs_raw]
    return album, tracks


def _netease_song_to_normalized(s: dict) -> dict:
    artists = s.get("artists") or []
    album = s.get("album") or {}
    artist_name = "/".join(a.get("name", "") for a in artists if a.get("name"))
    return {
        "source": "netease",
        "id": str(s.get("id", "")),
        "name": s.get("name", ""),
        "artist_name": artist_name,
        "album_id": str(album.get("id", "")),
        "album_name": album.get("name", ""),
        "album_artist_name": artist_name,
        "no": None,
        "cd": None,
        "aliases": [],
    }


def _netease_album_track_to_normalized(s: dict) -> dict:
    artists = s.get("ar") or []
    album = s.get("al") or {}
    artist_name = "/".join(a.get("name", "") for a in artists if a.get("name"))
    return {
        "source": "netease",
        "id": str(s.get("id", "")),
        "name": s.get("name", ""),
        "artist_name": artist_name,
        "album_id": str(album.get("id", "")),
        "album_name": album.get("name", ""),
        "album_artist_name": artist_name,
        "no": s.get("no"),
        "cd": s.get("cd"),
        "aliases": list(s.get("alia") or []),
    }


# ---------- dispatch ---------- #

def search_songs(source: str, query: str, opts: ScanOptions) -> list[dict]:
    if source == "itunes":
        return itunes_search_songs(query, opts)
    if source == "netease":
        return netease_search_songs(query, opts)
    return []


def album_detail(source: str, album_id: str, opts: ScanOptions) -> tuple[dict, list[dict]]:
    if source == "itunes":
        return itunes_album_detail(album_id, opts)
    if source == "netease":
        return netease_album_detail(album_id, opts)
    return {}, []


# ---------- matching (operates on normalized) ---------- #

def find_top_song(songs: list[dict]) -> tuple[dict | None, float]:
    if not songs:
        return None, 0.0

    def has_cn(s):
        return has_cjk(s.get("name", "")) or has_cjk(s.get("artist_name", ""))

    ranked = [(i, s) for i, s in enumerate(songs) if has_cn(s)]
    if not ranked:
        return None, 0.0
    rank, best = ranked[0]
    base = 0.85 if rank == 0 else (0.70 if rank == 1 else 0.55)
    artist = best.get("artist_name", "")
    if artist:
        same = sum(1 for s in songs if s.get("artist_name", "") == artist)
        if same >= 3:
            base = min(1.0, base + 0.10)
    return best, round(base, 2)


def build_song_query(tags: dict, fallback: str) -> str:
    parts, seen = [], set()
    for key in ("artist", "albumartist", "album", "title"):
        v = (tags.get(key) or "").strip()
        if v and v.lower() not in seen:
            parts.append(v)
            seen.add(v.lower())
    return " ".join(parts) or fallback


def match_file_to_track(tags: dict, ne_tracks: list[dict]) -> tuple[dict | None, str]:
    title = (tags.get("title") or "").strip()
    track_no = parse_int(tags.get("tracknumber"))
    disc_no = parse_int(tags.get("discnumber"))

    if track_no:
        cands = [t for t in ne_tracks if t.get("no") == track_no]
        if disc_no:
            cands = [t for t in cands if t.get("cd") == disc_no] or cands
        if len(cands) == 1:
            return cands[0], "tracknumber"
        if cands:
            for t in cands:
                if title and title.lower() in [a.lower() for a in t.get("aliases", [])]:
                    return t, "tracknumber+alias"
            return cands[0], "tracknumber"

    if title:
        tl = title.lower()
        for t in ne_tracks:
            if any(tl == a.lower() for a in t.get("aliases", [])):
                return t, "alias-exact"
        best, best_sim = None, 0.0
        for t in ne_tracks:
            cands = list(t.get("aliases") or []) + [t.get("name", "")]
            for c in cands:
                if not c:
                    continue
                s = similarity(title, c)
                if s > best_sim:
                    best_sim, best = s, t
        if best and best_sim >= 0.85:
            return best, f"alias-fuzzy({best_sim:.2f})"

    return None, "no-match"


# ---------- row building ---------- #

CSV_COLS = ["file", "apply", "confidence", "source", "match_method", "external_id"]
for _f in TAG_FIELDS:
    CSV_COLS += [f"old_{_f}", f"new_{_f}"]


def empty_new_tags():
    return {f: "" for f in TAG_FIELDS}


def build_row(path: Path, old: dict, new: dict, *, apply_default: int,
              confidence: float, source: str = "", external_id: str = "",
              method: str = "") -> dict:
    row = {c: "" for c in CSV_COLS}
    row["file"] = str(path)
    row["apply"] = apply_default
    row["confidence"] = f"{confidence:.2f}"
    row["source"] = source
    row["external_id"] = external_id
    row["match_method"] = method
    for f in TAG_FIELDS:
        row[f"old_{f}"] = (old or {}).get(f, "")
        row[f"new_{f}"] = (new or {}).get(f, "")
    return row


# ---------- scan core ---------- #

EmitFn = Callable[[dict], None]


def _noop(_event: dict) -> None:
    pass


def _score_album_candidate(rank: int, candidate_track_count: int, target_count: int) -> float:
    rank_score = max(0.0, 1.0 - rank * 0.08)
    diff = abs((candidate_track_count or 0) - target_count)
    if diff == 0:
        cs = 1.0
    elif diff <= 1:
        cs = 0.92
    elif diff <= 3:
        cs = 0.82
    else:
        cs = 0.65
    return rank_score * cs


def _itunes_lookup_artist_albums(artist_query: str, opts: ScanOptions) -> list[dict]:
    """Find the most likely artistId for an artist query, then return all of
    that artist's albums via /lookup. Falls back to []. Used as a second
    pass when direct album search misses (typical for albums whose English
    or pinyin alias isn't indexed)."""
    if not artist_query.strip():
        return []
    try:
        r = requests.get(ITUNES_SEARCH_URL,
                         params={"term": artist_query, "country": opts.country,
                                 "media": "music", "entity": "musicArtist", "limit": 5},
                         headers=ITUNES_HEADERS, timeout=10)
        r.raise_for_status()
        artists = r.json().get("results") or []
    except Exception:
        return []
    if not artists:
        return []

    # Prefer CJK-named artists (the genuine entry for Chinese pop is typically
    # registered with their Chinese name).
    cjk = [a for a in artists if has_cjk(a.get("artistName", ""))]
    pool = cjk if cjk else artists
    best = max(pool, key=lambda a: name_match_score(artist_query, a.get("artistName", "")))
    aid = best.get("artistId")
    if not aid:
        return []
    try:
        r = requests.get(ITUNES_LOOKUP_URL,
                         params={"id": aid, "country": opts.country,
                                 "entity": "album", "limit": 200},
                         headers=ITUNES_HEADERS, timeout=10)
        r.raise_for_status()
        results = r.json().get("results") or []
    except Exception:
        return []
    return [x for x in results if x.get("wrapperType") == "collection"]


def itunes_translate_album(raw_album: str, raw_artist: str, target_count: int,
                            opts: ScanOptions) -> dict | None:
    """Multi-phase iTunes translation:
       Phase A — direct combined album search (works when iTunes indexes
                 the album's English/pinyin alias, e.g. Jay Chou 'Yeh Hui-mei').
       Phase B — artist lookup → list all of artist's albums → match by
                 track count + pinyin/name similarity (rescues cases where
                 the alias isn't indexed, e.g. 'Dan Dan You Qing' → 淡淡幽情).
    """
    direct: dict | None = None

    # Phase A
    parts, seen = [], set()
    for v in (raw_artist, raw_album):
        v = (v or "").strip()
        if v and v.lower() not in seen:
            parts.append(v); seen.add(v.lower())
    if parts:
        query = " ".join(parts)
        albums = itunes_search_albums(query, opts)
        cjk = [a for a in albums if has_cjk(a.get("collectionName", "")) or has_cjk(a.get("artistName", ""))]
        if cjk:
            def s_direct(a):
                base = _score_album_candidate(albums.index(a), a.get("trackCount") or 0, target_count)
                ns = name_match_score(raw_album, a.get("collectionName", ""))
                return base * 0.5 + ns * 0.5
            best = max(cjk, key=s_direct)
            score = s_direct(best)
            direct = {
                "album_id": str(best.get("collectionId", "")),
                "album_name": to_zh_cn(best.get("collectionName", ""), opts),
                "artist_name": to_zh_cn(best.get("artistName", ""), opts),
                "track_count": best.get("trackCount") or 0,
                "score": round(min(1.0, score), 2),
                "query": query,
                "phase": "direct",
            }
            if direct["score"] >= 0.85:
                return direct

    # Phase B: artist → albums + name/count match
    if raw_artist:
        cands = _itunes_lookup_artist_albums(raw_artist, opts)
        if cands:
            window = [a for a in cands if abs((a.get("trackCount") or 0) - target_count) <= 2]
            pool = window if window else cands
            scored = [(a, name_match_score(raw_album, a.get("collectionName", ""))) for a in pool]
            scored.sort(key=lambda x: x[1], reverse=True)
            if scored and scored[0][1] >= 0.7:
                best, ns = scored[0]
                tc = best.get("trackCount") or 0
                tc_diff = abs(tc - target_count)
                tc_score = 1.0 if tc_diff == 0 else (0.85 if tc_diff <= 1 else 0.55)
                total = ns * 0.6 + tc_score * 0.4
                phase_b = {
                    "album_id": str(best.get("collectionId", "")),
                    "album_name": to_zh_cn(best.get("collectionName", ""), opts),
                    "artist_name": to_zh_cn(best.get("artistName", ""), opts),
                    "track_count": tc,
                    "score": round(min(1.0, total), 2),
                    "query": f"artist:{raw_artist}",
                    "phase": "artist-list",
                }
                if not direct or phase_b["score"] > direct["score"]:
                    return phase_b

    return direct


def netease_find_album(cn_album: str, cn_artist: str, target_count: int,
                        opts: ScanOptions) -> dict | None:
    """Find a NetEase album_id given Chinese album+artist. Tries album-search
    first (type=10), then song-search-extract as a fallback."""
    if not cn_album:
        return None
    query = f"{cn_artist} {cn_album}".strip()

    raw = _netease_search_albums_raw(query, opts.limit)
    cands = []
    if raw:
        # Prefer entries whose artist field contains cn_artist
        if cn_artist:
            primary = [a for a in raw if any(
                cn_artist in (x.get("name") or "") or (x.get("name") or "") in cn_artist
                for x in (a.get("artists") or [])
            )]
            cands = primary if primary else raw
        else:
            cands = raw
    if cands:
        def score(a):
            tc = a.get("size") or 0
            name_match = 1.0 if a.get("name") == cn_album else (
                0.7 if cn_album and cn_album in (a.get("name") or "") else 0.4)
            return _score_album_candidate(raw.index(a), tc, target_count) * name_match
        best = max(cands, key=score)
        s = score(best)
        if s >= 0.45:
            return {
                "album_id": str(best.get("id", "")),
                "album_name": best.get("name", ""),
                "album_artist": "/".join(x.get("name", "") for x in (best.get("artists") or [])),
                "track_count": best.get("size") or 0,
                "score": round(min(1.0, s), 2),
                "via": "album-search",
            }

    # Fallback: song search → extract album_id
    songs = _netease_search_raw(query, opts.limit)
    for s in songs:
        artists = s.get("artists") or []
        album = s.get("album") or {}
        a_name = album.get("name") or ""
        if cn_album and (a_name == cn_album or cn_album in a_name):
            if not cn_artist or any(cn_artist in (a.get("name") or "") for a in artists):
                return {
                    "album_id": str(album.get("id", "")),
                    "album_name": a_name,
                    "album_artist": "/".join(a.get("name", "") for a in artists if a.get("name")),
                    "track_count": 0,
                    "score": 0.75,
                    "via": "song-extract",
                }
    return None


def resolve_album_two_stage(tagged: list[tuple[Path, dict]], opts: ScanOptions, emit: EmitFn):
    """Two-stage resolution: translate names to Chinese, then look up the
    album in each enabled source by Chinese name. Returns
    (source, album_id, confidence) or None."""
    sample = next((t for _, t in tagged if t.get("album") or t.get("artist")), None)
    if not sample:
        return None

    raw_album = (sample.get("album") or "").strip()
    raw_artist = (sample.get("artist") or "").strip()
    target = len(tagged)

    cn_album, cn_artist = raw_album, raw_artist
    itunes_match: dict | None = None

    # Stage 1: translate via iTunes if input isn't already CJK
    if has_cjk(raw_album) and has_cjk(raw_artist):
        emit({"type": "translation_skipped", "album": cn_album, "artist": cn_artist})
    else:
        itunes_match = itunes_translate_album(raw_album, raw_artist, target, opts)
        if itunes_match:
            cn_album = itunes_match["album_name"]
            cn_artist = itunes_match["artist_name"]
            emit({
                "type": "translated",
                "via": "itunes",
                "from_album": raw_album, "from_artist": raw_artist,
                "to_album": cn_album, "to_artist": cn_artist,
                "itunes_album_id": itunes_match["album_id"],
                "track_count": itunes_match["track_count"],
            })
        else:
            emit({"type": "translation_failed",
                  "from_album": raw_album, "from_artist": raw_artist})

    # Stage 2: look up album in each source in priority order
    for source in opts.sources:
        if source == "netease":
            ne = netease_find_album(cn_album, cn_artist, target, opts)
            if ne:
                emit({
                    "type": "album_resolved",
                    "source": "netease",
                    "via": ne["via"],
                    "album_id": ne["album_id"],
                    "album_name": ne["album_name"],
                    "album_artist": ne["album_artist"],
                    "track_count": ne["track_count"] or target,
                })
                return ("netease", ne["album_id"], ne["score"])
        elif source == "itunes":
            if itunes_match:
                emit({
                    "type": "album_resolved",
                    "source": "itunes",
                    "via": "translation-result",
                    "album_id": itunes_match["album_id"],
                    "album_name": cn_album,
                    "album_artist": cn_artist,
                    "track_count": itunes_match["track_count"],
                })
                return ("itunes", itunes_match["album_id"], itunes_match["score"])
            t = itunes_translate_album(cn_album, cn_artist, target, opts)
            if t:
                emit({
                    "type": "album_resolved",
                    "source": "itunes",
                    "via": "second-pass",
                    "album_id": t["album_id"],
                    "album_name": t["album_name"],
                    "album_artist": t["artist_name"],
                    "track_count": t["track_count"],
                })
                return ("itunes", t["album_id"], t["score"])
    return None


def resolve_album_id(tagged: list[tuple[Path, dict]], opts: ScanOptions, emit: EmitFn,
                      source: str):
    candidates = [(f, t) for f, t in tagged if t.get("title")][: opts.vote_n]
    if not candidates:
        return None, 0.0

    votes: Counter = Counter()
    best_score = 0.0
    detail = {}
    for path, tags in candidates:
        q = build_song_query(tags, path.stem)
        songs = search_songs(source, q, opts)
        top, score = find_top_song(songs)
        if top and top.get("album_id"):
            aid = top["album_id"]
            votes[aid] += 1
            best_score = max(best_score, score)
            detail[aid] = (top.get("album_name", ""), top.get("artist_name", ""))
        time.sleep(opts.delay)

    if not votes:
        return None, 0.0
    winner, count = votes.most_common(1)[0]
    aname, aartist = detail.get(winner, ("?", "?"))
    emit({
        "type": "vote",
        "source": source,
        "votes": {str(k): v for k, v in votes.items()},
        "winner": str(winner),
        "n_voters": len(candidates),
        "count": count,
        "album_name": aname,
        "album_artist": aartist,
    })
    return winner, best_score


def _build_rows_for_album(tagged, source, album, ne_tracks, song_score, opts, emit):
    sorted_files = sorted(tagged, key=lambda x: x[0].name.lower())
    sorted_tracks = sorted(ne_tracks, key=lambda t: (t.get("cd") or 1, t.get("no") or 0))
    pos_map = {}
    if len(sorted_files) == len(sorted_tracks):
        pos_map = {sorted_files[i][0]: sorted_tracks[i] for i in range(len(sorted_files))}

    rows = []
    for path, tags in tagged:
        ne_track, method = match_file_to_track(tags, ne_tracks)
        if not ne_track and pos_map.get(path):
            ne_track, method = pos_map[path], "position"
        if not ne_track:
            row = build_row(path, tags, empty_new_tags(),
                            apply_default=0, confidence=0.0,
                            source=source, method=method)
            rows.append(row)
            emit({"type": "row", "row": row})
            continue
        new = {
            "title": ne_track.get("name", ""),
            "artist": ne_track.get("artist_name", ""),
            "album": album["name"],
            "albumartist": album["artist_name"],
            "genre": "",
        }
        if method.startswith(("tracknumber", "alias-exact")):
            conf = 0.95
        elif method.startswith("alias-fuzzy"):
            conf = 0.80
        elif method == "position":
            conf = 0.65
        else:
            conf = song_score
        result_cn = has_cjk(new["title"]) or has_cjk(new["artist"])
        apply_default = 1 if (conf >= opts.threshold and result_cn) else 0
        row = build_row(path, tags, new,
                        apply_default=apply_default, confidence=conf,
                        source=source, external_id=ne_track.get("id", ""), method=method)
        rows.append(row)
        emit({"type": "row", "row": row})
    return rows


def scan_folder_album(folder: Path, files: list[Path], opts: ScanOptions, emit: EmitFn):
    tagged = [(f, read_tags(f) or {}) for f in files]

    # Path 1: two-stage (translate via iTunes → look up in CN DB)
    result = resolve_album_two_stage(tagged, opts, emit)
    if result:
        source, album_id, conf = result
        album, ne_tracks = album_detail(source, album_id, opts)
        if album and ne_tracks and (has_cjk(album.get("name", "")) or has_cjk(album.get("artist_name", ""))):
            return _build_rows_for_album(tagged, source, album, ne_tracks, conf, opts, emit)

    # Path 2: NetEase song-vote — rescues folders whose album/artist tags are
    # missing or too garbled to translate but whose track titles search well.
    if "netease" in opts.sources:
        emit({"type": "source_try", "source": "netease"})
        album_id, song_score = resolve_album_id(tagged, opts, emit, "netease")
        if album_id:
            album, ne_tracks = album_detail("netease", album_id, opts)
            if album and ne_tracks and (has_cjk(album.get("name", "")) or has_cjk(album.get("artist_name", ""))):
                emit({
                    "type": "album_resolved", "source": "netease", "via": "song-vote",
                    "album_id": album_id,
                    "album_name": album["name"], "album_artist": album["artist_name"],
                    "track_count": len(ne_tracks),
                })
                return _build_rows_for_album(tagged, "netease", album, ne_tracks, song_score, opts, emit)
        emit({"type": "source_miss", "source": "netease"})
    return None


def scan_folder_per_track(folder: Path, files: list[Path], opts: ScanOptions, emit: EmitFn):
    rows = []
    for path in files:
        old = read_tags(path) or {}
        already_cn = has_cjk(old.get("title", "")) and has_cjk(old.get("artist", ""))
        if already_cn:
            row = build_row(path, old, empty_new_tags(),
                            apply_default=0, confidence=1.0, method="already-cn")
            rows.append(row)
            emit({"type": "row", "row": row})
            continue

        query = build_song_query(old, path.stem)
        best = None
        best_score = 0.0
        used_source = ""
        for source in opts.sources:
            songs = search_songs(source, query, opts)
            cand, score = find_top_song(songs)
            if cand:
                best, best_score, used_source = cand, score, source
                break

        new = empty_new_tags()
        ext_id = ""
        if best:
            ext_id = best.get("id", "")
            new["title"] = best.get("name", "")
            new["artist"] = best.get("artist_name", "")
            new["album"] = best.get("album_name", "")
            new["albumartist"] = best.get("album_artist_name", "") or best.get("artist_name", "")

        result_cn = best and (has_cjk(new["title"]) or has_cjk(new["artist"]))
        apply_default = 1 if (best and best_score >= opts.threshold and result_cn) else 0
        method = "per-track" if best else "no-match"

        row = build_row(path, old, new,
                        apply_default=apply_default, confidence=best_score,
                        source=used_source, external_id=ext_id, method=method)
        rows.append(row)
        emit({"type": "row", "row": row})
        time.sleep(opts.delay)
    return rows


def scan_directory(root: Path, opts: ScanOptions, on_event: EmitFn | None = None) -> list[dict]:
    emit: EmitFn = on_event or _noop
    root = Path(root).resolve()
    if not root.exists():
        emit({"type": "error", "message": f"directory not found: {root}"})
        return []

    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in MUSIC_EXTS)
    emit({"type": "start", "root": str(root), "file_count": len(files),
          "options": {"country": opts.country, "simplified": opts.simplified,
                      "sources": list(opts.sources)}})
    if not files:
        emit({"type": "done", "row_count": 0})
        return []

    groups: dict[Path, list[Path]] = {}
    for f in files:
        groups.setdefault(f.parent, []).append(f)
    folder_list = sorted(groups.items())
    emit({"type": "grouped", "folder_count": len(folder_list)})

    rows: list[dict] = []
    for i, (folder, group_files) in enumerate(folder_list, 1):
        emit({"type": "folder_start", "index": i, "total": len(folder_list),
              "folder": str(folder), "track_count": len(group_files)})

        new_rows = None
        if not opts.per_track:
            new_rows = scan_folder_album(folder, group_files, opts, emit)
            if new_rows is None:
                emit({"type": "fallback", "folder": str(folder)})
        if new_rows is None:
            new_rows = scan_folder_per_track(folder, group_files, opts, emit)
        rows.extend(new_rows)
        matched = sum(1 for r in new_rows if r.get("external_id"))
        emit({"type": "folder_done", "index": i,
              "matched": matched, "unmatched": len(new_rows) - matched})
        time.sleep(opts.delay)

    emit({"type": "done", "row_count": len(rows)})
    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------- CLI ---------- #

def cli_emitter(event: dict) -> None:
    t = event.get("type")
    if t == "start":
        print(f"found {event['file_count']} music file(s) under {event['root']}", flush=True)
        opts = event.get("options", {})
        print(f"  country={opts.get('country')}, simplified={opts.get('simplified')}, "
              f"sources={opts.get('sources')}", flush=True)
    elif t == "grouped":
        print(f"grouped into {event['folder_count']} folder(s)", flush=True)
    elif t == "folder_start":
        print(f"\n[{event['index']}/{event['total']}] {event['folder']}  "
              f"({event['track_count']} tracks)", flush=True)
    elif t == "source_try":
        print(f"  trying source: {event['source']}", flush=True)
    elif t == "source_miss":
        print(f"  · {event['source']} did not resolve", flush=True)
    elif t == "vote":
        print(f"  [{event['source']}] vote: {event['votes']} → winner={event['winner']} "
              f"({event['album_artist']} - {event['album_name']}, "
              f"{event['count']}/{event['n_voters']})", flush=True)
    elif t == "album_resolved":
        print(f"  [{event['source']}] album → {event['album_artist']} - {event['album_name']}  "
              f"({event['track_count']} tracks, id={event['album_id']})", flush=True)
    elif t == "fallback":
        print("  → all album sources failed; falling back to per-track search", flush=True)
    elif t == "row":
        r = event["row"]
        name = Path(r["file"]).name
        if r.get("external_id"):
            print(f"    ✓ {name}  [{r.get('source','')}/{r['match_method']}]  "
                  f"{r.get('old_title','')!r} → {r.get('new_title','')!r}", flush=True)
        else:
            print(f"    · {name}  → {r['match_method']}", flush=True)
    elif t == "error":
        print(f"ERROR: {event.get('message','')}", flush=True)


def cmd_scan(args):
    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    opts = ScanOptions(
        limit=args.limit, threshold=args.threshold, delay=args.delay,
        vote_n=args.vote_n, per_track=args.per_track,
        country=args.country, simplified=not args.no_simplified,
        sources=sources or ("itunes", "netease"),
    )
    rows = scan_directory(Path(args.directory), opts, on_event=cli_emitter)
    out_path = Path(args.output) if args.output else Path(args.directory).resolve() / "music_cn_suggestions.csv"
    write_csv(rows, out_path)
    auto = sum(1 for r in rows if str(r.get("apply")) == "1")
    print(f"\nwrote {len(rows)} row(s) to {out_path}")
    print(f"  {auto} row(s) marked apply=1 (confidence >= {args.threshold} and result has CJK)")


def cmd_apply(args):
    csv_path = Path(args.csv).resolve()
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    applied = skipped = failed = 0
    for row in rows:
        if str(row.get("apply", "0")).strip().lower() not in {"1", "true", "yes", "y"}:
            skipped += 1
            continue
        path = Path(row["file"])
        if not path.exists():
            print(f"[MISS] {path}")
            failed += 1
            continue
        new_tags = {}
        for f in TAG_FIELDS:
            v = (row.get(f"new_{f}") or "").strip()
            if v:
                new_tags[f] = v
        if not new_tags:
            skipped += 1
            continue
        if args.dry_run:
            print(f"[DRY] {path.name}")
            for k, v in new_tags.items():
                old_v = row.get(f"old_{k}", "")
                if old_v != v:
                    print(f"      {k}: {old_v!r} → {v!r}")
            applied += 1
            continue
        try:
            new_path = write_tags(path, new_tags, rename=args.rename)
            if new_path != path:
                print(f"[OK]  {path.name}  →  {new_path.name}")
            else:
                print(f"[OK]  {path.name}")
            applied += 1
        except Exception as e:
            print(f"[ERR] {path.name}: {e}")
            failed += 1

    label = "would apply" if args.dry_run else "applied"
    print(f"\n{label}: {applied}, skipped: {skipped}, failed: {failed}")


def main():
    p = argparse.ArgumentParser(description="Translate music tags to Chinese via iTunes/NetEase.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="scan a directory and produce a suggestion CSV")
    sp.add_argument("directory")
    sp.add_argument("-o", "--output")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--threshold", type=float, default=0.6)
    sp.add_argument("--delay", type=float, default=0.3)
    sp.add_argument("--vote-n", type=int, default=4)
    sp.add_argument("--per-track", action="store_true")
    sp.add_argument("--country", default="tw", help="iTunes storefront (default: tw)")
    sp.add_argument("--no-simplified", action="store_true",
                    help="keep Traditional Chinese as-is (default converts TW→CN)")
    sp.add_argument("--sources", default="itunes,netease",
                    help="comma-separated source order (default: itunes,netease)")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("apply", help="apply tag changes from a suggestion CSV")
    sp.add_argument("csv")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--rename", action="store_true",
                    help="also rename each file to '<NN> - <new_title>.<ext>'")
    sp.set_defaults(func=cmd_apply)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
