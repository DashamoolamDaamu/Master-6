# plugins/webcode.py
# Streaming server integrated from FileToLink (Thunder/server/stream_routes.py).
# Runs inside the filter bot's existing aiohttp instance.
#
# Routes:
#   GET /               → health check (existing)
#   GET /status         → uptime / workload JSON
#   GET /watch/{path}   → Vidstack web player page
#   GET /{path}         → stream or download the file (byte range supported)
#   OPTIONS /{path}     → CORS preflight

import asyncio
import re
import secrets
import time
import logging
from urllib.parse import quote, unquote

from aiohttp import web as webserver

from info import STREAM_BASE_URL, BIN_CHANNEL
from streaming.stream_dl import ByteStreamer, _FileNotFound, _InvalidHash
from streaming.stream_render import render_stream_page, render_stream_page_for_message

logger = logging.getLogger(__name__)

# ─── Globals (populated in bot_run once the bot client is available) ──────────

_streamer: ByteStreamer | None = None
_work_loads: dict[int, int] = {0: 0}   # client_id → active streams
_start_time: float = time.time()

# ─── Constants ────────────────────────────────────────────────────────────────

CHUNK_SIZE          = 1024 * 1024
SECURE_HASH_LENGTH  = 6
RANGE_REGEX         = re.compile(r"^bytes=(?P<start>\d*)-(?P<end>\d*)$")
PATTERN_HASH_FIRST  = re.compile(
    rf"^([a-zA-Z0-9_-]{{{SECURE_HASH_LENGTH}}})(\d+)(?:/.*)?$")
PATTERN_ID_FIRST    = re.compile(r"^(\d+)(?:/.*)?$")
VALID_DISPOSITIONS  = {"inline", "attachment"}

