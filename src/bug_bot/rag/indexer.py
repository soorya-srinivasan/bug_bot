import logging

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.models.models import BugReport, Investigation, InvestigationFinding
from bug_bot.rag.embeddings import embed_texts
from bug_bot.rag.vectorstore import store_embeddings, delete_by_source

logger = logging.getLogger(__name__)


def _build_bug_report_chunk(bug: BugReport) -> str:
    """Build a single text chunk from a bug report."""
    parts = [
        f"Bug ID: {bug.bug_id}",
        f"Severity: {bug.severity}",
        f"Status: {bug.status}",
        f"Report: {bug.original_message}",
    ]
    return "\n".join(parts)


def _build_investigation_chunk(inv: Investigation) -> str:
    """Build a text chunk from an investigation result."""
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
    return "\n".join(parts)


def _build_finding_chunk(finding: InvestigationFinding) -> str:
    """Build a text chunk from an investigation finding."""
    return (
        f"Bug ID: {finding.bug_id}\n"
        f"Category: {finding.category}\n"
        f"Severity: {finding.severity}\n"
        f"Finding: {finding.finding}"
    )


async def index_bug_report(session: AsyncSession, bug_id: str) -> int:
    """Index (or re-index) a single bug report."""
    stmt = select(BugReport).where(BugReport.bug_id == bug_id)
    result = await session.execute(stmt)
    bug = result.scalar_one_or_none()
    if not bug:
        logger.warning("Bug %s not found for indexing", bug_id)
        return 0

    await delete_by_source(session, "bug_report", bug_id)

    chunk_text = _build_bug_report_chunk(bug)
    embeddings = embed_texts([chunk_text])

    docs = [{
        "source_type": "bug_report",
        "source_id": bug_id,
        "chunk_text": chunk_text,
        "chunk_metadata": {
            "severity": bug.severity,
            "status": bug.status,
            "created_at": bug.created_at.isoformat() if bug.created_at else None,
        },
        "embedding": embeddings[0],
    }]
    return await store_embeddings(session, docs)


async def index_investigation(session: AsyncSession, bug_id: str) -> int:
    """Index (or re-index) an investigation result."""
    stmt = select(Investigation).where(Investigation.bug_id == bug_id)
    result = await session.execute(stmt)
    inv = result.scalar_one_or_none()
    if not inv:
        logger.warning("Investigation for %s not found for indexing", bug_id)
        return 0

    await delete_by_source(session, "investigation", bug_id)

    chunk_text = _build_investigation_chunk(inv)
    embeddings = embed_texts([chunk_text])

    docs = [{
        "source_type": "investigation",
        "source_id": bug_id,
        "chunk_text": chunk_text,
        "chunk_metadata": {
            "fix_type": inv.fix_type,
            "confidence": inv.confidence,
            "services": inv.relevant_services,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        },
        "embedding": embeddings[0],
    }]
    return await store_embeddings(session, docs)


async def index_finding(session: AsyncSession, finding_id: str) -> int:
    """Index a single investigation finding."""
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

    chunk_text = _build_finding_chunk(finding)
    embeddings = embed_texts([chunk_text])

    docs = [{
        "source_type": "finding",
        "source_id": source_id,
        "chunk_text": chunk_text,
        "chunk_metadata": {
            "bug_id": finding.bug_id,
            "category": finding.category,
            "severity": finding.severity,
        },
        "embedding": embeddings[0],
    }]
    return await store_embeddings(session, docs)


async def reindex_all(session: AsyncSession) -> dict:
    """Re-index all bug reports, investigations, and findings."""
    stats = {"bug_reports": 0, "investigations": 0, "findings": 0}

    bugs_q = await session.execute(select(BugReport))
    bugs = list(bugs_q.scalars().all())
    logger.info("Re-indexing %d bug reports", len(bugs))

    if bugs:
        chunks = [_build_bug_report_chunk(b) for b in bugs]
        embeddings = embed_texts(chunks)
        docs = []
        for bug, emb in zip(bugs, embeddings):
            await delete_by_source(session, "bug_report", bug.bug_id)
            docs.append({
                "source_type": "bug_report",
                "source_id": bug.bug_id,
                "chunk_text": _build_bug_report_chunk(bug),
                "chunk_metadata": {
                    "severity": bug.severity,
                    "status": bug.status,
                    "created_at": bug.created_at.isoformat() if bug.created_at else None,
                },
                "embedding": emb,
            })
        stats["bug_reports"] = await store_embeddings(session, docs)

    invs_q = await session.execute(select(Investigation))
    invs = list(invs_q.scalars().all())
    logger.info("Re-indexing %d investigations", len(invs))

    if invs:
        chunks = [_build_investigation_chunk(inv) for inv in invs]
        embeddings = embed_texts(chunks)
        docs = []
        for inv, emb in zip(invs, embeddings):
            await delete_by_source(session, "investigation", inv.bug_id)
            docs.append({
                "source_type": "investigation",
                "source_id": inv.bug_id,
                "chunk_text": _build_investigation_chunk(inv),
                "chunk_metadata": {
                    "fix_type": inv.fix_type,
                    "confidence": inv.confidence,
                    "services": inv.relevant_services,
                    "created_at": inv.created_at.isoformat() if inv.created_at else None,
                },
                "embedding": emb,
            })
        stats["investigations"] = await store_embeddings(session, docs)

    findings_q = await session.execute(select(InvestigationFinding))
    findings = list(findings_q.scalars().all())
    logger.info("Re-indexing %d findings", len(findings))

    if findings:
        chunks = [_build_finding_chunk(f) for f in findings]
        embeddings = embed_texts(chunks)
        docs = []
        for finding, emb in zip(findings, embeddings):
            source_id = f"{finding.bug_id}:{finding.id}"
            await delete_by_source(session, "finding", source_id)
            docs.append({
                "source_type": "finding",
                "source_id": source_id,
                "chunk_text": _build_finding_chunk(finding),
                "chunk_metadata": {
                    "bug_id": finding.bug_id,
                    "category": finding.category,
                    "severity": finding.severity,
                },
                "embedding": emb,
            })
        stats["findings"] = await store_embeddings(session, docs)

    total = sum(stats.values())
    logger.info("Re-indexing complete: %d total documents", total)
    return {"indexed": stats, "total": total}
