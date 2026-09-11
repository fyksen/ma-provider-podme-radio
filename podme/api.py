"""
Tolerant async client for the PodMe API.

Authentication is delegated to the `podme-api` package, whose Schibsted OAuth/PKCE
flow is the hard part and works well. The data layer is deliberately our own: the
published library parses responses into strict dataclasses, and several of them no
longer match what PodMe returns (an `int` field arriving as `null` or `""` raises
rather than degrading), which breaks the user's own podcast list among others. We
read the JSON defensively instead, so a field PodMe changes costs a missing value
rather than a dead provider.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from podme_api import PodMeDefaultAuthClient, PodMeUserCredentials
from podme_api.auth.models import SchibstedCredentials
from podme_api.models import PodMeRegion

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from aiohttp import ClientSession

LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.podme.com"

# PodMe answers 403 "Request forbidden by administrative rules" to the app version
# the published podme-api sends (6.29.3). This is the version its git main moved to.
API_USER_AGENT = "Podme android app/6.38.5 (Linux;Android 15) AndroidXMedia3/1.5.1"

DEFAULT_PAGE_SIZE = 50
# guard against a changed pagination contract turning into an endless loop
MAX_PAGES = 60

REGIONS: tuple[tuple[str, str], ...] = (
    ("NO", "Norway"),
    ("SE", "Sweden"),
    ("DK", "Denmark"),
    ("FI", "Finland"),
)


class PodMeApiError(Exception):
    """Raised when the PodMe API answers with something unusable."""


class PodMeAuthError(PodMeApiError):
    """Raised when PodMe/Schibsted rejects the credentials."""


class PodMeNotFoundError(PodMeApiError):
    """Raised when PodMe has no such podcast or episode."""


class PodMeSubscriptionRequiredError(PodMeApiError):
    """Raised when an episode needs an active PodMe subscription."""


class PodMeApi:
    """Minimal, tolerant wrapper around the PodMe endpoints this provider needs."""

    def __init__(
        self,
        session: ClientSession,
        email: str,
        password: str,
        region: str = "NO",
        *,
        credentials: str | None = None,
        on_credentials: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """
        Initialize the client.

        :param session: Shared aiohttp session.
        :param email: PodMe account email.
        :param password: PodMe account password.
        :param region: One of the keys in REGIONS.
        :param credentials: Previously stored Schibsted credentials (JSON), so a
            restart resumes the session instead of logging in again.
        :param on_credentials: Awaited with the credentials JSON whenever it changes,
            so the caller can persist it.
        """
        self.session = session
        self.region = PodMeRegion[region.upper()] if region else PodMeRegion.NO
        self._on_credentials = on_credentials
        self._auth = PodMeDefaultAuthClient(
            user_credentials=PodMeUserCredentials(email=email, password=password),
            session=session,
        )
        self._auth.region = self.region
        self._stored_credentials = credentials
        if credentials:
            try:
                self._auth.set_credentials(SchibstedCredentials.from_json(credentials))
            except Exception as err:
                LOGGER.debug("Ignoring unusable stored credentials: %s", err)

    # --- auth ---------------------------------------------------------------

    async def _token(self) -> str:
        """Return a valid access token, refreshing or logging in as needed."""
        try:
            token = await self._auth.async_get_access_token()
        except Exception as err:
            raise PodMeAuthError(f"PodMe login failed: {err}") from err
        # persist whatever came back, so a restart does not repeat the OAuth dance;
        # Schibsted throttles repeated logins
        if (creds := self._auth.get_credentials()) is not None:
            as_json = json.dumps(creds, default=str)
            if as_json != self._stored_credentials:
                self._stored_credentials = as_json
                if self._on_credentials is not None:
                    await self._on_credentials(as_json)
        return token

    async def _headers(self, *, auth: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Region": self.region.name,
            "User-Agent": API_USER_AGENT,
        }
        if auth:
            headers["Authorization"] = f"Bearer {await self._token()}"
        return headers

    # --- plumbing -----------------------------------------------------------

    async def get(
        self, path: str, *, web: bool = False, params: dict[str, Any] | None = None
    ) -> Any:
        """
        GET a PodMe API path and return the decoded JSON.

        :param path: Path below the api prefix, e.g. ``v2/user``.
        :param web: Use the unauthenticated ``web/api`` prefix instead of ``mobile/api``.
        :param params: Query parameters; None values are dropped.
        """
        prefix = "web/api" if web else "mobile/api"
        url = f"{API_BASE}/{prefix}/{path.lstrip('/')}"
        query = {k: str(v) for k, v in (params or {}).items() if v is not None}
        async with self.session.get(
            url, headers=await self._headers(auth=not web), params=query
        ) as response:
            if response.status == 404:
                raise PodMeNotFoundError(f"PodMe has no {path}")
            if response.status in (401, 403):
                body = await response.text()
                raise PodMeAuthError(f"PodMe refused the request ({response.status}): {body[:200]}")
            if response.status != 200:
                raise PodMeApiError(f"PodMe returned HTTP {response.status} for {path}")
            if not (text := await response.text()):
                return None
            return json.loads(text)

    async def post(self, path: str, *, method: str = "POST") -> bool:
        """Send a write request, returning whether PodMe accepted it."""
        url = f"{API_BASE}/mobile/api/{path.lstrip('/')}"
        async with self.session.request(method, url, headers=await self._headers()) as response:
            return response.status in (200, 201, 202, 204)

    async def paged(
        self,
        path: str,
        *,
        web: bool = False,
        params: dict[str, Any] | None = None,
        items_key: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Collect every page of a paginated endpoint.

        PodMe reports no total, so read until a short or empty page arrives.

        :param items_key: Key holding the list, when the payload is an object.
        :param limit: Stop once this many items are collected.
        """
        collected: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            payload = await self.get(
                path, web=web, params={**(params or {}), "page": page, "pageSize": page_size}
            )
            if payload is None:
                break
            # some endpoints wrap the list in an object, others return it bare, and
            # search has been seen doing both
            if isinstance(payload, list):
                batch = payload
            elif items_key and isinstance(payload, dict):
                batch = payload.get(items_key) or []
            else:
                batch = []
            if not isinstance(batch, list) or not batch:
                break
            collected.extend(batch)
            if limit is not None and len(collected) >= limit:
                return collected[:limit]
            if len(batch) < page_size:
                break
        return collected

    # --- account ------------------------------------------------------------

    async def get_user(self) -> dict[str, Any]:
        """Return the authenticated account, confirming the credentials work."""
        return await self.get("v2/user") or {}

    async def has_access(self) -> bool:
        """
        Return whether the account currently has PodMe premium access.

        ``hasAccess`` is the authoritative account-level signal; the ``isPremiumUser``
        field on the user object stays False even with an active subscription.
        """
        data = await self.get("v2/subscriptions") or {}
        return bool(data.get("hasAccess"))

    # --- podcasts -----------------------------------------------------------

    async def get_followed_podcasts(self) -> list[dict[str, Any]]:
        """Return the podcasts the account follows."""
        return await self.paged("v2/podcasts/mypodcasts")

    async def get_podcast(self, slug: str) -> dict[str, Any]:
        """Return a single podcast by slug."""
        data = await self.get(f"v2/podcast/slug/{slug}", web=True)
        if not data:
            raise PodMeNotFoundError(f"No podcast {slug}")
        return data

    async def get_categories(self) -> list[dict[str, Any]]:
        """Return the podcast categories for the configured region."""
        return await self.get("v2/podcasts/categories") or []

    async def get_category_podcasts(
        self, category_key: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return the podcasts in a category."""
        return await self.paged(
            "v2/podcast/popular", web=True, params={"category": category_key}, limit=limit
        )

    async def search_podcasts(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Search podcasts by free text."""
        return await self.paged(
            "v2/podcasts/search",
            params={"searchText": query},
            items_key="podcasts",
            limit=limit,
        )

    async def follow_podcast(self, podcast_id: int, follow: bool) -> bool:
        """Follow or unfollow a podcast, mirroring MA library membership."""
        return await self.post(
            f"v2/podcasts/bookmarks/{podcast_id}", method="POST" if follow else "DELETE"
        )

    # --- episodes -----------------------------------------------------------

    async def get_episodes(self, podcast_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        """Return the episodes of a podcast, newest first."""
        return await self.paged(f"v2/episodes/podcast/{podcast_id}", limit=limit)

    async def get_episode(self, episode_id: int) -> dict[str, Any]:
        """Return a single episode."""
        data = await self.get(f"v2/episodes/{episode_id}")
        if not data:
            raise PodMeNotFoundError(f"No episode {episode_id}")
        return data

    async def mark_played(self, episode_id: int, played: bool) -> bool:
        """Mark an episode played or unplayed in PodMe."""
        return await self.post(
            f"v2/episodes/{episode_id}/{'played' if played else 'unplayed'}", method="PATCH"
        )

    async def get_stream_url(self, episode: dict[str, Any]) -> str:
        """
        Return the audio URL for an episode.

        PodMe enforces its paywall in the client: the backend hands out signed CDN
        URLs regardless, and ``episodeCanBePlayed`` is what says whether the account
        is entitled to them. Honour it rather than streaming anyway.

        :raises PodMeSubscriptionRequiredError: If the episode is not playable on
            this account.
        :raises PodMeApiError: If no usable audio url is present.
        """
        if episode.get("episodeCanBePlayed") is False:
            raise PodMeSubscriptionRequiredError("This episode needs an active PodMe subscription")
        # a plain mp3 is preferred; the HLS variants are equivalent but need remuxing
        for key in ("url", "hlsV4Url", "hlsV3Url"):
            if url := episode.get(key):
                return str(url)
        raise PodMeApiError(f"No audio url for episode {episode.get('id')}")


# --- value parsers ----------------------------------------------------------


def parse_length(value: Any) -> int:
    """
    Return a PodMe ``HH:MM:SS`` length as whole seconds, 0 when unreadable.

    :param value: The raw value; PodMe also sends plain second counts on occasion.
    """
    if value is None:
        return 0
    if isinstance(value, int | float):
        return int(value)
    parts = str(value).split(":")
    if not all(p.strip().lstrip("-").replace(".", "", 1).isdigit() for p in parts if p.strip()):
        return 0
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return int(seconds)


def parse_int(value: Any) -> int | None:
    """
    Return an int from a PodMe field, or None.

    PodMe sends ``""`` and ``null`` interchangeably for absent numbers, which is
    what trips up strict parsing.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def image_urls(item: dict[str, Any]) -> list[str]:
    """Return an item's image urls, largest first, de-duplicated."""
    ordered = [
        item.get("largeImageUrl"),
        item.get("imageUrl"),
        item.get("mediumImageUrl"),
        item.get("smallImageUrl"),
    ]
    seen: list[str] = []
    for url in ordered:
        if url and url not in seen:
            seen.append(str(url))
    return seen
