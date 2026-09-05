"""Regression tests using signed JWTs and the real Cognito client."""

import base64
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pyemvue.auth
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pycognito import Cognito
from pycognito.exceptions import TokenVerificationException
from pyemvue import PyEmVue
from pyemvue.auth import CLIENT_ID, USER_POOL, USER_POOL_URL

from custom_components.emporia_vue import cognito_compat


@pytest.fixture
def signed_tokens():
    """Create tokens signed by a test Cognito pool key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "test-key"

    def sign(claims):
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": jwk["kid"]})

    common = {
        "iss": USER_POOL_URL,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    access_claims = {**common, "token_use": "access", "client_id": CLIENT_ID}
    access_token = sign(access_claims)
    access_hash = (
        base64.urlsafe_b64encode(
            hashlib.sha256(access_token.encode("ascii")).digest()[:16]
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    id_claims = {**common, "token_use": "id", "aud": CLIENT_ID, "at_hash": access_hash}
    return SimpleNamespace(
        jwk=jwk,
        sign=sign,
        access_claims=access_claims,
        id_claims=id_claims,
        access_token=access_token,
        id_token=sign(id_claims),
    )


def make_client(client_type, tokens):
    """Use a local signing key and prevent AWS client creation."""
    client = client_type(
        USER_POOL,
        CLIENT_ID,
        access_token=tokens.access_token,
        id_token=tokens.id_token,
        refresh_token="refresh",
        session=Mock(),
    )
    client.pool_jwk = {"keys": [tokens.jwk]}
    return client


def test_upstream_reproduces_hash_type_error(signed_tokens):
    """A valid federated ID token triggers the affected upstream bytes bug."""
    client = make_client(Cognito, signed_tokens)
    with pytest.raises(TypeError):
        client.verify_tokens()


def test_federated_tokens_verify(signed_tokens):
    """Valid signatures and at_hash pass without modifying token contents."""
    client = make_client(cognito_compat.EmporiaCognito, signed_tokens)
    client.verify_tokens()
    assert client.id_token == signed_tokens.id_token
    assert client.id_claims == signed_tokens.id_claims
    assert client.access_claims == signed_tokens.access_claims


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("aud", "different-client"),
        ("iss", "https://untrusted.example"),
        ("exp", 1),
        ("token_use", "access"),
        ("at_hash", "wrong"),
        ("at_hash", 12),
        ("at_hash", None),
        ("iat", "not-an-integer"),
        ("iat", None),
        ("nbf", 9999999999),
    ],
)
def test_invalid_signed_claim_rejected(signed_tokens, claim, value):
    """A valid signature never excuses invalid identity or hash claims."""
    client = make_client(cognito_compat.EmporiaCognito, signed_tokens)
    token = signed_tokens.sign({**signed_tokens.id_claims, claim: value})
    with pytest.raises(TokenVerificationException):
        client.verify_token(token, "id_token", "id")
    assert client.id_token == signed_tokens.id_token
    assert client.id_claims is None


@pytest.mark.parametrize("claim", ["iss", "exp", "aud", "token_use"])
def test_required_claim_rejected(signed_tokens, claim):
    """Missing required claims cannot be accepted."""
    claims = dict(signed_tokens.id_claims)
    del claims[claim]
    client = make_client(cognito_compat.EmporiaCognito, signed_tokens)
    with pytest.raises(TokenVerificationException):
        client.verify_token(signed_tokens.sign(claims), "id_token", "id")


def test_wrong_access_client_rejected(signed_tokens):
    """Cognito access tokens identify the app using client_id, not aud."""
    client = make_client(cognito_compat.EmporiaCognito, signed_tokens)
    token = signed_tokens.sign({**signed_tokens.access_claims, "client_id": "other"})
    with pytest.raises(TokenVerificationException):
        client.verify_token(token, "access_token", "access")


@pytest.mark.parametrize("kind", ["signature", "algorithm", "unknown_key", "malformed"])
def test_untrusted_tokens_rejected(signed_tokens, kind):
    """Bad signatures, keys, algorithms, and malformed tokens fail closed."""
    client = make_client(cognito_compat.EmporiaCognito, signed_tokens)
    if kind == "signature":
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(
            signed_tokens.id_claims,
            other_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
    elif kind == "algorithm":
        token = jwt.encode(
            signed_tokens.id_claims,
            "a" * 32,
            algorithm="HS256",
            headers={"kid": "test-key"},
        )
    elif kind == "unknown_key":
        client.pool_jwk = {"keys": []}
        token = signed_tokens.id_token
    else:
        token = "invalid"
    with pytest.raises(TokenVerificationException):
        client.verify_token(token, "id_token", "id")


def test_password_token_without_hash(signed_tokens):
    """The non-federated token format remains supported."""
    claims = dict(signed_tokens.id_claims)
    del claims["at_hash"]
    client = make_client(cognito_compat.EmporiaCognito, signed_tokens)
    assert client.verify_token(signed_tokens.sign(claims), "id_token", "id") == claims


def test_patch_is_scoped_and_version_guarded(monkeypatch):
    """Other Cognito consumers and future library versions remain untouched."""
    monkeypatch.setattr(pyemvue.auth, "Cognito", Cognito)
    monkeypatch.setattr(cognito_compat, "version", lambda _: "2025.1.0")
    cognito_compat.apply_cognito_compat()
    assert pyemvue.auth.Cognito is Cognito
    monkeypatch.setattr(cognito_compat, "version", lambda _: "2024.5.1")
    cognito_compat.apply_cognito_compat()
    cognito_compat.apply_cognito_compat()
    assert pyemvue.auth.Cognito is cognito_compat.EmporiaCognito
    import pycognito

    assert pycognito.Cognito is Cognito


def test_pyemvue_login_and_refresh(signed_tokens, monkeypatch):
    """Real PyEmVue login and later refresh both use the corrected verifier."""
    aws = Mock()
    aws.exceptions.NotAuthorizedException = type(
        "NotAuthorizedException", (Exception,), {}
    )
    aws.initiate_auth.return_value = {
        "AuthenticationResult": {
            "AccessToken": signed_tokens.access_token,
            "IdToken": signed_tokens.id_token,
            "TokenType": "Bearer",
        }
    }
    aws.get_user.return_value = {
        "Username": "test-user",
        "UserAttributes": [{"Name": "email", "Value": "user@example.com"}],
    }
    monkeypatch.setattr("pycognito.boto3.client", lambda *args, **kwargs: aws)
    monkeypatch.setattr(Cognito, "get_keys", lambda self: {"keys": [signed_tokens.jwk]})
    customer = SimpleNamespace(email="user@example.com", customer_gid=123)
    monkeypatch.setattr(PyEmVue, "get_customer_details", lambda self: customer)
    vue = PyEmVue()
    assert vue.login(
        id_token=signed_tokens.id_token,
        access_token=signed_tokens.access_token,
        refresh_token="refresh",
    )
    assert isinstance(vue.auth.cognito, cognito_compat.EmporiaCognito)
    aws.get_user.assert_called_once_with(AccessToken=signed_tokens.access_token)
    updater = Mock()
    vue.auth.token_updater = updater
    refreshed = vue.auth.refresh_tokens()
    assert refreshed["refresh_token"] == "refresh"
    updater.assert_called_once_with(refreshed)
