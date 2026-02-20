import asyncio
import os
from typing import Any, Optional

import httpx

try:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None  # type: ignore

from claude_agent_sdk import tool, create_sdk_mcp_server

from bug_bot.config import settings


def _postgres_conninfo() -> Optional[str]:
    """Return psycopg-compatible connection string for the read-only external DB."""
    url = (settings.postgres_readonly_url or "").strip()
    if not url:
        return None
    return url.replace("postgresql+asyncpg", "postgresql", 1)


def _bugbot_conninfo() -> str:
    """Return psycopg-compatible connection string for the main Bug Bot database."""
    return settings.database_url.replace("postgresql+asyncpg", "postgresql", 1)


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _postgres_show_tables_sync(schema: str) -> str:
    if psycopg is None:
        return "Error: psycopg not installed."
    conninfo = _postgres_conninfo()
    if not conninfo:
        return "Error: postgres_readonly_url is not configured."
    try:
        with psycopg.connect(conninfo, row_factory=dict_row) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name;",
                    (schema,),
                )
                rows = cur.fetchall()
                if not rows:
                    return f"No tables found in schema '{schema}'."
                return "\n".join(r["table_name"] for r in rows)
    except Exception as e:
        return f"Error: {e}"


def _postgres_describe_table_sync(table: str, schema: str) -> str:
    if psycopg is None:
        return "Error: psycopg not installed."
    conninfo = _postgres_conninfo()
    if not conninfo:
        return "Error: postgres_readonly_url is not configured."
    try:
        with psycopg.connect(conninfo, row_factory=dict_row) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name, data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position;
                    """,
                    (schema, table),
                )
                rows = cur.fetchall()
                if not rows:
                    return f"Table '{table}' not found in schema '{schema}'."
                lines = [f"{r['column_name']}\t{r['data_type']}\t{r['is_nullable']}" for r in rows]
                return "column_name\tdata_type\tis_nullable\n" + "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _postgres_run_query_sync(query: str) -> str:
    if psycopg is None:
        return "Error: psycopg not installed."
    conninfo = _postgres_conninfo()
    if not conninfo:
        return "Error: postgres_readonly_url is not configured."
    try:
        with psycopg.connect(conninfo, row_factory=dict_row) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(query)
                if cur.description is None:
                    return cur.statusmessage or "Query executed successfully with no output."
                columns = [d[0] for d in cur.description]
                rows = cur.fetchall()
                if not rows:
                    return f"Query returned no results.\nColumns: {', '.join(columns)}"
                header = ",".join(columns)
                data_rows = [",".join(str(v) for v in row.values()) for row in rows]
                return f"{header}\n" + "\n".join(data_rows)
    except Exception as e:
        return f"Error: {e}"


def _postgres_summarize_table_sync(table: str, schema: str) -> str:
    if psycopg is None:
        return "Error: psycopg not installed."
    conninfo = _postgres_conninfo()
    if not conninfo:
        return "Error: postgres_readonly_url is not configured."
    try:
        with psycopg.connect(conninfo, row_factory=dict_row) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position;",
                    (schema, table),
                )
                columns = cur.fetchall()
                if not columns:
                    return f"Table '{table}' not found in schema '{schema}'."
                table_id = sql.Identifier(schema, table)
                parts = [f"Summary for table: {table}\n"]
                for col in columns:
                    cname, dtype = col["column_name"], col["data_type"]
                    cid = sql.Identifier(cname)
                    if any(
                        t in dtype
                        for t in ("integer", "numeric", "real", "double precision", "bigint", "smallint")
                    ):
                        q = sql.SQL("""
                            SELECT COUNT(*) AS total_rows, COUNT({col}) AS non_null_rows,
                                   MIN({col}) AS min, MAX({col}) AS max, AVG({col}) AS average
                            FROM {tbl};
                        """).format(col=cid, tbl=table_id)
                    elif any(t in dtype for t in ("char", "text", "uuid")):
                        q = sql.SQL("""
                            SELECT COUNT(*) AS total_rows, COUNT({col}) AS non_null_rows,
                                   COUNT(DISTINCT {col}) AS unique_values
                            FROM {tbl};
                        """).format(col=cid, tbl=table_id)
                    else:
                        q = sql.SQL("SELECT COUNT(*) AS total_rows, COUNT({col}) AS non_null_rows FROM {tbl};").format(
                            col=cid, tbl=table_id
                        )
                    cur.execute(q)
                    row = cur.fetchone()
                    parts.append(f"\n--- {cname} ({dtype}) ---")
                    if row:
                        for k, v in row.items():
                            parts.append(f"  {k}: {v}")
                return "\n".join(parts)
    except Exception as e:
        return f"Error: {e}"


def _postgres_inspect_query_sync(query: str) -> str:
    if psycopg is None:
        return "Error: psycopg not installed."
    conninfo = _postgres_conninfo()
    if not conninfo:
        return "Error: postgres_readonly_url is not configured."
    try:
        with psycopg.connect(conninfo, row_factory=dict_row) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN {query}")
                rows = cur.fetchall()
                if not rows:
                    return "No plan returned."
                return "\n".join(str(next(iter(r.values()))) for r in rows)
    except Exception as e:
        return f"Error: {e}"


def _lookup_service_owner_sync(service_name: str) -> str:
    """Synchronous psycopg query — avoids event-loop conflicts in the MCP server."""
    if psycopg is None:
        return "Error: psycopg not installed."
    try:
        with psycopg.connect(_bugbot_conninfo(), row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT service_name, github_repo, team_slack_group,
                           primary_oncall, tech_stack
                    FROM service_team_mapping
                    WHERE lower(service_name) = lower(%s)
                    LIMIT 1;
                    """,
                    (service_name,),
                )
                row = cur.fetchone()
                if not row:
                    return f"No mapping found for service: {service_name}"
                return (
                    f"Service: {row['service_name']}\n"
                    f"GitHub repo: {row['github_repo']}\n"
                    f"Team Slack group: {row['team_slack_group'] or 'N/A'}\n"
                    f"Primary on-call: {row['primary_oncall'] or 'N/A'}\n"
                    f"Tech stack: {row['tech_stack']}"
                )
    except Exception as e:
        return f"Error querying service mapping: {e}"


