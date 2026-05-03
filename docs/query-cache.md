# 10. Query cache

> **Tier 3 — Medium.** Useful for repeated dashboard queries with stable parameters; useless for everything else. Default TTL is 60 seconds — the cache is *transactionally inconsistent* by design.

## What it is

A server-side result cache keyed on the **query text**. Hits return the previous full result without re-running the query. Per the [docs](https://clickhouse.com/docs/en/operations/query-cache):

- Storage: in-memory on each replica.
- TTL-based eviction (default 60s) — entries are not invalidated on writes.
- Skips caching for non-deterministic functions (`now()`, `rand()`, `dictGet()`).
- Skips queries that read system tables.
- Failed queries are never cached.

This makes it suited for OLAP — repeated dashboard reads where data evolves slowly and a 60-second window of staleness is acceptable.

## Enabling

Per-query opt-in:

```sql
SELECT count() FROM events WHERE event_type = 'click'
SETTINGS use_query_cache = true;
```

Per-user / per-session:

```sql
ALTER USER dashboard_user SETTINGS
    use_query_cache = 1,
    query_cache_ttl = 300;
```

## Independent read/write controls

Both default to `true`. Useful in testing (write only):

```sql
SELECT … SETTINGS
    use_query_cache = true,
    enable_reads_from_query_cache = false,    -- always re-execute
    enable_writes_to_query_cache = true;      -- but populate the cache
```

## Tags for selective invalidation

```sql
SELECT … SETTINGS use_query_cache = true, query_cache_tag = 'dashboard_v1';

-- Drop only entries with that tag
SYSTEM CLEAR QUERY CACHE TAG 'dashboard_v1';

-- Drop everything
SYSTEM CLEAR QUERY CACHE;
```

## Server-side capacity

Configured per server / per Cloud service:

| Setting | Default |
|---|---|
| `max_size_in_bytes` | 1 GB |
| `max_entries` | 1,024 |
| `max_entry_size_in_bytes` | 1 MB |

`max_entry_size_in_bytes` deliberately skips caching huge results — the cache is for many small queries, not a few giant ones.

### Eviction

Lazy. Stale entries stay until space is needed; at that point all expired entries are purged first. If still insufficient, **new entries are rejected** rather than evicting fresh ones.

## Verifying hits

```sql
-- Per-query: query_cache_usage is 'Read', 'Write', or 'None'
SELECT
    event_time, query, query_cache_usage,
    query_duration_ms, read_rows
FROM system.query_log
WHERE query_cache_usage != 'None' AND type = 'QueryFinish'
ORDER BY event_time DESC LIMIT 20;
```

```sql
-- Aggregate counters
SELECT event, value
FROM system.events
WHERE event IN ('QueryCacheHits', 'QueryCacheMisses');
```

A hit shows `query_duration_ms` near zero and `read_rows = 0`.

## When NOT to use

- Queries containing `now()`, `rand()`, `today()`, etc. — non-deterministic, never cached.
- Queries containing `dictGet()` — disables cache for that query.
- Queries against `system.*` tables.
- Real-time dashboards where 60-second staleness is unacceptable.
- One-off ad-hoc queries — the cache won't be hit again.
- Very large result sets (>1 MB by default) — exceeds `max_entry_size_in_bytes`.

## Pitfalls

- **Treating it as a substitute for proper indexing.** The cache only helps repeated queries. The first run is still slow. Fix the underlying query before reaching for the cache.
- **Caching a query that includes the date.** `WHERE event_time >= today() - INTERVAL 1 DAY` is non-deterministic (`today()` ticks). Either skip caching or use a fixed window.
- **Forgetting that the cache is per-replica.** On Cloud's 3-replica services, each replica has its own cache; identical queries that route to different replicas may all miss before warming.

## How this project tests it

There's no dedicated comparison for the query cache yet. The right place to add one is `src/schema_variants.py` as a query-level setting test rather than a schema variant: run a heavy aggregation twice with `use_query_cache = true` and compare the second run's `query_duration_ms` and `query_cache_usage` from `system.query_log`.

## Sources

- ClickHouse, *Query Cache*. <https://clickhouse.com/docs/en/operations/query-cache>
