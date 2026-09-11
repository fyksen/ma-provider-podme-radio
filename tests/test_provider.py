"""Exercise the PodMeProvider against the live PodMe API.

Music Assistant itself is not installed here, so the few symbols the provider imports
from it are stubbed. `rank_episodes_by_date` is copied verbatim from the MA source so
ordering matches the real host.

Needs PODME_EMAIL and PODME_PASSWORD in the environment. The Schibsted session is
cached to PODME_TOKEN_CACHE (default .podme_token.json) because repeated logins get
throttled - keep that file out of version control.
"""

import asyncio
import importlib.util
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from music_assistant_models.enums import MediaType

PROVIDER_DIR = str(Path(__file__).resolve().parent.parent / "podme")
TOKEN_CACHE = Path(os.environ.get("PODME_TOKEN_CACHE", ".podme_token.json"))

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}  {detail}")
    if not cond:
        FAILS.append(label)


# --- stub out the music_assistant host package ------------------------------


def _module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


ma = _module("music_assistant")
ma.__path__ = []

controllers = _module("music_assistant.controllers")
controllers.__path__ = []
cache = _module("music_assistant.controllers.cache")
cache.use_cache = lambda *a, **kw: lambda fn: fn  # passthrough

helpers = _module("music_assistant.helpers")
helpers.__path__ = []
pp = _module("music_assistant.helpers.podcast_parsers")


# copied from music_assistant/helpers/podcast_parsers.py
def rank_episodes_by_date(dates: list[Any]) -> list[int]:
    total = len(dates)
    dated = [idx for idx, date in enumerate(dates) if date is not None]
    if not dated:
        return [total - idx for idx in range(total)]
    undated = [idx for idx, date in enumerate(dates) if date is None]
    positions = [0] * total
    for position, idx in enumerate(undated + sorted(dated, key=lambda idx: dates[idx]), 1):
        positions[idx] = position
    return positions


pp.rank_episodes_by_date = rank_episodes_by_date

models = _module("music_assistant.models")
models.__path__ = []
mp = _module("music_assistant.models.music_provider")


@dataclass
class _Config:
    values: dict

    def get_value(self, key):
        return self.values.get(key)


class _ConfigController:
    """Records the credential writes the provider makes."""

    def __init__(self):
        self.written = {}

    def set_raw_provider_config_value(self, instance_id, key, value, encrypted=False):
        self.written[key] = value


class MusicProvider:
    """Minimal stand-in for the MA base provider."""

    def __init__(self, mass, manifest, config, supported_features=None):
        self.mass = mass
        self.manifest = manifest
        self.config = config
        self.supported_features = supported_features or set()
        self.domain = "podme"
        self.instance_id = "podme--test"
        self.logger = types.SimpleNamespace(
            warning=lambda *a, **k: print("     [warn]", a[0] % a[1:] if len(a) > 1 else a[0]),
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
        )


mp.MusicProvider = MusicProvider

providers = _module("music_assistant.providers")
providers.__path__ = []
pkg = _module("music_assistant.providers.podme")
pkg.__path__ = [PROVIDER_DIR]

