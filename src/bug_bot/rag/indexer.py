import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bug_bot.models.models import BugReport, Investigation, InvestigationFinding, ServiceTeamMapping, Team
from bug_bot.rag.embeddings import embed_texts
from bug_bot.rag.vectorstore import store_embeddings, delete_by_source

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Contextual Retrieval: each builder returns (context_prefix, chunk_body).
# The prefix adds document-level context so the embedding captures richer
# semantics.  We embed ``f"{prefix}\n\n{body}"``, store *body* in
# ``chunk_text`` (for display), and store *prefix* in ``context_prefix``.
# ---------------------------------------------------------------------------


def _build_bug_report_enriched(bug: BugReport) -> tuple[str, str]:
    prefix = (
        f"This is a bug report (ID: {bug.bug_id}) with severity {bug.severity}, "
        f"status {bug.status}. It was reported in the ShopTech bug tracking system."
    )
    body = (
        f"Bug ID: {bug.bug_id}\n"
        f"Severity: {bug.severity}\n"
        f"Status: {bug.status}\n"
        f"Report: {bug.original_message}"
    )
    return prefix, body


def _build_investigation_enriched(inv: Investigation) -> tuple[str, str]:
    services_str = ", ".join(str(s) for s in inv.relevant_services) if inv.relevant_services else "unknown"
    prefix = (
        f"This is an investigation result for bug {inv.bug_id}. "
        f"The fix type is {inv.fix_type} with confidence {inv.confidence}. "
        f"Services involved: {services_str}."
    )
    parts = [
        f"Bug ID: {inv.bug_id}",
        f"Summary: {inv.summary}",
    ]
    if inv.root_cause:
        parts.append(f"Root Cause: {inv.root_cause}")
    parts.append(f"Fix Type: {inv.fix_type}")
    parts.append(f"Confidence: {inv.confidence}")
    if inv.recommended_actions:
        actions = inv.recommended_actions
        if isinstance(actions, list):
            parts.append(f"Recommended Actions: {'; '.join(str(a) for a in actions)}")
    if inv.relevant_services:
        services = inv.relevant_services
        if isinstance(services, list):
            parts.append(f"Services: {', '.join(str(s) for s in services)}")
    if inv.pr_url:
        parts.append(f"PR: {inv.pr_url}")
    body = "\n".join(parts)
    return prefix, body


def _build_finding_enriched(finding: InvestigationFinding) -> tuple[str, str]:
    prefix = (
        f"This is an investigation finding for bug {finding.bug_id}, "
        f"category: {finding.category}, severity: {finding.severity}."
    )
    body = (
        f"Bug ID: {finding.bug_id}\n"
        f"Category: {finding.category}\n"
        f"Severity: {finding.severity}\n"
        f"Finding: {finding.finding}"
    )
    return prefix, body


def _build_service_mapping_enriched(
    mapping: ServiceTeamMapping, team: Team | None = None,
) -> tuple[str, str]:
    prefix = (
        f"This is a service mapping for {mapping.service_name} "
        f"({mapping.tech_stack} stack, repo: {mapping.github_repo})."
    )
    parts = [
        f"Service: {mapping.service_name}",
        f"GitHub Repo: {mapping.github_repo}",
        f"Tech Stack: {mapping.tech_stack}",
    ]
    if mapping.description:
        parts.append(f"Description: {mapping.description}")
    if mapping.service_owner:
        parts.append(f"Service Owner (Slack ID): {mapping.service_owner}")
    if mapping.primary_oncall:
        parts.append(f"Primary On-Call (Slack ID): {mapping.primary_oncall}")
    if mapping.team_slack_group:
        parts.append(f"Team Slack Group: {mapping.team_slack_group}")
    if team and team.oncall_engineer:
        parts.append(f"Current Team On-Call (Slack ID): {team.oncall_engineer}")
    body = "\n".join(parts)
    return prefix, body


# ---------------------------------------------------------------------------
# Single-document indexers
# ---------------------------------------------------------------------------


