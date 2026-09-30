"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Conflict, ValidationError, boolean, integer, moment, number, text


INITIAL_STATE = "detected"
RECTIFICATION_STATE = "rectification"
DEFAULT_SPLICE_BUDGET_DB = 0.5
SINGLE_SPLICE_LIMIT_DB = 0.2
TEST_LOSS_LIMIT_DB = 0.5
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'backfill': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced', 'rectification': 'spliced'}, 'backfill': {'spliced': 'spliced', 'tested': 'tested'}, 'test': {'spliced': 'tested', 'tested': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled', 'rectification': 'cancelled'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    RECTIFICATION_STATE = RECTIFICATION_STATE
    DEFAULT_SPLICE_BUDGET_DB = DEFAULT_SPLICE_BUDGET_DB
    SINGLE_SPLICE_LIMIT_DB = SINGLE_SPLICE_LIMIT_DB

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "cable")
        text(p, "segment")
        start = number(p, "start_km", 0)
        end = number(p, "end_km", 0)
        number(p, "depth_m", 1)
        integer(p, "sea_state", 0, 9)
        boolean(p, "vessel_available")
        number(p, "spare_length_km", 0)
        boolean(p, "permit_valid")
        integer(p, "capacity_gbps", 1)
        p["splice_loss_budget_db"] = number(p, "splice_loss_budget_db", 0) if "splice_loss_budget_db" in p and p["splice_loss_budget_db"] is not None else DEFAULT_SPLICE_BUDGET_DB
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        distance = float(p["end_km"]) - float(p["start_km"])
        p["repair_distance_km"] = round(distance, 2)
        p["required_spare_km"] = round(distance * 1.05, 2)
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + int(p["sea_state"]) * 2.0, 2)
        p["repair_feasible"] = bool(p["vessel_available"] and p["permit_valid"] and p["spare_length_km"] >= p["required_spare_km"] and int(p["sea_state"]) <= 5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"restored", "cancelled"} or item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        """无需接续档案上下文的动作（approve/mobilize/survey/cancel）。"""
        if action in {"splice", "backfill", "test", "restore"}:
            raise Conflict("%s必须通过接续档案用例执行" % action)
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "approve":
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            summary = "抢修方案已批准"
        elif action == "mobilize":
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            if float(data.get("available_spare_km", 0)) < float(p["required_spare_km"]):
                raise ValidationError("船上备缆不足")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["vessel_name"] = text(data, "vessel_name")
            summary = "抢修船已动员"
        elif action == "survey":
            if not boolean(data, "survey_complete"):
                raise ValidationError("勘察尚未完成")
            fault_km = number(data, "fault_location_km", 0)
            if not (float(p["start_km"]) <= fault_km <= float(p["end_km"])):
                raise ValidationError("故障点不在申报区段")
            changes["fault_location_km"] = fault_km
            summary = "故障点勘察完成"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ------------------------------------------------------------------
    # 接续档案相关规则
    # ------------------------------------------------------------------
    def prepare_splice_report(self, data: Dict[str, Any], engineer_id: str) -> Dict[str, Any]:
        """把现场上报整理为接续条目，单条阈值在此拦截。"""
        d = dict(data or {})
        loss = number(d, "splice_loss_db", 0)
        if loss > SINGLE_SPLICE_LIMIT_DB:
            raise ValidationError("接续损耗超过阈值")
        raw_engineer = d.get("engineer_id")
        engineer_id = raw_engineer.strip() if isinstance(raw_engineer, str) and raw_engineer.strip() else engineer_id
        item = {
            "occurred_at": moment(d, "occurred_at"),
            "splice_loss_db": loss,
            "engineer_id": engineer_id,
            "report_id": d.get("report_id").strip() if isinstance(d.get("report_id"), str) and d.get("report_id").strip() else None,
            "idempotency_key": d.get("idempotency_key").strip() if isinstance(d.get("idempotency_key"), str) and d.get("idempotency_key").strip() else None,
            "note": d.get("note").strip() if isinstance(d.get("note"), str) else "",
        }
        if "spare_used_km" in d and d["spare_used_km"] is not None:
            item["spare_used_km"] = number(d, "spare_used_km", 0)
        return item

    def accept_splice(self, record: Dict[str, Any], entry: Dict[str, Any], archive: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, float]:
        """接续确认入库后调用：按最新档案累计损耗判定工单走向。"""
        new_state = self.require_transition(record, "splice")
        p = dict(record["payload"])
        budget = float(p.get("splice_loss_budget_db", DEFAULT_SPLICE_BUDGET_DB))
        total = round(float(archive["total_loss_db"]), 6)
        changes: Dict[str, Any] = {
            "splice_loss_db": float(entry["splice_loss_db"]),
            "segment_splice_count": int(archive["confirmed_count"]),
            "segment_total_splice_loss_db": total,
            "splice_budget_db": budget,
        }
        if entry.get("spare_used_km") is not None:
            if float(entry["spare_used_km"]) < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            changes["spare_used_km"] = float(entry["spare_used_km"])
        p.update(changes)
        # 重新接续后原测试结果自然失效，清除测试标记
        for stale_key in ("end_to_end_loss_db", "test_passed", "archive_seq_at_test"):
            p.pop(stale_key, None)
        if total > budget:
            return RECTIFICATION_STATE, p, "接续已归档，区段累计损耗%s超过预算%s，退回待整改" % (total, budget), total
        return new_state, p, "光缆接续完成，区段累计损耗%s" % total, total

    def prepare_backfill_items(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        d = dict(data or {})
        raw_items = d.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError("items必须是非空历史接续列表")
        items: List[Dict[str, Any]] = []
        seen_times = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ValidationError("items中每一项必须是对象")
            item = {
                "occurred_at": moment(raw, "occurred_at"),
                "splice_loss_db": number(raw, "splice_loss_db", 0),
                "engineer_id": text(raw, "engineer_id") if raw.get("engineer_id") else "backfill",
                "report_id": raw.get("report_id").strip() if isinstance(raw.get("report_id"), str) and raw.get("report_id").strip() else None,
                "note": raw.get("note").strip() if isinstance(raw.get("note"), str) else "历史项补录",
            }
            if item["occurred_at"] in seen_times:
                raise ValidationError("补录历史项的现场时刻不能重复")
            seen_times.add(item["occurred_at"])
            items.append(item)
        return items

    def after_backfill(self, record: Dict[str, Any], archive: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str, float]:
        """补录历史项后按最新档案复核工单。"""
        p = dict(record["payload"])
        budget = float(p.get("splice_loss_budget_db", DEFAULT_SPLICE_BUDGET_DB))
        total = round(float(archive["total_loss_db"]), 6)
        changes: Dict[str, Any] = {
            "segment_splice_count": int(archive["confirmed_count"]),
            "segment_total_splice_loss_db": total,
            "splice_budget_db": budget,
        }
        p.update(changes)
        p.pop("archive_seq_at_test", None)
        if total > budget:
            p.pop("test_passed", None)
            return RECTIFICATION_STATE, p, "历史项补齐后累计损耗%s超过预算%s，退回待整改" % (total, budget), total
        if record["state"] == "tested":
            p.pop("test_passed", None)
            return "tested", p, "历史项已补齐，档案发生变化，恢复流量前需复测", total
        return "spliced", p, "历史接续项已补齐", total

    def apply_test(self, record: Dict[str, Any], data: Dict[str, Any], archive: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, "test")
        if int(archive["confirmed_count"]) <= 0:
            raise Conflict("旧单缺少接续记录，请先通过backfill补齐历史项")
        end_loss = number(data, "end_to_end_loss_db", 0)
        if end_loss > TEST_LOSS_LIMIT_DB:
            raise ValidationError("端到端损耗不合格")
        p = dict(record["payload"])
        p["end_to_end_loss_db"] = end_loss
        p["test_passed"] = True
        p["segment_total_splice_loss_db"] = round(float(archive["total_loss_db"]), 6)
        p["archive_seq_at_test"] = int(archive["last_entry_id"])
        if record["state"] == "tested":
            summary = "已按最新接续档案复测通过"
        else:
            summary = "系统测试通过"
        return new_state, p, summary

    def apply_restore(self, record: Dict[str, Any], data: Dict[str, Any], archive: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, "restore")
        if int(archive["confirmed_count"]) <= 0:
            raise Conflict("旧单缺少接续记录，请先通过backfill补齐历史项")
        if not record["payload"].get("test_passed"):
            raise Conflict("尚未完成系统测试")
        if int(record["payload"].get("archive_seq_at_test", 0) or 0) != int(archive["last_entry_id"]):
            raise Conflict("接续档案在测试后有更新（晚到记录），请先复测再恢复流量")
        budget = float(record["payload"].get("splice_loss_budget_db", DEFAULT_SPLICE_BUDGET_DB))
        total = round(float(archive["total_loss_db"]), 6)
        if total > budget:
            raise Conflict("区段累计损耗%s超过预算%s，不得恢复流量" % (total, budget))
        d = dict(data or {})
        if not boolean(d, "traffic_restored"):
            raise ValidationError("业务流量尚未恢复")
        p = dict(record["payload"])
        p["traffic_restored"] = True
        p["restore_capacity_gbps"] = integer(d, "restore_capacity_gbps", 1)
        p["segment_total_splice_loss_db"] = total
        return new_state, p, "通信恢复，已按最新接续档案复核"