@tool(
    "lookup_service_owner",
    "Look up the team, on-call engineer, and GitHub repo for a given service name.",
    {"service_name": str},
)
async def lookup_service_owner(args: dict[str, Any]) -> dict[str, Any]:
    # Run synchronously in a thread to avoid asyncpg event-loop conflicts
    # that occur when the MCP server runs in a different loop than the worker.
    text = await asyncio.to_thread(_lookup_service_owner_sync, args["service_name"])
    return _text_result(text)


@tool(
    "report_finding",
    "Log a significant finding during investigation. Use for key observations, errors found, or metrics anomalies.",
    {"category": str, "finding": str, "severity": str},
)
async def report_finding(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": f"Finding recorded: [{args['category']}] ({args['severity']}) {args['finding']}",
            }
        ]
    }


@tool(
    "postgres_show_tables",
    "List all tables in a PostgreSQL schema. Uses the configured read-only Postgres connection.",
    {"schema": str},
)
async def postgres_show_tables(args: dict[str, Any]) -> dict[str, Any]:
    schema = args.get("schema", "public")
    text = await asyncio.to_thread(_postgres_show_tables_sync, schema)
    return _text_result(text)


@tool(
    "postgres_describe_table",
    "Return the schema (column name, data type, is_nullable) for a given table in PostgreSQL.",
    {"table": str, "schema": str},
)
async def postgres_describe_table(args: dict[str, Any]) -> dict[str, Any]:
    schema = args.get("schema", "public")
    text = await asyncio.to_thread(
        _postgres_describe_table_sync, args["table"], schema
    )
    return _text_result(text)


