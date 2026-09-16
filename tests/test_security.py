from etpos_assistant.markdown import render_safe_markdown
from etpos_assistant.security import hash_password, verify_password


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
