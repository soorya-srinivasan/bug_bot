# Database Investigation

When investigating data-related bugs:

## PostgreSQL
- Check for recent schema changes or migrations
- Look for constraint violations in error logs
- Check for long-running queries or locks (pg_stat_activity)
- Verify indexes exist for commonly queried columns
- Check for data inconsistencies between related tables

## MySQL
- Check for deadlocks in SHOW ENGINE INNODB STATUS
- Look for slow queries in the slow query log
- Verify foreign key constraints are enforced
- Check character encoding issues (utf8 vs utf8mb4)

## General
- All queries MUST be read-only (SELECT only)
- Always LIMIT result sets to avoid pulling excessive data
- Check for NULL values in columns that should have defaults
- Look for orphaned records (foreign key references to deleted rows)
- Compare timestamps to find data that was modified at bug report time
