"""业务用例编排、权限检查与审计。"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import (
    BACKFILL_STATES,
    DEFAULT_SPLICE_BUDGET_DB,
    SPLICE_REPORT_STATES,
    DomainRules,
)


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def _archive_decision_workflow(self, new_state, prepared_payload):
        def decide(connection, current, confirmed, cumulative):
            # workflow以规则层计算出的接续结果为准（spare用量等），目标状态固定为new_state
            state, payload, summary = self.rules.archive_effect(prepared_payload, new_state, confirmed, cumulative)
            return state, payload, "splice", summary
        return decide

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        before_commit = None
        if action == "splice":
            # 流程内接续：落库同一事务写入区段接续档案，版本检查与累加原子完成
            entry = self._build_workflow_entry(record, data or {}, actor.user_id)
            return self.repository.submit_splice(
                entry=entry,
                record=record,
                actor_id=actor.user_id,
                allowed_states={"surveyed", "rectification"},
                expected_version=int(expected_version),
                decide=self._archive_decision_workflow(new_state, new_payload),
            )["record"]
        if action == "restore":
            before_commit = self._restore_gate(record)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            before_commit=before_commit,
        )

    def _build_workflow_entry(self, record: Dict[str, Any], data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        entry_data = {
            "splice_loss_db": data["splice_loss_db"],
            "splice_point_km": data.get("splice_point_km", record["payload"].get("fault_location_km")),
            "occurred_at": data.get("occurred_at") or datetime.now(timezone.utc).isoformat(),
            "engineer_id": data.get("engineer_id") or actor_id,
        }
        if data.get("client_token"):
            entry_data["client_token"] = data["client_token"]
        if data.get("note"):
            entry_data["note"] = data["note"]
        return self.rules.validate_splice_entry(entry_data, record["payload"], require_token=False)

    def _restore_gate(self, record: Dict[str, Any]):
        """恢复流量前按最新档案复核，返回事务内回调。"""
        payload = record["payload"]
        cable, segment = payload["cable"], payload["segment"]

        def gate(connection, record_id: int) -> None:
            linked = connection.execute(
                "SELECT * FROM segment_splices WHERE record_id=? AND status='confirmed' ORDER BY occurred_at, id",
                (record_id,),
            ).fetchall()
            if not linked:
                raise Conflict("区段接续档案缺少历史接续记录，请先补录（backfill_splices）后再恢复")
            pending = connection.execute(
                "SELECT COUNT(*) AS total FROM segment_splices WHERE cable=? AND segment=? AND status='pending_confirmation'",
                (cable, segment),
            ).fetchone()
            if int(pending["total"]) > 0:
                raise Conflict("存在待确认的现场接续数据，处理完后再恢复")
            confirmed = self.repository.confirmed_splices(connection, cable, segment)
            cumulative = self.rules.cumulative_loss(confirmed)
            budget = float(payload.get("splice_loss_budget_db", DEFAULT_SPLICE_BUDGET_DB))
            if cumulative - budget > 1e-9:
                raise Conflict("累计接续损耗%.3fdB超过预算%.3fdB，不能恢复" % (cumulative, budget))
            row = connection.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
            current_payload = json.loads(row["payload"])
            if not current_payload.get("test_passed"):
                raise Conflict("原测试结果已失效，请重新测试后再恢复")

        return gate

    def report_field_splice(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """现场补报接续数据（晚到记录/两次抢修间的额外接续）。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "report_splice"):
            raise PermissionDenied("角色无权上报现场接续")
        record = self.repository.get(record_id)
        if record["state"] not in SPLICE_REPORT_STATES:
            raise Conflict("当前状态不允许补报接续数据")
        entry = self.rules.validate_splice_entry(data or {}, record["payload"], require_token=True)

        def decide(connection, current, confirmed, cumulative):
            state, payload, summary = self.rules.archive_effect(dict(current["payload"]), current["state"], confirmed, cumulative)
            return state, payload, "report_splice", summary

        return self.repository.submit_splice(
            entry=entry,
            record=record,
            actor_id=actor.user_id,
            allowed_states=SPLICE_REPORT_STATES,
            expected_version=None,
            decide=decide,
        )

    def backfill(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """旧单缺少接续记录时补录历史项，整批原子生效。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "backfill_splices"):
            raise PermissionDenied("角色无权补录历史接续")
        record = self.repository.get(record_id)
        if record["state"] not in BACKFILL_STATES:
            raise Conflict("当前状态不允许补录历史接续")
        items = (data or {}).get("splices")
        if not isinstance(items, list) or not items:
            raise ValidationError("splices至少需要一条历史接续")
        entries = []
        seen_tokens = set()
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise ValidationError("splices必须是对象列表")
            entry = self.rules.validate_splice_entry(raw, record["payload"], require_token=False, source="backfill")
            token = "backfill:%s:%s:%s" % (record_id, entry["occurred_at"], entry["splice_point_km"])
            if token in seen_tokens:
                raise ValidationError("补录批次内存在重复的现场时刻和接续点")
            seen_tokens.add(token)
            entry["client_token"] = token
            entries.append(entry)

        def decide(connection, current, confirmed, cumulative):
            state, payload, summary = self.rules.archive_effect(dict(current["payload"]), current["state"], confirmed, cumulative)
            return state, payload, "backfill_splices", "补录历史接续%d条；%s" % (len(entries), summary)

        return self.repository.backfill_splices(entries, record, actor.user_id, decide)

    def resolve_pending_splice(self, actor: Actor, splice_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "resolve_splice"):
            raise PermissionDenied("角色无权裁决待确认接续")
        resolution = text(data or {}, "resolution")
        if resolution not in ("confirm", "reject"):
            raise ValidationError("resolution只能是confirm/reject")
        note = text(data or {}, "note")

        def reevaluate(rec, confirmed, cumulative, now):
            p = dict(rec["payload"])
            p["cumulative_splice_loss_db"] = cumulative
            p["archived_splice_count"] = len(confirmed)
            p["last_splice_at"] = confirmed[-1]["occurred_at"]
            p.pop("budget_exceeded_at", None)
            new_state = rec["state"]
            summary = "待确认数据已替换入档，档案已复核"
            budget = float(p.get("splice_loss_budget_db", DEFAULT_SPLICE_BUDGET_DB))
            if cumulative - budget > 1e-9:
                p["budget_exceeded_at"] = now
                new_state = "rectification"
                summary = "替换入档后累计损耗%.3fdB超过预算，退回待整改" % cumulative
            elif rec["state"] == "tested":
                p["test_passed"] = False
                p["test_invalidated_at"] = now
                p.pop("end_to_end_loss_db", None)
                new_state = "spliced"
                summary = "替换入档后原测试结果失效，需重新测试"
            return new_state, p, summary

        return self.repository.resolve_splice(splice_id, actor.user_id, resolution, note, reevaluate)

    def get_splice(self, actor: Actor, splice_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_splice(splice_id)

    def list_splices(self, actor: Actor, cable: str = None, segment: str = None, status: str = None, limit: int = 500) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        items = self.repository.list_splices(cable=cable, segment=segment, status=status, limit=limit)
        result: Dict[str, Any] = {"items": items}
        if cable and segment:
            confirmed = [item for item in items if item["status"] == "confirmed"]
            result["confirmed_count"] = len(confirmed)
            result["cumulative_splice_loss_db"] = self.rules.cumulative_loss(confirmed)
        return result

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
