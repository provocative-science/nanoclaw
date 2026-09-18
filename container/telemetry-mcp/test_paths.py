"""Unit tests for plant telemetry path/SQL helpers (no S3)."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from paths import (
    assert_read_only_sql,
    day_range,
    glob_s3_url,
    hive_prefix,
    parse_hive_key,
    parse_timestamp,
    resolve_host,
    s3_endpoint_host,
    strip_outbound_proxy,
)


class ResolveHostTests(unittest.TestCase):
    def test_alias_airbed(self):
        self.assertEqual(resolve_host("airbed"), "airbed-host")

    def test_known_host_wins(self):
        self.assertEqual(resolve_host("airbed", ["airbed"]), "airbed")

    def test_container_alias(self):
        self.assertEqual(resolve_host("container-ship"), "bop-host")

    def test_empty(self):
        with self.assertRaises(ValueError):
            resolve_host("  ")


class HiveKeyTests(unittest.TestCase):
    def test_parse(self):
        key = (
            "logs/control/telemetry/host=airbed-host/year=2026/month=08/day=30/"
            "1789682659-2f941b4c-16e1-4742-8a64-9cfb28333374.parquet"
        )
        meta = parse_hive_key(key)
        assert meta is not None
        self.assertEqual(meta["component"], "control")
        self.assertEqual(meta["stream"], "telemetry")
        self.assertEqual(meta["host"], "airbed-host")
        self.assertEqual(meta["day"], "30")

    def test_prefix_and_glob(self):
        d = date(2026, 8, 30)
        self.assertEqual(
            hive_prefix("control", "telemetry", "airbed-host", d),
            "logs/control/telemetry/host=airbed-host/year=2026/month=08/day=30/",
        )
        self.assertTrue(
            glob_s3_url("provocative-dac-dev-beef", "control", "telemetry", "airbed-host", d).endswith(
                "day=30/*.parquet"
            )
        )


class TimestampTests(unittest.TestCase):
    def test_date_only(self):
        dt = parse_timestamp("2026-08-30")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.date(), date(2026, 8, 30))

    def test_zulu(self):
        dt = parse_timestamp("2026-08-30T15:11:02Z")
        self.assertEqual(dt, datetime(2026, 8, 30, 15, 11, 2, tzinfo=timezone.utc))

    def test_day_range(self):
        start = parse_timestamp("2026-08-30")
        end = parse_timestamp("2026-09-01")
        days = day_range(start, end)
        self.assertEqual(days[0], date(2026, 8, 30))
        self.assertEqual(days[-1], date(2026, 9, 1))
        self.assertEqual(len(days), 3)


class SqlGuardTests(unittest.TestCase):
    def test_select_ok(self):
        self.assertTrue(assert_read_only_sql("SELECT 1").startswith("SELECT"))

    def test_with_ok(self):
        assert_read_only_sql("WITH x AS (SELECT 1) SELECT * FROM x")

    def test_rejects_copy(self):
        with self.assertRaises(ValueError):
            assert_read_only_sql("COPY t TO 'x.parquet'")

    def test_rejects_multi(self):
        with self.assertRaises(ValueError):
            assert_read_only_sql("SELECT 1; SELECT 2")

    def test_strips_comment(self):
        assert_read_only_sql("-- hi\nSELECT 1")


class ProxyStripTests(unittest.TestCase):
    def test_removes_onecli_style_proxy(self):
        env = {
            "HTTPS_PROXY": "http://x:token@127.0.0.1:10255",
            "http_proxy": "http://x:token@127.0.0.1:10255",
            "TELEMETRY_S3_BUCKET": "provocative-dac-dev-beef",
        }
        removed = strip_outbound_proxy(env)
        self.assertIn("HTTPS_PROXY", removed)
        self.assertIn("http_proxy", removed)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertEqual(env["TELEMETRY_S3_BUCKET"], "provocative-dac-dev-beef")


class EndpointTests(unittest.TestCase):
    def test_strips_scheme(self):
        self.assertEqual(
            s3_endpoint_host("https://s3.us-east-1.wasabisys.com"),
            "s3.us-east-1.wasabisys.com",
        )


if __name__ == "__main__":
    unittest.main()
