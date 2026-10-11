"""Browser login tests using Home Assistant's native OAuth flow manager."""

from unittest.mock import patch

import jwt
import pytest
from aiohttp.test_utils import make_mocked_request
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.config_entry_oauth2_flow import (
    DATA_JWT_SECRET,
    MY_AUTH_CALLBACK_PATH,
    LocalOAuth2ImplementationWithPkce,
    OAuth2AuthorizeCallbackView,
)
from homeassistant.helpers.http import KEY_HASS
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from custom_components.emporia_vue.config_flow import InvalidAuth
from custom_components.emporia_vue.const import (
    AUTH_METHOD,
    AUTH_METHOD_APPLE,
    AUTH_METHOD_GOOGLE,
    AUTH_METHOD_TOKENS,
    CONF_OAUTH_PROVIDER,
    DOMAIN,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations"),
]
TOKENS = {
    "id_token": "id",
    "access_token": "access",
    "refresh_token": "refresh",
    "expires_in": 3600,
}
OPTIONS = {
    "enable_1m": True,
    "enable_1d": True,
    "enable_1mon": True,
    "solar_invert": True,
}


@pytest.fixture
def validate():
    """Mock only Emporia's network login, preserving the actual HA flow."""

    async def validated(data):
        return {**data, "customer_gid": "123", "title": "user@example.com (123)"}

    with patch(
        "custom_components.emporia_vue.config_flow.validate_input",
        side_effect=validated,
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def setup_entry():
    """Do not set up device polling after creating the test config entry."""
    with patch("custom_components.emporia_vue.async_setup_entry", return_value=True):
        yield


async def start(hass, provider):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {AUTH_METHOD: provider}
    )


async def finish(hass, result):
    state = jwt.decode(
        URL(result["url"]).query["state"],
        hass.data[DATA_JWT_SECRET],
        algorithms=["HS256"],
    )
    done = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "test-code", "state": state}
    )
    assert done["type"] == FlowResultType.EXTERNAL_STEP_DONE
    return await hass.config_entries.flow.async_configure(result["flow_id"])


@pytest.mark.parametrize(
    "provider,identity",
    [(AUTH_METHOD_GOOGLE, "Google"), (AUTH_METHOD_APPLE, "SignInWithApple")],
)
async def test_native_browser_login(hass, aioclient_mock, validate, provider, identity):
    """Both providers retain native PKCE/state and create existing token entries."""
    result = await start(hass, provider)
    assert result["type"] == FlowResultType.EXTERNAL_STEP
    query = URL(result["url"]).query
    assert query["identity_provider"] == identity
    assert set(query["scope"].split()) == {
        "openid",
        "email",
        "aws.cognito.signin.user.admin",
    }
    assert query["redirect_uri"] == MY_AUTH_CALLBACK_PATH
    assert query["code_challenge_method"] == "S256"
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert isinstance(flow.flow_impl, LocalOAuth2ImplementationWithPkce)
    verifier = flow.flow_impl.code_verifier
    assert query[
        "code_challenge"
    ] == LocalOAuth2ImplementationWithPkce.compute_code_challenge(verifier)
    aioclient_mock.post(
        "https://auth.emporiaenergy.com/oauth2/token", json=dict(TOKENS)
    )
    created = await finish(hass, result)
    assert created["type"] == FlowResultType.CREATE_ENTRY
    assert created["data"][AUTH_METHOD] == AUTH_METHOD_TOKENS
    assert created["data"][CONF_OAUTH_PROVIDER] == provider
    assert "token" not in created["data"]
    assert "password" not in created["data"]
    assert {key: created["data"][key] for key in OPTIONS} == OPTIONS
    request = aioclient_mock.mock_calls[0][2]
    assert request["code_verifier"] == verifier
    assert request["redirect_uri"] == MY_AUTH_CALLBACK_PATH
    assert "client_secret" not in request
    validate.assert_awaited_once()
    await hass.async_block_till_done()


async def test_pkce_is_per_flow(hass):
    """Separate attempts cannot overwrite each other's code verifier."""
    first = await start(hass, AUTH_METHOD_GOOGLE)
    second = await start(hass, AUTH_METHOD_APPLE)
    assert (
        URL(first["url"]).query["code_challenge"]
        != URL(second["url"]).query["code_challenge"]
    )


async def test_authorization_denied(hass):
    """Provider cancellation uses Home Assistant's standard error path."""
    result = await start(hass, AUTH_METHOD_GOOGLE)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"error": "access_denied"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["reason"] == "user_rejected_authorize"