async def index_bug_report(session: AsyncSession, bug_id: str) -> int:
    stmt = select(BugReport).where(BugReport.bug_id == bug_id)
    result = await session.execute(stmt)
    bug = result.scalar_one_or_none()
    if not bug:
        logger.warning("Bug %s not found for indexing", bug_id)
        return 0

    await delete_by_source(session, "bug_report", bug_id)

    prefix, body = _build_bug_report_enriched(bug)
    embeddings = embed_texts([f"{prefix}\n\n{body}"])

    docs = [{
        "source_type": "bug_report",
        "source_id": bug_id,
        "chunk_text": body,
        "context_prefix": prefix,
        "chunk_metadata": {
            "severity": bug.severity,
            "status": bug.status,
            "created_at": bug.created_at.isoformat() if bug.created_at else None,
        },
        "embedding": embeddings[0],
        "severity": bug.severity,
        "status": bug.status,
        "created_date": bug.created_at.date() if bug.created_at else None,
    }]
    return await store_embeddings(session, docs)


async def index_investigation(session: AsyncSession, bug_id: str) -> int:
    stmt = select(Investigation).where(Investigation.bug_id == bug_id)
    result = await session.execute(stmt)
    inv = result.scalar_one_or_none()
    if not inv:
        logger.warning("Investigation for %s not found for indexing", bug_id)
        return 0

    await delete_by_source(session, "investigation", bug_id)

    prefix, body = _build_investigation_enriched(inv)
    embeddings = embed_texts([f"{prefix}\n\n{body}"])

    first_service = None
    if inv.relevant_services and isinstance(inv.relevant_services, list) and inv.relevant_services:
        first_service = str(inv.relevant_services[0])

    docs = [{
        "source_type": "investigation",
        "source_id": bug_id,
        "chunk_text": body,
        "context_prefix": prefix,
        "chunk_metadata": {
            "fix_type": inv.fix_type,
            "confidence": inv.confidence,
            "services": inv.relevant_services,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        },
        "embedding": embeddings[0],
        "service_name": first_service,
        "created_date": inv.created_at.date() if inv.created_at else None,
    }]
    return await store_embeddings(session, docs)


async def index_finding(session: AsyncSession, finding_id: str) -> int:
    stmt = select(InvestigationFinding).where(
        InvestigationFinding.id == finding_id  # type: ignore[arg-type]
    )
    result = await session.execute(stmt)
    finding = result.scalar_one_or_none()
    if not finding:
        logger.warning("Finding %s not found for indexing", finding_id)
        return 0

    source_id = f"{finding.bug_id}:{finding_id}"
    await delete_by_source(session, "finding", source_id)

    prefix, body = _build_finding_enriched(finding)
    embeddings = embed_texts([f"{prefix}\n\n{body}"])

    docs = [{
        "source_type": "finding",
        "source_id": source_id,
        "chunk_text": body,
        "context_prefix": prefix,
        "chunk_metadata": {
            "bug_id": finding.bug_id,
            "category": finding.category,
            "severity": finding.severity,
        },
        "embedding": embeddings[0],
        "severity": finding.severity,
        "created_date": finding.created_at.date() if hasattr(finding, "created_at") and finding.created_at else None,
    }]
    return await store_embeddings(session, docs)


async def index_service_mapping(session: AsyncSession, service_name: str) -> int:
    stmt = (
        select(ServiceTeamMapping)
        .options(selectinload(ServiceTeamMapping.team))
        .where(ServiceTeamMapping.service_name == service_name)
    )
    result = await session.execute(stmt)
    mapping = result.scalar_one_or_none()
    if not mapping:
        logger.warning("Service mapping %s not found for indexing", service_name)
        return 0

    source_id = f"service:{mapping.service_name}"
    await delete_by_source(session, "service_mapping", source_id)

    prefix, body = _build_service_mapping_enriched(mapping, mapping.team)
    embeddings = embed_texts([f"{prefix}\n\n{body}"])

    docs = [{
        "source_type": "service_mapping",
        "source_id": source_id,
        "chunk_text": body,
        "context_prefix": prefix,
        "chunk_metadata": {
            "service_name": mapping.service_name,
            "tech_stack": mapping.tech_stack,
            "github_repo": mapping.github_repo,
        },
        "embedding": embeddings[0],
        "service_name": mapping.service_name,
    }]
    return await store_embeddings(session, docs)


# ---------------------------------------------------------------------------
# Bulk re-index
# ---------------------------------------------------------------------------


