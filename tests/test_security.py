from starlette.requests import Request

from etpos_assistant.config import settings
from etpos_assistant.markdown import render_safe_markdown
from etpos_assistant.security import client_ip, hash_password, same_origin_request, verify_password


def test_password_hash_is_argon2id():
    hashed = hash_password("un-mot-de-passe-long")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "un-mot-de-passe-long")
    assert not verify_password(hashed, "mauvais")


def test_markdown_does_not_allow_raw_html_or_links():
    rendered = render_safe_markdown('<script>alert(1)</script>\n\n[piège](javascript:alert(1))')
    assert "<script" not in rendered
    assert "href=" not in rendered
    assert "<a" not in rendered


def _request(*, method="POST", client_host="127.0.0.1", headers=None):
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/login",
            "raw_path": b"/login",
            "query_string": b"",
            "headers": raw_headers,
            "client": (client_host, 12345),
            "server": ("127.0.0.1", 8787),
        }
    )


def test_client_ip_trusts_x_real_ip_only_from_loopback_proxy():
    proxied = _request(
        client_host="127.0.0.1",
        headers={"x-real-ip": "203.0.113.10", "x-forwarded-for": "198.51.100.7"},
    )
    direct = _request(
        client_host="198.51.100.20",
        headers={"x-real-ip": "203.0.113.10", "x-forwarded-for": "198.51.100.7"},
    )
    assert client_ip(proxied) == "203.0.113.10"
    assert client_ip(direct) == "198.51.100.20"


def test_same_origin_request_uses_explicit_public_origin_in_production():
    previous_env = settings.env
    previous_origin = settings.public_origin
    try:
        object.__setattr__(settings, "env", "production")
        object.__setattr__(settings, "public_origin", "https://agent.matblock.com")

        valid = _request(
            headers={"origin": "https://agent.matblock.com", "host": "agent.matblock.com"},
        )
        invalid = _request(
            headers={"origin": "https://evil.example", "host": "agent.matblock.com"},
        )
        missing = _request(headers={"host": "agent.matblock.com"})

        assert same_origin_request(valid)
        assert not same_origin_request(invalid)
        assert not same_origin_request(missing)
    finally:
        object.__setattr__(settings, "env", previous_env)
        object.__setattr__(settings, "public_origin", previous_origin)
