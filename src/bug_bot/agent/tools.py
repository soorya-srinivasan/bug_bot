import asyncio
import os
import re as _re
from typing import Any, Optional

import httpx

try:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None  # type: ignore

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    pymysql = None  # type: ignore

from claude_agent_sdk import tool, create_sdk_mcp_server

from bug_bot.config import settings


def _postgres_conninfo(database_name: Optional[str] = None) -> Optional[str]:
    """Return psycopg-compatible connection string for the read-only external DB.

    If database_name is provided, overrides the database component of the URL so
    the agent can connect to the specific service database returned by lookup_service_owner.
    """
    url = (settings.postgres_readonly_url or "").strip()
    if not url:
        return None
    conninfo = url.replace("postgresql+asyncpg", "postgresql", 1)
    if database_name:
        # Replace the database name: postgresql://user:pass@host:port/DBNAME
        base, _, _ = conninfo.partition("?")
        parts = base.rsplit("/", 1)
        if len(parts) == 2:
            conninfo = parts[0] + "/" + database_name
    return conninfo


def _bugbot_conninfo() -> str:
    """Return psycopg-compatible connection string for the main Bug Bot database."""
    return settings.database_url.replace("postgresql+asyncpg", "postgresql", 1)


def _mysql_conninfo(database_name: Optional[str] = None) -> Optional[dict]:
    """Parse MYSQL_READONLY_URL into a kwargs dict for pymysql.connect().

    Accepts 'mysql://user:pass@host:port/dbname' or 'mysql+pymysql://...' format.
    If database_name is provided, overrides the database in the URL so the agent
    can connect to the specific service database returned by lookup_service_owner.
    Returns None if not configured.
    """
    raw = (settings.mysql_readonly_url or "").strip()
    if not raw:
        return None
    for prefix in ("mysql+pymysql://", "mysql://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    try:
        userinfo, hostpart = raw.split("@", 1)
        user, password = userinfo.split(":", 1)
        if "/" in hostpart:
            hostport, dbname = hostpart.split("/", 1)
        else:
            hostport, dbname = hostpart, ""
        if ":" in hostport:
            host, port_str = hostport.split(":", 1)
            port = int(port_str)
        else:
            host, port = hostport, 3306
    except (ValueError, IndexError):
        return None
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database_name or dbname,
    }


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


def _postgres_run_query_sync(query: str, database_name: Optional[str] = None) -> str:
    if psycopg is None:
        return "Error: psycopg not installed."
    conninfo = _postgres_conninfo(database_name)
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


def _mysql_show_tables_sync(database_name: Optional[str] = None) -> str:
    if pymysql is None:
        return "Error: pymysql not installed."
    cfg = _mysql_conninfo(database_name)
    if not cfg:
        return "Error: mysql_readonly_url is not configured."
    try:
        with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES;")
                rows = cur.fetchall()
                if not rows:
                    db = database_name or cfg.get("database", "configured database")
                    return f"No tables found in '{db}'."
                return "\n".join(next(iter(r.values())) for r in rows)
    except Exception as e:
        return f"Error: {e}"


