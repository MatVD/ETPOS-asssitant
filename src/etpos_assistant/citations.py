from __future__ import annotations

from urllib.parse import urlencode


def source_lookup_url(
    *,
    official_url: str,
    document_hash: str | None = None,
    version: str | None = None,
    revision_date: str | None = None,
    heading_path: str | None = None,
) -> str:
    params = {"url": official_url}
    if document_hash:
        params["hash"] = document_hash
    if version:
        params["version"] = version
    if revision_date:
        params["revision"] = revision_date
    if heading_path:
        params["path"] = heading_path
    return "/sources/view?" + urlencode(params)


def normalize_citation_link(citation: dict) -> dict:
    result = dict(citation)
    official_url = str(result.get("official_url") or "").strip()
    if not official_url:
        return result
    result["internal_url"] = source_lookup_url(
        official_url=official_url,
        document_hash=str(result.get("document_hash") or "").strip() or None,
        version=str(result.get("version") or "").strip() or None,
        revision_date=str(result.get("revision_date") or "").strip() or None,
        heading_path=str(result.get("heading_path") or "").strip() or None,
    )
    return result