async def reindex_all(session: AsyncSession) -> dict:
    """Re-index all bug reports, investigations, findings, and service mappings."""
    stats = {"bug_reports": 0, "investigations": 0, "findings": 0, "service_mappings": 0}

    # Bug reports
    bugs_q = await session.execute(select(BugReport))
    bugs = list(bugs_q.scalars().all())
    logger.info("Re-indexing %d bug reports", len(bugs))

    if bugs:
        enriched = [_build_bug_report_enriched(b) for b in bugs]
        full_texts = [f"{prefix}\n\n{body}" for prefix, body in enriched]
        embeddings = embed_texts(full_texts)
        docs = []
        for bug, (prefix, body), emb in zip(bugs, enriched, embeddings):
            await delete_by_source(session, "bug_report", bug.bug_id)
            docs.append({
                "source_type": "bug_report",
                "source_id": bug.bug_id,
                "chunk_text": body,
                "context_prefix": prefix,
                "chunk_metadata": {
                    "severity": bug.severity,
                    "status": bug.status,
                    "created_at": bug.created_at.isoformat() if bug.created_at else None,
                },
                "embedding": emb,
                "severity": bug.severity,
                "status": bug.status,
                "created_date": bug.created_at.date() if bug.created_at else None,
            })
        stats["bug_reports"] = await store_embeddings(session, docs)

    # Investigations
    invs_q = await session.execute(select(Investigation))
    invs = list(invs_q.scalars().all())
    logger.info("Re-indexing %d investigations", len(invs))

    if invs:
        enriched = [_build_investigation_enriched(inv) for inv in invs]
        full_texts = [f"{prefix}\n\n{body}" for prefix, body in enriched]
        embeddings = embed_texts(full_texts)
        docs = []
        for inv, (prefix, body), emb in zip(invs, enriched, embeddings):
            await delete_by_source(session, "investigation", inv.bug_id)
            first_service = None
            if inv.relevant_services and isinstance(inv.relevant_services, list) and inv.relevant_services:
                first_service = str(inv.relevant_services[0])
            docs.append({
                "source_type": "investigation",
                "source_id": inv.bug_id,
                "chunk_text": body,
                "context_prefix": prefix,
                "chunk_metadata": {
                    "fix_type": inv.fix_type,
                    "confidence": inv.confidence,
                    "services": inv.relevant_services,
                    "created_at": inv.created_at.isoformat() if inv.created_at else None,
                },
                "embedding": emb,
                "service_name": first_service,
                "created_date": inv.created_at.date() if inv.created_at else None,
            })
        stats["investigations"] = await store_embeddings(session, docs)

    # Findings
    findings_q = await session.execute(select(InvestigationFinding))
    findings = list(findings_q.scalars().all())
    logger.info("Re-indexing %d findings", len(findings))

    if findings:
        enriched = [_build_finding_enriched(f) for f in findings]
        full_texts = [f"{prefix}\n\n{body}" for prefix, body in enriched]
        embeddings = embed_texts(full_texts)
        docs = []
        for finding, (prefix, body), emb in zip(findings, enriched, embeddings):
            source_id = f"{finding.bug_id}:{finding.id}"
            await delete_by_source(session, "finding", source_id)
            docs.append({
                "source_type": "finding",
                "source_id": source_id,
                "chunk_text": body,
                "context_prefix": prefix,
                "chunk_metadata": {
                    "bug_id": finding.bug_id,
                    "category": finding.category,
                    "severity": finding.severity,
                },
                "embedding": emb,
                "severity": finding.severity,
                "created_date": finding.created_at.date() if hasattr(finding, "created_at") and finding.created_at else None,
            })
        stats["findings"] = await store_embeddings(session, docs)

    # Service mappings
    mappings_q = await session.execute(
        select(ServiceTeamMapping).options(selectinload(ServiceTeamMapping.team))
    )
    mappings = list(mappings_q.scalars().all())
    logger.info("Re-indexing %d service mappings", len(mappings))

    if mappings:
        enriched = [_build_service_mapping_enriched(m, m.team) for m in mappings]
        full_texts = [f"{prefix}\n\n{body}" for prefix, body in enriched]
        embeddings = embed_texts(full_texts)
        docs = []
        for mapping, (prefix, body), emb in zip(mappings, enriched, embeddings):
            source_id = f"service:{mapping.service_name}"
            await delete_by_source(session, "service_mapping", source_id)
            docs.append({
                "source_type": "service_mapping",
                "source_id": source_id,
                "chunk_text": body,
                "context_prefix": prefix,
                "chunk_metadata": {
                    "service_name": mapping.service_name,
                    "tech_stack": mapping.tech_stack,
                    "github_repo": mapping.github_repo,
                },
                "embedding": emb,
                "service_name": mapping.service_name,
            })
        stats["service_mappings"] = await store_embeddings(session, docs)

    total = sum(stats.values())
    logger.info("Re-indexing complete: %d total documents", total)
    return {"indexed": stats, "total": total}
