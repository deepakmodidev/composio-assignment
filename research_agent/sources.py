from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
import tldextract
from bs4 import BeautifulSoup

from research_agent.models import AppInput, DiscoveredSource, FetchedPage


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36 ComposioResearch/1.0"
)
COMMUNITY_REPOSITORY_HOSTS = {"github.com", "gitlab.com", "codeberg.org", "bitbucket.org"}
SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(?:ghp|github_pat|xox[ab])-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{24,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?:OPENAI|COMPOSIO)_API_KEY\s*=\s*['\"]?[^'\"\s]+"),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_url(url: str) -> str:
    return url.rstrip("/")


def redact_sensitive_text(text: str) -> str:
    for pattern in SENSITIVE_PATTERNS:
        text = pattern.sub("[REDACTED_TOKEN]", text)
    return text


def hint_domains(website_hint: str) -> list[str]:
    matches = re.findall(
        r"(?<![\w.-])(?:[a-z0-9-]+\.)+[a-z]{2,}(?![\w.-])",
        website_hint.lower(),
    )
    return list(dict.fromkeys(match.strip(".") for match in matches))


def registrable_domain(host: str) -> str:
    parsed = tldextract.extract(host)
    if not parsed.domain or not parsed.suffix:
        return host.lower()
    return f"{parsed.domain}.{parsed.suffix}".lower()


def source_is_allowed(app: AppInput, source: DiscoveredSource) -> tuple[bool, bool]:
    host = (urlparse(str(source.url)).hostname or "").lower()
    if source.source_type == "community_repository":
        return host in COMMUNITY_REPOSITORY_HOSTS, False

    allowed_hosts = hint_domains(app.website_hint)
    if source.source_type == "official_repository" and host in COMMUNITY_REPOSITORY_HOSTS:
        path_parts = [part for part in urlparse(str(source.url)).path.split("/") if part]
        owner = re.sub(r"[^a-z0-9]", "", path_parts[0].lower()) if path_parts else ""
        app_key = re.sub(r"[^a-z0-9]", "", app.name.lower().split("(", 1)[0])
        domain_keys = {
            re.sub(r"[^a-z0-9]", "", registrable_domain(domain).split(".", 1)[0])
            for domain in allowed_hosts
        }
        organization_matches = any(
            key and (owner.startswith(key) or key.startswith(owner))
            for key in {app_key, *domain_keys}
        )
        return organization_matches, organization_matches

    allowed = any(
        host == allowed_host
        or host.endswith(f".{allowed_host}")
        or registrable_domain(host) == registrable_domain(allowed_host)
        for allowed_host in allowed_hosts
    )
    return allowed, allowed


def page_text(content: str, content_type: str) -> tuple[str, str]:
    if "html" not in content_type.lower():
        text = re.sub(r"\s+", " ", content).strip()
        return "", redact_sensitive_text(text[:30_000])

    soup = BeautifulSoup(content, "html.parser")
    for element in soup(["script", "style", "noscript", "svg"]):
        element.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    return title, redact_sensitive_text(text[:30_000])


async def fetch_with_http(
    client: httpx.AsyncClient,
    source: DiscoveredSource,
    official_source: bool,
) -> FetchedPage | None:
    try:
        response = await client.get(str(source.url), follow_redirects=True)
    except httpx.HTTPError:
        return None

    if response.status_code != 200:
        return None
    title, text = page_text(response.text, response.headers.get("content-type", ""))
    if len(text) < 350:
        return None
    return FetchedPage(
        requested_url=source.url,
        final_url=str(response.url),
        page_title=title or urlparse(str(response.url)).hostname or "Official source",
        text=text,
        fetched_at=utc_now(),
        http_status=response.status_code,
        method="http",
        source_type=source.source_type,
        official_source=official_source,
    )


async def fetch_with_browser(
    source: DiscoveredSource,
    official_source: bool,
) -> FetchedPage | None:
    results = await fetch_many_with_browser([(source, official_source)])
    return results[0] if results else None


async def fetch_many_with_browser(
    sources: list[tuple[DiscoveredSource, bool]],
) -> list[FetchedPage | None]:
    if not sources:
        return []
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            semaphore = asyncio.Semaphore(3)

            async def fetch_one(
                source: DiscoveredSource, official_source: bool
            ) -> FetchedPage | None:
                async with semaphore:
                    page = await browser.new_page(user_agent=USER_AGENT)
                    try:
                        response = await page.goto(
                            str(source.url),
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                        title = await page.title()
                        text = re.sub(
                            r"\s+", " ", await page.locator("body").inner_text()
                        ).strip()
                        final_url = page.url
                        status = response.status if response else 200
                    except Exception:
                        return None
                    finally:
                        await page.close()
                if status != 200 or len(text) < 350:
                    return None
                return FetchedPage(
                    requested_url=str(source.url),
                    final_url=final_url,
                    page_title=title
                    or urlparse(final_url).hostname
                    or "Official source",
                    text=redact_sensitive_text(text[:30_000]),
                    fetched_at=utc_now(),
                    http_status=status,
                    method="browser",
                    source_type=source.source_type,
                    official_source=official_source,
                )

            results = await asyncio.gather(
                *(fetch_one(source, official) for source, official in sources)
            )
            await browser.close()
    except Exception:
        return [None] * len(sources)
    return results


async def fetch_sources(
    app: AppInput,
    sources: list[DiscoveredSource],
    *,
    browser_fallback: bool = True,
) -> tuple[list[FetchedPage], list[str]]:
    pages: list[FetchedPage] = []
    rejected: list[str] = []
    seen: set[str] = set()
    accepted: list[tuple[DiscoveredSource, bool]] = []
    for source in sources[:14]:
        key = canonical_url(str(source.url))
        if key in seen:
            continue
        seen.add(key)
        allowed, official = source_is_allowed(app, source)
        if not allowed:
            rejected.append(f"Rejected non-first-party source: {source.url}")
            continue
        accepted.append((source, official))

    limits = httpx.Limits(max_connections=12, max_keepalive_connections=6)
    timeout = httpx.Timeout(25.0, connect=12.0)
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=timeout, limits=limits
    ) as client:
        http_pages = await asyncio.gather(
            *(fetch_with_http(client, source, official) for source, official in accepted)
        )

    browser_inputs = [
        pair for pair, page in zip(accepted, http_pages, strict=True) if page is None
    ]
    browser_pages: list[FetchedPage | None] = []
    if browser_fallback:
        browser_pages = await fetch_many_with_browser(browser_inputs)
    browser_iterator = iter(browser_pages)

    for (source, _official), http_page in zip(accepted, http_pages, strict=True):
        page = http_page
        if page is None and browser_fallback:
            page = next(browser_iterator)
        if page is None:
            rejected.append(f"Could not fetch source: {source.url}")
            continue
        pages.append(page)
    return pages, rejected


def passage_is_on_page(passage: str, page_text_value: str) -> bool:
    normalized_passage = re.sub(r"\W+", " ", passage).lower().strip()
    normalized_page = re.sub(r"\W+", " ", page_text_value).lower()
    return len(normalized_passage) >= 20 and normalized_passage in normalized_page
