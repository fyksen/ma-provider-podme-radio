"""
Setup flow for the PodMe provider.

Credentials cannot be declared as ordinary config entries: those are resolved from a
provider instance, which does not exist yet when the provider is first added. They are
collected here instead, and read back at runtime with ``get_setup_value``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.podme.api import REGIONS

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_REGION = "region"

_ENTRIES = (
    ConfigEntry(key=CONF_EMAIL, type=ConfigEntryType.STRING, required=True),
    ConfigEntry(key=CONF_PASSWORD, type=ConfigEntryType.SECURE_STRING, required=True),
    ConfigEntry(
        key=CONF_REGION,
        type=ConfigEntryType.STRING,
        required=True,
        default_value="NO",
        options=[ConfigValueOption(key, title) for key, title in REGIONS],
    ),
)


async def run_setup(session: SetupSession) -> None:
    """Collect the PodMe account details and create the provider."""
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value)) for entry in _ENTRIES
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            # most likely a bad password, or Schibsted throttling repeated sign-ins
            errors = {"base": err.translation_key or str(err)}
