"""Schema/retention guards in gs_common.db."""

import pytest

from gs_common.db import ensure_schema


class FakeConn:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return "OK"


class FakePool:
    """Minimal stand-in for asyncpg.Pool.acquire() as an async context manager."""

    def __init__(self):
        self.conn = FakeConn()

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _retention_sql(conn):
    return [s for s in conn.executed if "retention_policy" in s]


async def test_retention_none_leaves_existing_policies_alone():
    """`gs backfill` against the unbounded archive must not impose a window.

    gs-common defaults to 90 days, which is right for the collector. A batch job
    that inherited it would attach a retention policy to the archive and drop the
    history it had just written — so None must emit no retention statement at all,
    neither add nor remove."""
    pool = FakePool()
    await ensure_schema(pool, retention_days=None)
    assert _retention_sql(pool.conn) == []
    assert any("CREATE TABLE IF NOT EXISTS klines" in s for s in pool.conn.executed)


@pytest.mark.parametrize("days", [90, 30])
async def test_retention_positive_adds_policy_to_every_hypertable(days):
    pool = FakePool()
    await ensure_schema(pool, retention_days=days)
    sql = _retention_sql(pool.conn)
    assert len(sql) == 4 and all(f"INTERVAL '{days} days'" in s for s in sql)
    assert all("add_retention_policy" in s for s in sql)


async def test_retention_zero_removes_policies():
    """0 is the archive service's explicit "unbounded" — it actively strips a
    policy an earlier misconfigured run may have left behind."""
    pool = FakePool()
    await ensure_schema(pool, retention_days=0)
    sql = _retention_sql(pool.conn)
    assert len(sql) == 4 and all("remove_retention_policy" in s for s in sql)