CORS_HEADERS = {
    "Access-Control-Allow-Origin":   "*",
    "Access-Control-Allow-Methods":  "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers":  "Range, Content-Type, *",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Content-Disposition",
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_media_request(path: str, query: dict) -> tuple[int, str]:
    clean = unquote(path).strip("/")

    m = PATTERN_HASH_FIRST.match(clean)
    if m:
        try:
            msg_id      = int(m.group(2))
            secure_hash = m.group(1)
            if len(secure_hash) == SECURE_HASH_LENGTH:
                return msg_id, secure_hash
        except ValueError:
            pass

    m = PATTERN_ID_FIRST.match(clean)
    if m:
        try:
            msg_id      = int(m.group(1))
            secure_hash = query.get("hash", "").strip()
            if len(secure_hash) == SECURE_HASH_LENGTH:
                return msg_id, secure_hash
        except ValueError:
            pass

    raise _InvalidHash("Invalid URL structure or missing hash")


def _get_content_disposition(request: webserver.Request) -> str:
    disp = request.query.get("disposition", "attachment").strip().lower()
    return disp if disp in VALID_DISPOSITIONS else "attachment"


def _parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    if not range_header:
        return 0, file_size - 1

    m = RANGE_REGEX.fullmatch(range_header)
    if not m:
        raise webserver.HTTPBadRequest(text=f"Invalid Range: {range_header}")

    start_str, end_str = m.group("start"), m.group("end")
    if start_str:
        start = int(start_str)
        end   = int(end_str) if end_str else file_size - 1
    else:
        suffix = int(end_str)
        if suffix <= 0:
            raise webserver.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{file_size}"})
        start = max(file_size - suffix, 0)
        end   = file_size - 1

    if start < 0 or end >= file_size or start > end:
        raise webserver.HTTPRequestRangeNotSatisfiable(
            headers={"Content-Range": f"bytes */{file_size}"})

    return start, end


def _resolve_filename(file_info: dict, mime_type: str) -> str:
    name = file_info.get("file_name")
    if name:
        return name
    ext = (mime_type or "").split("/")[-1]
    ext_map = {"jpeg": "jpg", "mpeg": "mp3", "octet-stream": "bin"}
    ext = ext_map.get(ext, ext) or "bin"
    return f"file_{secrets.token_hex(4)}.{ext}"


def _get_readable_time(seconds: float) -> str:
    parts = []
    for label, period in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= period:
            value, seconds = divmod(int(seconds), period)
            parts.append(f"{value}{label}")
    return " ".join(parts) if parts else "0s"


# ─── Core streaming response ──────────────────────────────────────────────────

async def _serve_media(
    request: webserver.Request,
    *,
    file_info: dict,
    media_ref: int,
) -> webserver.StreamResponse:
    if _streamer is None:
        raise webserver.HTTPServiceUnavailable(text="Streamer not initialised")

    file_size = int(file_info.get("file_size", 0) or 0)
    if file_size == 0:
        raise webserver.HTTPNotFound(text="File size unavailable")

    req_id = secrets.token_hex(4)  # correlates every log line for this request

    range_header = request.headers.get("Range", "")
    had_range    = bool(range_header)
    start, end   = _parse_range_header(range_header, file_size)
    content_len  = end - start + 1

    mime_type = file_info.get("mime_type") or "application/octet-stream"
    filename  = _resolve_filename(file_info, mime_type)
    disp      = _get_content_disposition(request)

    headers = {
        "Content-Type":        mime_type,
        "Content-Length":      str(content_len),
        "Content-Disposition": f"{disp}; filename*=UTF-8''{quote(filename, safe='')}",
        "Accept-Ranges":       "bytes",
        "Cache-Control":       "public, max-age=31536000",
        "Connection":          "keep-alive",
        **CORS_HEADERS,
        "X-Content-Type-Options": "nosniff",
    }
    if had_range:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    status = 206 if had_range else 200

    logger.info(
        f"[{req_id}] REQUEST method={request.method} path={request.path} "
        f"range={range_header!r} start={start} end={end} content_len={content_len} "
        f"file_size={file_size} status={status} disposition={disp} mime={mime_type} "
        f"remote={request.remote} ua={request.headers.get('User-Agent', '')[:80]!r}"
    )

    if request.method == "HEAD":
        logger.info(f"[{req_id}] HEAD response only, no body sent")
        return webserver.Response(status=status, headers=headers)

    # ── StreamResponse: explicit prepare/write/write_eof, no ambiguity about
    #    chunked vs fixed-length framing (unlike Response(body=async_gen)).
    resp = webserver.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)
    logger.info(f"[{req_id}] headers sent, status={status}")

    _work_loads[0] += 1
    bytes_sent    = 0
    bytes_to_skip = start % CHUNK_SIZE
    chunk_index   = 0

    try:
        async for chunk in _streamer.stream_file(
            media_ref, offset=start, limit=content_len
        ):
            if bytes_to_skip > 0:
                if len(chunk) <= bytes_to_skip:
                    bytes_to_skip -= len(chunk)
                    continue
                chunk = chunk[bytes_to_skip:]
                bytes_to_skip = 0

            remaining = content_len - bytes_sent
            if len(chunk) > remaining:
                chunk = chunk[:remaining]

            if chunk:
                await resp.write(chunk)
                bytes_sent += len(chunk)
                chunk_index += 1
                # Per-chunk detail at DEBUG (can be thousands of lines for a
                # full movie); a running summary every 20 chunks at INFO so
                # you can watch progress live in Koyeb logs without noise.
                logger.debug(
                    f"[{req_id}] chunk#{chunk_index} size={len(chunk)} "
                    f"bytes_sent={bytes_sent}/{content_len}"
                )
                if chunk_index % 20 == 0:
                    logger.info(
                        f"[{req_id}] progress chunk#{chunk_index} "
                        f"bytes_sent={bytes_sent}/{content_len} "
                        f"({bytes_sent * 100 // content_len}%)"
                    )

            if bytes_sent >= content_len:
                break

        await resp.write_eof()
        logger.info(
            f"[{req_id}] COMPLETE chunks={chunk_index} bytes_sent={bytes_sent} "
            f"expected={content_len} match={bytes_sent == content_len}"
        )

    except (ConnectionResetError, asyncio.CancelledError) as e:
        # Client (or the mobile OS switching apps / locking screen) closed
        # the connection mid-stream. Not a server bug, but we want it visible
        # and distinguishable from a real exception below.
        logger.warning(
            f"[{req_id}] CLIENT DISCONNECTED after chunk#{chunk_index} "
            f"bytes_sent={bytes_sent}/{content_len}: {type(e).__name__}"
        )
        raise
    except Exception as e:
        logger.error(
            f"[{req_id}] STREAM ERROR after chunk#{chunk_index} "
            f"bytes_sent={bytes_sent}/{content_len}: {e}",
            exc_info=True,
        )
        raise
    finally:
        _work_loads[0] -= 1

    return resp


# ─── Route table ──────────────────────────────────────────────────────────────

routes = webserver.RouteTableDef()


@routes.options("/client-log")
async def client_log_options(request: webserver.Request):
    return webserver.Response(
        headers={**CORS_HEADERS, "Access-Control-Max-Age": "86400"})


@routes.post("/client-log")
async def client_log_endpoint(request: webserver.Request):
    """
    Receives small JSON beacons from the in-page instrumentation script
    (see req.html) and writes them into the same server logs. This is what
    lets us see player/JS state from real mobile browsers without DevTools.
    Body is capped and best-effort: a bad/oversized payload must never 500.
    """
    try:
        raw = await request.text()
        if len(raw) > 4096:
            raw = raw[:4096]
        try:
            import json
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw}
        logger.info(
            f"[CLIENT] remote={request.remote} ua={request.headers.get('User-Agent','')[:80]!r} "
            f"event={data.get('event')!r} detail={data.get('detail')!r} "
            f"src={data.get('src')!r} t={data.get('t')!r}"
        )
    except Exception as e:
        logger.debug(f"client-log beacon parse failure: {e}")
    return webserver.Response(status=204, headers=CORS_HEADERS)


