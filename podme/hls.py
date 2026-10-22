"""
HLS handling for the PodMe episodes PodMe serves as encrypted video streams.

PodMe publishes some episodes as video. The API then returns an HLS master playlist
url for every stream field of the episode, and that playlist contains video
renditions alongside a single audio-only rendition. Music Assistant's generic HLS
handling picks the highest-bandwidth (video) variant and hands the child playlist
to a separate ffmpeg process, which the PodMe CDN rejects — its tokens are bound
to the session that fetched the master playlist.

So the audio is extracted here instead: the audio-only variant is resolved, the
AES-128 encrypted media segments are downloaded through the caller's (cookie
carrying) session and yielded as decrypted fragmented MP4 bytes, which Music
Assistant pipes into ffmpeg.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from music_assistant.providers.podme.api import PodMeApiError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiohttp import ClientSession

# media segments to download ahead of the bytes already yielded, so playback
# starts while the rest of the episode is still being fetched
HLS_CONCURRENCY = 8

_IV_PATTERN = re.compile(r"IV=0[xX]([0-9a-fA-F]+)")


class PodMeHlsError(PodMeApiError):
    """Raised when a PodMe HLS stream cannot be resolved or fetched."""


def resolve_uri(base_url: str, uri: str) -> str:
    """
    Return uri resolved against the playlist url base_url.

    Plain string handling on purpose: URL libraries re-encode the percent-escaped
    values inside the Akamai tokens (hdntl/hdnts) that PodMe's CDN URLs carry,
    which invalidates their HMAC. Playlists are also served as plain text, so the
    raw URI lines are usable as-is.

    :param base_url: The URL of the playlist the URI belongs to.
    :param uri: A possibly relative playlist or segment URI.
    :return: The absolute URI, with base_url's query string dropped.
    """
    if uri.startswith(("http://", "https://")):
        return uri
    return base_url.split("?", 1)[0].rsplit("/", 1)[0] + "/" + uri


def find_audio_variant(master: str) -> str | None:
    """
    Return the audio-only variant URI of an HLS master playlist, if present.

    Video episodes carry several #EXT-X-STREAM-INF entries; the audio-only one is
    the rendition without video codecs (no "avc" in its CODECS attribute). Its
    URI is on the line following the attribute line.

    :param master: The master playlist text.
    :return: The variant URI, or None when there is no audio-only variant.
    """
    lines = master.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and "avc" not in line:
            for raw_candidate in lines[index + 1 :]:
                candidate = raw_candidate.strip()
                if candidate and not candidate.startswith("#"):
                    return candidate
    return None


@dataclass
class AudioPlaylist:
    """
    The parts of a PodMe HLS audio playlist the streamer needs.

    :ivar init_uri: URI of the (unencrypted) fMP4 init segment, or None.
    :ivar key_uri: URI of the AES-128 key segment, or None.
    :ivar iv: Explicit key IV from the playlist, or None when the IV is the
        media sequence number of each segment.
    :ivar media_sequence: Value of #EXT-X-MEDIA-SEQUENCE, defaulting to 0.
    :ivar segments: The media segments as (duration in seconds, URI) pairs.
    """

    init_uri: str | None = None
    key_uri: str | None = None
    iv: bytes | None = None
    media_sequence: int = 0
    segments: list[tuple[float, str]] = field(default_factory=list)


def parse_audio_playlist(playlist: str) -> AudioPlaylist:
    """
    Parse a PodMe HLS audio playlist.

    :param playlist: The playlist text.
    :return: The parsed playlist parts.
    :raises PodMeHlsError: When the playlist carries an unsupported key method.
    """
    parsed = AudioPlaylist()
    duration = 0.0
    for line in playlist.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#EXTINF:"):
            try:
                duration = float(stripped.removeprefix("#EXTINF:").split(",", 1)[0])
            except ValueError:
                duration = 0.0
        elif stripped.startswith("#EXT-X-MAP:"):
            parsed.init_uri = stripped.split('URI="', 1)[1].split('"', 1)[0]
        elif stripped.startswith("#EXT-X-KEY:"):
            if "METHOD=AES-128" not in stripped:
                raise PodMeHlsError(f"unsupported HLS key method: {stripped[:80]}")
            parsed.key_uri = stripped.split('URI="', 1)[1].split('"', 1)[0]
            if iv_match := _IV_PATTERN.search(stripped):
                parsed.iv = bytes.fromhex(iv_match.group(1).zfill(32))
        elif stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            parsed.media_sequence = int(stripped.removeprefix("#EXT-X-MEDIA-SEQUENCE:"))
        elif not stripped.startswith("#"):
            parsed.segments.append((duration, stripped))
            duration = 0.0
    return parsed


def _strip_pkcs7(data: bytes) -> bytes:
    """Return data with its (always present) PKCS7 padding removed."""
    return data[: -data[-1]]


def _decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    """
    Return a decrypted HLS media segment.

    :param data: The encrypted segment, AES-128-CBC with PKCS7 padding.
    :param key: The 16-byte key from the playlist's EXT-X-KEY.
    :param iv: The segment IV: the explicit playlist IV, else its media
        sequence number as a 128-bit big-endian integer.
    :raises PodMeHlsError: When the segment is not decryptable.
    """
    if len(data) % 16 != 0:
        raise PodMeHlsError(f"encrypted segment is {len(data)} bytes, not 16-aligned")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    try:
        padded = decryptor.update(data) + decryptor.finalize()
    except ValueError as err:
        raise PodMeHlsError(f"segment decryption failed: {err}") from err
    return _strip_pkcs7(padded)


async def _fetch_bytes(session: ClientSession, url: str) -> bytes:
    """
    Return the body of url, raising on any HTTP error.

    :param session: The session that fetched the master playlist; the PodMe CDN
        binds child URLs to it, so a fresh session gets 403s.
    :param url: The URL to fetch.
    :raises PodMeHlsError: On connection or HTTP errors.
    """
    try:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()
    except TimeoutError as err:
        raise PodMeHlsError(f"timed out fetching {url.split('?', 1)[0]}") from err
    except Exception as err:
        raise PodMeHlsError(f"could not fetch {url.split('?', 1)[0]}: {err}") from err


async def _fetch_segment(session: ClientSession, url: str, key: bytes, iv: bytes) -> bytes:
    """Return one downloaded and decrypted media segment."""
    return _decrypt_segment(await _fetch_bytes(session, url), key, iv)


async def iter_hls_audio(
    session: ClientSession,
    master_url: str,
    *,
    seek_position: float = 0.0,
    concurrency: int = HLS_CONCURRENCY,
) -> AsyncGenerator[bytes]:
    """
    Yield the audio track of a PodMe HLS episode as fragmented MP4 bytes.

    The fMP4 init segment is yielded first, followed by the media segments in
    playlist order, so the output can be piped into ffmpeg directly (e.g. with
    ``-f m4a -i pipe:0``). Up to ``concurrency`` segments are downloaded ahead
    of the bytes already yielded.

    :param session: The aiohttp session that will also fetch the playlists and
        segments; it must be the session that talked to the PodMe API, as the
        CDN binds requests to it.
    :param master_url: The episode's HLS master playlist URL.
    :param seek_position: Start yielding from the segment containing this many
        seconds; playback then begins at most one segment early (PodMe uses
        3.2 second segments).
    :param concurrency: How many segments to download ahead.
    :raises PodMeHlsError: When the stream cannot be resolved or fetched.
    """
    master = (await _fetch_bytes(session, master_url)).decode("utf-8", errors="replace")
    variant = find_audio_variant(master)
    if variant is None:
        raise PodMeHlsError("no audio-only variant in the HLS master playlist")
    playlist_url = resolve_uri(master_url, variant)
    playlist = parse_audio_playlist((await _fetch_bytes(session, playlist_url)).decode("utf-8"))
    if playlist.init_uri is None or playlist.key_uri is None or not playlist.segments:
        raise PodMeHlsError("HLS audio playlist is missing its init segment, key or segments")
    key = await _fetch_bytes(session, resolve_uri(playlist_url, playlist.key_uri))
    if len(key) != 16:
        raise PodMeHlsError(f"HLS key is {len(key)} bytes, expected 16")

    # the init segment carries the fMP4 headers and is not encrypted
    yield await _fetch_bytes(session, resolve_uri(playlist_url, playlist.init_uri))

    segments = playlist.segments
    # locate the segment containing seek_position; seeking is segment-granular
    start = len(segments)
    elapsed = 0.0
    for index, (duration, _uri) in enumerate(segments):
        if elapsed + duration > seek_position:
            start = index
            break
        elapsed += duration

    # slide a window of download tasks over the remaining segments, yielding
    # each segment in playlist order the moment it is its turn
    pending: deque[asyncio.Task[bytes]] = deque()
    next_index = start

    def start_next() -> None:
        """Queue the download of the next unqueued segment, if any."""
        nonlocal next_index
        if next_index >= len(segments):
            return
        _duration, uri = segments[next_index]
        iv = (
            playlist.iv
            if playlist.iv is not None
            else (playlist.media_sequence + next_index).to_bytes(16, "big")
        )
        pending.append(
            asyncio.create_task(_fetch_segment(session, resolve_uri(playlist_url, uri), key, iv))
        )
        next_index += 1

    try:
        for _ in range(concurrency):
            start_next()
        while pending:
            data = await pending.popleft()
            start_next()
            yield data
    finally:
        # playback stopped (or the generator was closed early): drop queued work
        for task in pending:
            task.cancel()
