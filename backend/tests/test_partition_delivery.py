"""分区交付场景回归：南北双任务、北区缺明细、修复后补算、文件对账。"""
from __future__ import annotations

import unittest
from unittest import mock

from app.services import service_for
from app.services import delivery_support
from app.services.delivery_support import PartitionCatalog


class PartitionDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        catalog = PartitionCatalog()
        patcher = mock.patch.object(
            delivery_support, "_CATALOG", catalog,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.export = service_for("adhoc_export")

    def _create(self, task_ids=(4, 5)):
        created = self.export.create_delivery_batch(list(task_ids), month="202610")
        self.assertTrue(created["ok"], created.get("message"))
        return created["batch"]["batch_id"]

    def test_ready_zone_delivered_first_and_incomplete_zone_waits(self) -> None:
        batch_id = self._create()
        result = self.export.dispatch_delivery_batch(batch_id)

        self.assertEqual([item["zone"] for item in result["delivered"]], ["南区"])
        self.assertEqual([item["zone"] for item in result["pending"]], ["北区"])
        self.assertIn("N-004", result["pending"][0]["reason"])
        self.assertEqual(result["scan_count"], 1)

        batch = self.export.get_delivery_batch(batch_id)["batch"]
        self.assertEqual(len(batch["artifacts"]), 1)
        self.assertEqual(batch["artifacts"][0]["zone"], "南区")
        north = [p for p in batch["partitions"] if p["zone"] == "北区"][0]
        self.assertEqual(north["status"], "待补算")
        self.assertEqual(north["coverage"]["missing"], 1)

    def test_retry_after_repair_only_recomputes_missing_zone(self) -> None:
        batch_id = self._create()
        self.export.dispatch_delivery_batch(batch_id)
        delivery_support.get_partition_catalog().repair("北区")

        retried = self.export.retry_delivery_batch(batch_id)
        self.assertEqual([item["zone"] for item in retried["recomputed"]], ["北区"])
        self.assertEqual([item["zone"] for item in retried["untouched"]], ["南区"])
        self.assertTrue(retried["fully_delivered"])
        self.assertEqual(retried["scan_count"], 2)

        batch = self.export.get_delivery_batch(batch_id)["batch"]
        north = [a for a in batch["artifacts"] if a["zone"] == "北区"][0]
        self.assertEqual(north["row_count"], 4)
        self.assertEqual(north["version"], 1)

    def test_reconcile_uses_files_for_cross_zone_totals(self) -> None:
        batch_id = self._create()
        self.export.dispatch_delivery_batch(batch_id)
        delivery_support.get_partition_catalog().repair("北区")
        self.export.retry_delivery_batch(batch_id)

        report = self.export.reconcile_delivery_batch(batch_id)
        self.assertTrue(report["ok"])
        self.assertEqual(report["cross_zone"]["combined_totals"], {"GMV": 859000, "订单数": 2650})
        self.assertTrue(report["cross_zone"]["matches_upstream"])
        self.assertEqual(
            report["cross_zone"]["zones"]["北区"], {"GMV": 407000, "订单数": 1250},
        )

    def test_reconcile_reports_incomplete_batch_before_repair(self) -> None:
        batch_id = self._create()
        self.export.dispatch_delivery_batch(batch_id)
        report = self.export.reconcile_delivery_batch(batch_id)
        self.assertFalse(report["ok"])
        self.assertEqual(report["pending_zones"], ["北区"])
        self.assertFalse(report["cross_zone"]["matches_upstream"])

    def test_tampered_artifact_is_detected_and_recomputed(self) -> None:
        batch_id = self._create()
        self.export.dispatch_delivery_batch(batch_id)
        delivery_support.get_partition_catalog().repair("北区")
        self.export.retry_delivery_batch(batch_id)

        south = [
            a for a in self.export.get_delivery_batch(batch_id)["batch"]["artifacts"]
            if a["zone"] == "南区" and a["status"] == "有效"
        ][0]
        with open(south["file_path"], "a", encoding="utf-8") as handle:
            handle.write("S-999,202610,南区,伪造,999999,9999\n")

        report = self.export.reconcile_delivery_batch(batch_id)
        self.assertFalse(report["ok"])
        south_check = [c for c in report["checks"] if c["zone"] == "南区"][0]
        self.assertFalse(south_check["valid"])
        self.assertFalse(south_check["checksum_ok"])

        retried = self.export.retry_delivery_batch(batch_id)
        self.assertEqual([x["zone"] for x in retried["recomputed"]], ["南区"])
        self.assertIn("校验和", retried["recomputed"][0]["recompute_reason"])
        self.assertTrue(self.export.reconcile_delivery_batch(batch_id)["ok"])

    def test_mixed_calibers_or_unapproved_cube_are_rejected(self) -> None:
        task, _ = self.export.create_entry({
            "任务名称": "北区行为明细",
            "查询SQL": "SELECT city FROM cube_user_behavior WHERE month='202610'",
            "导出格式": "csv", "行数限制": "1000", "是否压缩": "否",
            "分区过滤": "month='202610';核算区='北区'", "创建时间": "2026-11-21",
        })
        rejected = self.export.create_delivery_batch([4, task["id"]], month="202610")
        self.assertFalse(rejected["ok"])
        self.assertIn("未登记口径", rejected["message"])

    def test_duplicate_zone_claim_is_rejected(self) -> None:
        task, _ = self.export.create_entry({
            "任务名称": "北区重复任务",
            "查询SQL": "SELECT 商品类目, SUM(GMV) FROM cube_trade WHERE month='202610'",
            "导出格式": "csv", "行数限制": "1000", "是否压缩": "否",
            "分区过滤": "month='202610';核算区='北区'", "创建时间": "2026-11-21",
        })
        rejected = self.export.create_delivery_batch([4, task["id"]], month="202610")
        self.assertFalse(rejected["ok"])
        self.assertIn("重复认领", rejected["message"])


if __name__ == "__main__":
    unittest.main()
