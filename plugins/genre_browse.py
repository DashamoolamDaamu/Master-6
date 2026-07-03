# plugins/genre_browse.py
# Browse Movies by Genre — completely independent plugin.
# Reuses existing file-button layout and cb_handler from pm_filter.py.
# Does NOT modify any existing handler, search logic, or database layer.

import asyncio
import logging
import math

from pyrogram import Client, filters, enums
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.genre_cache_db import (
    ensure_indexes,
    get_cached,
    store_cache,
    get_titles_by_genre,
    genre_count,
)
from database.ia_filterdb import get_search_results
from utils import get_size, get_poster, get_settings, styled_button

logger = logging.getLogger(__name__)

# ── Startup: ensure DB indexes ────────────────────────────────────────────────

# ensure_indexes() is called lazily on first genre_menu open

# ── Genre definitions ─────────────────────────────────────────────────────────
# Each entry: (callback_code, display_name, emoji, imdb_match_terms)
# imdb_match_terms: lowercase strings that may appear in the IMDb "Genres" field.

GENRES = [
    ("action",      "Action",          "🎬", ["action"]),
    ("adventure",   "Adventure",       "⚔️",  ["adventure"]),
    ("comedy",      "Comedy",          "😂", ["comedy"]),
    ("romance",     "Romance",         "❤️",  ["romance"]),
    ("horror",      "Horror",          "😱", ["horror"]),
    ("thriller",    "Thriller",        "👻", ["thriller"]),
    ("mystery",     "Mystery",         "🔍", ["mystery"]),
    ("crime",       "Crime",           "🚓", ["crime"]),
    ("scifi",       "Science Fiction", "🚀", ["sci-fi", "science fiction", "scifi"]),
    ("fantasy",     "Fantasy",         "🧙", ["fantasy"]),
    ("drama",       "Drama",           "🎞",  ["drama"]),
    ("family",      "Family",          "👨‍👩‍👧", ["family"]),
    ("animation",   "Animation",       "🧒", ["animation", "animated"]),
    ("anime",       "Anime",           "🎌", ["anime"]),
    ("biography",   "Biography",       "🎭", ["biography", "biographical"]),
    ("history",     "History",         "📜", ["history", "historical"]),
    ("documentary", "Documentary",     "📖", ["documentary"]),
    ("musical",     "Musical",         "🎵", ["musical"]),
    ("music",       "Music",           "🎤", ["music"]),
    ("war",         "War",             "⚖️",  ["war"]),
    ("western",     "Western",         "🤠", ["western"]),
    ("sports",      "Sports",          "🏆", ["sport", "sports"]),
    ("superhero",   "Superhero",       "🌌", ["superhero", "super hero"]),
    ("zombie",      "Zombie",          "🧟", ["zombie"]),
    ("supernatural","Supernatural",    "👻", ["supernatural"]),
    ("psychological","Psychological",  "🧠", ["psychological"]),
    ("disaster",    "Disaster",        "💥", ["disaster"]),
    ("romcom",      "Romantic Comedy", "💞", ["romantic comedy", "romcom"]),
    ("darkcomedy",  "Dark Comedy",     "😈", ["dark comedy", "black comedy"]),
    ("kids",        "Kids",            "👶", ["kids", "children"]),
    ("tvmovie",     "TV Movie",        "📺", ["tv movie"]),
    ("political",   "Political",       "🏛",  ["political"]),
    ("comingofage", "Coming of Age",   "🎓", ["coming of age", "coming-of-age"]),
    ("independent", "Independent",     "🎥", ["independent", "indie"]),
]

# code → (display_name, emoji, match_terms)
_CODE_MAP: dict = {code: (name, emoji, terms) for code, name, emoji, terms in GENRES}

# ── In-memory genre result store ──────────────────────────────────────────────
# Key: f"{user_id}_{genre_code}"
# Value: list of file objects (same objects returned by get_search_results)

GENRE_RESULTS: dict = {}

FILES_PER_PAGE = 10


# ── Helpers ───────────────────────────────────────────────────────────────────

def _genre_info(code: str) -> tuple[str, str, list]:
    entry = _CODE_MAP.get(code)
    if not entry:
        return code.title(), "🎬", [code]
    return entry  # (display_name, emoji, match_terms)


