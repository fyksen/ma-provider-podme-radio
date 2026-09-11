# PodMe provider for Music Assistant

Listen to [PodMe](https://podme.com) podcasts inside
[Music Assistant](https://music-assistant.io).

PodMe is a Nordic podcast service. You sign in with your own account; the premium
catalogue needs an active PodMe subscription, and free titles play without one.

## Features

- **Your library** — the podcasts you follow on PodMe sync into Music Assistant, and
  following or unfollowing from Music Assistant writes back to your PodMe account.
- **Browse** the full catalogue by PodMe's categories (21 of them in the Norwegian
  region: Premium, True crime, Dokumentar, Samtaler, Samfunn, Teknologi, …).
- **Search** podcasts by name.
- **Playback** of premium and free episodes, as plain MP3.
- **Progress carries across** — PodMe's own resume position and played state are read
  into Music Assistant, and finished episodes are reported back to PodMe.
- **Regions** — Norway, Sweden, Denmark and Finland (see the caveat below).

## Install

Music Assistant loads providers only from inside its own package directory, so
installing means getting `podme/` to `music_assistant/providers/podme`. The path
contains the image's Python version, which changes between releases — the included
script finds it for you.

```bash
git clone https://github.com/fyksen/podme-music-assistant.git
cd podme-music-assistant
./install.sh
```

That prints the exact `volumes:` line for your setup, for example:

```yaml
services:
  music-assistant-server:
    volumes:
      - /path/to/podme-music-assistant/podme:/app/venv/lib/python3.14/site-packages/music_assistant/providers/podme:ro
```

Add it to your `compose.yml`, then `docker compose up -d`.

Finally, in Music Assistant go to **Settings → Music Providers → Add Provider →
PodMe** and sign in. The `podme-api` dependency is pinned in `manifest.json` and
installed by Music Assistant itself when the provider first loads.

### Settings

| Setting | Meaning |
| --- | --- |
| E-mail / Password | Your PodMe account |
| Region | Which PodMe catalogue to show (default Norway) |
| Maximum episodes per podcast | 0 for all; keeps the most recent otherwise |

### Other install options

| Command | What it does |
| --- | --- |
| `./install.sh` | Print the bind-mount line for a running container (recommended) |
| `./install.sh --copy` | Copy straight into the running container — quick to try, lost when it is recreated |
| `./install.sh --venv <path>` | Install into a source checkout or venv |

Set `MA_CONTAINER=<name>` if your container isn't called `music-assistant-server`.

## Notes and limitations

- **A subscription is needed for premium titles.** PodMe enforces its paywall in the
  client: the backend hands out signed CDN URLs regardless of entitlement, and the
  per-episode `episodeCanBePlayed` flag is what says whether your account may use
  them. This provider honours that flag and refuses to stream episodes your account
  isn't entitled to, rather than fetching the audio anyway. Without a subscription
  you'll still see the catalogue, and free episodes play normally.
- **Sign-in is cached.** Schibsted throttles repeated logins, so the session is
  stored (encrypted) in the provider config and reused across restarts. The provider
  authenticates once, then refreshes.
- **`isPremiumUser` is not the subscription signal.** It stays `false` even with an
  active subscription; `hasAccess` on `v2/subscriptions` is the account-level truth,
  and `episodeCanBePlayed` the per-episode one. Both are used here.
- **Denmark and Finland are untested.** Upstream `podme-api` hardcodes Norway's OAuth
  client id for both, marked `TODO: Check`, so sign-in may fail there. Norway and
  Sweden have real client ids.
- **Podcasts only.** PodMe has no other media types.

## Relationship to `podme_api`

This provider uses [`podme_api`](https://github.com/bendikrb/podme_api) (MIT, by
Bendik R. Brenne) for **authentication only**. Its Schibsted OAuth/PKCE flow — CSRF,
authorize, finalize, token exchange, refresh — is the hard part and works well.

The data layer is deliberately this repo's own (`podme/api.py`), because the
published `podme-api==1.4.3` does not work against today's API:

| Problem | Detail |
| --- | --- |
| Stale User-Agent | Sends `Podme android app/6.29.3`; PodMe answers `403 Forbidden`. Git `main` bumps it to `6.38.5`, which works, but that is unreleased. |
| `get_username` | `subscription_platform: int` receives `null` |
| `get_user_subscription` | `adyen_subscriptions: list` receives `null` |
| `get_user_podcasts` | `author_id: Optional[int]` receives `""` |
| `get_home_screen` | section schema changed |

Those four are strict-dataclass failures: `mashumaro` raises rather than degrading
when a field's type no longer matches. `get_user_podcasts` is the user's own followed
list, so this is not a corner case. Git `main` fixes none of them.

Hence: borrow the auth, parse the JSON defensively. A field PodMe changes then costs
a missing value instead of a dead provider. These bugs are worth reporting upstream.

## How it works

All endpoints live under `https://api.podme.com`, split between an authenticated
`mobile/api` prefix and an unauthenticated `web/api` one:

| Purpose | Endpoint |
| --- | --- |
| Account | `mobile/api/v2/user` |
| Subscription | `mobile/api/v2/subscriptions` (`hasAccess`) |
| Followed podcasts | `mobile/api/v2/podcasts/mypodcasts` |
| Follow / unfollow | `mobile/api/v2/podcasts/bookmarks/{id}` (POST / DELETE) |
| Categories | `mobile/api/v2/podcasts/categories` |
| Category listing | `web/api/v2/podcast/popular?category=` |
| Search | `mobile/api/v2/podcasts/search?searchText=` |
| Podcast | `web/api/v2/podcast/slug/{slug}` |
| Episodes | `mobile/api/v2/episodes/podcast/{id}` |
| Episode | `mobile/api/v2/episodes/{id}` |
| Mark played | `mobile/api/v2/episodes/{id}/played` (PATCH) |

Episodes carry a signed Akamai MP3 URL plus HLS and Smooth Streaming variants; the
MP3 is preferred. Item ids are the podcast `slug`, and `"{slug} {episodeId}"` for an
episode — PodMe slugs contain no spaces, so that round-trips.

## Development

`tests/test_provider.py` drives the provider against the live API: sign-in, library,
browse, search, episode listing and ordering, stream resolution, and fetching real
audio bytes. It stubs the few symbols the provider imports from Music Assistant, so
it runs without a server install.

```bash
python3 -m venv .venv
.venv/bin/pip install aiohttp music-assistant-models podme-api==1.4.3 ruff

export PODME_EMAIL='you@example.com'
export PODME_PASSWORD='...'
.venv/bin/python tests/test_provider.py

.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The test caches the Schibsted session to `.podme_token.json` (gitignored) so reruns
don't re-authenticate — **repeated logins get throttled**, so keep that cache. Never
commit it, or your credentials.

Lint settings are taken from the Music Assistant server repository so the code stays
ready to upstream.

## License

Apache-2.0, matching Music Assistant. See [LICENSE](LICENSE).
`podme_api` is MIT, by Bendik R. Brenne.

Not affiliated with or endorsed by PodMe. Your PodMe subscription and
[their terms](https://podme.com) still apply.