def _mysql_describe_table_sync(table: str, database_name: Optional[str] = None) -> str:
    if pymysql is None:
        return "Error: pymysql not installed."
    cfg = _mysql_conninfo(database_name)
    if not cfg:
        return "Error: mysql_readonly_url is not configured."
    try:
        with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COLUMN_NAME AS column_name,
                           DATA_TYPE   AS data_type,
                           IS_NULLABLE AS is_nullable
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION;
                    """,
                    (table,),
                )
                rows = cur.fetchall()
                if not rows:
                    return f"Table '{table}' not found."
                lines = [
                    f"{r['column_name']}\t{r['data_type']}\t{r['is_nullable']}" for r in rows
                ]
                return "column_name\tdata_type\tis_nullable\n" + "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _mysql_run_query_sync(query: str, database_name: Optional[str] = None) -> str:
    if pymysql is None:
        return "Error: pymysql not installed."
    cfg = _mysql_conninfo(database_name)
    if not cfg:
        return "Error: mysql_readonly_url is not configured."
    try:
        with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                if not rows:
                    return "Query returned no results."
                columns = list(rows[0].keys())
                header = ",".join(columns)
                data_rows = [",".join(str(r[c]) for c in columns) for r in rows]
                return f"{header}\n" + "\n".join(data_rows)
    except Exception as e:
        return f"Error: {e}"


def _mysql_summarize_table_sync(table: str, database_name: Optional[str] = None) -> str:
    if pymysql is None:
        return "Error: pymysql not installed."
    cfg = _mysql_conninfo(database_name)
    if not cfg:
        return "Error: mysql_readonly_url is not configured."
    numeric_types = {"int", "integer", "bigint", "smallint", "tinyint",
                     "float", "double", "decimal", "numeric"}
    text_types = {"char", "varchar", "text", "tinytext", "mediumtext", "longtext", "enum", "set"}
    try:
        with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COLUMN_NAME AS column_name,
                           DATA_TYPE   AS data_type
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION;
                    """,
                    (table,),
                )
                columns = cur.fetchall()
                if not columns:
                    return f"Table '{table}' not found."
                parts = [f"Summary for table: {table}\n"]
                for col in columns:
                    cname = col["column_name"]
                    dtype = col["data_type"].lower()
                    if any(t in dtype for t in numeric_types):
                        cur.execute(
                            f"SELECT COUNT(*) AS total_rows, COUNT(`{cname}`) AS non_null_rows, "
                            f"MIN(`{cname}`) AS min, MAX(`{cname}`) AS max, "
                            f"AVG(`{cname}`) AS average FROM `{table}`;"
                        )
                    elif any(t in dtype for t in text_types):
                        cur.execute(
                            f"SELECT COUNT(*) AS total_rows, COUNT(`{cname}`) AS non_null_rows, "
                            f"COUNT(DISTINCT `{cname}`) AS unique_values FROM `{table}`;"
                        )
                    else:
                        cur.execute(
                            f"SELECT COUNT(*) AS total_rows, COUNT(`{cname}`) AS non_null_rows "
                            f"FROM `{table}`;"
                        )
                    row = cur.fetchone()
                    parts.append(f"\n--- {cname} ({dtype}) ---")
                    if row:
                        for k, v in row.items():
                            parts.append(f"  {k}: {v}")
                return "\n".join(parts)
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
                    SELECT
                        s.service_name, s.github_repo, s.tech_stack, s.description,
                        s.team_slack_group, s.service_owner, s.primary_oncall,
                        s.database_name, s.dialect,
                        t.slack_group_id, t.oncall_engineer AS team_oncall
                    FROM service_team_mapping s
                    LEFT JOIN teams t ON s.team_id = t.id
                    WHERE lower(s.service_name) = lower(%s)
                    LIMIT 1;
                    """,
                    (service_name,),
                )
                row = cur.fetchone()
                if not row:
                    return f"No mapping found for service: {service_name}"
                desc_line = f"Description: {row['description']}\n" if row.get("description") else ""
                db_line = (
                    f"Database: {row['database_name']} (dialect: {row['dialect']})\n"
                    if row.get("database_name") else ""
                )
                return (
                    f"Service: {row['service_name']}\n"
                    f"{desc_line}"
                    f"GitHub repo: {row['github_repo']}\n"
                    f"Tech stack: {row['tech_stack']}\n"
                    f"{db_line}"
                    f"Service owner: {row['service_owner'] or row['primary_oncall'] or 'N/A'}\n"
                    f"Team Slack group: {row['slack_group_id'] or row['team_slack_group'] or 'N/A'}\n"
                    f"Team on-call: {row['team_oncall'] or 'N/A'}"
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


def _list_services_sync() -> str:
    """Return all canonical service names from service_team_mapping."""
    if psycopg is None:
        return "Error: psycopg not installed."
    try:
        with psycopg.connect(_bugbot_conninfo(), row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT service_name, tech_stack, description, github_repo FROM service_team_mapping ORDER BY service_name;"
                )
                rows = cur.fetchall()
                if not rows:
                    return "No services registered."
                lines = []
                for r in rows:
                    desc = f" — {r['description']}" if r.get("description") else ""
                    repo = f" [{r['github_repo']}]" if r.get("github_repo") else ""
                    lines.append(f"- {r['service_name']} ({r['tech_stack']}){desc}{repo}")
                return "Known services:\n" + "\n".join(lines)
    except Exception as e:
        return f"Error listing services: {e}"


@tool(
    "list_services",
    "List all known service names registered in Bug Bot. Use this to find the canonical service name before calling lookup_service_owner or populating relevant_services.",
    {},
)
async def list_services(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(_list_services_sync)
    return _text_result(text)
  
 
def _list_datasources_sync() -> str:
    """Query Grafana HTTP API to list all configured datasources (name, type, UID)."""
    grafana_url = (settings.grafana_url or "http://localhost:3000").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if settings.grafana_api_key:
        headers["Authorization"] = f"Bearer {settings.grafana_api_key}"
    try:
        with httpx.Client(timeout=10.0) as http:
            resp = http.get(f"{grafana_url}/api/datasources", headers=headers)
        if resp.status_code != 200:
            return f"Error: Grafana returned HTTP {resp.status_code} at {grafana_url}. Response: {resp.text[:300]}"
        datasources = resp.json()
        if not datasources:
            return "No datasources configured in Grafana."
        lines = []
        for ds in datasources:
            lines.append(
                f"- name={ds.get('name')}  type={ds.get('type')}  uid={ds.get('uid')}  url={ds.get('url')}"
            )
        return f"Grafana datasources ({grafana_url}):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error reaching Grafana at {grafana_url}: {e}"


@tool(
    "list_datasources",
    "List all Grafana datasources (name, type, UID). Call this first to get the Loki datasource UID before querying logs.",
    {},
)
async def list_datasources(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(_list_datasources_sync)
    return _text_result(text)


# Word → integer mapping used by _resolve_time_expression.
_WORD_TO_NUM = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "couple": 2, "few": 3, "half": 0.5,
}
# Regex fragment that matches either a decimal digit string or a word number.
_NUM_FRAG = r"(\d+(?:\.\d+)?|" + "|".join(_WORD_TO_NUM.keys()) + r")"


def _parse_num(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return _WORD_TO_NUM.get(s.lower(), 1)


def _resolve_time_expression(expr: str, local_tz) -> tuple:
    """Resolve a natural-language time expression to (from_dt, to_dt) datetimes.

    Supported patterns (case-insensitive, 'last'/'past' interchangeable):
      "last/past N min[ute][s]"    → [now − N min, now]
      "last/past N hour[s]"        → [now − N h,   now]
      "last/past N day[s]"         → [now − N d,   now]
      — N may be a digit string OR a word ("two", "couple", "half", "a", "an", …)
      "yesterday"                  → [start-of-yesterday, end-of-yesterday] (local tz)
      "today"                      → [start-of-today, now]                  (local tz)

    Returns (None, None) if the expression is not recognised.
    """
    import datetime as _dt

    now = _dt.datetime.now(local_tz)
    e = expr.lower().strip()

    trigger = r"(?:last|past|previous|in\s+the\s+last|in\s+the\s+past)"

    # Optional "of" between the count and the unit: "last couple of hours"
    _OF = r"(?:\s+of)?"

    # minutes
    m = _re.search(rf"{trigger}\s+{_NUM_FRAG}{_OF}\s+min(?:ute)?s?", e)
    if m:
        return now - _dt.timedelta(minutes=_parse_num(m.group(1))), now

    # hours
    m = _re.search(rf"{trigger}\s+{_NUM_FRAG}{_OF}\s+hours?", e)
    if m:
        return now - _dt.timedelta(hours=_parse_num(m.group(1))), now

    # days
    m = _re.search(rf"{trigger}\s+{_NUM_FRAG}{_OF}\s+days?", e)
    if m:
        return now - _dt.timedelta(days=_parse_num(m.group(1))), now

    # shorthand without trigger: "2h", "30m", "3d"
    m = _re.search(r"\b(\d+(?:\.\d+)?)\s*h(?:rs?|ours?)?\b", e)
    if m:
        return now - _dt.timedelta(hours=float(m.group(1))), now

    m = _re.search(r"\b(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?\b", e)
    if m:
        return now - _dt.timedelta(minutes=float(m.group(1))), now

    m = _re.search(r"\b(\d+(?:\.\d+)?)\s*d(?:ays?)?\b", e)
    if m:
        return now - _dt.timedelta(days=float(m.group(1))), now

    if "yesterday" in e:
        yesterday = (now - _dt.timedelta(days=1)).date()
        return (
            _dt.datetime.combine(yesterday, _dt.time.min, tzinfo=local_tz),
            _dt.datetime.combine(yesterday, _dt.time.max, tzinfo=local_tz),
        )

    if "today" in e:
        return _dt.datetime.combine(now.date(), _dt.time.min, tzinfo=local_tz), now

    return None, None


def _build_keyword_filter(keywords: list) -> str:
    """Return a LogQL |~ filter that matches any keyword (case-insensitive OR)."""
    escaped = [_re.escape(kw.strip()) for kw in keywords if kw.strip()]
    if not escaped:
        return ""
    return f'|~ "(?i){"|".join(escaped)}"'


def _inject_keyword_filter(query: str, kw_filter: str) -> str:
    """Splice kw_filter into the query before '| json' if present, else append."""
    if not kw_filter:
        return query
    if "| json" in query:
        return query.replace("| json", f"{kw_filter} | json", 1)
    return f"{query.rstrip()} {kw_filter}"


def _keyword_hits(results: list, keywords: list) -> dict:
    """Count log lines containing each keyword (case-insensitive) across all streams."""
    counts: dict = {kw: 0 for kw in keywords}
    for stream in results:
        for _, log_line in stream.get("values", []):
            ll = log_line.lower()
            for kw in keywords:
                if kw.lower() in ll:
                    counts[kw] += 1
    return counts


def _query_loki_logs_sync(
    query: str,
    start_offset_minutes: int,
    limit: int,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    keywords: Optional[list] = None,
    time_expression: Optional[str] = None,
) -> str:
    """Query Loki directly via HTTP API.

    Time range resolution priority (highest → lowest):
    1. time_expression  — natural language, e.g. "last 2 hours", "yesterday" (computed
                          server-side from the current local time, so always accurate).
    2. from_time / to_time — ISO 8601 strings; naive datetimes treated as local timezone.
    3. start_offset_minutes — relative look-back from now (default fallback).

    Keyword search:
    When `keywords` is provided the tool:
    - Builds a combined OR filter and runs one query for all keywords together.
    - Reports per-keyword hit counts so coverage gaps are immediately visible.
    - Automatically retries any keyword that got zero hits with its own individual query.

    All timestamps in the response are rendered in the server's local timezone.
    """
    import time as _time
    import datetime as _dt

    loki_url = (settings.loki_url or "http://localhost:3100").rstrip("/")
    local_tz = _dt.datetime.now().astimezone().tzinfo

    # ── 1. Resolve time range ────────────────────────────────────────────────
    start_ns: int
    end_ns: int
    range_desc: str

    te_warn: str = ""
    te_from, te_to = (None, None)
    if time_expression:
        te_from, te_to = _resolve_time_expression(time_expression, local_tz)
        if te_from is None:
            te_warn = (
                f"WARNING: time_expression {time_expression!r} was not recognised and was ignored. "
                f"Fell back to start_offset_minutes={start_offset_minutes}. "
                f"Supported forms: 'last N hours/minutes/days', 'yesterday', 'today', "
                f"shorthand '2h'/'30m'/'3d'."
            )

    if te_from is not None:
        # time_expression recognised → highest priority
        start_ns = int(te_from.timestamp() * 1e9)
        end_ns = int(te_to.timestamp() * 1e9)  # type: ignore[union-attr]
        range_desc = (
            f"{te_from.strftime('%Y-%m-%d %H:%M:%S %Z')} → "
            f"{te_to.strftime('%Y-%m-%d %H:%M:%S %Z')}"  # type: ignore[union-attr]
        )
    elif from_time or to_time:
        def _parse_iso(ts_str: str) -> _dt.datetime:
            parsed = _dt.datetime.fromisoformat(ts_str)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=local_tz)
            return parsed

        now_local = _dt.datetime.now(local_tz)
        end_dt = _parse_iso(to_time) if to_time else now_local
        start_dt = (
            _parse_iso(from_time)
            if from_time
            else end_dt - _dt.timedelta(minutes=start_offset_minutes)
        )
        start_ns = int(start_dt.timestamp() * 1e9)
        end_ns = int(end_dt.timestamp() * 1e9)
        range_desc = (
            f"{start_dt.strftime('%Y-%m-%d %H:%M:%S %Z')} → "
            f"{end_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
    else:
        now_ns = int(_time.time() * 1e9)
        start_ns = now_ns - int(start_offset_minutes * 60 * 1e9)
        end_ns = now_ns
        range_desc = f"last {start_offset_minutes} minute(s)"

    tz_label = _dt.datetime.now(local_tz).strftime("%Z (UTC%z)")
    # Millisecond timestamps for the Grafana Explore URL range.from / range.to fields.
    start_ms = start_ns // 1_000_000
    end_ms = end_ns // 1_000_000

    # ── 2. Build LogQL query (inject keyword OR-filter if provided) ──────────
    base_query = query
    kw_filter = _build_keyword_filter(keywords) if keywords else ""
    combined_query = _inject_keyword_filter(base_query, kw_filter) if kw_filter else base_query

    def _run(q: str) -> list:
        params = {
            "query": q,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(min(limit or 100, 500)),
            "direction": "backward",
        }
        with httpx.Client(timeout=30.0) as http:
            resp = http.get(f"{loki_url}/loki/api/v1/query_range", params=params)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Loki returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json().get("data", {}).get("result", [])

    try:
        results = _run(combined_query)

        sections: list[str] = []

        if te_warn:
            sections.append(te_warn)

        # ── 3. Keyword coverage report ───────────────────────────────────────
        extra_by_kw: dict = {}
        if keywords:
            hit_counts = _keyword_hits(results, keywords)
            missed = [kw for kw, cnt in hit_counts.items() if cnt == 0]

            # Individual retry for each missed keyword
            for kw in missed:
                solo_q = _inject_keyword_filter(base_query, _build_keyword_filter([kw]))
                try:
                    solo_results = _run(solo_q)
                    if solo_results:
                        extra_by_kw[kw] = solo_results
                        hit_counts[kw] = sum(
                            len(r.get("values", [])) for r in solo_results
                        )
                except Exception:
                    pass

            cov_lines = [f"Keyword coverage ({len(keywords)} keyword(s) searched):"]
            for kw, cnt in hit_counts.items():
                icon = "✓" if cnt > 0 else "✗"
                note = f"{cnt} hit(s)" if cnt > 0 else "NOT FOUND in combined query"
                cov_lines.append(f"  [{icon}] {kw!r} — {note}")
            sections.append("\n".join(cov_lines))

        # ── 4. Combined results ──────────────────────────────────────────────
        def _format_stream_lines(stream_list: list, cap: int = 25) -> list[str]:
            out = []
            for stream in stream_list:
                labels = stream.get("stream", {})
                label_str = ", ".join(f'{k}="{v}"' for k, v in labels.items())
                out.append(f"Stream [{label_str}]:")
                for ts_ns, log_line in stream.get("values", [])[:cap]:
                    ts = _dt.datetime.fromtimestamp(
                        int(ts_ns) / 1e9, tz=local_tz
                    ).strftime("%Y-%m-%d %H:%M:%S %Z")
                    out.append(f"  [{ts}] {log_line}")
            return out

        if results:
            total = sum(len(r.get("values", [])) for r in results)
            header = (
                f"Found {total} log line(s) across {len(results)} stream(s).\n"
                f"Query: {combined_query}\n"
                f"Time range: {range_desc} | Timestamps in: {tz_label}\n"
                f"Grafana range → start_ms={start_ms}  end_ms={end_ms}"
            )
            sections.append(header)
            sections.append("\n".join(_format_stream_lines(results)))
        else:
            sections.append(
                f"No log lines matched the combined query.\n"
                f"Query: {combined_query}\n"
                f"Range: {range_desc}\n"
                f"Grafana range → start_ms={start_ms}  end_ms={end_ms}"
            )

        # ── 5. Individual results for keywords that missed the combined run ──
        for kw, kw_results in extra_by_kw.items():
            kw_total = sum(len(r.get("values", [])) for r in kw_results)
            solo_q = _inject_keyword_filter(base_query, _build_keyword_filter([kw]))
            kw_header = (
                f"Individual results for keyword {kw!r} "
                f"({kw_total} line(s) — retried separately):\n"
                f"Query: {solo_q}"
            )
            sections.append(kw_header)
            sections.append("\n".join(_format_stream_lines(kw_results, cap=10)))

        return "\n\n".join(sections)

    except Exception as e:
        return f"Error reaching Loki at {loki_url}: {e}"


@tool(
    "query_loki_logs",
    (
        "Query Grafana Loki logs using a LogQL expression. "
        "Use label selectors like {app=\"payment-service-sample\"} or {job=\"vconnect\"} "
        "with filters like |= \"error\".\n\n"

        "TIME RANGE — three options (highest priority first):\n"
        "  1. time_expression: natural language string resolved server-side from the current "
        "local time. Supported: 'last N minutes', 'last N hours', 'last N days', "
        "'yesterday', 'today'. ALWAYS use this when the reporter says something like "
        "'last 2 hours', 'yesterday afternoon', etc.\n"
        "  2. from_time / to_time: ISO 8601 strings (e.g. '2026-02-22T14:00:00'). "
        "Naive datetimes are treated as the server's local timezone.\n"
        "  3. start_offset_minutes: relative look-back from now (default 60; "
        "use 360 for 6 hours, 1440 for 24 hours).\n\n"

        "KEYWORD SEARCH — pass keywords as a list of individual terms "
        "(e.g. ['international', 'payment', 'error']). The tool will:\n"
        "  • Run one combined OR query across all keywords.\n"
        "  • Report per-keyword hit counts so you can see coverage gaps.\n"
        "  • Automatically retry any keyword that had zero hits with its own query.\n"
        "ALWAYS decompose a multi-word description into individual keywords. "
        "For 'international payment errors' pass keywords=['international','payment','error'].\n\n"

        "Returned timestamps are displayed in the server's local timezone."
    ),
    {
        "query": str,
        "start_offset_minutes": int,
        "limit": int,
        "from_time": Optional[str],
        "to_time": Optional[str],
        "keywords": Optional[list],
        "time_expression": Optional[str],
    },
)
async def query_loki_logs(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(
        _query_loki_logs_sync,
        args["query"],
        args.get("start_offset_minutes", 60),
        args.get("limit", 100),
        args.get("from_time"),
        args.get("to_time"),
        args.get("keywords"),
        args.get("time_expression"),
    )
    return _text_result(text)


def _report_finding_sync(bug_id: str, category: str, finding: str, severity: str) -> str:
    if psycopg is None:
        return "Error: psycopg not installed."
    try:
        with psycopg.connect(_bugbot_conninfo(), row_factory=dict_row, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO investigation_findings (id, bug_id, category, finding, severity)
                    VALUES (gen_random_uuid(), %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (bug_id, category, finding, severity),
                )
                row = cur.fetchone()
                finding_id = row["id"] if row else "unknown"
        return f"Finding recorded (id={finding_id}): [{category}] ({severity}) {finding}"
    except Exception as e:
        return f"Error recording finding: {e}"


@tool(
    "report_finding",
    "Log a significant finding during investigation. Use for key observations, errors found, "
    "or metrics anomalies. Always pass the bug_id of the current investigation.",
    {"bug_id": str, "category": str, "finding": str, "severity": str},
)
async def report_finding(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(
        _report_finding_sync,
        args["bug_id"],
        args["category"],
        args["finding"],
        args["severity"],
    )
    return _text_result(text)


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
    (
        "Run a read-only SQL query against the configured PostgreSQL database and return the result. "
        "Optionally pass database_name (obtained from lookup_service_owner) to target the specific "
        "service database instead of the default POSTGRES_READONLY_URL database."
    ),
    {"query": str, "database_name": Optional[str]},
)
async def postgres_run_query(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(
        _postgres_run_query_sync, args["query"], args.get("database_name")
    )
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


@tool(
    "mysql_show_tables",
    (
        "List all tables in a MySQL database. Uses the configured read-only MySQL connection. "
        "Optionally pass database_name (obtained from lookup_service_owner) to target the specific "
        "service database instead of the default MYSQL_READONLY_URL database."
    ),
    {"database_name": Optional[str]},
)
async def mysql_show_tables(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(_mysql_show_tables_sync, args.get("database_name"))
    return _text_result(text)


@tool(
    "mysql_describe_table",
    (
        "Return the schema (column name, data type, is_nullable) for a table in MySQL. "
        "Optionally pass database_name (obtained from lookup_service_owner) to target the specific "
        "service database instead of the default MYSQL_READONLY_URL database."
    ),
    {"table": str, "database_name": Optional[str]},
)
async def mysql_describe_table(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(
        _mysql_describe_table_sync, args["table"], args.get("database_name")
    )
    return _text_result(text)


@tool(
    "mysql_run_query",
    (
        "Run a read-only SQL query against the configured MySQL database and return the result. "
        "Optionally pass database_name (obtained from lookup_service_owner) to target the specific "
        "service database instead of the default MYSQL_READONLY_URL database."
    ),
    {"query": str, "database_name": Optional[str]},
)
async def mysql_run_query(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(
        _mysql_run_query_sync, args["query"], args.get("database_name")
    )
    return _text_result(text)


@tool(
    "mysql_summarize_table",
    (
        "Compute key summary statistics (counts, min/max, distinct values) for columns in a MySQL table. "
        "Optionally pass database_name (obtained from lookup_service_owner) to target the specific "
        "service database instead of the default MYSQL_READONLY_URL database."
    ),
    {"table": str, "database_name": Optional[str]},
)
async def mysql_summarize_table(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(
        _mysql_summarize_table_sync, args["table"], args.get("database_name")
    )
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
            list_datasources,
            query_loki_logs,
            lookup_service_owner,
            list_services,
            report_finding,
            close_bug,
            get_bug_conversations,
            download_slack_attachment,
            postgres_show_tables,
            postgres_describe_table,
            postgres_run_query,
            postgres_summarize_table,
            postgres_inspect_query,
            mysql_show_tables,
            mysql_describe_table,
            mysql_run_query,
            mysql_summarize_table,
        ],
    )
