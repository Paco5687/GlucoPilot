"""Trusted-source retrieval to ground the AI and cut hallucination on specifics.

Free NIH/NLM APIs are the always-available core (no key required):
  - MedlinePlus — plain-language consumer health topics (conditions, symptoms).
  - PubMed — peer-reviewed research abstracts.
An optional web-search layer (Tavily or Brave), restricted to a trusted-domain
allowlist, adds breadth when a key is configured on the Settings page.

Only the distilled medical query is ever sent out — never the user's records.
"""

import asyncio
import html
import logging
import re
from typing import Any

import httpx

from .db import config_value

log = logging.getLogger("glucopilot.research")

# Reputable medical/clinical domains the optional web layer is restricted to.
TRUSTED_DOMAINS = [
    "medlineplus.gov", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "niddk.nih.gov",
    "nia.nih.gov", "nih.gov", "cdc.gov", "fda.gov", "mayoclinic.org", "clevelandclinic.org",
    "hopkinsmedicine.org", "merckmanuals.com", "statpearls.com", "ncbi.nlm.nih.gov/books",
    "testing.com", "labtestsonline.org", "thyroid.org", "endocrine.org", "diabetes.org",
    "rheumatology.org", "aafp.org", "acog.org",
]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_FILLER = {
    "reference", "range", "ranges", "level", "levels", "meaning", "test", "results", "result",
    "normal", "high", "low", "elevated", "value", "values", "what", "is", "the", "of", "a", "an",
    "in", "and", "for", "does", "mean", "vs", "explain", "about", "symptoms", "causes", "treatment",
}


def _clean(text: str, cap: int = 900) -> str:
    # Unescape first: MedlinePlus entity-encodes its highlight <span> tags, so
    # they only become strippable tags after unescaping.
    text = html.unescape(text or "")
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()
    return text[:cap] + ("…" if len(text) > cap else "")


def _qterms(query: str) -> list[str]:
    return [t for t in re.split(r"\W+", query.lower()) if len(t) > 2 and t not in _FILLER]


async def _medlineplus(client: httpx.AsyncClient, query: str, n: int = 2) -> list[dict[str, Any]]:
    r = await client.get(
        "https://wsearch.nlm.nih.gov/ws/query",
        params={"db": "healthTopics", "term": query, "retmax": n},
    )
    if r.status_code >= 400:
        return []
    out: list[dict[str, Any]] = []
    for url, body in re.findall(r'<document[^>]*url="([^"]+)">(.*?)</document>', r.text, re.DOTALL):
        def field(name: str) -> str:
            m = re.search(rf'<content name="{name}">(.*?)</content>', body, re.DOTALL)
            return _clean(m.group(1)) if m else ""
        title, summary = field("title"), field("FullSummary") or field("snippet")
        if title and summary:
            out.append({"title": title, "url": html.unescape(url), "snippet": summary, "source": "MedlinePlus"})
    return out


async def _pubmed(client: httpx.AsyncClient, query: str, n: int = 2) -> list[dict[str, Any]]:
    # Keep it to two eutils calls — no-key E-utilities allows only 3 req/sec.
    common: dict[str, Any] = {"db": "pubmed"}
    api_key = config_value("ncbi_api_key")
    if api_key:
        common["api_key"] = api_key
    r = await client.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={**common, "term": query, "retmax": n, "retmode": "json", "sort": "relevance"},
    )
    ids = (r.json().get("esearchresult", {}) or {}).get("idlist", [])[:n]
    if not ids:
        return []
    xml = (await client.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={**common, "id": ",".join(ids), "retmode": "xml"},
    )).text
    out: list[dict[str, Any]] = []
    for art in re.findall(r"<PubmedArticle>(.*?)</PubmedArticle>", xml, re.DOTALL):
        pmid_m = re.search(r"<PMID[^>]*>(\d+)</PMID>", art)
        title_m = re.search(r"<ArticleTitle>(.*?)</ArticleTitle>", art, re.DOTALL)
        abstracts = re.findall(r"<AbstractText[^>]*>(.*?)</AbstractText>", art, re.DOTALL)
        if not pmid_m:
            continue
        pmid = pmid_m.group(1)
        title = _clean(title_m.group(1), 160) if title_m else "PubMed article"
        snippet = _clean(" ".join(abstracts)) or title
        out.append({"title": title, "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "snippet": snippet, "source": "PubMed"})
    return out


async def _web(client: httpx.AsyncClient, query: str, n: int = 3) -> list[dict[str, Any]]:
    provider = (config_value("web_search_provider") or "").strip().lower()
    key = config_value("web_search_key")
    if not provider or not key:
        return []
    try:
        if provider == "tavily":
            r = await client.post("https://api.tavily.com/search", json={
                "api_key": key, "query": query, "max_results": n,
                "include_domains": TRUSTED_DOMAINS, "search_depth": "basic",
            })
            return [
                {"title": _clean(x.get("title", ""), 160), "url": x.get("url", ""),
                 "snippet": _clean(x.get("content", "")), "source": "Web"}
                for x in (r.json().get("results") or [])
            ]
        if provider == "brave":
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
                params={"q": query, "count": 8},
            )
            results = ((r.json().get("web") or {}).get("results") or [])
            allowed = [
                x for x in results
                if any(d in (x.get("url") or "") for d in TRUSTED_DOMAINS)
            ][:n]
            return [
                {"title": _clean(x.get("title", ""), 160), "url": x.get("url", ""),
                 "snippet": _clean(x.get("description", "")), "source": "Web"}
                for x in allowed
            ]
    except Exception:
        log.warning("web search (%s) failed", provider, exc_info=True)
    return []


def _simplify(query: str) -> str:
    return " ".join(_qterms(query)[:4])


async def _run(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    results = await asyncio.gather(
        _medlineplus(client, query, 2),
        _pubmed(client, query, 2),
        _web(client, query, 3),
        return_exceptions=True,
    )
    out: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, list):
            out.extend(r)
        else:
            log.warning("research backend failed: %r", r)
    return out


async def gather(query: str, max_sources: int = 4) -> list[dict[str, Any]]:
    """Retrieve trusted sources for a distilled medical query. Best-effort: any
    backend that errors or times out is skipped; a simplified retry runs if the
    full query finds nothing."""
    query = (query or "").strip()
    if not query:
        return []
    async with httpx.AsyncClient(timeout=18, headers={"User-Agent": "GlucoPilot/1.0 (research)"}) as client:
        sources = await _run(client, query)
        if not sources:
            simp = _simplify(query)
            if simp and simp != query.lower():
                sources = await _run(client, simp)
    # de-dupe by url, keep order (MedlinePlus first — most readable)
    seen, deduped = set(), []
    for s in sources:
        if s.get("url") and s["url"] not in seen and s.get("snippet"):
            seen.add(s["url"])
            deduped.append(s)
    return deduped[:max_sources]
