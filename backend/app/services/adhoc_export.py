"""即席导出领域服务：异步任务调度、文件生成、格式转换、分区交付与对账。"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
from typing import Any

from app.services.base import BaseModuleService
from app.services.delivery_support import (
    APPROVED_CALIBERS,
    CUBE_CALIBERS,
    SQL_CUBE_ALIASES,
    get_partition_catalog,
    read_artifact_csv,
    totals_from_rows,
    write_artifact_csv,
    write_artifact_manifest,
)
from app.store import store
from app.analytics_engine import ExportScheduler


class AdhocExportService(BaseModuleService):
    MODULE = "adhoc_export"
    ENTITY = "导出任务"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["任务名称", "查询SQL", "导出格式", "行数限制", "是否压缩", "分区过滤", "创建时间"]
    REQUIRED_FIELDS = ["任务名称", "查询SQL", "导出格式"]
    STATUS_ORDER = ["排队中", "执行中", "已完成", "导出失败", "已过期"]
    ACTION_RULES = {"开始导出": "执行中", "完成导出": "已完成", "标记失败": "导出失败", "清理过期": "已过期"}
    NEGATIVE_ACTIONS = ["标记失败"]

    def __init__(self) -> None:
        super().__init__()
        self._scheduler = ExportScheduler(max_concurrent=3)
        self._batches: dict[int, dict[str, Any]] = {}
        self._next_batch_id = 1
        self._next_artifact_id = 1
        # 切片分析缓存：同一条明细输入不重复扫描上游。
        self._analysis_cache: dict[tuple, dict[str, Any]] = {}
        self._scan_count = 0

    # ---- 业务方法 ----

    def submit_export(
        self, task_id: int, query_data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """提交导出任务并异步执行。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "message": f"导出任务 {task_id} 不存在"}

        export_format = str(task.get("导出格式", "csv")).lower()
        row_limit = _parse_int(task.get("行数限制"), 1000)
        compressed = str(task.get("是否压缩", "否")).lower() in ("是", "true", "yes", "1")

        self._scheduler.enqueue(
            task_id=str(task_id),
            priority=1,
            sql=str(task.get("查询SQL", "")),
            format=export_format,
        )

        self.run_action(task_id, "开始导出")

        # 同步执行导出（简化版）
        try:
            file_content = _generate_export_file(
                query_data[:row_limit], export_format, compressed,
            )
            self.run_action(task_id, "完成导出")
            return {
                "ok": True,
                "task_id": task_id,
                "task_name": str(task.get("任务名称", "")),
                "format": export_format,
                "row_count": min(len(query_data), row_limit),
                "compressed": compressed,
                "file_size_bytes": len(file_content) if isinstance(file_content, bytes) else len(file_content.encode()),
                "content_preview": (
                    file_content[:500] if isinstance(file_content, str)
                    else file_content[:500].decode("utf-8", errors="replace")
                ),
            }
        except Exception as exc:
            self.run_action(task_id, "标记失败")
            return {"ok": False, "message": f"导出失败: {exc}"}

    def batch_export(
        self, task_ids: list[int], query_data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """批量提交导出任务。"""
        results: list[dict[str, Any]] = []
        for tid in task_ids:
            result = self.submit_export(tid, query_data)
            results.append(result)
        success = sum(1 for r in results if r.get("ok"))
        return {
            "ok": True,
            "total_tasks": len(task_ids),
            "success_count": success,
            "fail_count": len(task_ids) - success,
            "results": results,
        }

    # ---- 分区交付批：多任务 → 各自分区 → 可对账产物 ----

    def create_delivery_batch(
        self, task_ids: list[int], *,
        batch_name: str = "", month: str | None = None,
    ) -> dict[str, Any]:
        """登记一个交付批：先核对口径，再把每个任务绑定到自己的分区。

        口径未登记、或同一批里出现不同口径时整批拒绝，不落任何交付状态，
        避免把未经核对的数据混装进同一批产物。
        """
        if not task_ids:
            return {"ok": False, "message": "交付批至少需要一个导出任务"}

        bindings: list[dict[str, Any]] = []
        errors: list[str] = []
        calibers: set[str] = set()

        for task_id in task_ids:
            task = store.find(self.MODULE, task_id)
            if task is None:
                errors.append(f"导出任务 {task_id} 不存在")
                continue
            binding = self._resolve_task_binding(task, month)
            if not binding.get("ok"):
                errors.append(f"任务 {task_id}：{binding.get('message')}")
                continue
            calibers.add(binding["caliber_version"])
            bindings.append(binding)

        if errors:
            return {"ok": False, "message": "；".join(errors), "calibers": sorted(calibers)}

        unknown = [c for c in calibers if c not in APPROVED_CALIBERS]
        if unknown:
            return {
                "ok": False,
                "message": f"存在未核对口径 {','.join(sorted(unknown))}，整批不允许交付",
                "calibers": sorted(calibers),
            }
        if len(calibers) > 1:
            return {
                "ok": False,
                "message": f"同一交付批混装不同口径 {','.join(sorted(calibers))}，整批不允许交付",
                "calibers": sorted(calibers),
            }

        caliber = next(iter(calibers))
        seen_zones: dict[str, int] = {}
        for binding in bindings:
            zone = binding["zone"]
            if zone in seen_zones:
                return {
                    "ok": False,
                    "message": f"核算区 {zone} 被任务 {seen_zones[zone]} 和 {binding['task_id']} 重复认领",
                }
            seen_zones[zone] = binding["task_id"]

        batch_id = self._next_batch_id
        self._next_batch_id += 1
        batch = {
            "batch_id": batch_id,
            "batch_name": batch_name or f"分区交付批-{batch_id}",
            "month": month or bindings[0].get("month"),
            "caliber_version": caliber,
            "caliber_desc": APPROVED_CALIBERS[caliber],
            "task_ids": [b["task_id"] for b in bindings],
            "bindings": bindings,
            "partitions": {},
            "artifacts": [],
            "events": [],
            "status": "已登记",
            "created_at": _now(),
        }
        self._batches[batch_id] = batch
        self._log(batch, "登记交付批", {
            "task_ids": batch["task_ids"], "caliber_version": caliber,
        })
        return {"ok": True, "batch": self._batch_view(batch)}

    def get_delivery_batch(self, batch_id: int) -> dict[str, Any]:
        batch = self._batches.get(batch_id)
        if batch is None:
            return {"ok": False, "message": f"交付批 {batch_id} 不存在"}
        return {"ok": True, "batch": self._batch_view(batch)}

    def list_delivery_batches(self) -> dict[str, Any]:
        return {
            "ok": True,
            "items": [self._batch_view(batch) for batch in self._batches.values()],
        }

    def dispatch_delivery_batch(
        self, batch_id: int, task_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """领取并执行交付批里已齐备的分区；未齐的分区保持待补算。"""
        batch = self._batches.get(batch_id)
        if batch is None:
            return {"ok": False, "message": f"交付批 {batch_id} 不存在"}

        guard = self._caliber_guard(batch)
        if not guard.get("ok"):
            self._log(batch, "口径校验失败", guard)
            return guard

        selected = set(task_ids) if task_ids else set(batch["task_ids"])
        results: list[dict[str, Any]] = []
        for binding in batch["bindings"]:
            if binding["task_id"] not in selected:
                continue
            results.append(self._deliver_binding(batch, binding))

        delivered = [r for r in results if r.get("delivered")]
        pending = [r for r in results if not r.get("delivered")]
        batch["status"] = "已完成" if not pending else "部分交付"
        self._log(batch, "领取交付批", {
            "delivered_zones": [r["zone"] for r in delivered],
            "pending_zones": [r["zone"] for r in pending],
            "scan_count": self._scan_count,
        })
        return {
            "ok": True,
            "batch_id": batch_id,
            "caliber_version": batch["caliber_version"],
            "delivered": delivered,
            "pending": pending,
            "fully_delivered": not pending,
            "scan_count": self._scan_count,
            "batch": self._batch_view(batch),
        }

    def retry_delivery_batch(self, batch_id: int) -> dict[str, Any]:
        """失败后重试：只补算仍缺明细或产物与输入指纹不一致的分区。"""
        batch = self._batches.get(batch_id)
        if batch is None:
            return {"ok": False, "message": f"交付批 {batch_id} 不存在"}

        guard = self._caliber_guard(batch)
        if not guard.get("ok"):
            self._log(batch, "口径校验失败", guard)
            return guard

        recomputed: list[dict[str, Any]] = []
        still_pending: list[dict[str, Any]] = []
        untouched: list[dict[str, Any]] = []
        for binding in batch["bindings"]:
            state = batch["partitions"].get(binding["zone"])
            stale_reason = self._stale_reason(batch, binding)
            if state and state.get("status") == "已交付" and not stale_reason:
                untouched.append({
                    "task_id": binding["task_id"], "zone": binding["zone"],
                    "artifact_id": state.get("latest_artifact_id"),
                    "reason": "产物指纹和文件校验仍与当前一致，无需补算",
                })
                continue
            result = self._deliver_binding(batch, binding, retry=True)
            result["recompute_reason"] = stale_reason or "尚未产出完整产物"
            if result.get("delivered"):
                recomputed.append(result)
            else:
                still_pending.append(result)

        pending_left = [s for s in batch["partitions"].values() if s.get("status") != "已交付"]
        batch["status"] = "已完成" if not pending_left else "部分交付"
        self._log(batch, "重试补算", {
            "recomputed_zones": [r["zone"] for r in recomputed],
            "still_pending_zones": [r["zone"] for r in still_pending],
            "untouched_zones": [r["zone"] for r in untouched],
        })
        return {
            "ok": True,
            "batch_id": batch_id,
            "recomputed": recomputed,
            "still_pending": still_pending,
            "untouched": untouched,
            "fully_delivered": not pending_left,
            "scan_count": self._scan_count,
            "batch": self._batch_view(batch),
        }

    def reconcile_delivery_batch(self, batch_id: int) -> dict[str, Any]:
        """对账：逐个核验产物文件，并只用文件计算跨区总账。"""
        batch = self._batches.get(batch_id)
        if batch is None:
            return {"ok": False, "message": f"交付批 {batch_id} 不存在"}

        checks: list[dict[str, Any]] = []
        combined_totals: dict[str, int] = {}
        combined_rows = 0
        from_files_ok = True

        for binding in batch["bindings"]:
            artifact = self._latest_artifact(batch, binding["task_id"], binding["zone"])
            if artifact is None:
                checks.append({
                    "zone": binding["zone"],
                    "task_id": binding["task_id"],
                    "delivered": False,
                    "reason": "该分区尚未产出完整产物，不参与总账",
                })
                from_files_ok = False
                continue

            file_check = read_artifact_csv(artifact["file_path"])
            checksum_ok = file_check["checksum"] == artifact["checksum"]
            row_count_ok = file_check["row_count"] == artifact["row_count"]
            current_fp = self._current_fingerprint(binding)
            fingerprint_ok = current_fp == artifact["input_fingerprint"]
            caliber_ok = artifact["caliber_version"] == batch["caliber_version"]

            if not checksum_ok:
                validity_reason = (
                    f"文件校验和 {file_check['checksum'][:12]} 与登记值 {artifact['checksum'][:12]} 不符，"
                    f"产物被改动，需重算"
                )
            elif not row_count_ok:
                validity_reason = (
                    f"文件行数 {file_check['row_count']} 与登记行数 {artifact['row_count']} 不符，需重算"
                )
            elif not fingerprint_ok:
                validity_reason = (
                    f"上游输入已变为 {current_fp}，产物仍是 {artifact['input_fingerprint']}，需重算"
                )
            elif not caliber_ok:
                validity_reason = (
                    f"产物口径 {artifact['caliber_version']} 与批次口径 {batch['caliber_version']} 不一致"
                )
            else:
                validity_reason = (
                    f"产物输入指纹 {artifact['input_fingerprint']} 与当前上游一致"
                    f"（输入修订 {artifact['input_revision']}），文件校验和与行数均核对通过"
                )

            valid = checksum_ok and row_count_ok and fingerprint_ok and caliber_ok
            check = {
                "zone": binding["zone"],
                "task_id": binding["task_id"],
                "delivered": True,
                "artifact_id": artifact["artifact_id"],
                "artifact_version": artifact["version"],
                "file_path": artifact["file_path"],
                "checksum": artifact["checksum"],
                "checksum_ok": checksum_ok,
                "row_count": artifact["row_count"],
                "file_row_count": file_check["row_count"],
                "row_count_ok": row_count_ok,
                "input_fingerprint": artifact["input_fingerprint"],
                "current_input_fingerprint": current_fp,
                "fingerprint_ok": fingerprint_ok,
                "caliber_version": artifact["caliber_version"],
                "caliber_ok": caliber_ok,
                "totals": file_check["totals"],
                "valid": valid,
                "reason": validity_reason,
            }
            checks.append(check)
            if valid:
                combined_rows += file_check["row_count"]
                for measure, value in file_check["totals"].items():
                    combined_totals[measure] = combined_totals.get(measure, 0) + value
            else:
                from_files_ok = False

        upstream_totals = self._upstream_totals(batch)
        zone_file_totals = {
            check["zone"]: check["totals"] for check in checks if check.get("delivered")
        }
        cross_zone = {
            "zones": zone_file_totals,
            "combined_row_count": combined_rows,
            "combined_totals": combined_totals,
            "upstream_totals": upstream_totals,
            "matches_upstream": _totals_equal(combined_totals, upstream_totals),
        }

        pending_zones = [
            binding["zone"] for binding in batch["bindings"]
            if batch["partitions"].get(binding["zone"], {}).get("status") != "已交付"
        ]
        return {
            "ok": from_files_ok,
            "batch_id": batch_id,
            "caliber_version": batch["caliber_version"],
            "checks": checks,
            "cross_zone": cross_zone,
            "pending_zones": pending_zones,
            "message": (
                "各分区产物均可核验，跨区总账与上游一致"
                if from_files_ok and cross_zone["matches_upstream"]
                else "存在未交付或不可核验的分区，跨区总账尚不完整"
            ),
        }

    # ---- 交付批内部 ----

    def _resolve_task_binding(
        self, task: dict[str, Any], month_override: str | None,
    ) -> dict[str, Any]:
        task_id = int(task.get("id", 0))
        partition_filter = str(task.get("分区过滤", ""))
        sql = str(task.get("查询SQL", ""))

        zone = _partition_value(partition_filter, "核算区") or _partition_value(sql, "核算区")
        month = month_override or _partition_value(partition_filter, "month") or _partition_value(
            partition_filter, "月份"
        ) or _partition_value(sql, "month")
        if not zone:
            return {"ok": False, "task_id": task_id, "message": "无法从分区过滤确定核算区"}
        if not month:
            return {"ok": False, "task_id": task_id, "message": "无法从分区过滤确定月份"}

        cube = _detect_cube(sql) or _detect_cube(partition_filter)
        if not cube:
            return {"ok": False, "task_id": task_id, "message": "查询SQL 未关联到已知数据立方"}

        template = _pick_template(cube, month)
        if template is None:
            return {
                "ok": False, "task_id": task_id,
                "message": f"立方 {cube} 没有可用的已发布切片模板",
            }

        caliber = CUBE_CALIBERS.get(cube, "")
        if not caliber:
            return {
                "ok": False, "task_id": task_id,
                "message": f"立方 {cube} 未登记口径版本，不能进入交付批",
            }

        return {
            "ok": True,
            "task_id": task_id,
            "task_name": str(task.get("任务名称", "")),
            "zone": zone,
            "month": month,
            "cube_name": cube,
            "template_id": int(template["id"]),
            "template_name": str(template.get("模板名称", "")),
            "caliber_version": caliber,
        }

    def _deliver_binding(
        self, batch: dict[str, Any], binding: dict[str, Any], *,
        retry: bool = False,
    ) -> dict[str, Any]:
        zone = binding["zone"]
        task_id = binding["task_id"]
        analysis = self._analyze(binding)
        partition = next(
            (p for p in analysis["partitions"] if p["zone"] == zone), None
        )
        if partition is None:
            return {
                "task_id": task_id, "zone": zone, "delivered": False,
                "reason": "切片分析未返回该分区",
            }

        state = batch["partitions"].setdefault(zone, {
            "zone": zone,
            "task_id": task_id,
            "status": "待补算",
            "latest_artifact_id": None,
            "coverage": {},
            "attempts": 0,
        })
        state["attempts"] = int(state.get("attempts", 0)) + 1
        state["coverage"] = partition["coverage"]
        state["missing_detail_ids"] = partition["missing_detail_ids"]
        state["included_detail_ids"] = partition["included_detail_ids"]
        state["input_fingerprint"] = partition["input_fingerprint"]

        task = store.find(self.MODULE, task_id)
        if task is not None and task.get("status") != "执行中":
            self.run_action(task_id, "开始导出")

        if not partition["ready"]:
            # 一个任务只能交付完整分区：缺明细时不写文件、不登记产物。
            state["status"] = "待补算"
            state["reason"] = partition["reason"]
            self._log(batch, "分区未齐备", {
                "zone": zone, "task_id": task_id,
                "missing_detail_ids": partition["missing_detail_ids"],
                "coverage": partition["coverage"],
            })
            return {
                "task_id": task_id,
                "zone": zone,
                "delivered": False,
                "status": "待补算",
                "reason": partition["reason"],
                "coverage": partition["coverage"],
                "missing_detail_ids": partition["missing_detail_ids"],
            }

        version = sum(
            1 for a in batch["artifacts"]
            if a["task_id"] == task_id and a["zone"] == zone
        ) + 1
        artifact_id = self._next_artifact_id
        self._next_artifact_id += 1

        file_meta = write_artifact_csv(batch["batch_id"], artifact_id, zone, partition["rows"])
        artifact = {
            "artifact_id": artifact_id,
            "task_id": task_id,
            "task_name": binding["task_name"],
            "zone": zone,
            "month": binding["month"],
            "version": version,
            "template_id": binding["template_id"],
            "template_name": binding["template_name"],
            "caliber_version": batch["caliber_version"],
            "input_fingerprint": partition["input_fingerprint"],
            "input_revision": partition["input_revision"],
            "included_detail_ids": partition["included_detail_ids"],
            "row_count": file_meta["row_count"],
            "file_path": file_meta["file_path"],
            "file_size_bytes": file_meta["file_size_bytes"],
            "checksum": file_meta["checksum"],
            "created_at": _now(),
            "status": "有效",
        }
        for previous in batch["artifacts"]:
            if (
                previous["task_id"] == task_id and previous["zone"] == zone
                and previous["status"] == "有效"
            ):
                previous["status"] = "已替换"
        batch["artifacts"].append(artifact)

        manifest_path = write_artifact_manifest(batch["batch_id"], artifact_id, artifact)
        artifact["manifest_path"] = manifest_path

        state.update({
            "status": "已交付",
            "reason": "",
            "latest_artifact_id": artifact_id,
            "artifact_version": version,
            "row_count": artifact["row_count"],
        })
        if task is not None:
            self.run_action(task_id, "完成导出")

        self._log(batch, "分区交付" if not retry else "分区补算交付", {
            "zone": zone,
            "task_id": task_id,
            "artifact_id": artifact_id,
            "artifact_version": version,
            "row_count": artifact["row_count"],
            "included_detail_ids": partition["included_detail_ids"],
        })
        return {
            "task_id": task_id,
            "zone": zone,
            "delivered": True,
            "status": "已交付",
            "artifact_id": artifact_id,
            "artifact_version": version,
            "row_count": artifact["row_count"],
            "file_path": artifact["file_path"],
            "checksum": artifact["checksum"],
            "coverage": partition["coverage"],
            "included_detail_ids": partition["included_detail_ids"],
        }

    def _analyze(self, binding: dict[str, Any]) -> dict[str, Any]:
        """调用切片查询生成带覆盖信息的分析结果。

        同模板同月只扫描一次上游，一次调用就把所有核算区的覆盖情况算齐，
        同批多个分区任务共享这份结果，避免逐任务重复扫描。
        """
        from app.services import service_for

        slice_svc = service_for("slice_query")
        if slice_svc is None:
            raise RuntimeError("切片查询服务未就绪")

        catalog = get_partition_catalog()
        month = binding["month"]
        key = (
            binding["template_id"], month,
            tuple((zone, catalog.fingerprint(zone, month)) for zone in catalog.zones()),
        )
        cached = self._analysis_cache.get(key)
        if cached is None:
            self._scan_count += 1
            cached = slice_svc.analyze_partitions(
                binding["template_id"], zones=catalog.zones(), month=month,
            )
            self._analysis_cache[key] = cached
        return cached

    def _current_fingerprint(self, binding: dict[str, Any]) -> str:
        return get_partition_catalog().fingerprint(binding["zone"], binding["month"])

    def _stale_reason(self, batch: dict[str, Any], binding: dict[str, Any]) -> str:
        artifact = self._latest_artifact(batch, binding["task_id"], binding["zone"])
        if artifact is None:
            return "尚无产物"
        if artifact["input_fingerprint"] != self._current_fingerprint(binding):
            return "上游输入已变化"
        try:
            file_check = read_artifact_csv(artifact["file_path"])
        except OSError:
            return "产物文件不可读"
        if file_check["checksum"] != artifact["checksum"]:
            return "产物文件校验和不符"
        if file_check["row_count"] != artifact["row_count"]:
            return "产物文件行数与登记不符"
        return ""

    def _latest_artifact(
        self, batch: dict[str, Any], task_id: int, zone: str,
    ) -> dict[str, Any] | None:
        for artifact in reversed(batch["artifacts"]):
            if artifact["task_id"] == task_id and artifact["zone"] == zone:
                return artifact
        return None

    def _upstream_totals(self, batch: dict[str, Any]) -> dict[str, int]:
        """按当前上游明细独立复算，用作文件总账的对照值。"""
        catalog = get_partition_catalog()
        rows: list[dict[str, Any]] = []
        for binding in batch["bindings"]:
            rows.extend(catalog.available_rows(binding["zone"], binding["month"]))
        return totals_from_rows(rows)

    def _caliber_guard(self, batch: dict[str, Any]) -> dict[str, Any]:
        caliber = batch["caliber_version"]
        if caliber not in APPROVED_CALIBERS:
            return {"ok": False, "message": f"口径 {caliber} 未核对，交付批已冻结"}
        mismatched = [
            artifact["artifact_id"] for artifact in batch["artifacts"]
            if artifact["caliber_version"] != caliber
        ]
        if mismatched:
            return {
                "ok": False,
                "message": f"交付批内产物口径不一致：{mismatched}",
            }
        return {"ok": True}

    def _log(self, batch: dict[str, Any], event: str, detail: dict[str, Any]) -> None:
        batch["events"].append({"at": _now(), "event": event, "detail": detail})

    def _batch_view(self, batch: dict[str, Any]) -> dict[str, Any]:
        return {
            "batch_id": batch["batch_id"],
            "batch_name": batch["batch_name"],
            "month": batch["month"],
            "caliber_version": batch["caliber_version"],
            "caliber_desc": batch["caliber_desc"],
            "status": batch["status"],
            "created_at": batch["created_at"],
            "task_ids": list(batch["task_ids"]),
            "bindings": [dict(b) for b in batch["bindings"]],
            "partitions": [dict(p) for p in batch["partitions"].values()],
            "artifacts": [dict(a) for a in batch["artifacts"]],
            "events": [dict(e) for e in batch["events"]],
        }

    def get_queue_status(self) -> dict[str, Any]:
        """获取导出队列状态。"""
        return {
            "ok": True,
            "queue_size": self._scheduler.queue_size(),
            "running_count": self._scheduler.running_count(),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["artifact"] = "导出任务数据集"
        base["queue_size"] = self._scheduler.queue_size()
        base["delivery_batches"] = len(self._batches)
        return base


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _partition_value(text: str, key: str) -> str:
    """从 `k='v';k2=v2` 或 `WHERE k='v'` 里取参数值。"""
    if not text:
        return ""
    normalized = text.replace(" ", "")
    for part in normalized.replace("AND", ";").replace("and", ";").split(";"):
        if "=" not in part:
            continue
        raw_key, raw_val = part.split("=", 1)
        raw_key = raw_key.split("WHERE")[-1].split("where")[-1]
        if raw_key.strip("'\"`()") != key:
            continue
        return raw_val.strip().strip("'\"`()")
    return ""


def _detect_cube(sql: str) -> str:
    lowered = sql.lower()
    for alias, cube in SQL_CUBE_ALIASES.items():
        if alias.lower() in lowered:
            return cube
    return ""


def _pick_template(cube_name: str, month: str) -> dict[str, Any] | None:
    candidates = [
        row for row in store.rows("slice_query")
        if row.get("数据立方") == cube_name and row.get("status") == "已发布"
    ]
    if not candidates:
        return None
    for row in candidates:
        if month and month in str(row.get("查询维值", "")):
            return row
    return candidates[0]


def _totals_equal(left: dict[str, int], right: dict[str, int]) -> bool:
    keys = set(left) | set(right)
    return all(int(left.get(key, 0)) == int(right.get(key, 0)) for key in keys)


def _parse_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _generate_export_file(
    data: list[dict[str, Any]], format: str, compressed: bool,
) -> str | bytes:
    """生成导出文件内容。"""
    if not data:
        return "" if format == "csv" else "[]"

    if format == "csv":
        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(list(data[0].keys()))
        for row in data:
            writer.writerow(list(row.values()))
        return output.getvalue()

    elif format in ("json", "jsonl"):
        if format == "jsonl":
            return "\n".join(json.dumps(row) for row in data)
        return json.dumps(data, ensure_ascii=False, indent=2)

    elif format == "parquet":
        # 简化parquet: JSON序列化
        return json.dumps(data, ensure_ascii=False).encode()

    return json.dumps(data)