@pytest.mark.parametrize(
    "status,reason", [(400, "oauth_unauthorized"), (500, "oauth_failed")]
)
async def test_exchange_error(hass, aioclient_mock, status, reason):
    """Token exchange failures never create config entries."""
    result = await start(hass, AUTH_METHOD_GOOGLE)
    aioclient_mock.post(
        "https://auth.emporiaenergy.com/oauth2/token",
        status=status,
        json={"error": "invalid_grant"},
    )
    assert (await finish(hass, result))["reason"] == reason


@pytest.mark.parametrize("missing", ["id_token", "access_token", "refresh_token"])
async def test_incomplete_tokens(hass, aioclient_mock, validate, missing):
    """A partial response cannot reach the Emporia login path."""
    result = await start(hass, AUTH_METHOD_GOOGLE)
    tokens = dict(TOKENS)
    del tokens[missing]
    aioclient_mock.post("https://auth.emporiaenergy.com/oauth2/token", json=tokens)
    assert (await finish(hass, result))["reason"] == "oauth_error"
    validate.assert_not_called()


async def test_emporia_rejects_login(hass, aioclient_mock, validate):
    """An OAuth success alone is not enough to create a working account."""
    validate.side_effect = InvalidAuth
    result = await start(hass, AUTH_METHOD_GOOGLE)
    aioclient_mock.post(
        "https://auth.emporiaenergy.com/oauth2/token", json=dict(TOKENS)
    )
    assert (await finish(hass, result))["reason"] == "invalid_auth"


@pytest.mark.parametrize("same_account", [True, False])
async def test_browser_reauth(hass, aioclient_mock, validate, same_account):
    """Reauth preserves sensor options and cannot replace a different account."""
    old_data = {
        **OPTIONS,
        AUTH_METHOD: AUTH_METHOD_TOKENS,
        CONF_OAUTH_PROVIDER: "google",
        "enable_1m": False,
        "solar_invert": False,
        "id_token": "old-id",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
    }
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="123" if same_account else "456", data=old_data
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_oauth"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    aioclient_mock.post(
        "https://auth.emporiaenergy.com/oauth2/token", json=dict(TOKENS)
    )
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        completed = await finish(hass, result)
        await hass.async_block_till_done()
    assert completed["reason"] == (
        "reauth_successful" if same_account else "wrong_account"
    )
    assert entry.data["enable_1m"] is False
    assert entry.data["solar_invert"] is False
    assert entry.data["id_token"] == ("id" if same_account else "old-id")


@pytest.mark.parametrize(
    "method,step", [("email_password", "email_password"), ("tokens", "tokens")]
)
async def test_existing_login_choices(hass, method, step):
    """Existing authentication forms remain available."""
    assert (await start(hass, method))["step_id"] == step


@pytest.mark.parametrize("tampered", [False, True])
async def test_native_callback_state(hass, tampered):
    """Only a callback signed by this HA instance can resume its flow."""
    result = await start(hass, AUTH_METHOD_GOOGLE)
    state = URL(result["url"]).query["state"]
    if tampered:
        state = jwt.encode(
            {"flow_id": result["flow_id"], "redirect_uri": MY_AUTH_CALLBACK_PATH},
            "different-instance-secret" * 2,
            algorithm="HS256",
        )
    url = URL("/auth/external/callback").with_query(code="test-code", state=state)
    request = make_mocked_request("GET", str(url), app={KEY_HASS: hass})
    response = await OAuth2AuthorizeCallbackView().get(request)
    assert response.status == (400 if tampered else 200)
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    if tampered:
        assert flow.external_data is None
    else:
        assert flow.external_data["code"] == "test-code"


async def test_duplicate_account(hass, aioclient_mock, validate):
    """A browser login cannot create another entry for an existing account."""
    # Start before the other entry appears, as with two setup windows.
    result = await start(hass, AUTH_METHOD_GOOGLE)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="123", data={})
    entry.add_to_hass(hass)
    aioclient_mock.post(
        "https://auth.emporiaenergy.com/oauth2/token", json=dict(TOKENS)
    )
    assert (await finish(hass, result))["reason"] == "already_configured"
    assert entry.data == {}


@pytest.mark.parametrize("method", ["email_password", "tokens"])
async def test_existing_login_submission(hass, validate, method):
    """Switching the base flow class does not change existing entry formats."""
    result = await start(hass, method)
    data = {**OPTIONS}
    if method == "tokens":
        data.update(
            {key: TOKENS[key] for key in ("id_token", "access_token", "refresh_token")}
        )
    else:
        data.update(email="user@example.com", password="test-password")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][AUTH_METHOD] == method
    assert CONF_OAUTH_PROVIDER not in result["data"]
    await hass.async_block_till_done()
