"""Encyclopedia-based artist/album resolution.

Resolves transliterated / English-tagged artist+album names into their
canonical Chinese names by consulting external knowledge bases. Used as
Stage 0 of the matching pipeline so that downstream iTunes/NetEase
searches can hit the canonical CJK entries directly.

Sources (in priority order):

1. **MusicBrainz** — has artist aliases tagged with locale=zh and full
   discography per artist. Covers HK / TW / CN releases well. Rate-
   limited to ~1 req/sec per their terms.
2. **Wikidata** — fallback when MB lacks a Chinese alias. Pulls
   labels.zh / P1448 (official name) and discography via P175.

This module returns *raw candidates* (artist with all aliases, albums
with all aliases) — the actual scoring/picking is left to tagger.py
which already has pinyin-aware matching.

All API responses are cached in cache.py for 30 days, keyed by the
normalized query, so scanning many albums by the same artist only hits
each remote endpoint once.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any

import requests

import cache as _cache

# ---------- shared helpers ---------- #

CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")


def has_cjk(s: str) -> bool:
    return bool(s and CJK_RE.search(s))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# ---------- MusicBrainz ---------- #

MB_BASE = "https://musicbrainz.org/ws/2"
MB_HEADERS = {
    "User-Agent": "music-cn-tagger/0.2 ( https://github.com/anthropics/claude-code )",
    "Accept": "application/json",
}
MB_MIN_INTERVAL = 1.05  # MusicBrainz allows ~1 req/sec; pad slightly

_mb_last_request = 0.0


def _mb_throttle() -> None:
    global _mb_last_request
    elapsed = time.time() - _mb_last_request
    if elapsed < MB_MIN_INTERVAL:
        time.sleep(MB_MIN_INTERVAL - elapsed)
    _mb_last_request = time.time()


def _mb_get(path: str, params: dict) -> dict | None:
    """GET an MB endpoint. Returns parsed JSON on 200, None on error.
    Retries once on transient SSL/connection errors (MB is intermittently
    flaky from some networks)."""
    _mb_throttle()
    p = dict(params)
    p["fmt"] = "json"
    last_exc = None
    for attempt in range(2):
        try:
            r = requests.get(f"{MB_BASE}/{path}", params=p, headers=MB_HEADERS, timeout=15)
            if r.status_code == 503:
                # MB rate-limit; back off and retry once
                time.sleep(2.0)
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            last_exc = e
            time.sleep(1.0 + attempt)
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            break
    return None


def _pick_zh_name(aliases: list[dict], primary_name: str = "") -> str | None:
    """From an MB alias list, pick the best Chinese name. Higher-priority
    aliases (locale=zh* + primary + type=Artist name) win. Falls back to
    the primary name if it's already CJK."""
    if has_cjk(primary_name):
        return primary_name
    candidates: list[tuple[int, str]] = []
    for al in aliases or []:
        nm = al.get("name") or ""
        if not has_cjk(nm):
            continue
        prio = 0
        if (al.get("locale") or "").lower().startswith("zh"):
            prio += 4
        if al.get("primary"):
            prio += 2
        if (al.get("type") or "").lower() in ("artist name", "release name"):
            prio += 1
        candidates.append((prio, nm))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _mb_find_artist(name: str) -> dict | None:
    """Return the most likely MB artist record for `name`. The record
    includes aliases; cn_name is computed and stored."""
    cache_key = _norm(name)
    cached = _cache.get("mb_artist", cache_key)
    if cached is not None:
        return cached or None  # {} sentinel = remembered miss

    # MB Lucene query — quote the phrase to keep multi-word artist names
    # together but rely on default-field tokenized matching (the explicit
    # `artist:"…"` form is too strict and returns 0 hits for some names).
    safe = name.replace('"', '').replace("\\", "")
    data = _mb_get("artist", {"query": f'"{safe}" OR {safe}', "limit": 8})
    if not data:
        return None
    artists = data.get("artists") or []
    if not artists:
        _cache.set("mb_artist", cache_key, {})
        return None

    # Score: prefer artists with Chinese aliases (or Chinese primary name),
    # then MB's own search score, then country=CN/HK/TW/SG.
    def cjk_alias_score(a):
        if has_cjk(a.get("name") or ""):
            return 1
        for al in a.get("aliases") or []:
            if has_cjk(al.get("name") or ""):
                return 1
        return 0

    def country_score(a):
        return 1 if (a.get("country") or "") in ("CN", "HK", "TW", "SG", "MO") else 0

    artists.sort(
        key=lambda a: (cjk_alias_score(a), country_score(a), a.get("score", 0)),
        reverse=True,
    )

    best = artists[0]
    # The search-result aliases are sometimes incomplete — fetch full detail.
    detail = _mb_get(f"artist/{best['id']}", {"inc": "aliases"})
    if detail:
        best["aliases"] = detail.get("aliases") or best.get("aliases") or []

    cn_name = _pick_zh_name(best.get("aliases") or [], best.get("name") or "")
    record = {
        "source": "mb",
        "id": best["id"],
        "name": best.get("name") or "",
        "cn_name": cn_name,
        "aliases": [a.get("name") for a in (best.get("aliases") or []) if a.get("name")],
        "country": best.get("country"),
        "score": best.get("score", 0),
    }
    _cache.set("mb_artist", cache_key, record)
    return record


