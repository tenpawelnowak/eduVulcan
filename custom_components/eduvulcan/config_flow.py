"""Config flow for the eduVULCAN integration.

The user pastes either the raw JWT token(s) or the whole JSON blob from the
hidden <input> on https://eduvulcan.pl/api/ap (visible in the page source
after logging in). We decode the tenant from the JWT payload, register a
fresh RSA credential against the hebeCE API and store the serialized
credential in the config entry. The JWTs themselves are one-time use and
are NOT stored.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CREDENTIAL,
    CONF_PASSWORD,
    CONF_TENANT,
    CONF_TOKENS,
    CONF_USERNAME,
    DEVICE_MODEL,
    DEVICE_OS,
    DOMAIN,
)
from .iris._exceptions import (
    ExpiredTokenException,
    FailedRequestException,
    IrisApiException,
    UsedTokenException,
    WrongTokenException,
)
from .iris.api import IrisHebeCeApi
from .iris.credentials import RsaCredential

_LOGGER = logging.getLogger(__name__)

JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKENS): TextSelector(
            TextSelectorConfig(multiline=True)
        ),
    }
)


def _extract_tokens(raw: str) -> list[str]:
    """Pull JWT tokens out of whatever the user pasted."""
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            blob = None
        if isinstance(blob, dict):
            tokens = blob.get("Tokens") or blob.get("tokens") or []
            if isinstance(tokens, list) and tokens:
                return [str(token) for token in tokens]
    return JWT_RE.findall(raw)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode the (unverified) payload segment of a JWT."""
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


class EduVulcanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the eduVULCAN config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EduVulcanOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            tokens = _extract_tokens(user_input[CONF_TOKENS])
            if not tokens:
                errors["base"] = "no_tokens"
            else:
                try:
                    payload = _decode_jwt_payload(tokens[0])
                    tenant = payload["tenant"]
                except (IndexError, KeyError, ValueError, binascii.Error):
                    errors["base"] = "invalid_jwt"
                    tenant = None

                if tenant:
                    await self.async_set_unique_id(f"{DOMAIN}_{tenant}_{payload.get('uid', tokens[0][-16:])}")
                    self._abort_if_unique_id_configured()

                    # RSA key generation is CPU-bound -> executor.
                    credential = await self.hass.async_add_executor_job(
                        RsaCredential.create_new, DEVICE_OS, DEVICE_MODEL
                    )
                    api = IrisHebeCeApi(credential)
                    try:
                        await api.register_by_jwt(tokens=tokens, tenant=tenant)
                        accounts = await api.get_accounts()
                    except (WrongTokenException, UsedTokenException, ExpiredTokenException):
                        errors["base"] = "invalid_token"
                    except FailedRequestException:
                        errors["base"] = "cannot_connect"
                    except IrisApiException:
                        _LOGGER.exception("Unexpected eduVULCAN API error")
                        errors["base"] = "unknown"
                    else:
                        if not accounts:
                            errors["base"] = "no_pupils"
                        else:
                            pupils = ", ".join(
                                account.pupil.first_name for account in accounts
                            )
                            await api._http.close()  # noqa: SLF001
                            return self.async_create_entry(
                                title=f"eduVULCAN ({pupils})",
                                data={
                                    CONF_CREDENTIAL: credential.model_dump_json(),
                                    CONF_TENANT: tenant,
                                },
                            )
                    finally:
                        if errors:
                            await api._http.close()  # noqa: SLF001

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class EduVulcanOptionsFlow(OptionsFlow):
    """eduVULCAN e-mail and password for reading messages from the web module.

    Stored in plain text in the config entry. Leave both empty to turn it off.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            username = (user_input.get(CONF_USERNAME) or "").strip()
            password = user_input.get(CONF_PASSWORD) or ""
            if not username or not password:
                return self.async_create_entry(data={})
            return self.async_create_entry(
                data={CONF_USERNAME: username, CONF_PASSWORD: password}
            )

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_USERNAME,
                    description={"suggested_value": options.get(CONF_USERNAME)},
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
                vol.Optional(
                    CONF_PASSWORD,
                    description={"suggested_value": options.get(CONF_PASSWORD)},
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