def _build_genre_menu() -> InlineKeyboardMarkup:
    """All genre buttons in 2 columns + Back button."""
    rows = []
    row = []
    for code, name, emoji, _ in GENRES:
        row.append(styled_button(
            f"{emoji} {name}", callback_data=f"g_{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([styled_button("⬅ Back", callback_data="start")])
    return InlineKeyboardMarkup(rows)


def _build_file_buttons(files: list, settings: dict) -> list:
    """
    Builds the same file button rows as pm_filter.
    Callback data uses `file#{file_id}` so cb_handler in pm_filter handles them.
    """
    pre = "filep" if settings.get("file_secure") else "file"
    if settings.get("button"):
        return [
            [styled_button(
                f"📂[{get_size(f.file_size)}]--{f.file_name}",
                callback_data=f"{pre}#{f.file_id}")]
            for f in files
        ]
    return [
        [
            styled_button(f.file_name, callback_data=f"{pre}#{f.file_id}"),
            styled_button(get_size(f.file_size), callback_data=f"{pre}#{f.file_id}"),
        ]
        for f in files
    ]


def _build_pagination(uid: int, code: str, offset: int, total: int) -> list:
    page  = math.ceil(offset / FILES_PER_PAGE) if offset else 0
    pages = max(math.ceil(total / FILES_PER_PAGE), 1)
    row   = []
    if offset > 0:
        row.append(styled_button(
            "◀ Prev", callback_data=f"gnxt_{uid}_{code}_{max(offset - FILES_PER_PAGE, 0)}"))
    row.append(styled_button(f"📃 {page + 1} / {pages}", callback_data="pages"))
    if offset + FILES_PER_PAGE < total:
        row.append(styled_button(
            "Next ▶", callback_data=f"gnxt_{uid}_{code}_{offset + FILES_PER_PAGE}"))
    return row


async def _fetch_genre_files(code: str) -> list:
    """
    Collect all files matching a genre:
      Phase 1 — IMDb cache: titles known to belong to the genre
      Phase 2 — Fallback name search using the genre keyword itself
    Deduplicated by file_id.
    """
    display_name, _, match_terms = _genre_info(code)
    seen:   dict = {}   # file_id → file object

    # ── Phase 1: cache-based ─────────────────────────────────────────────────
    cached_titles = await get_titles_by_genre(display_name)
    for term in match_terms:
        extra = await get_titles_by_genre(term)
        cached_titles = list(set(cached_titles + extra))

    for title in cached_titles[:120]:   # cap to avoid timeout
        try:
            files, _, _, _ = await get_search_results(
                title, offset=0, max_results=20, filter=True, fast=True, return_time=True)
            for f in files:
                seen[f.file_id] = f
        except Exception:
            pass

    # ── Phase 2: direct name-search fallback ─────────────────────────────────
    for term in match_terms:
        try:
            files, _, _, _ = await get_search_results(
                term, offset=0, max_results=100, filter=True, fast=True, return_time=True)
            for f in files:
                seen[f.file_id] = f
        except Exception:
            pass

    # ── Phase 3: background-cache any newly found file names via IMDb ────────
    asyncio.create_task(_background_cache(list(seen.values())))

    return list(seen.values())


async def _background_cache(files: list):
    """
    For each file whose title isn't cached yet, fetch IMDb and store.
    Runs in the background; failures are silently ignored.
    """
    for f in files[:30]:     # limit background work per call
        name = f.file_name or ""
        key  = name.lower().strip()
        try:
            cached = await get_cached(key)
            if cached:
                continue
            imdb = await get_poster(name, file=name)
            if imdb and imdb.get("genres") and imdb["genres"] != "N/A":
                await store_cache(key, imdb)
        except Exception:
            pass
        await asyncio.sleep(0.3)   # stay well under IMDb rate limit


# ── Callback handlers ─────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^genre_menu$"))
async def genre_menu(bot, query):
    """Show the genre picker."""
    await ensure_indexes()   # idempotent, fast after first call
    await query.answer()
    try:
        await query.edit_message_text(
            "🎬 **Browse Movies by Genre**\n\nSelect a genre to explore:",
            reply_markup=_build_genre_menu(),
        )
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^g_[a-z]+$"))
async def genre_info_page(bot, query):
    """Show the genre summary page before searching."""
    code = query.data[2:]       # strip "g_"
    if code not in _CODE_MAP:
        return await query.answer("Unknown genre.", show_alert=True)

    display_name, emoji, _ = _genre_info(code)
    uid = query.from_user.id

    cached = await genre_count(display_name)
    cache_note = (
        f"📊 {cached} titles cached for this genre."
        if cached else
        "🔄 No cache yet — results will be searched fresh."
    )

    text = (
        f"{emoji} **Genre Information**\n\n"
        f"**Genre:** {display_name}\n\n"
        f"Movies matching this genre will be searched from the indexed database "
        f"using cached IMDb metadata.\n\n"
        f"{cache_note}\n\n"
        f"Press the button below to view matching movies."
    )
    markup = InlineKeyboardMarkup([
        [styled_button("🎥 Get Movies", callback_data=f"gm_{code}_{uid}")],
        [styled_button("⬅ Back",        callback_data="genre_menu")],
    ])
    await query.answer()
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^gm_[a-z]+_\d+$"))
async def get_genre_movies(bot, query):
    """Fetch and display genre results using the existing file-list layout."""
    parts = query.data.split("_")
    # format: gm_{code}_{uid}
    code = parts[1]
    try:
        uid = int(parts[2])
    except (IndexError, ValueError):
        uid = query.from_user.id

    if query.from_user.id != uid:
        return await query.answer("This is not your search.", show_alert=True)

    if code not in _CODE_MAP:
        return await query.answer("Unknown genre.", show_alert=True)

    display_name, emoji, _ = _genre_info(code)
    await query.answer(f"Searching {display_name}…")

    # Show loading indicator
    try:
        await query.edit_message_text(
            f"{emoji} **Searching {display_name} movies…**\n\nPlease wait ⏳")
    except MessageNotModified:
        pass

    files = await _fetch_genre_files(code)

    if not files:
        return await query.edit_message_text(
            f"{emoji} **{display_name}**\n\nNo matching movies found yet.\n"
            f"Try again later as the cache fills up.",
            reply_markup=InlineKeyboardMarkup([
                [styled_button("⬅ Back to Genres", callback_data="genre_menu")]
            ]),
        )

    # Store in memory for pagination
    store_key = f"{uid}_{code}"
    GENRE_RESULTS[store_key] = files

    settings = await get_settings(query.message.chat.id)
    page_files = files[:FILES_PER_PAGE]
    total      = len(files)

    btn  = _build_file_buttons(page_files, settings)
    prow = _build_pagination(uid, code, 0, total)
    if prow:
        btn.append(prow)
    btn.append([styled_button("⬅ Back to Genres", callback_data="genre_menu")])

    header = (
        f"{emoji} **{display_name} Movies**\n"
        f"Found **{total}** files — Page 1 / {max(math.ceil(total / FILES_PER_PAGE), 1)}"
    )
    try:
        await query.edit_message_text(
            header,
            reply_markup=InlineKeyboardMarkup(btn),
        )
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^gnxt_\d+_[a-z]+_\d+$"))
async def genre_next_page(bot, query):
    """Paginate genre results — preserves in-memory file list."""
    # format: gnxt_{uid}_{code}_{offset}
    parts  = query.data.split("_")
    uid    = int(parts[1])
    code   = parts[2]
    offset = int(parts[3])

    if query.from_user.id != uid:
        return await query.answer("This is not your search.", show_alert=True)

    store_key = f"{uid}_{code}"
    files     = GENRE_RESULTS.get(store_key)

    if not files:
        # Cache expired or bot restarted — re-fetch silently
        await query.answer("Refreshing…")
        files = await _fetch_genre_files(code)
        if not files:
            return await query.answer("No results found.", show_alert=True)
        GENRE_RESULTS[store_key] = files

    total      = len(files)
    page_files = files[offset: offset + FILES_PER_PAGE]
    page       = math.ceil(offset / FILES_PER_PAGE) if offset else 0
    pages      = max(math.ceil(total / FILES_PER_PAGE), 1)

    display_name, emoji, _ = _genre_info(code)
    settings = await get_settings(query.message.chat.id)

    btn  = _build_file_buttons(page_files, settings)
    prow = _build_pagination(uid, code, offset, total)
    if prow:
        btn.append(prow)
    btn.append([styled_button("⬅ Back to Genres", callback_data="genre_menu")])

    header = (
        f"{emoji} **{display_name} Movies**\n"
        f"Found **{total}** files — Page {page + 1} / {pages}"
    )
    await query.answer()
    try:
        await query.edit_message_text(
            header,
            reply_markup=InlineKeyboardMarkup(btn),
        )
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^back_genre$"))
async def back_to_genre_menu(bot, query):
    await query.answer()
    try:
        await query.edit_message_text(
            "🎬 **Browse Movies by Genre**\n\nSelect a genre to explore:",
            reply_markup=_build_genre_menu(),
        )
    except MessageNotModified:
        pass