def _mb_artist_albums(artist_id: str) -> list[dict]:
    """Fetch all release-groups (type=album) for an MB artist, with their
    aliases. Returned shape: list of
        {id, name, cn_name, aliases, year, primary_type}
    """
    cache_key = artist_id
    cached = _cache.get("mb_albums", cache_key)
    if cached is not None:
        return cached

    rgs: list[dict] = []
    offset = 0
    # Pull up to 400 release-groups for prolific artists
    while True:
        data = _mb_get(
            "release-group",
            {
                "artist": artist_id,
                "type": "album",
                "inc": "aliases",
                "limit": 100,
                "offset": offset,
            },
        )
        if not data:
            break
        chunk = data.get("release-groups") or []
        rgs.extend(chunk)
        if len(chunk) < 100 or len(rgs) >= 400:
            break
        offset += 100

    out: list[dict] = []
    for rg in rgs:
        aliases = [a.get("name") for a in (rg.get("aliases") or []) if a.get("name")]
        cn = _pick_zh_name(rg.get("aliases") or [], rg.get("title") or "")
        out.append(
            {
                "id": rg.get("id"),
                "name": rg.get("title") or "",
                "cn_name": cn,
                "aliases": aliases,
                "year": (rg.get("first-release-date") or "")[:4] or None,
                "primary_type": rg.get("primary-type"),
            }
        )
    _cache.set("mb_albums", cache_key, out)
    return out


# ---------- Wikidata ---------- #

WD_API = "https://www.wikidata.org/w/api.php"
WD_SPARQL = "https://query.wikidata.org/sparql"
WD_HEADERS = {
    "User-Agent": "music-cn-tagger/0.2 ( https://github.com/anthropics/claude-code )",
    "Accept": "application/json",
}


def _wd_get(params: dict) -> dict | None:
    p = dict(params)
    p["format"] = "json"
    for attempt in range(2):
        try:
            r = requests.get(WD_API, params=p, headers=WD_HEADERS, timeout=15)
            if r.status_code != 200:
                return None
            return r.json()
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
            time.sleep(1.0 + attempt)
        except (requests.RequestException, ValueError):
            return None
    return None


def _wd_find_artist(name: str) -> dict | None:
    """Search Wikidata for an artist matching `name`, return the entry
    with Chinese label/aliases if any."""
    cache_key = _norm(name)
    cached = _cache.get("wd_artist", cache_key)
    if cached is not None:
        return cached or None

    search = _wd_get({
        "action": "wbsearchentities",
        "search": name,
        "language": "en",
        "type": "item",
        "limit": 5,
    })
    if not search:
        return None
    hits = search.get("search") or []
    if not hits:
        _cache.set("wd_artist", cache_key, {})
        return None

    # Fetch each candidate's labels + claims to find one that's a person/band
    # AND has a Chinese label.
    qids = [h["id"] for h in hits]
    detail = _wd_get({
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "props": "labels|aliases|claims|sitelinks",
        "languages": "zh|zh-hans|zh-hant|zh-cn|zh-tw|zh-hk|en",
        "sitefilter": "zhwiki|enwiki",
    })
    if not detail:
        return None
    entities = detail.get("entities") or {}

    # Music-related instance-of QIDs: human, musician, band, music group, etc.
    MUSIC_QIDS = {"Q5", "Q215380", "Q2088357", "Q105756498", "Q177220"}

    def is_artist(ent):
        claims = ent.get("claims") or {}
        for c in claims.get("P31") or []:
            try:
                qid = c["mainsnak"]["datavalue"]["value"]["id"]
            except (KeyError, TypeError):
                continue
            if qid in MUSIC_QIDS:
                return True
            # P136 (genre) presence is a strong hint too
        return bool(claims.get("P136") or claims.get("P175") or claims.get("P1303"))

    candidates = [entities[qid] for qid in qids if qid in entities and is_artist(entities[qid])]
    if not candidates:
        _cache.set("wd_artist", cache_key, {})
        return None

    # Prefer candidates that have a Chinese label
    def zh_label(ent):
        labels = ent.get("labels") or {}
        for k in ("zh", "zh-hans", "zh-cn", "zh-hant", "zh-tw", "zh-hk"):
            if k in labels:
                return labels[k]["value"]
        return None

    candidates.sort(key=lambda e: 1 if zh_label(e) else 0, reverse=True)
    chosen = candidates[0]
    cn = zh_label(chosen)
    if not cn:
        # Try P1448 (official name)
        for c in (chosen.get("claims") or {}).get("P1448") or []:
            try:
                v = c["mainsnak"]["datavalue"]["value"]["text"]
            except (KeyError, TypeError):
                continue
            if has_cjk(v):
                cn = v
                break

    # Collect all aliases across CJK languages
    aliases = []
    al_map = chosen.get("aliases") or {}
    for k in ("zh", "zh-hans", "zh-cn", "zh-hant", "zh-tw", "zh-hk", "en"):
        for a in al_map.get(k, []):
            aliases.append(a["value"])

    en_label = (chosen.get("labels") or {}).get("en", {}).get("value", "")

    record = {
        "source": "wd",
        "id": chosen["id"],
        "name": en_label or name,
        "cn_name": cn,
        "aliases": aliases,
        "country": None,
        "score": 100,
    }
    _cache.set("wd_artist", cache_key, record)
    return record