for name, filename in (
    ("music_assistant.providers.podme.api", "api.py"),
    ("music_assistant.providers.podme", "__init__.py"),
):
    spec = importlib.util.spec_from_file_location(name, f"{PROVIDER_DIR}/{filename}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)

prov_mod = sys.modules["music_assistant.providers.podme"]
print("provider module imported OK")


async def main():
    async with aiohttp.ClientSession() as session:
        cfg_controller = _ConfigController()
        mass = types.SimpleNamespace(http_session=session, config=cfg_controller)
        config = _Config(
            {
                "email": os.environ["PODME_EMAIL"],
                "password": os.environ["PODME_PASSWORD"],
                "region": os.environ.get("PODME_REGION", "NO"),
                "max_episodes": 0,
                "credentials": TOKEN_CACHE.read_text() if TOKEN_CACHE.exists() else None,
            }
        )
        prov = prov_mod.PodMeProvider(mass, None, config, prov_mod.SUPPORTED_FEATURES)

        print("\n== auth ==")
        try:
            await prov.handle_async_init()
            check("sign in", True, "authenticated")
        except Exception as err:
            check("sign in", False, f"{type(err).__name__}: {err}")
            print("\nFAILURES:", FAILS)
            return 1
        await prov.loaded_in_mass()

        # persist whatever session the provider produced, so reruns do not re-login
        if creds := cfg_controller.written.get("credentials"):
            TOKEN_CACHE.write_text(creds)
            TOKEN_CACHE.chmod(0o600)
            print("     (session cached for next run)")

        has_access = await prov.api.has_access()
        check("subscription active", has_access, f"hasAccess={has_access}")

        print("\n== library (followed podcasts) ==")
        lib = [p async for p in prov.get_library_podcasts()]
        check("library podcasts", len(lib) > 0, f"{len(lib)} followed")
        for p in lib[:4]:
            print(f"     {p.item_id:<28} {p.name[:30]:<32} version={p.version!r}")
        check("library items have images", all(p.metadata.images for p in lib))
        check(
            "library marked in_library", all(m.in_library for p in lib for m in p.provider_mappings)
        )

        print("\n== browse ==")
        root = await prov.browse("podme--test://")
        check("browse root", len(root) > 0, f"{len(root)} categories")
        check("root is folders", all(f.media_type == MediaType.FOLDER for f in root))
        print("    ", [f.name for f in root][:6], "...")
        first = root[0]
        sub = await prov.browse(f"podme--test://{first.item_id}")
        check(
            "browse category",
            len(sub) > 0,
            f"{first.name}: {len(sub)} podcasts, first={sub[0].name if sub else None}",
        )
        check("category items are podcasts", all(p.media_type == MediaType.PODCAST for p in sub))

        print("\n== search ==")
        res = await prov.search("krim", [MediaType.PODCAST], limit=8)
        check("search returns podcasts", len(res.podcasts) > 0, f"{len(res.podcasts)} hits")
        check(
            "search ignores other types",
            len((await prov.search("krim", [MediaType.TRACK], limit=5)).podcasts) == 0,
        )

        print("\n== podcast + episodes ==")
        target = lib[0] if lib else res.podcasts[0]
        slug = target.item_id
        pod = await prov.get_podcast(slug)
        check("get_podcast", pod.name == target.name, f"{pod.name} | publisher={pod.publisher}")
        check("podcast uri", bool(pod.uri), pod.uri)

        eps = [e async for e in prov.get_podcast_episodes(slug)]
        check("episodes", len(eps) > 0, f"{len(eps)} episodes")
        check(
            "episode durations",
            all(e.duration > 0 for e in eps),
            f"min={min((e.duration for e in eps), default=0)}s",
        )
        positions = sorted(e.position for e in eps)
        check(
            "positions 1..n unique",
            positions == list(range(1, len(eps) + 1)),
            f"{positions[:3]}..{positions[-3:]}",
        )
        check("episode podcast ref", all(e.podcast is not None for e in eps))
        newest = eps[0]
        print(f"     newest: {newest.name[:48]} ({newest.duration}s) playable={newest.is_playable}")
        print(f"     resume_ms={newest.resume_position_ms} fully_played={newest.fully_played}")

        one = await prov.get_podcast_episode(newest.item_id)
        check("get_podcast_episode round-trip", one.item_id == newest.item_id)

        print("\n== playback ==")
        sd = await prov.get_stream_details(newest.item_id, MediaType.PODCAST_EPISODE)
        check("streamdetails path", sd.path.startswith("https://"), sd.path[:78])
        check("streamdetails duration", sd.duration > 0, f"{sd.duration}s")
        check("streamdetails seekable", sd.can_seek and sd.allow_seek)
        print(f"     stream_type={sd.stream_type} content_type={sd.audio_format.content_type}")

        async with session.get(sd.path, headers={"Range": "bytes=0-4095"}) as resp:
            blob = await resp.read()
        check("audio fetchable", resp.status in (200, 206), f"HTTP {resp.status} {len(blob)}B")
        check(
            "audio is real",
            blob[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3") or b"ftyp" in blob[:64],
            repr(blob[:4]),
        )
        full_len = resp.headers.get("Content-Range", "")
        print(f"     content-range: {full_len}")

        print("\n== error handling ==")
        try:
            await prov.get_podcast("definitely-not-a-real-podcast-xyz")
            check("missing podcast raises", False)
        except Exception as err:
            check(
                "missing podcast raises",
                type(err).__name__ == "MediaNotFoundError",
                type(err).__name__,
            )

    print("\n" + ("ALL PASSED" if not FAILS else f"FAILURES: {FAILS}"))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
