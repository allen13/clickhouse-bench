# ClickHouse Agent Skills — local copy

Snapshot of the official [ClickHouse/agent-skills](https://github.com/ClickHouse/agent-skills)
repository, kept here so the project's `clickhouse-schema-design` skill
can cite authoritative content offline.

## Snapshot

- **Source**: <https://github.com/ClickHouse/agent-skills>
- **Commit pinned**: `d28416142e5634ce288602cd0c8f3457e68177ff`
- **Captured**: 2026-05-04
- **License**: see [`LICENSE`](LICENSE) (Apache 2.0, unchanged from upstream)

## What's included

| Path | Source | Notes |
|---|---|---|
| `clickhouse-best-practices/` | `skills/clickhouse-best-practices/` upstream | 33 rules across schema design, query optimization, and data ingestion. The `clickhouse:clickhouse-best-practices` plugin loaded into this Claude Code session points at an older slice of the same skill (28 rules); this local copy is the current upstream. |
| `clickhouse-architecture-advisor/` | `skills/clickhouse-architecture-advisor/` upstream | 5 decision frameworks (ingestion strategy, time-series partitioning, JOIN-vs-dict-vs-denorm, late-arriving data, real-time pre-aggregation) plus example workloads. **Explicitly not a schema generator** — this skill is for architecture-shape decisions; the project's `clickhouse-schema-design` skill turns those decisions into a concrete `CREATE TABLE`. |
| `AGENTS.md`, `README.md`, `LICENSE` | repo root | Top-level upstream docs, kept for context. |

## How this is used in the project

`.claude/skills/clickhouse-schema-design/SKILL.md` cites both skills as sibling tools:

- *Reviewing* an existing DDL → use `clickhouse-best-practices` (the rule checker).
- *Choosing* an architecture pattern (ingest, MV vs. dict, etc.) → use `clickhouse-architecture-advisor` (decision framework).
- *Writing* the actual `CREATE TABLE` from a settled architecture → use `clickhouse-schema-design` (this project's local skill).

## Refresh

To re-snapshot from upstream:

```bash
TMP=$(mktemp -d)
git clone --depth=1 https://github.com/ClickHouse/agent-skills.git "$TMP/agent-skills"
for s in clickhouse-best-practices clickhouse-architecture-advisor; do
  rm -rf "docs/references/clickhouse-skills/$s"
  cp -R "$TMP/agent-skills/skills/$s" "docs/references/clickhouse-skills/$s"
done
cp "$TMP/agent-skills/AGENTS.md" "docs/references/clickhouse-skills/AGENTS.md"
cp "$TMP/agent-skills/README.md" "docs/references/clickhouse-skills/README.md"
cp "$TMP/agent-skills/LICENSE"   "docs/references/clickhouse-skills/LICENSE"
rm -rf "$TMP"
# Update the commit hash in this PROVENANCE.md afterwards.
```
