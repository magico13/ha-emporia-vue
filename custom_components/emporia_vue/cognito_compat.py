"""Compatibility for pycognito's text/bytes handling of OIDC access-token hashes."""

import base64
import hashlib
import hmac
from importlib.metadata import version
from typing import Any

import jwt
import pyemvue.auth
from pycognito import Cognito
from pycognito.exceptions import TokenVerificationException


class EmporiaCognito(Cognito):
    """Verify Cognito JWTs, including the at_hash in federated ID tokens."""

    def verify_token(self, token: str, id_name: str, token_use: str) -> dict[str, Any]:
        """Validate signed claims before updating the client's token state."""
        try:
            key = jwt.PyJWK(self.get_key(jwt.get_unverified_header(token)["kid"]))
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.user_pool_url,
                audience=self.client_id if token_use == "id" else None,
                options={
                    "require": ["iss", "exp", "token_use"]
                    + (["aud"] if token_use == "id" else ["client_id"]),
                    "verify_iat": False,
                },
            )
            if claims["token_use"] != token_use:
                raise ValueError("Unexpected token use")
            if token_use == "access" and claims["client_id"] != self.client_id:
                raise ValueError("Unexpected access-token client")
            if "iat" in claims:
                int(claims["iat"])
            if "at_hash" in claims:
                # Cognito signs with RS256, so OIDC at_hash uses half of SHA-256.
                digest = hashlib.sha256(self.access_token.encode("ascii")).digest()
                expected = base64.urlsafe_b64encode(digest[:16]).rstrip(b"=")
                if not hmac.compare_digest(expected, claims["at_hash"].encode("ascii")):
                    raise ValueError("Access-token hash mismatch")
        except (
            jwt.PyJWTError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
        ) as err:
            raise TokenVerificationException(
                "Cognito token verification failed"
            ) from err

        setattr(self, id_name, token)
        setattr(self, f"{token_use}_claims", claims)
        return claims


def apply_cognito_compat() -> None:
    """Replace only PyEmVue's client binding for the affected library release."""
    if version("pycognito") == "2024.5.1" and pyemvue.auth.Cognito is Cognito:
        pyemvue.auth.Cognito = EmporiaCognito
