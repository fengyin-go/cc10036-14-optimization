"""分区交付支撑：上游分区明细、输入指纹、产物文件和对账读取。

这里只放无状态的支撑逻辑，批次状态仍由 `adhoc_export` 领域服务维护。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from typing import Any

# 经过核对、允许交付的口径版本。未登记的口径一律不允许进入同一个交付批。
APPROVED_CALIBERS: dict[str, str] = {
    "trade_gmv_v3": "GMV=已完成订单实付金额之和",
}

# 切片模板/立方到口径版本的映射：交付批只能使用已核对口径。
CUBE_CALIBERS: dict[str, str] = {
    "交易分析立方": "trade_gmv_v3",
}

# 导出任务 SQL 里的表名/别名到立方名称，用于找到关联的切片模板。
SQL_CUBE_ALIASES: dict[str, str] = {
    "cube_trade": "交易分析立方",
    "dwd_trade_fact": "交易分析立方",
    "cube_user_behavior": "用户行为分析立方",
    "dwd_user_behavior_log": "用户行为分析立方",
    "cube_funnel": "用户增长漏斗立方",
    "dwd_funnel_events": "用户增长漏斗立方",
}

# 上游分区明细：同一批南北两个核算区各自独立，明细粒度到类目。
BASE_DETAILS: dict[str, list[dict[str, Any]]] = {
    "北区": [
        {"detail_id": "N-001", "月份": "202610", "核算区": "北区", "商品类目": "家电", "GMV": 120000, "订单数": 320},
        {"detail_id": "N-002", "月份": "202610", "核算区": "北区", "商品类目": "服饰", "GMV": 86000, "订单数": 540},
        {"detail_id": "N-003", "月份": "202610", "核算区": "北区", "商品类目": "数码", "GMV": 154000, "订单数": 210},
        {"detail_id": "N-004", "月份": "202610", "核算区": "北区", "商品类目": "家居", "GMV": 47000, "订单数": 180},
    ],
    "南区": [
        {"detail_id": "S-001", "月份": "202610", "核算区": "南区", "商品类目": "家电", "GMV": 138000, "订单数": 350},
        {"detail_id": "S-002", "月份": "202610", "核算区": "南区", "商品类目": "服饰", "GMV": 92000, "订单数": 610},
        {"detail_id": "S-003", "月份": "202610", "核算区": "南区", "商品类目": "数码", "GMV": 171000, "订单数": 240},
        {"detail_id": "S-004", "月份": "202610", "核算区": "南区", "商品类目": "家居", "GMV": 51000, "订单数": 200},
    ],
}

# 初始上游输入缺失：北区家居明细尚未到齐，南区已经算好。
INITIAL_MISSING: dict[str, set[str]] = {"北区": {"N-004"}}

ARTIFACT_COLUMNS = ["detail_id", "月份", "核算区", "商品类目", "GMV", "订单数"]


def fingerprint_rows(rows: list[dict[str, Any]]) -> str:
    """对输入明细做稳定指纹，用来证明产物是否仍对应当前上游输入。"""
    canonical = json.dumps(
        sorted(rows, key=lambda row: str(row.get("detail_id", ""))),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def artifact_dir() -> str:
    """产物目录：默认落到临时目录，可用 EXPORT_ARTIFACT_DIR 固定下来备查。"""
    root = os.environ.get("EXPORT_ARTIFACT_DIR") or os.path.join(
        tempfile.gettempdir(), "multi_analysis_delivery"
    )
    os.makedirs(root, exist_ok=True)
    return root


def write_artifact_csv(batch_id: int, artifact_id: int, zone: str,
                       rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把分区结果写成可被对账方直接读取的 CSV 文件。"""
    directory = os.path.join(artifact_dir(), f"batch_{batch_id}")
    os.makedirs(directory, exist_ok=True)
    file_path = os.path.join(directory, f"artifact_{artifact_id}_{zone}.csv")

    output = io.StringIO()
    writer = csv.DictWriter(
        output, fieldnames=ARTIFACT_COLUMNS, lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in ARTIFACT_COLUMNS})
    content = output.getvalue()
    with open(file_path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(content)

    with open(file_path, "rb") as handle:
        checksum = hashlib.sha256(handle.read()).hexdigest()
    return {
        "file_path": file_path,
        "file_size_bytes": os.path.getsize(file_path),
        "checksum": checksum,
        "row_count": len(rows),
    }


def write_artifact_manifest(batch_id: int, artifact_id: int,
                            metadata: dict[str, Any]) -> str:
    directory = os.path.join(artifact_dir(), f"batch_{batch_id}")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"artifact_{artifact_id}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return path


def read_artifact_csv(file_path: str) -> dict[str, Any]:
    """对账只从文件读数，避免回读内存里的切片结果造成口径漂移。"""
    with open(file_path, "rb") as handle:
        checksum = hashlib.sha256(handle.read()).hexdigest()
    with open(file_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]

    totals: dict[str, int] = {}
    for row in rows:
        for measure in ("GMV", "订单数"):
            raw = row.get(measure, "0")
            try:
                totals[measure] = totals.get(measure, 0) + int(float(raw))
            except (TypeError, ValueError):
                totals[measure] = totals.get(measure, 0)
    return {
        "rows": rows,
        "row_count": len(rows),
        "checksum": checksum,
        "totals": totals,
    }


def totals_from_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {"GMV": 0, "订单数": 0}
    for row in rows:
        for measure in totals:
            try:
                totals[measure] += int(float(row.get(measure, 0)))
            except (TypeError, ValueError):
                pass
    return totals


class PartitionCatalog:
    """上游分区明细目录：记录哪些明细已经可用，以及输入被修复过几次。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._missing: dict[str, set[str]] = {
            zone: set(ids) for zone, ids in INITIAL_MISSING.items()
        }
        self._revision: dict[str, int] = {zone: 0 for zone in BASE_DETAILS}
        self._repairs: list[dict[str, Any]] = []

    def zones(self) -> list[str]:
        return list(BASE_DETAILS)

    def expected_rows(self, zone: str, month: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in BASE_DETAILS.get(zone, [])]
        if month:
            rows = [row for row in rows if str(row.get("月份")) == month]
        return rows

    def available_rows(self, zone: str, month: str | None = None) -> list[dict[str, Any]]:
        missing = self._missing.get(zone, set())
        return [
            dict(row) for row in self.expected_rows(zone, month)
            if str(row.get("detail_id")) not in missing
        ]

    def missing_ids(self, zone: str, month: str | None = None) -> list[str]:
        return [
            str(row.get("detail_id")) for row in self.expected_rows(zone, month)
            if str(row.get("detail_id")) in self._missing.get(zone, set())
        ]

    def fingerprint(self, zone: str, month: str | None = None) -> str:
        return fingerprint_rows(self.available_rows(zone, month))

    def revision(self, zone: str) -> int:
        return self._revision.get(zone, 0)

    def repairs(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._repairs]

    def repair(self, zone: str, detail_ids: list[str] | None = None) -> dict[str, Any]:
        """修复上游输入：默认补齐该区所有缺失明细，也可只补指定明细。"""
        if zone not in BASE_DETAILS:
            return {"ok": False, "message": f"未知核算区：{zone}"}

        missing_before = self.missing_ids(zone)
        targets = set(detail_ids) if detail_ids else set(missing_before)
        restored: list[str] = []
        for detail_id in sorted(targets):
            if detail_id in self._missing.get(zone, set()):
                self._missing[zone].discard(detail_id)
                restored.append(detail_id)
        self._revision[zone] = self._revision.get(zone, 0) + 1
        record = {
            "zone": zone,
            "restored_detail_ids": restored,
            "missing_before": missing_before,
            "missing_after": self.missing_ids(zone),
            "input_revision": self._revision[zone],
        }
        self._repairs.append(record)
        return {"ok": True, **record}


_CATALOG = PartitionCatalog()


def get_partition_catalog() -> PartitionCatalog:
    """进程内共享的上游分区目录，供切片查询和即席导出共同消费。"""
    return _CATALOG