def _wd_artist_albums(qid: str) -> list[dict]:
    """SPARQL: get all studio albums where the artist is performer (P175)."""
    cached = _cache.get("wd_albums", qid)
    if cached is not None:
        return cached

    query = f"""
    SELECT ?album ?albumLabel ?albumLabelZh ?p1448 ?date WHERE {{
      ?album wdt:P175 wd:{qid}.
      ?album wdt:P31/wdt:P279* wd:Q482994.
      OPTIONAL {{ ?album rdfs:label ?albumLabelZh. FILTER(LANG(?albumLabelZh) = "zh") }}
      OPTIONAL {{ ?album wdt:P1448 ?p1448. }}
      OPTIONAL {{ ?album wdt:P577 ?date. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    LIMIT 200
    """
    try:
        r = requests.get(
            WD_SPARQL,
            params={"query": query, "format": "json"},
            headers=WD_HEADERS,
            timeout=20,
        )
        if r.status_code != 200:
            _cache.set("wd_albums", qid, [])
            return []
        data = r.json()
    except (requests.RequestException, ValueError):
        return []

    out: list[dict] = []
    bindings = (data.get("results") or {}).get("bindings") or []
    seen = set()
    for b in bindings:
        album_uri = (b.get("album") or {}).get("value") or ""
        album_id = album_uri.rsplit("/", 1)[-1]
        if not album_id or album_id in seen:
            continue
        seen.add(album_id)
        en = (b.get("albumLabel") or {}).get("value") or ""
        zh = (b.get("albumLabelZh") or {}).get("value") or ""
        official = (b.get("p1448") or {}).get("value") or ""
        date = (b.get("date") or {}).get("value") or ""
        cn = zh if has_cjk(zh) else (official if has_cjk(official) else None)
        aliases = [x for x in (zh, official) if x and x != en]
        out.append({
            "id": album_id,
            "name": en or zh or official,
            "cn_name": cn,
            "aliases": aliases,
            "year": (date or "")[:4] or None,
            "primary_type": "Album",
        })
    _cache.set("wd_albums", qid, out)
    return out


# ---------- public API ---------- #

def find_artist(name: str) -> dict | None:
    """Best-effort single-record resolution. MB primary, WD fallback when
    MB lacks a Chinese name. Use find_artist_candidates() instead when you
    want to try matching against discographies from both sources."""
    name = (name or "").strip()
    if not name:
        return None
    mb = _mb_find_artist(name)
    if mb and mb.get("cn_name"):
        return mb
    wd = _wd_find_artist(name)
    if wd and wd.get("cn_name"):
        return wd
    return mb or wd


def find_artist_candidates(name: str) -> list[dict]:
    """Return all source-records (MB and/or WD) that resolved the artist
    to a Chinese name. Caller can then try matching the album against
    each source's discography and pick the best across both. Order:
    MB first (more reliable for Asian pop), WD second (broader English
    aliases)."""
    name = (name or "").strip()
    if not name:
        return []
    out = []
    mb = _mb_find_artist(name)
    if mb and mb.get("cn_name"):
        out.append(mb)
    wd = _wd_find_artist(name)
    if wd and wd.get("cn_name"):
        out.append(wd)
    return out


def artist_albums(artist: dict) -> list[dict]:
    """List all known albums for an artist record returned by find_artist().
    Each album is::

        {"id": str, "name": str, "cn_name": str | None,
         "aliases": [str, ...], "year": str | None, "primary_type": str | None}
    """
    if not artist or not artist.get("id"):
        return []
    if artist["source"] == "mb":
        return _mb_artist_albums(artist["id"])
    if artist["source"] == "wd":
        return _wd_artist_albums(artist["id"])
    return []
