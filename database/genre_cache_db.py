# database/genre_cache_db.py
# IMDb metadata cache for Browse by Genre feature.
# Stores genre + metadata per movie title so we avoid repeated IMDb API calls.
# Completely independent — does not touch any existing collection.

import logging
from datetime import datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorClient

from info import DATABASE_URI, DATABASE_NAME

logger = logging.getLogger(__name__)

# ── Connection (reuses same Atlas cluster, separate collection) ───────────────

_client = AsyncIOMotorClient(DATABASE_URI)
_db     = _client[DATABASE_NAME]
_col    = _db["imdb_genre_cache"]       # new collection, never conflicts

CACHE_TTL_DAYS = 30


async def ensure_indexes():
    """Call once at startup to create query indexes."""
    try:
        await _col.create_index("search_key", unique=True)
        await _col.create_index("genres")           # fast genre queries
        await _col.create_index("cached_at")
    except Exception as e:
        logger.warning(f"[GenreCache] Index creation: {e}")


# ── Read / Write ──────────────────────────────────────────────────────────────

async def get_cached(search_key: str) -> dict | None:
    """Return cached IMDb entry for search_key, or None if missing / expired."""
    try:
        doc = await _col.find_one({"search_key": search_key.lower().strip()})
        if not doc:
            return None
        age = datetime.now() - doc.get("cached_at", datetime.min)
        if age > timedelta(days=CACHE_TTL_DAYS):
            return None
        return doc
    except Exception as e:
        logger.debug(f"[GenreCache] get_cached error: {e}")
        return None


async def store_cache(search_key: str, imdb_data: dict):
    """Cache IMDb result for search_key. genres stored as lowercase list."""
    try:
        raw_genres = imdb_data.get("genres") or ""
        if isinstance(raw_genres, str):
            genres = [g.strip().lower() for g in raw_genres.split(",") if g.strip()]
        else:
            genres = [g.strip().lower() for g in raw_genres if g.strip()]

        await _col.update_one(
            {"search_key": search_key.lower().strip()},
            {"$set": {
                "search_key":  search_key.lower().strip(),
                "title":       imdb_data.get("title"),
                "imdb_id":     imdb_data.get("imdb_id"),
                "genres":      genres,
                "year":        imdb_data.get("year"),
                "language":    imdb_data.get("languages"),
                "cached_at":   datetime.now(),
            }},
            upsert=True,
        )
    except Exception as e:
        logger.debug(f"[GenreCache] store_cache error: {e}")


async def get_titles_by_genre(genre_display: str) -> list[str]:
    """
    Return all cached search_keys whose genres list contains `genre_display`
    (case-insensitive).  Returns up to 500 titles.
    """
    try:
        cursor = _col.find(
            {"genres": genre_display.lower()},
            {"search_key": 1, "_id": 0},
        ).limit(500)
        docs = await cursor.to_list(length=500)
        return [d["search_key"] for d in docs]
    except Exception as e:
        logger.debug(f"[GenreCache] get_titles_by_genre error: {e}")
        return []


async def genre_count(genre_display: str) -> int:
    """Number of cached titles for a genre."""
    try:
        return await _col.count_documents({"genres": genre_display.lower()})
    except Exception:
        return 0