@routes.get("/", allow_head=True)
async def root_route_handler(request):
    return webserver.json_response(
        {"status": "ok", "service": "ShobanaFilterBot", "stream": bool(_streamer)}
    )


@routes.get("/status", allow_head=True)
async def status_endpoint(request):
    uptime = time.time() - _start_time
    return webserver.json_response(
        {
            "server":    {"status": "operational", "uptime": _get_readable_time(uptime)},
            "streaming": {
                "enabled":   _streamer is not None,
                "active":    _work_loads.get(0, 0),
                "bin_channel": bool(BIN_CHANNEL),
            },
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


@routes.options("/status")
async def status_options(request: webserver.Request):
    return webserver.Response(
        headers={**CORS_HEADERS, "Access-Control-Max-Age": "86400"})


@routes.options(r"/{path:.+}")
async def media_options(request: webserver.Request):
    return webserver.Response(
        headers={**CORS_HEADERS, "Access-Control-Max-Age": "86400"})


@routes.get(r"/watch/{path:.+}", allow_head=True)
async def media_preview(request: webserver.Request):
    """Serve the Vidstack web player page."""
    try:
        path = request.match_info["path"]
        msg_id, secure_hash = _parse_media_request(path, request.query)

        if _streamer is None:
            raise webserver.HTTPServiceUnavailable(text="Streaming not configured")

        file_info   = await _streamer.get_file_info(msg_id)
        if "error" in file_info:
            raise webserver.HTTPNotFound(text="File not found")

        file_name   = file_info.get("file_name", f"file_{msg_id}")
        unique_id   = file_info.get("unique_id", "")
        encoded_n   = quote(file_name.replace("/", "_"), safe="")
        src         = f"{STREAM_BASE_URL}/{secure_hash}{msg_id}/{encoded_n}"

        if unique_id and unique_id[:SECURE_HASH_LENGTH] != secure_hash:
            raise webserver.HTTPNotFound(text="Hash mismatch")

        html = await render_stream_page(file_name, src)

        resp = webserver.Response(
            text=html,
            content_type="text/html",
            headers={
                "Access-Control-Allow-Origin": "*",
                "X-Content-Type-Options": "nosniff",
            },
        )
        resp.enable_compression()
        return resp

    except (_InvalidHash, _FileNotFound) as e:
        raise webserver.HTTPNotFound(text="Resource not found") from e
    except webserver.HTTPException:
        raise
    except Exception as e:
        eid = secrets.token_hex(6)
        logger.error(f"Preview error {eid}: {e}", exc_info=True)
        raise webserver.HTTPInternalServerError(
            text=f"Server error: {eid}") from e


@routes.get(r"/{path:.+}", allow_head=True)
async def media_delivery(request: webserver.Request):
    """Stream or download the file (byte-range aware)."""
    try:
        path = request.match_info["path"]
        msg_id, secure_hash = _parse_media_request(path, request.query)

        if _streamer is None:
            raise webserver.HTTPServiceUnavailable(text="Streaming not configured")

        file_info = await _streamer.get_file_info(msg_id)
        if "error" in file_info:
            raise webserver.HTTPNotFound(text="File not found")

        unique_id = file_info.get("unique_id", "")
        if unique_id and unique_id[:SECURE_HASH_LENGTH] != secure_hash:
            raise webserver.HTTPNotFound(text="Hash mismatch")

        return await _serve_media(request, file_info=file_info, media_ref=msg_id)

    except (_InvalidHash, _FileNotFound) as e:
        raise webserver.HTTPNotFound(text="Resource not found") from e
    except webserver.HTTPException:
        raise
    except Exception as e:
        eid = secrets.token_hex(6)
        logger.error(f"Delivery error {eid}: {e}", exc_info=True)
        raise webserver.HTTPInternalServerError(
            text=f"Server error: {eid}") from e


# ─── App factory ──────────────────────────────────────────────────────────────

async def bot_run(client=None):
    """
    Create and return the aiohttp Application.
    Pass the Pyrogram Client to initialise the ByteStreamer for streaming.
    """
    global _streamer
    if client is not None and BIN_CHANNEL:
        _streamer = ByteStreamer(client, BIN_CHANNEL)
        logger.info(f"ByteStreamer initialised (BIN_CHANNEL={BIN_CHANNEL})")
    elif not BIN_CHANNEL:
        logger.warning(
            "BIN_CHANNEL not set — streaming disabled. "
            "Set STREAM_BASE_URL and BIN_CHANNEL env vars to enable.")

    _app = webserver.Application(client_max_size=30_000_000)
    _app.add_routes(routes)
    return _app
