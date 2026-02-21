import asyncio
import datetime
import json
import logging
import time
import urllib.parse
from typing import Optional

import anthropic
import httpx
from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from bug_bot.config import settings
from bug_bot.db.session import async_session
from bug_bot.models.models import ServiceTeamMapping
from bug_bot.schemas.logs import LogLine, LogQueryRequest, LogQueryResponse
from bug_bot.service_matcher import _fetch_all_services, match_services

logger = logging.getLogger(__name__)
router = APIRouter()

_TODAY = datetime.date.today().isoformat()

_EXTRACT_PROMPT = f"""\
Extract structured information from the following log query request.

Respond ONLY with a valid JSON object — no markdown, no explanation, no code fences.

Fields to extract:
- service_name: string (the service or app being asked about, or null if unknown)
- request_id: string or null
- mobile_number: string or null
- start_time: ISO8601 string or null (resolve relative times to absolute UTC)
- end_time: ISO8601 string or null (resolve relative times to absolute UTC)
- keywords: array of strings (other filter terms like "error", "timeout"; always include severity words like "error"/"warn")

Today is {_TODAY} (UTC). Resolve "yesterday", "last 2 hours", etc. to absolute ISO8601 UTC timestamps.

Query: """


async def _extract_log_intent(query: str) -> dict:
    """Call Claude Haiku to extract structured intent from a natural language log query."""
    logger.info("[logs] Extracting intent from query: %r", query)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    prompt = _EXTRACT_PROMPT + query

    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    logger.debug("[logs] Raw Claude intent response: %s", raw)

    # Strip any accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(raw)
        logger.info(
            "[logs] Extracted intent — service=%r request_id=%r mobile=%r "
            "start=%r end=%r keywords=%r",
            result.get("service_name"),
            result.get("request_id"),
            result.get("mobile_number"),
            result.get("start_time"),
            result.get("end_time"),
            result.get("keywords"),
        )
        return result
    except json.JSONDecodeError:
        logger.warning("[logs] Failed to parse Claude intent JSON: %s", raw[:200])
        return {
            "service_name": None,
            "request_id": None,
            "mobile_number": None,
            "start_time": None,
            "end_time": None,
            "keywords": [],
            "_parse_error": raw[:200],
        }


