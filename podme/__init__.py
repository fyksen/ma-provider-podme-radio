"""
PodMe podcast provider for Music Assistant.

PodMe is a Nordic podcast service whose premium catalogue needs a paid subscription.
Sign in with your own account and your followed podcasts, the catalogue and playback
all become available in Music Assistant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.podcast_parsers import rank_episodes_by_date
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.podme.api import (
    PodMeApi,
    PodMeApiError,
    PodMeAuthError,
    PodMeNotFoundError,
    PodMeSubscriptionRequiredError,
    image_urls,
    parse_length,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_REGION = "region"
CONF_MAX_EPISODES = "max_episodes"
# stored Schibsted session, so a restart resumes instead of logging in again
CONF_CREDENTIALS = "credentials"

CACHE_CATEGORIES = 3600 * 24
CACHE_PODCAST = 3600 * 6
CACHE_EPISODES = 3600 * 2
CACHE_SEARCH = 3600 * 12

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_PODCASTS,
    # PodMe's "bookmarks" are its followed-podcast list, which maps onto the MA library
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PodMeProvider(mass, manifest, config, SUPPORTED_FEATURES)


class PodMeProvider(MusicProvider):
    """Podcast provider for PodMe."""

    api: PodMeApi

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """
        Return Config entries to setup this provider.

        The account details are deliberately absent: config entries are resolved from an
        existing provider instance, so they cannot be collected when the provider is first
        added. They live in setup_flow.py and are read back with get_setup_value.
        """
        return (
            ConfigEntry(
                key=CONF_MAX_EPISODES,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=0,
            ),
            ConfigEntry(
                key=CONF_CREDENTIALS,
                type=ConfigEntryType.STRING,
                required=False,
                hidden=True,
                default_value=None,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.max_episodes = int(str(self.config.get_value(CONF_MAX_EPISODES) or 0))
        stored = self.config.get_value(CONF_CREDENTIALS)
        # collected by the setup flow, not by the (instance-resolved) config entries
        email = str(self.get_setup_value(CONF_EMAIL) or "")
        password = str(self.get_setup_value(CONF_PASSWORD) or "")
        if not email or not password:
            raise LoginFailed("No PodMe account configured; re-run the provider setup")
        self.api = PodMeApi(
            self.mass.http_session,
            email=email,
            password=password,
            region=str(self.get_setup_value(CONF_REGION) or "NO"),
            credentials=str(stored) if stored else None,
            on_credentials=self._store_credentials,
        )
        # slug -> numeric id; the episode endpoints are keyed by id, the rest by slug
        self._podcast_ids: dict[str, int] = {}
        try:
            await self.api.get_user()
        except PodMeAuthError as err:
            raise LoginFailed(f"Could not sign in to PodMe: {err}") from err

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        try:
            if not await self.api.has_access():
                self.logger.warning(
                    "This PodMe account has no active subscription. Free podcasts will play, "
                    "but premium episodes will be skipped until a subscription is active."
                )
        except PodMeApiError as err:
            self.logger.debug("Could not check subscription status: %s", err)

    async def _store_credentials(self, credentials: str) -> None:
        """Persist the Schibsted session so restarts do not re-run the OAuth flow."""
        self.mass.config.set_raw_provider_config_value(
            self.instance_id, CONF_CREDENTIALS, credentials, encrypted=True
        )

    # --- search -------------------------------------------------------------

    @use_cache(CACHE_SEARCH)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """Perform search on musicprovider."""
        if MediaType.PODCAST not in media_types:
            return SearchResults()
        hits = await self.api.search_podcasts(search_query, limit=max(limit, 1))
        return SearchResults(podcasts=[self._parse_podcast(hit) for hit in hits])

    # --- library ------------------------------------------------------------

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve the podcasts the account follows on PodMe."""
        for item in await self.api.get_followed_podcasts():
            yield self._parse_podcast(item, in_library=True)

    async def library_add(self, item: MediaItemType) -> bool:
        """Follow a podcast on PodMe."""
        if item.media_type != MediaType.PODCAST:
            return False
        podcast_id = await self._resolve_id(item.item_id)
        return await self.api.follow_podcast(podcast_id, follow=True)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Unfollow a podcast on PodMe."""
        if media_type != MediaType.PODCAST:
            return False
        podcast_id = await self._resolve_id(prov_item_id)
        return await self.api.follow_podcast(podcast_id, follow=False)

    # --- podcasts and episodes ----------------------------------------------

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        return self._parse_podcast(await self._get_podcast_raw(prov_podcast_id))

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Get all PodcastEpisodes for given podcast id."""
        for episode in await self._get_episodes(prov_podcast_id):
            yield episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        slug, episode_id = self._split_episode_id(prov_episode_id)
        for episode in await self._get_episodes(slug):
            if episode.item_id == prov_episode_id:
                return episode
        raise MediaNotFoundError(f"Episode not found: {episode_id}")

    async def _get_episodes(self, slug: str) -> list[PodcastEpisode]:
        """Return every episode of a podcast as MA items."""
        raw_podcast = await self._get_podcast_raw(slug)
        podcast = self._parse_podcast(raw_podcast)
        mapping = ItemMapping.from_item(podcast)
        raw_episodes = await self._get_episodes_raw(raw_podcast["id"])
        # PodMe lists newest first; MA numbers episodes oldest to newest
        positions = rank_episodes_by_date([ep.get("dateAdded") for ep in raw_episodes])
        return [
            self._parse_episode(slug, raw, position, mapping)
            for position, raw in zip(positions, raw_episodes, strict=True)
        ]

    async def _resolve_id(self, slug: str) -> int:
        """Return the numeric podcast id for a slug."""
        if slug not in self._podcast_ids:
            await self._get_podcast_raw(slug)
        return self._podcast_ids[slug]

    # --- streaming ----------------------------------------------------------

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a podcast episode."""
        _, episode_id = self._split_episode_id(item_id)
        try:
            episode = await self.api.get_episode(int(episode_id))
            stream_url = await self.api.get_stream_url(episode)
        except PodMeNotFoundError as err:
            raise MediaNotFoundError(f"Episode not found: {episode_id}") from err
        except PodMeSubscriptionRequiredError as err:
            raise UnplayableMediaError(str(err)) from err
        except PodMeApiError as err:
            raise UnplayableMediaError(f"PodMe could not serve this episode: {err}") from err
        # PodMe signs its CDN urls, so the extension is followed by a query string that
        # would otherwise hide it from try_parse
        bare_url = stream_url.split("?", 1)[0]
        is_hls = bare_url.endswith(".m3u8")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN if is_hls else ContentType.try_parse(bare_url)
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HLS if is_hls else StreamType.HTTP,
            path=stream_url,
            duration=parse_length(episode.get("length")),
            can_seek=True,
            allow_seek=True,
        )

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Report playback back to PodMe so progress follows the account."""
        if streamdetails.media_type != MediaType.PODCAST_EPISODE:
            return
        if not streamdetails.fully_played:
            return
        try:
            _, episode_id = self._split_episode_id(streamdetails.item_id)
            await self.api.mark_played(int(episode_id), played=True)
        except (PodMeApiError, ValueError) as err:
            self.logger.debug("Could not report playback to PodMe: %s", err)

    # --- browse -------------------------------------------------------------

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        subpath = path.split("://", 1)[1] if "://" in path else ""
        parts = [part for part in subpath.split("/") if part]
        categories = await self._get_categories()
        if not parts:
            return [
                BrowseFolder(
                    item_id=str(category.get("key")),
                    provider=self.instance_id,
                    path=f"{self.instance_id}://{category.get('key')}",
                    name=str(category.get("name") or category.get("key")),
                )
                for category in categories
                if category.get("key")
            ]
        key = parts[0]
        if not any(str(category.get("key")) == key for category in categories):
            raise MediaNotFoundError(f"Unknown browse path: {path}")
        podcasts = await self.api.get_category_podcasts(key, limit=200)
        return [self._parse_podcast(item) for item in podcasts]

    # --- cached api calls ---------------------------------------------------

    @use_cache(CACHE_CATEGORIES)
    async def _get_categories(self) -> list[dict[str, Any]]:
        return await self.api.get_categories()

    @use_cache(CACHE_PODCAST)
    async def _get_podcast_raw_cached(self, slug: str) -> dict[str, Any]:
        try:
            return await self.api.get_podcast(slug)
        except PodMeNotFoundError as err:
            raise MediaNotFoundError(f"Podcast not found: {slug}") from err

    async def _get_podcast_raw(self, slug: str) -> dict[str, Any]:
        """Return a podcast payload, remembering its numeric id."""
        data = await self._get_podcast_raw_cached(slug)
        if (podcast_id := data.get("id")) is not None:
            self._podcast_ids[slug] = int(podcast_id)
        return data

    @use_cache(CACHE_EPISODES)
    async def _get_episodes_raw(self, podcast_id: int) -> list[dict[str, Any]]:
        return await self.api.get_episodes(
            podcast_id, limit=self.max_episodes if self.max_episodes > 0 else None
        )

    # --- parsers ------------------------------------------------------------

    @staticmethod
    def _split_episode_id(item_id: str) -> tuple[str, str]:
        """Return the (podcast slug, PodMe episode id) an episode item id addresses."""
        slug, _, episode_id = item_id.rpartition(" ")
        if not slug or not episode_id:
            raise MediaNotFoundError(f"Not an episode id: {item_id!r}")
        return slug, episode_id

    def _provider_mapping(self, item_id: str, *, in_library: bool | None = None) -> ProviderMapping:
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
            in_library=in_library,
        )

    def _images(self, item: dict[str, Any]) -> UniqueList[MediaItemImage]:
        return UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
                for url in image_urls(item)
            ]
        )

    def _parse_podcast(self, item: dict[str, Any], *, in_library: bool | None = None) -> Podcast:
        """Parse a PodMe podcast, from any of the endpoints that return one."""
        # search results name the fields differently from the catalogue
        slug = str(item.get("slug") or "")
        title = item.get("title") or item.get("podcastTitle") or slug
        followed = in_library if in_library is not None else item.get("isFollowed")
        podcast = Podcast(
            item_id=slug,
            provider=self.instance_id,
            name=str(title),
            publisher=item.get("authorFullName") or "PodMe",
            provider_mappings={self._provider_mapping(slug, in_library=followed)},
        )
        if description := (item.get("description") or None):
            podcast.metadata.description = str(description)
        podcast.metadata.images = self._images(item)
        if categories := item.get("categories"):
            names = {
                str(c.get("name")) for c in categories if isinstance(c, dict) and c.get("name")
            }
            if names:
                podcast.metadata.genres = names
        # premium titles are the reason this provider needs an account; say so in the UI
        if item.get("isPremium"):
            podcast.metadata.explicit = None
            podcast.version = "Premium"
        return podcast

    def _parse_episode(
        self,
        slug: str,
        item: dict[str, Any],
        position: int,
        podcast: Podcast | ItemMapping,
    ) -> PodcastEpisode:
        """Parse a PodMe episode."""
        item_id = f"{slug} {item['id']}"
        episode = PodcastEpisode(
            item_id=item_id,
            provider=self.instance_id,
            name=str(item.get("title") or item["id"]),
            position=position,
            podcast=podcast,
            duration=parse_length(item.get("length")),
            provider_mappings={self._provider_mapping(item_id)},
        )
        if description := (item.get("description") or None):
            episode.metadata.description = str(description)
        episode.metadata.images = self._images(item)
        # PodMe tracks progress per account; surface it so MA can resume in step
        if (played := item.get("hasPlayed")) is not None:
            episode.fully_played = bool(played)
        if (spot := item.get("currentSpotSec")) is not None:
            try:
                episode.resume_position_ms = int(spot) * 1000
            except TypeError, ValueError:
                episode.resume_position_ms = None
        # an episode the account cannot play is listed but not streamable
        if item.get("episodeCanBePlayed") is False:
            episode.is_playable = False
        return episode
