"""
Unit tests for pure-logic functions in src/app.py.

Run:
    pytest tests/python/ -v
"""

import base64
import json
from pathlib import Path

import pytest

from src.app import (
    _build_provider_logout_url,
    _compute_client_principal,
    _decode_jwt_claims,
    _decode_principal,
    _idp_auth_provider,
    _idp_cfg_prefix,
    _idp_logout_endpoint,
    _idp_user_id_claim,
    _load_config,
    _parse_bool_cfg,
    _parse_skip_routes,
    _provider_logout_bridge_url,
    _safe_redirect,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jwt(claims: dict) -> str:
    """Build a minimal unsigned JWT suitable for testing."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{payload}.fakesig"


def _decode_b64_json(value: str) -> dict:
    return json.loads(base64.b64decode(value).decode())


# ---------------------------------------------------------------------------
# _decode_jwt_claims
# ---------------------------------------------------------------------------

class TestDecodeJwtClaims:
    def test_valid_jwt_returns_claims(self):
        claims = {"sub": "user1", "email": "user1@example.com", "exp": 9999999999}
        result = _decode_jwt_claims(_make_jwt(claims))
        assert result["sub"] == "user1"
        assert result["email"] == "user1@example.com"
        assert result["exp"] == 9999999999

    def test_empty_string_returns_empty(self):
        assert _decode_jwt_claims("") == {}

    @pytest.mark.parametrize("token", ["only.two", "one", "a.b.c.d"])
    def test_wrong_segment_count_returns_empty(self, token):
        assert _decode_jwt_claims(token) == {}

    def test_invalid_base64_returns_empty(self):
        assert _decode_jwt_claims("header.!!!invalid!!!.sig") == {}

    def test_non_json_payload_returns_empty(self):
        payload = base64.urlsafe_b64encode(b"not-json").decode()
        assert _decode_jwt_claims(f"header.{payload}.sig") == {}

    def test_padding_variants_handled(self):
        """Payloads whose length mod 4 != 0 must be padded correctly."""
        assert _decode_jwt_claims(_make_jwt({"k": "v"})) == {"k": "v"}


# ---------------------------------------------------------------------------
# _decode_principal
# ---------------------------------------------------------------------------

class TestDecodePrincipal:
    def test_valid_base64_json_returns_dict(self):
        data = {"auth_typ": "aad", "claims": []}
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        assert _decode_principal(encoded) == data

    def test_empty_string_returns_none(self):
        assert _decode_principal("") is None

    def test_invalid_base64_returns_none(self):
        assert _decode_principal("!!!not-base64!!!") is None

    def test_valid_base64_non_json_returns_none(self):
        assert _decode_principal(base64.b64encode(b"not json").decode()) is None


# ---------------------------------------------------------------------------
# _compute_client_principal
# ---------------------------------------------------------------------------

class TestComputeClientPrincipal:
    def test_with_id_token_uses_jwt_claims(self):
        claims = {"preferred_username": "user@example.com", "name": "Test User", "oid": "abc123"}
        result = _compute_client_principal(
            "user@example.com", "", "aad", "preferred_username", _make_jwt(claims)
        )
        data = _decode_b64_json(result)
        assert data["auth_typ"] == "aad"
        assert data["name_typ"] == "preferred_username"
        assert data["role_typ"] == "roles"
        claim_types = {c["typ"] for c in data["claims"]}
        assert "preferred_username" in claim_types
        assert "name" in claim_types

    def test_without_id_token_uses_fallback_claims(self):
        result = _compute_client_principal("u", "u@x.com", "aad", "preferred_username", "")
        data = _decode_b64_json(result)
        claim_types = {c["typ"] for c in data["claims"]}
        assert "preferred_username" in claim_types
        assert "emails" in claim_types
        assert "name" in claim_types

    def test_empty_user_and_email_returns_empty_string(self):
        assert _compute_client_principal("", "", "aad", "preferred_username", "") == ""

    def test_list_claim_joined_with_comma(self):
        token = _make_jwt({"roles": ["admin", "editor"]})
        result = _compute_client_principal("u", "e@x.com", "aad", "preferred_username", token)
        data = _decode_b64_json(result)
        roles_claim = next(c for c in data["claims"] if c["typ"] == "roles")
        assert roles_claim["val"] == "admin,editor"

    def test_output_has_required_keys(self):
        result = _compute_client_principal("user", "u@x.com", "aad", "preferred_username")
        data = _decode_b64_json(result)
        assert {"auth_typ", "name_typ", "role_typ", "claims"} <= data.keys()
        assert isinstance(data["claims"], list)


# ---------------------------------------------------------------------------
# _safe_redirect
# ---------------------------------------------------------------------------

class TestSafeRedirect:
    def test_relative_path_passes_through(self):
        assert _safe_redirect("/foo/bar") == "/foo/bar"

    def test_root_passes_through(self):
        assert _safe_redirect("/") == "/"

    def test_protocol_relative_url_blocked(self):
        assert _safe_redirect("//evil.com/path") == "/"

    def test_absolute_http_url_blocked(self):
        assert _safe_redirect("http://evil.com") == "/"

    def test_absolute_https_url_blocked(self):
        assert _safe_redirect("https://evil.com/steal") == "/"


# ---------------------------------------------------------------------------
# _parse_skip_routes
# ---------------------------------------------------------------------------

class TestParseSkipRoutes:
    def test_method_and_pattern(self):
        routes = _parse_skip_routes("GET=/api/.*")
        assert len(routes) == 1
        method, pattern = routes[0]
        assert method == "GET"
        assert pattern.search("/api/users") is not None

    def test_pattern_only_gets_wildcard_method(self):
        routes = _parse_skip_routes("/health")
        assert len(routes) == 1
        assert routes[0][0] == "*"
        assert routes[0][1].search("/health") is not None

    def test_multiple_entries(self):
        routes = _parse_skip_routes("GET=/api/.*,/health")
        assert len(routes) == 2
        assert routes[0][0] == "GET"
        assert routes[1][0] == "*"

    def test_empty_string_returns_empty_list(self):
        assert _parse_skip_routes("") == []

    def test_whitespace_only_entries_skipped(self):
        assert _parse_skip_routes("  ,  ") == []

    def test_method_uppercased(self):
        assert _parse_skip_routes("post=/submit")[0][0] == "POST"

    def test_whitespace_around_entry_stripped(self):
        routes = _parse_skip_routes("  GET=/api  ")
        assert routes[0][0] == "GET"


# ---------------------------------------------------------------------------
# _idp_cfg_prefix
# ---------------------------------------------------------------------------

class TestIdpCfgPrefix:
    @pytest.mark.parametrize("idp, expected", [
        ("entra",          "IDP_ENTRA"),
        ("google",         "IDP_GOOGLE"),
        ("my-idp",         "IDP_MY_IDP"),
        ("openid-connect", "IDP_OPENID_CONNECT"),
        ("GITHUB",         "IDP_GITHUB"),
    ])
    def test_prefix(self, idp, expected):
        assert _idp_cfg_prefix(idp) == expected


# ---------------------------------------------------------------------------
# _provider_logout_bridge_url
# ---------------------------------------------------------------------------

class TestProviderLogoutBridgeUrl:
    def test_path_starts_correctly(self):
        url = _provider_logout_bridge_url("entra", "/")
        assert url.startswith("/.auth/provider_logout/entra")

    def test_redirect_param_present(self):
        url = _provider_logout_bridge_url("entra", "/")
        assert "post_logout_redirect_uri" in url

    def test_redirect_uri_is_url_encoded(self):
        url = _provider_logout_bridge_url("entra", "/path?foo=bar")
        # The redirect URI must be URL-encoded; raw '?' must not appear in the query value
        _, qs = url.split("?", 1)
        assert qs.startswith("post_logout_redirect_uri=")
        encoded_value = qs[len("post_logout_redirect_uri="):]
        assert "?" not in encoded_value


# ---------------------------------------------------------------------------
# _load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_missing_file_returns_empty(self):
        assert _load_config(Path("/nonexistent/config.toml")) == {}

    def test_bool_converted_to_string(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("FLAG = true\nFLAG2 = false\n")
        result = _load_config(cfg)
        assert result["FLAG"] == "true"
        assert result["FLAG2"] == "false"

    def test_list_converted_to_comma_string(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('IDP_LIST = ["entra", "google"]\n')
        assert _load_config(cfg)["IDP_LIST"] == "entra,google"

    def test_string_value_preserved(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('SITE_URL = "http://localhost"\n')
        assert _load_config(cfg)["SITE_URL"] == "http://localhost"

    def test_integer_converted_to_string(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("SITE_PORT = 8080\n")
        assert _load_config(cfg)["SITE_PORT"] == "8080"


# ---------------------------------------------------------------------------
# _parse_bool_cfg  (Group B — env-dependent)
# ---------------------------------------------------------------------------

class TestParseBoolCfg:
    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("_TEST_BOOL", value)
        assert _parse_bool_cfg("_TEST_BOOL") is True

    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", ""])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("_TEST_BOOL", value)
        assert _parse_bool_cfg("_TEST_BOOL") is False

    def test_default_false_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("_TEST_BOOL", raising=False)
        assert _parse_bool_cfg("_TEST_BOOL") is False

    def test_custom_default_used_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("_TEST_BOOL_DEF", raising=False)
        assert _parse_bool_cfg("_TEST_BOOL_DEF", "true") is True


# ---------------------------------------------------------------------------
# _idp_auth_provider  (Group B)
# ---------------------------------------------------------------------------

class TestIdpAuthProvider:
    @pytest.fixture(autouse=True)
    def clean_config(self, monkeypatch):
        import src.app as m
        monkeypatch.setattr(m, "_CONFIG", {})

    def test_entra_default_is_aad(self, monkeypatch):
        monkeypatch.delenv("IDP_ENTRA_KIND", raising=False)
        monkeypatch.delenv("IDP_ENTRA_AUTH_PROVIDER", raising=False)
        assert _idp_auth_provider("entra") == "aad"

    def test_google_default_is_google(self, monkeypatch):
        monkeypatch.delenv("IDP_GOOGLE_KIND", raising=False)
        monkeypatch.delenv("IDP_GOOGLE_AUTH_PROVIDER", raising=False)
        assert _idp_auth_provider("google") == "google"

    def test_kind_oidc_maps_to_oidc(self, monkeypatch):
        monkeypatch.setenv("IDP_ENTRA_KIND", "oidc")
        monkeypatch.delenv("IDP_ENTRA_AUTH_PROVIDER", raising=False)
        assert _idp_auth_provider("entra") == "oidc"

    def test_explicit_auth_provider_overrides(self, monkeypatch):
        monkeypatch.setenv("IDP_ENTRA_AUTH_PROVIDER", "custom-provider")
        assert _idp_auth_provider("entra") == "custom-provider"


# ---------------------------------------------------------------------------
# _idp_user_id_claim  (Group B)
# ---------------------------------------------------------------------------

class TestIdpUserIdClaim:
    @pytest.fixture(autouse=True)
    def clean_config(self, monkeypatch):
        import src.app as m
        monkeypatch.setattr(m, "_CONFIG", {})

    @pytest.mark.parametrize("idp, expected", [
        ("entra",    "preferred_username"),
        ("google",   "email"),
        ("apple",    "email"),
        ("facebook", "id"),
        ("github",   "login"),
    ])
    def test_builtin_defaults(self, monkeypatch, idp, expected):
        monkeypatch.delenv(f"IDP_{idp.upper()}_KIND", raising=False)
        monkeypatch.delenv(f"IDP_{idp.upper()}_AUTH_USER_ID_CLAIM", raising=False)
        assert _idp_user_id_claim(idp) == expected

    def test_custom_claim_via_env(self, monkeypatch):
        monkeypatch.setenv("IDP_ENTRA_AUTH_USER_ID_CLAIM", "oid")
        assert _idp_user_id_claim("entra") == "oid"


# ---------------------------------------------------------------------------
# _idp_logout_endpoint  (Group B)
# ---------------------------------------------------------------------------

class TestIdpLogoutEndpoint:
    @pytest.fixture(autouse=True)
    def clean_config(self, monkeypatch):
        import src.app as m
        monkeypatch.setattr(m, "_CONFIG", {})

    def test_explicit_env_var_used(self, monkeypatch):
        monkeypatch.setenv("IDP_ENTRA_LOGOUT_ENDPOINT", "https://custom.example.com/logout")
        assert _idp_logout_endpoint("entra") == "https://custom.example.com/logout"

    def test_microsoft_v2_issuer_auto_derived(self, monkeypatch):
        monkeypatch.delenv("IDP_ENTRA_LOGOUT_ENDPOINT", raising=False)
        monkeypatch.setenv("IDP_ENTRA_KIND", "microsoft")
        monkeypatch.setenv("IDP_ENTRA_OIDC_ISSUER_URL", "https://login.microsoftonline.com/tenant/v2.0")
        result = _idp_logout_endpoint("entra")
        assert result == "https://login.microsoftonline.com/tenant/oauth2/v2.0/logout"

    def test_no_endpoint_when_kind_is_google(self, monkeypatch):
        monkeypatch.delenv("IDP_GOOGLE_LOGOUT_ENDPOINT", raising=False)
        monkeypatch.delenv("IDP_GOOGLE_KIND", raising=False)
        monkeypatch.delenv("IDP_GOOGLE_OIDC_ISSUER_URL", raising=False)
        assert _idp_logout_endpoint("google") == ""

    def test_microsoft_without_v2_suffix_returns_empty(self, monkeypatch):
        monkeypatch.delenv("IDP_ENTRA_LOGOUT_ENDPOINT", raising=False)
        monkeypatch.setenv("IDP_ENTRA_KIND", "microsoft")
        monkeypatch.setenv("IDP_ENTRA_OIDC_ISSUER_URL", "https://login.microsoftonline.com/tenant")
        assert _idp_logout_endpoint("entra") == ""


# ---------------------------------------------------------------------------
# _build_provider_logout_url  (Group B + module globals)
# ---------------------------------------------------------------------------

class TestBuildProviderLogoutUrl:
    @pytest.fixture(autouse=True)
    def clean_state(self, monkeypatch):
        import src.app as m
        monkeypatch.setattr(m, "_CONFIG", {})
        monkeypatch.setattr(m, "SITE_URL", "http://localhost")
        monkeypatch.setattr(m, "SITE_PORT", "8080")

    def test_no_endpoint_returns_empty(self, monkeypatch):
        monkeypatch.delenv("IDP_ENTRA_LOGOUT_ENDPOINT", raising=False)
        monkeypatch.delenv("IDP_ENTRA_KIND", raising=False)
        monkeypatch.delenv("IDP_ENTRA_OIDC_ISSUER_URL", raising=False)
        assert _build_provider_logout_url("entra", "/") == ""

    def test_relative_redirect_made_absolute(self, monkeypatch):
        from urllib.parse import unquote
        monkeypatch.setenv("IDP_ENTRA_LOGOUT_ENDPOINT", "https://logout.example.com/end")
        result = _build_provider_logout_url("entra", "/bye")
        assert "post_logout_redirect_uri" in result
        decoded = unquote(result)
        assert "localhost" in decoded
        assert "/bye" in decoded

    def test_existing_query_params_preserved(self, monkeypatch):
        monkeypatch.setenv("IDP_ENTRA_LOGOUT_ENDPOINT", "https://logout.example.com/end?client_id=abc")
        result = _build_provider_logout_url("entra", "/")
        assert "client_id=abc" in result
        assert "post_logout_redirect_uri" in result

    def test_absolute_redirect_uri_used_as_is(self, monkeypatch):
        monkeypatch.setenv("IDP_ENTRA_LOGOUT_ENDPOINT", "https://logout.example.com/end")
        result = _build_provider_logout_url("entra", "https://myapp.example.com/signed-out")
        assert "myapp.example.com" in result

    def test_relative_redirect_uses_request_host(self, monkeypatch):
        from urllib.parse import unquote
        monkeypatch.setenv("IDP_ENTRA_LOGOUT_ENDPOINT", "https://logout.example.com/end")
        result = _build_provider_logout_url(
            "entra", "/bye", proto="https", host="xxx-8080.usw2.devtunnels.ms"
        )
        decoded = unquote(result)
        assert "https://xxx-8080.usw2.devtunnels.ms/bye" in decoded

    def test_request_host_without_proto_falls_back_to_default(self, monkeypatch):
        from urllib.parse import unquote
        monkeypatch.setenv("IDP_ENTRA_LOGOUT_ENDPOINT", "https://logout.example.com/end")
        result = _build_provider_logout_url("entra", "/bye", host="localhost:8080")
        decoded = unquote(result)
        assert "://localhost:8080/bye" in decoded

    def test_fallback_omits_listen_port_behind_tls_front(self, monkeypatch):
        from urllib.parse import unquote
        import src.app as m
        monkeypatch.setattr(m, "SITE_URL", "https://xxx-8080.usw2.devtunnels.ms")
        monkeypatch.setattr(m, "SITE_PORT", "8080")
        monkeypatch.setattr(m, "_TLS_ENABLED", False)
        monkeypatch.setenv("IDP_ENTRA_LOGOUT_ENDPOINT", "https://logout.example.com/end")
        result = _build_provider_logout_url("entra", "/bye")
        decoded = unquote(result)
        assert "https://xxx-8080.usw2.devtunnels.ms/bye" in decoded
        assert "devtunnels.ms:8080" not in decoded