@tool(
    "postgres_run_query",
    "Run a read-only SQL query against the configured PostgreSQL database and return the result.",
    {"query": str},
)
async def postgres_run_query(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(_postgres_run_query_sync, args["query"])
    return _text_result(text)


@tool(
    "postgres_summarize_table",
    "Compute key summary statistics (counts, min/max, distinct values) for columns in a table.",
    {"table": str, "schema": str},
)
async def postgres_summarize_table(args: dict[str, Any]) -> dict[str, Any]:
    schema = args.get("schema", "public")
    text = await asyncio.to_thread(
        _postgres_summarize_table_sync, args["table"], schema
    )
    return _text_result(text)


@tool(
    "postgres_inspect_query",
    "Show the execution plan (EXPLAIN) for a SQL query without executing it.",
    {"query": str},
)
async def postgres_inspect_query(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(_postgres_inspect_query_sync, args["query"])
    return _text_result(text)


def _close_bug_sync(bug_id: str, reason: str) -> str:
    """Update bug status to 'resolved' in the Bug Bot database."""
    if psycopg is None:
        return "Error: psycopg not installed."
    try:
        with psycopg.connect(_bugbot_conninfo(), row_factory=dict_row, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bug_reports SET status = 'resolved' WHERE bug_id = %s RETURNING bug_id;",
                    (bug_id,),
                )
                row = cur.fetchone()
                if not row:
                    return f"Bug {bug_id} not found."
                return f"Bug {bug_id} has been marked as resolved. Reason: {reason}"
    except Exception as e:
        return f"Error closing bug: {e}"


@tool(
    "close_bug",
    (
        "Mark a bug report as resolved/closed. Use when the reporter confirms the issue "
        "no longer exists, the bug is a duplicate, it is not reproducible, or the report "
        "is clearly not a real bug. Requires bug_id and a brief reason."
    ),
    {"bug_id": str, "reason": str},
)
async def close_bug(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(_close_bug_sync, args["bug_id"], args.get("reason", ""))
    return _text_result(text)


def _get_bug_conversations_sync(bug_id: str, conversation_ids_str: Optional[str] = None) -> str:
    if psycopg is None:
        return "Error: psycopg not installed."
    try:
        ids = [i.strip() for i in conversation_ids_str.split(",") if i.strip()] if conversation_ids_str else []
        with psycopg.connect(_bugbot_conninfo(), row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                if ids:
                    cur.execute(
                        """
                        SELECT sender_type, sender_id, message_type, message_text, metadata, created_at
                        FROM bug_conversations
                        WHERE bug_id = %s AND id = ANY(%s::uuid[])
                        ORDER BY created_at;
                        """,
                        (bug_id, ids),
                    )
                else:
                    cur.execute(
                        """
                        SELECT sender_type, sender_id, message_type, message_text, metadata, created_at
                        FROM bug_conversations
                        WHERE bug_id = %s
                        ORDER BY created_at;
                        """,
                        (bug_id,),
                    )
                rows = cur.fetchall()
        if not rows:
            return f"No conversation history found for {bug_id}."
        lines = []
        for r in rows:
            ts = r["created_at"].strftime("%H:%M:%S") if r["created_at"] else "?"
            line = f"[{ts}] {r['sender_type'].upper()} ({r['message_type']}): {r['message_text'] or ''}"
            attachments = (r.get("metadata") or {}).get("attachments", [])
            if attachments:
                att_parts = []
                for a in attachments:
                    name = a.get("name", "file")
                    mime = a.get("mimetype", "unknown")
                    url = a.get("url_private", "")
                    att_parts.append(
                        f"    - {name} ({mime})"
                        + (f' → download_slack_attachment(bug_id="{bug_id}", url_private="{url}", filename="{name}")' if url else "")
                    )
                line += "\n  [Attachments]\n" + "\n".join(att_parts)
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _download_slack_attachment_sync(bug_id: str, url_private: str, filename: str) -> str:
    attachments_dir = f"/tmp/bugbot-workspace/{bug_id}/attachments"
    os.makedirs(attachments_dir, exist_ok=True)
    local_path = f"{attachments_dir}/{filename}"
    token = settings.slack_bot_token
    if not token:
        return "Error: slack_bot_token is not configured."
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0) as http:
            resp = http.get(url_private, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 200:
            with open(local_path, "wb") as f:
                f.write(resp.content)
            return f"Downloaded to {local_path}"
        return f"Error: HTTP {resp.status_code} fetching attachment."
    except Exception as e:
        return f"Error: {e}"


@tool(
    "download_slack_attachment",
    (
        "Download a Slack file attachment to the bug workspace so it can be read or inspected. "
        "Use the url_private and filename values shown in get_bug_conversations output. "
        "Returns the local file path on success."
    ),
    {"bug_id": str, "url_private": str, "filename": str},
)
async def download_slack_attachment(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(
        _download_slack_attachment_sync,
        args["bug_id"],
        args["url_private"],
        args["filename"],
    )
    return _text_result(text)


@tool(
    "get_bug_conversations",
    (
        "Retrieve conversation history for a bug. bug_id is mandatory and always scopes results "
        "to that bug only. Optionally pass conversation_ids (comma-separated UUIDs) to retrieve "
        "only specific entries — useful for inspecting the new messages listed in the continuation "
        "prompt. Omit conversation_ids to get the full history."
    ),
    {"bug_id": str, "conversation_ids": Optional[str]},
)
async def get_bug_conversations(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(
        _get_bug_conversations_sync, args["bug_id"], args.get("conversation_ids")
    )
    return _text_result(text)


def build_custom_tools_server():
    """Create an in-process MCP server with Bug Bot custom tools."""
    return create_sdk_mcp_server(
        name="bugbot_tools",
        version="1.0.0",
        tools=[
            lookup_service_owner,
            report_finding,
            close_bug,
            get_bug_conversations,
            download_slack_attachment,
            postgres_show_tables,
            postgres_describe_table,
            postgres_run_query,
            postgres_summarize_table,
            postgres_inspect_query,
        ],
    )
