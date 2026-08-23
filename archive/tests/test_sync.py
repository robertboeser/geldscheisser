import re

import pytest

from gs_archive.sync import TABLES, _insert_sql, _select_sql, sync_table


def find_table(name: str):
    return next(t for t in TABLES if t.name == name)


class FakeSource:
    """In-memory stand-in for `source.fetch(sql, *args)` against one table."""

    def __init__(self, table, rows: list[dict]):
        self.table = table
        self.rows = sorted(rows, key=lambda r: tuple(r[c] for c in table.keyset_cols))
        self.fetch_calls = 0

    async def fetch(self, sql, *args):
        self.fetch_calls += 1
        # asyncpg raises InterfaceError when the statement's placeholder count
        # doesn't match the args passed. Mirror that here — without it this fake
        # reads the cursor straight out of *args and never looks at the SQL, so a
        # statement/arg mismatch (e.g. reusing the no-watermark statement for a
        # later page) passes the suite and only fails against a real server.
        n_placeholders = len({int(n) for n in re.findall(r"\$(\d+)", sql)})
        assert n_placeholders == len(args), (
            f"statement expects {n_placeholders} args, got {len(args)}: {sql}"
        )
        limit = args[-1]
        cursor = args[:-1]
        if cursor:
            filtered = [r for r in self.rows if tuple(r[c] for c in self.table.keyset_cols) > cursor]
        else:
            filtered = self.rows
        return filtered[:limit]


class FakeDest:
    """In-memory stand-in for the archive DB, enforcing ON CONFLICT DO NOTHING semantics."""

    def __init__(self, table, existing: list[dict] | None = None):
        self.table = table
        self.rows = list(existing or [])

    async def fetchrow(self, _sql, *_args):
        if not self.rows:
            return None
        return max(self.rows, key=lambda r: tuple(r[c] for c in self.table.keyset_cols))

    async def executemany(self, _sql, records):
        existing_keys = {tuple(r[c] for c in self.table.conflict_cols) for r in self.rows}
        for rec in records:
            row = dict(zip(self.table.select_cols, rec))
            key = tuple(row[c] for c in self.table.conflict_cols)
            if key not in existing_keys:
                self.rows.append(row)
                existing_keys.add(key)


def make_rollup_row(ts: int, symbol: str = "BTCUSDT") -> dict:
    table = find_table("book_rollups")
    return {c: 0.0 for c in table.select_cols} | {"ts": ts, "symbol": symbol}


class TestSyncTablePagination:
    async def _run(self, n_rows: int, batch_size: int, existing: int = 0):
        table = find_table("book_rollups")
        rows = [make_rollup_row(ts) for ts in range(n_rows)]
        source = FakeSource(table, rows)
        dest = FakeDest(table, existing=rows[:existing])
        total = await sync_table(source, dest, table, batch_size)
        return table, source, dest, total

    @pytest.mark.asyncio
    async def test_full_sync_from_empty(self):
        table, source, dest, total = await self._run(n_rows=25, batch_size=10)
        assert total == 25
        assert len(dest.rows) == 25
        assert {r["ts"] for r in dest.rows} == set(range(25))

    @pytest.mark.asyncio
    async def test_from_empty_switches_to_the_watermark_statement_after_page_one(self):
        # Regression: the select statement used to be built once, from the
        # watermark state at entry. Starting from an empty table that meant the
        # no-WHERE statement (one placeholder, the limit) was reused for page 2
        # onwards, once a cursor existed — asyncpg rejected it outright, so the
        # very first sync of any table could never get past its first page.
        table, source, dest, total = await self._run(n_rows=30, batch_size=10)
        assert source.fetch_calls == 4  # 3 full pages + the empty confirming fetch
        assert total == 30

    @pytest.mark.asyncio
    async def test_resume_only_fetches_new_rows(self):
        table, source, dest, total = await self._run(n_rows=25, batch_size=10, existing=20)
        assert total == 5  # only rows 20..24 were new
        assert len(dest.rows) == 25

    @pytest.mark.asyncio
    async def test_exact_batch_multiple_terminates(self):
        # 20 rows / batch 10 -> two full pages, then a 3rd (empty) fetch to confirm done.
        table, source, dest, total = await self._run(n_rows=20, batch_size=10)
        assert total == 20
        assert source.fetch_calls == 3

    @pytest.mark.asyncio
    async def test_nothing_new_is_a_single_probe_call(self):
        table, source, dest, total = await self._run(n_rows=10, batch_size=10, existing=10)
        assert total == 0
        assert source.fetch_calls == 1

    @pytest.mark.asyncio
    async def test_no_double_insert_across_multiple_symbols_same_timestamp(self):
        table = find_table("book_rollups")
        rows = [make_rollup_row(0, "BTCUSDT"), make_rollup_row(0, "ETHUSDT"), make_rollup_row(1, "BTCUSDT")]
        source = FakeSource(table, rows)
        dest = FakeDest(table)
        total = await sync_table(source, dest, table, batch_size=1)
        assert total == 3
        assert len(dest.rows) == 3  # no ties were skipped or duplicated across page boundaries


class TestSqlGeneration:
    def test_select_sql_omits_where_without_watermark(self):
        table = find_table("funding_rates")
        sql = _select_sql(table, has_watermark=False)
        assert "WHERE" not in sql
        assert sql.strip().endswith("LIMIT $1")

    def test_select_sql_includes_composite_keyset_with_watermark(self):
        table = find_table("klines")
        sql = _select_sql(table, has_watermark=True)
        assert "WHERE (open_time, symbol, interval) > ($1, $2, $3)" in sql
        assert sql.strip().endswith("LIMIT $4")

    def test_insert_sql_casts_jsonb_columns(self):
        table = find_table("book_snapshots")
        sql = _insert_sql(table)
        assert "$4::jsonb" in sql  # bids
        assert "$5::jsonb" in sql  # asks
        assert "ON CONFLICT (symbol, ts) DO NOTHING" in sql

    def test_insert_sql_conflict_target_matches_primary_key(self):
        table = find_table("klines")
        sql = _insert_sql(table)
        assert "ON CONFLICT (symbol, interval, open_time) DO NOTHING" in sql


class TestTableConfigConsistency:
    @pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
    def test_keyset_is_a_reordering_of_conflict_cols(self, table):
        # Keyset pagination is only safe (no ties) if it spans the full primary key.
        assert set(table.keyset_cols) == set(table.conflict_cols)

    @pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
    def test_conflict_cols_are_a_subset_of_select_cols(self, table):
        assert set(table.conflict_cols) <= set(table.select_cols)
