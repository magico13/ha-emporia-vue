"""Emporia's registered redirect for Home Assistant's native PKCE flow."""

from homeassistant.helpers.config_entry_oauth2_flow import (
    MY_AUTH_CALLBACK_PATH,
    LocalOAuth2ImplementationWithPkce,
)


class EmporiaOAuth2Implementation(LocalOAuth2ImplementationWithPkce):
    """Use the callback registered for Emporia's public Cognito client."""

    @property
    def redirect_uri(self) -> str:
        """Return the registered callback, including on local-only instances."""
        return MY_AUTH_CALLBACK_PATH
