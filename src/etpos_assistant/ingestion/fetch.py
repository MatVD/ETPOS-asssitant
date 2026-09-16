from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..config import settings

ALLOWED_HOSTS = {"etcloud.pt", "www.etcloud.pt", "etpos.pt", "www.etpos.pt"}


def validate_source_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"Source non autorisée : {url}")


async def fetch_html(source_key: str, url: str) -> tuple[str, str, str]:
    validate_source_url(url)
    headers = {"User-Agent": "ETPOS-Assistant/0.1 (+document-ingestion)"}
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            raise ValueError(f"Type de contenu inattendu pour {url}: {content_type}")
        html = response.text
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = settings.snapshot_dir / f"{source_key}-{stamp}-{digest[:12]}.html"
    path.write_text(html, encoding="utf-8")
    return html, digest, str(path)
