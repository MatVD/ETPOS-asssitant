from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from ..config import settings

ALLOWED_HOSTS = {
    "etcloud.pt",
    "www.etcloud.pt",
    "etpos.pt",
    "www.etpos.pt",
    "etpos.fr",
    "www.etpos.fr",
}


def validate_source_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.port not in (None, 443)
    ):
        raise ValueError(f"Source non autorisée : {url}")


async def _download_text(url: str, *, expected_content_types: tuple[str, ...]) -> tuple[str, str]:
    validate_source_url(url)
    headers = {"User-Agent": "ETPOS-Assistant/0.1 (+document-ingestion)"}
    current_url = url
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=False, headers=headers) as client:
        for _redirect in range(6):
            response = await client.get(current_url)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError(f"Redirection sans destination pour {current_url}")
                next_url = urljoin(current_url, location)
                validate_source_url(next_url)
                current_url = next_url
                continue

            response.raise_for_status()
            validate_source_url(str(response.url))
            content_type = response.headers.get("content-type", "").lower()
            if not any(expected in content_type for expected in expected_content_types):
                raise ValueError(f"Type de contenu inattendu pour {current_url}: {content_type}")
            text = response.text
            break
        else:
            raise ValueError(f"Trop de redirections pour {url}")

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, digest


async def download_html(url: str) -> tuple[str, str]:
    return await _download_text(url, expected_content_types=("html",))


async def download_javascript(url: str) -> tuple[str, str]:
    return await _download_text(
        url,
        expected_content_types=("javascript", "ecmascript", "text/plain"),
    )


def save_snapshot(source_key: str, text: str, digest: str, *, suffix: str = ".html") -> str:
    if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
        raise ValueError(f"Suffixe de snapshot invalide : {suffix}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = settings.snapshot_dir / f"{source_key}-{stamp}-{digest[:12]}{suffix}"
    path.write_text(text, encoding="utf-8")
    return str(path)


async def fetch_html(source_key: str, url: str) -> tuple[str, str, str]:
    html, digest = await download_html(url)
    return html, digest, save_snapshot(source_key, html, digest)
