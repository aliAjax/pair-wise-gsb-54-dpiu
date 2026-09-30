"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Conflict, ValidationError, boolean, integer, number, text, timestamp


INITIAL_STATE = "detected"
# 单次接续损耗阈值
SINGLE_SPLICE_LOSS_LIMIT_DB = 0.2
# 区段接续累计损耗预算默认值（dB）
DEFAULT_SPLICE_BUDGET_DB = 0.5
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}, 'rectify': {'repair_manager'}, 'backfill_splices': {'cable_engineer', 'repair_manager'}, 'report_splice': {'cable_engineer'}, 'resolve_splice': {'repair_manager'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced', 'rectification': 'spliced'}, 'test': {'spliced': 'tested'}, 'restore': {'tested': 'restored'}, 'rectify': {'rectification': 'spliced'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled', 'rectification': 'cancelled'}}
# 允许补报现场接续数据的故障单状态
SPLICE_REPORT_STATES = {'spliced', 'tested', 'rectification'}
# 允许补录历史接续的故障单状态
BACKFILL_STATES = {'spliced', 'tested', 'rectification'}
PENDING_STATUS = 'pending_confirmation'
CONFIRMED_STATUS = 'confirmed'
REJECTED_STATUS = 'rejected'
SUPERSEDED_STATUS = 'superseded'


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

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
        if "splice_loss_budget_db" in p:
            number(p, "splice_loss_budget_db", 0.0001)
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p.setdefault("splice_loss_budget_db", DEFAULT_SPLICE_BUDGET_DB)
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

    def validate_splice_entry(self, payload: Dict[str, Any], segment: Dict[str, Any], require_token: bool, source: str = "field") -> Dict[str, Any]:
        """校验一条现场接续数据，segment来自故障单payload。"""
        data = dict(payload or {})
        loss = number(data, "splice_loss_db", 0, SINGLE_SPLICE_LOSS_LIMIT_DB)
        point = number(data, "splice_point_km", float(segment["start_km"]), float(segment["end_km"]))
        occurred_at = timestamp(data, "occurred_at")
        entry: Dict[str, Any] = {"splice_loss_db": loss, "splice_point_km": point, "occurred_at": occurred_at, "engineer_id": text(data, "engineer_id"), "source": source}
        if "note" in data:
            entry["note"] = text(data, "note")
        token = data.get("client_token")
        if require_token:
            entry["client_token"] = text(data, "client_token")
        elif token is not None:
            entry["client_token"] = text(data, "client_token")
        return entry

    @staticmethod
    def sorted_confirmed(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按现场发生时刻（其次档案ID）排序，保证晚到数据按时刻归位。"""
        return sorted(entries, key=lambda item: (item["occurred_at"], item["id"]))

    @staticmethod
    def cumulative_loss(entries: List[Dict[str, Any]]) -> float:
        return round(sum(float(item["splice_loss_db"]) for item in entries), 6)

    def archive_effect(self, record_payload: Dict[str, Any], state: str, confirmed: List[Dict[str, Any]], cumulative: float) -> Tuple[str, Dict[str, Any], str]:
        """确认一条接续后对故障单的影响：超预算退回待整改，tested后的晚到数据使原测试失效。"""
        p = dict(record_payload)
        budget = float(p.get("splice_loss_budget_db", DEFAULT_SPLICE_BUDGET_DB))
        p["splice_loss_db"] = float(confirmed[-1]["splice_loss_db"])
        p["archived_splice_count"] = len(confirmed)
        p["cumulative_splice_loss_db"] = cumulative
        p["last_splice_at"] = confirmed[-1]["occurred_at"]
        if cumulative - budget > 1e-9:
            p["budget_exceeded_at"] = confirmed[-1]["occurred_at"]
            return "rectification", p, "累计接续损耗%.3fdB超过预算%.3fdB，退回待整改" % (cumulative, budget)
        if state == "tested":
            # 晚到接续使原测试结果失效，必须重新测试
            p["test_passed"] = False
            p["test_invalidated_at"] = confirmed[-1]["occurred_at"]
            p.pop("end_to_end_loss_db", None)
            return "spliced", p, "晚到接续已入档，原测试结果失效，需重新测试"
        return state, p, "区段接续档案已更新"

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
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
        elif action == "splice":
            loss = number(data, "splice_loss_db", 0)
            if loss > 0.2:
                raise ValidationError("接续损耗超过阈值")
            if float(data.get("spare_used_km", 0)) < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            changes["splice_loss_db"] = loss
            changes["spare_used_km"] = float(data["spare_used_km"])
            summary = "光缆接续完成"
        elif action == "test":
            end_loss = number(data, "end_to_end_loss_db", 0)
            if end_loss > 0.5:
                raise ValidationError("端到端损耗不合格")
            changes["end_to_end_loss_db"] = end_loss
            changes["test_passed"] = True
            p.pop("test_invalidated_at", None)
            summary = "系统测试通过"
        elif action == "restore":
            if not boolean(data, "traffic_restored"):
                raise ValidationError("业务流量尚未恢复")
            changes["traffic_restored"] = True
            changes["restore_capacity_gbps"] = integer(data, "restore_capacity_gbps", 1)
            summary = "通信恢复"
        elif action == "rectify":
            if not boolean(data, "rectified"):
                raise ValidationError("整改尚未完成")
            cumulative = float(p.get("cumulative_splice_loss_db", 0))
            revised = number(data, "splice_loss_budget_db", 0.0001)
            if cumulative - revised > 1e-9:
                raise ValidationError("整改后预算%.3fdB仍低于累计损耗%.3fdB" % (revised, cumulative))
            changes["splice_loss_budget_db"] = revised
            changes["rectify_note"] = text(data, "rectify_note")
            p.pop("budget_exceeded_at", None)
            summary = "整改完成，待重新测试"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