async def _fetch_service_repo(service_name: str) -> Optional[str]:
    """Return github_repo for a canonical service_name, or None if not found."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(ServiceTeamMapping.github_repo).where(
                    ServiceTeamMapping.service_name == service_name
                )
            )
            row = result.first()
            return row[0] if row else None
    except Exception:
        return None


async def _resolve_log_service(query: str) -> tuple[Optional[str], Optional[str]]:
    """
    Wrapper around match_services for the log query use case.

    Passes the natural language log query to the service matcher (Claude + DB service list)
    and returns (canonical_service_name, github_repo) for the best match,
    or (None, None) if no service could be identified.
    """
    # Fetch and log the full services list so it's visible what Claude had to choose from
    all_services = await _fetch_all_services()
    if all_services:
        service_names = [s["service_name"] for s in all_services]
        logger.info(
            "[logs] Service matching pool (%d services): %s",
            len(service_names),
            ", ".join(service_names),
        )
    else:
        logger.warning("[logs] No services found in DB — service matching will return empty")

    matched = await match_services(query)
    logger.info("[logs] service_matcher returned: %r", matched)

    if not matched:
        return None, None
    service_name = matched[0]
    github_repo = await _fetch_service_repo(service_name)
    logger.info(
        "[logs] Resolved service=%r  github_repo=%r", service_name, github_repo
    )
    return service_name, github_repo


def _normalize_label(name: str) -> str:
    """Lowercase and replace dots, underscores, spaces with hyphens."""
    return name.lower().replace(".", "-").replace("_", "-").replace(" ", "-")


def _candidate_label_values(service_name: str, github_repo: Optional[str]) -> list[str]:
    """Return deduplicated label value candidates from service name and repo."""
    candidates = [_normalize_label(service_name)]
    if github_repo:
        # e.g. "shopuptech/payment-service" → "payment-service"
        repo_segment = _normalize_label(github_repo.split("/")[-1])
        if repo_segment not in candidates:
            candidates.append(repo_segment)
    return candidates


def _build_logql(label_key: str, label_value: str, filters: list[str]) -> str:
    """Build a LogQL expression with stream selector and line filters."""
    selector = '{' + f'{label_key}="{label_value}"' + '}'
    non_empty = [f for f in filters if f]
    if not non_empty:
        return selector
    filter_clauses = " ".join(f'|= "{f}"' for f in non_empty)
    return f"{selector} {filter_clauses}"


def _grafana_explore_url(query: str, start_ns: int, end_ns: int) -> str:
    """Build a Grafana Explore deep-link for the given LogQL query and time range."""
    grafana_url = (settings.grafana_url or "http://localhost:3000").rstrip("/")
    start_ms = start_ns // 1_000_000
    end_ms = end_ns // 1_000_000
    payload = {
        "datasource": "loki",
        "queries": [{"expr": query, "refId": "A"}],
        "range": {"from": str(start_ms), "to": str(end_ms)},
    }
    encoded = urllib.parse.quote(json.dumps(payload))
    return f"{grafana_url}/explore?left={encoded}"


def _loki_http_query(
    query: str,
    start_ns: int,
    end_ns: int,
    limit: int = 100,
) -> tuple[list[LogLine], int]:
    """Query Loki HTTP API with absolute nanosecond timestamps. Returns (log_lines, total_count)."""
    loki_url = (settings.loki_url or "http://localhost:3100").rstrip("/")
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(min(limit, 500)),
        "direction": "backward",
    }
    with httpx.Client(timeout=30.0) as http:
        resp = http.get(f"{loki_url}/loki/api/v1/query_range", params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"Loki returned HTTP {resp.status_code}: {resp.text[:300]}")

    results = resp.json().get("data", {}).get("result", [])
    log_lines: list[LogLine] = []
    for stream in results:
        labels = stream.get("stream", {})
        for ts_ns_str, line_text in stream.get("values", []):
            ts = datetime.datetime.fromtimestamp(int(ts_ns_str) / 1e9, tz=datetime.timezone.utc)
            log_lines.append(
                LogLine(
                    timestamp=ts.isoformat(),
                    stream_labels=labels,
                    line=line_text,
                )
            )
    return log_lines, len(log_lines)


def _search_with_fallback(
    service_name: str,
    github_repo: Optional[str],
    filters: list[str],
    start_ns: int,
    end_ns: int,
    limit: int = 100,
) -> tuple[list[LogLine], str, str, str]:
    """
    Try label × time combinations until logs are found.
    Returns (log_lines, query_used, strategy_label, grafana_url).
    """
    now_ns = int(time.time() * 1e9)
    candidates = _candidate_label_values(service_name, github_repo)
    norm_service = candidates[0]
    repo_name = candidates[1] if len(candidates) > 1 else None

    DAY_NS = 24 * 60 * 60 * int(1e9)

    attempts: list[tuple[str, str, int, int, str]] = []

    # Rounds 1–3: exact range, three label keys, normalized service
    for key in ("app", "service", "job"):
        attempts.append((key, norm_service, start_ns, end_ns, "exact_range"))

    # Round 4: app + repo-derived name, exact range
    if repo_name and repo_name != norm_service:
        attempts.append(("app", repo_name, start_ns, end_ns, "exact_range"))

    # Round 5–6: expanded 3-day window
    start_3d = now_ns - 3 * DAY_NS
    attempts.append(("app", norm_service, start_3d, now_ns, "expanded_3d"))
    attempts.append(("service", norm_service, start_3d, now_ns, "expanded_3d"))

    # Round 7: expanded 7-day window
    start_7d = now_ns - 7 * DAY_NS
    attempts.append(("app", norm_service, start_7d, now_ns, "expanded_7d"))

    last_query = ""
    last_strategy = "exact_range"

    for idx, (label_key, label_value, t_start, t_end, strategy) in enumerate(attempts, 1):
        logql = _build_logql(label_key, label_value, filters)
        last_query = logql
        last_strategy = strategy
        logger.info(
            "[logs] Loki attempt %d/%d — strategy=%s  logql=%r",
            idx, len(attempts), strategy, logql,
        )
        try:
            lines, total = _loki_http_query(logql, t_start, t_end, limit)
        except Exception as exc:
            logger.warning("[logs] Loki attempt %d failed: %s", idx, exc)
            continue
        logger.info("[logs] Loki attempt %d → %d line(s)", idx, len(lines))
        if lines:
            grafana_url = _grafana_explore_url(logql, t_start, t_end)
            return lines, logql, strategy, grafana_url

    logger.info("[logs] All %d Loki attempts exhausted — no logs found", len(attempts))
    # Nothing found — return empty result with last attempt metadata
    grafana_url = _grafana_explore_url(last_query, start_ns, end_ns)
    return [], last_query, last_strategy, grafana_url


def _parse_iso(ts: Optional[str]) -> Optional[datetime.datetime]:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


@router.post("/query", response_model=LogQueryResponse)
async def query_logs(payload: LogQueryRequest) -> LogQueryResponse:
    """
    Accept a natural language log query, extract intent via Claude, resolve the service,
    query Loki with fallback strategies, and return matching logs + a Grafana Explore URL.
    """
    # Step 1: extract intent
    interpreted = await _extract_log_intent(payload.query)

    service_name: Optional[str] = interpreted.get("service_name")
    if not service_name:
        raise HTTPException(
            status_code=422,
            detail="Could not identify a service name from your query. Please specify the service (e.g. 'payment service', 'billing', 'auth').",
        )

    # Step 2: resolve service via service_matcher (Claude + DB service list)
    logger.info("[logs] Resolving service for query: %r", payload.query)
    matched_service, github_repo = await _resolve_log_service(payload.query)

    if not matched_service:
        logger.warning("[logs] Service resolution failed for extracted name=%r", service_name)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Could not match '{service_name}' to any registered service. "
                "Use GET /api/admin/service-team-mappings to see known services, "
                "or be more specific in your query."
            ),
        )

    effective_service = matched_service

    # Step 3: compute time range
    now_ns = int(time.time() * 1e9)
    DAY_NS = 24 * 60 * 60 * int(1e9)

    start_dt = _parse_iso(interpreted.get("start_time"))
    end_dt = _parse_iso(interpreted.get("end_time"))

    if start_dt:
        start_ns = int(start_dt.timestamp() * 1e9)
    else:
        start_ns = now_ns - DAY_NS  # default: last 24 h

    if end_dt:
        end_ns = int(end_dt.timestamp() * 1e9)
    else:
        end_ns = now_ns

    # Step 4: build filter list
    filters: list[str] = []
    if interpreted.get("request_id"):
        filters.append(interpreted["request_id"])
    if interpreted.get("mobile_number"):
        filters.append(interpreted["mobile_number"])
    filters.extend(interpreted.get("keywords") or [])

    logger.info(
        "[logs] Starting Loki search — service=%r  repo=%r  filters=%r  "
        "start=%s  end=%s",
        effective_service, github_repo, filters,
        datetime.datetime.fromtimestamp(start_ns / 1e9, tz=datetime.timezone.utc).isoformat(),
        datetime.datetime.fromtimestamp(end_ns / 1e9, tz=datetime.timezone.utc).isoformat(),
    )

    # Step 5: search Loki with fallback
    log_lines, query_used, strategy, grafana_url = await asyncio.to_thread(
        _search_with_fallback,
        effective_service,
        github_repo,
        filters,
        start_ns,
        end_ns,
    )

    logger.info(
        "[logs] Search complete — strategy=%r  total_lines=%d  query=%r",
        strategy, len(log_lines), query_used,
    )

    message = (
        f"Found {len(log_lines)} log line(s) using strategy '{strategy}'."
        if log_lines
        else f"No logs found for '{effective_service}' even after widening the search window."
    )

    return LogQueryResponse(
        original_query=payload.query,
        interpreted=interpreted,
        matched_service=matched_service,
        logs=log_lines,
        total_lines=len(log_lines),
        query_used=query_used,
        search_strategy=strategy,
        grafana_url=grafana_url,
        message=message,
    )
