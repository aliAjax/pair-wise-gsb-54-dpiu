"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DEFAULT_SPLICE_BUDGET_DB, RECTIFICATION_STATE, DomainRules


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

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if action == "splice":
            return self._submit_splice(actor, record_id, expected_version, data or {})
        if action == "backfill":
            return self._backfill(actor, record_id, expected_version, data or {})
        if action == "test":
            return self._test(actor, record_id, expected_version, data or {})
        if action == "restore":
            return self._restore(actor, record_id, expected_version, data or {})
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    # ------------------------------------------------------------------
    # 接续上报（两名工程师同时提交只收一条；重试不重复累加）
    # ------------------------------------------------------------------
    def _submit_splice(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        # 单条阈值在事务前校验，避免无效数据进入现场暂存
        entry = self.rules.prepare_splice_report(data, actor.user_id)
        with self.repository.transaction() as tx:
            record = tx.get_record(record_id)
            cable = record["payload"]["cable"]
            segment = record["payload"]["segment"]

            # 1) 幂等回放：相同report_id或幂等键的重试直接返回既有结果，不重复累加
            if entry["report_id"]:
                existing = tx.find_entry_by_report(entry["report_id"])
                if existing is not None:
                    return self._report_envelope(tx.get_record(existing["record_id"] or record_id), existing, "replayed",
                                                 "report_id已存在，返回首次处理结果（幂等）", tx)
            if entry["idempotency_key"]:
                existing = tx.find_entry_by_idempotency(entry["idempotency_key"])
                if existing is not None:
                    return self._report_envelope(tx.get_record(existing["record_id"] or record_id), existing, "replayed",
                                                 "幂等键已存在，返回首次处理结果（幂等）", tx)

            # 2) 同一现场事件（同区段同时刻）：判定是否可并入当前工单
            event_entries = tx.find_segment_event(cable, segment, entry["occurred_at"])
            confirmed_event = next((item for item in event_entries if item["status"] == "confirmed"), None)
            same_engineer_pending = next(
                (item for item in event_entries if item["status"] == "pending" and item["engineer_id"] == entry["engineer_id"]),
                None,
            )

            can_apply = record["state"] in {"surveyed", RECTIFICATION_STATE}
            if not can_apply:
                # 晚到的现场数据：不与当前工单状态绑定，留待确认
                saved = tx.insert_entry({**entry, "cable": cable, "segment": segment, "status": "pending", "record_id": record_id})
                tx.add_audit(record_id, "splice_pending", actor.user_id, record["version"],
                             {"summary": "现场接续数据已留存待确认（当前状态不接受接续）", "entry_id": saved["id"], "input": data})
                return self._report_envelope(record, saved, "pending", "当前工单状态不接受接续，现场数据留存待确认", tx)

            if int(record["version"]) != int(expected_version):
                # 版本落后：先到者已推进工单，本次上报作为晚到现场数据留存待确认，
                # 而不是直接抛出冲突（现场数据不会因此丢失，也不参与累计）
                saved = tx.insert_entry({**entry, "cable": cable, "segment": segment,
                                         "status": "pending", "record_id": record_id})
                tx.add_audit(record_id, "splice_pending", actor.user_id, record["version"],
                             {"summary": "工单版本已被先到提交推进，本次上报作为晚到现场数据留存待确认",
                              "entry_id": saved["id"], "input": data})
                return self._report_envelope(record, saved, "pending",
                                             "工单已被先到提交推进，现场数据留存待确认", tx)

            if confirmed_event is not None:
                # 另一名工程师同一时刻的上报：先到已收，本条留现场数据
                saved = tx.insert_entry({**entry, "cable": cable, "segment": segment, "status": "pending", "record_id": record_id})
                tx.add_audit(record_id, "splice_pending", actor.user_id, record["version"],
                             {"summary": "同一现场时刻已由%s确认，本次上报留存待确认" % confirmed_event["engineer_id"],
                              "entry_id": saved["id"], "input": data})
                return self._report_envelope(record, saved, "pending",
                                             "同一现场时刻已由%s确认，现场数据留存待确认" % confirmed_event["engineer_id"], tx)
            if same_engineer_pending is not None:
                # 工程师自己的重复上报（无幂等键的重试），回放既有暂存不重复累加
                return self._report_envelope(record, same_engineer_pending, "pending",
                                             "同一现场时刻已有本人上报，返回既有现场数据（幂等）", tx)

            # 3) 首次提交：确认入档案，按区段累计损耗决定走向
            saved = tx.insert_entry({**entry, "cable": cable, "segment": segment, "status": "confirmed", "record_id": record_id})
            archive = tx.archive_snapshot(cable, segment)
            new_state, new_payload, summary, total = self.rules.accept_splice(record, saved, archive)
            saved = tx.get_entry(saved["id"])
            updated = tx.update_record(record, new_state, new_payload, actor.user_id, expected_version)
            tx.add_audit(record_id, "splice", actor.user_id, updated["version"],
                         {"summary": summary, "input": data, "from": record["state"], "to": new_state,
                          "entry_id": saved["id"], "segment_total_loss_db": total})
            self._rollback_siblings(tx, cable, segment, record_id, actor)
            updated = tx.get_record(record_id)
            return self._report_envelope(updated, saved, "confirmed", summary, tx)

    # ------------------------------------------------------------------
    # 旧单补历史项
    # ------------------------------------------------------------------
    def _backfill(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        items = self.rules.prepare_backfill_items(data)
        with self.repository.transaction() as tx:
            record = tx.get_record(record_id)
            cable = record["payload"]["cable"]
            segment = record["payload"]["segment"]
            self.rules.require_transition(record, "backfill")
            if int(record["version"]) != int(expected_version):
                raise Conflict("版本冲突，请刷新后重试")
            archive = tx.archive_snapshot(cable, segment)
            if int(archive["confirmed_count"]) > 0:
                raise Conflict("该区段接续档案已有确认记录，无需补历史项")
            existing_times = {entry["occurred_at"] for entry in archive["entries"]}
            saved_items = []
            for item in items:
                if item["occurred_at"] in existing_times:
                    raise Conflict("现场时刻%s的接续记录已存在" % item["occurred_at"])
                saved = tx.insert_entry({**item, "cable": cable, "segment": segment,
                                         "status": "confirmed", "record_id": record_id,
                                         "idempotency_key": None})
                saved_items.append(saved)
                existing_times.add(item["occurred_at"])
            archive = tx.archive_snapshot(cable, segment)
            new_state, new_payload, summary, total = self.rules.after_backfill(record, archive)
            updated = tx.update_record(record, new_state, new_payload, actor.user_id, expected_version)
            tx.add_audit(record_id, "backfill", actor.user_id, updated["version"],
                         {"summary": summary, "from": record["state"], "to": new_state,
                          "entry_ids": [item["id"] for item in saved_items], "segment_total_loss_db": total})
            self._rollback_siblings(tx, cable, segment, record_id, actor)
            updated = tx.get_record(record_id)
            updated["splice_report"] = {"status": "confirmed", "message": summary,
                                        "archive": tx.archive_snapshot(cable, segment)}
            return updated

    # ------------------------------------------------------------------
    # 测试 / 恢复：均在锁内按最新档案复核
    # ------------------------------------------------------------------
    def _test(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.repository.transaction() as tx:
            record = tx.get_record(record_id)
            cable = record["payload"]["cable"]
            segment = record["payload"]["segment"]
            archive = tx.archive_snapshot(cable, segment)
            new_state, new_payload, summary = self.rules.apply_test(record, data, archive)
            updated = tx.update_record(record, new_state, new_payload, actor.user_id, expected_version)
            tx.add_audit(record_id, "test", actor.user_id, updated["version"],
                         {"summary": summary, "input": data, "from": record["state"], "to": new_state,
                          "archive_seq": archive["last_entry_id"], "segment_total_loss_db": archive["total_loss_db"]})
            return updated

    def _restore(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        with self.repository.transaction() as tx:
            record = tx.get_record(record_id)
            cable = record["payload"]["cable"]
            segment = record["payload"]["segment"]
            archive = tx.archive_snapshot(cable, segment)
            new_state, new_payload, summary = self.rules.apply_restore(record, data, archive)
            updated = tx.update_record(record, new_state, new_payload, actor.user_id, expected_version)
            tx.add_audit(record_id, "restore", actor.user_id, updated["version"],
                         {"summary": summary, "input": data, "from": record["state"], "to": new_state,
                          "archive_seq": archive["last_entry_id"], "segment_total_loss_db": archive["total_loss_db"]})
            return updated

    # ------------------------------------------------------------------
    # 暂存现场数据确认
    # ------------------------------------------------------------------
    def confirm_entry(self, actor: Actor, entry_id: int, expected_version: Optional[int] = None,
                      decision: str = "confirm") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "cable_engineer" and actor.role != "admin":
            raise PermissionDenied("仅接续工程师可确认现场数据")
        if decision not in {"confirm", "duplicate"}:
            raise ValidationError("decision只能是confirm/duplicate")
        with self.repository.transaction() as tx:
            entry = tx.get_entry(entry_id)
            if entry["status"] != "pending":
                bound = tx.get_record(entry["record_id"]) if entry["record_id"] else None
                return self._report_envelope(bound, entry, "replayed",
                                             "该接续条目已处理（%s，幂等）" % entry["status"], tx)
            if decision == "duplicate":
                resolved = tx.resolve_entry(entry_id, "duplicate")
                if entry["record_id"]:
                    record = tx.get_record(entry["record_id"])
                    tx.add_audit(entry["record_id"], "splice_duplicate", actor.user_id, record["version"],
                                 {"summary": "现场数据复核判定为重复上报，不计入接续档案", "entry_id": entry_id})
                return {"entry": resolved, "status": "duplicate",
                        "message": "已判定为重复上报，不重复累加",
                        "archive": tx.archive_snapshot(entry["cable"], entry["segment"])}
            archive_before = tx.archive_snapshot(entry["cable"], entry["segment"])
            duplicates = tx.find_segment_event(entry["cable"], entry["segment"], entry["occurred_at"])
            if any(item["status"] == "confirmed" for item in duplicates):
                # 同一物理接续已有确认条目：现场复核判重，不允许二次累计
                raise Conflict("该现场时刻已有确认记录；若本条确为重复请提交decision=duplicate")
            bound_record = None
            if entry["record_id"]:
                bound_record = tx.get_record(entry["record_id"])
            if bound_record is not None and bound_record["state"] in {"surveyed", RECTIFICATION_STATE}:
                if expected_version is not None and int(bound_record["version"]) != int(expected_version):
                    raise Conflict("版本冲突，请刷新后重试")
                # 沿用接续规则复核单条阈值与备缆
                bound_record["payload"]["splice_loss_db"] = float(entry["splice_loss_db"])
                archive_after = {
                    "cable": entry["cable"], "segment": entry["segment"],
                    "confirmed_count": int(archive_before["confirmed_count"]) + 1,
                    "total_loss_db": round(float(archive_before["total_loss_db"]) + float(entry["splice_loss_db"]), 6),
                    "last_entry_id": int(entry["id"]), "entries": [],
                }
                new_state, new_payload, summary, total = self.rules.accept_splice(bound_record, entry, archive_after)
                tx.confirm_entry(entry_id)
                updated = tx.update_record(bound_record, new_state, new_payload, actor.user_id, expected_version)
                tx.add_audit(bound_record["id"], "splice_confirm", actor.user_id, updated["version"],
                             {"summary": summary, "entry_id": entry_id, "from": bound_record["state"], "to": new_state,
                              "segment_total_loss_db": total})
                self._rollback_siblings(tx, entry["cable"], entry["segment"], bound_record["id"], actor)
                updated = tx.get_record(bound_record["id"])
                return self._report_envelope(updated, tx.get_entry(entry_id), "confirmed", summary, tx)
            # 无在途接续环节工单（含工单已越过接续、晚到数据）：更新档案并联动其他工单
            tx.confirm_entry(entry_id)
            self._rollback_siblings(tx, entry["cable"], entry["segment"], None, actor,
                                    invalidate_tested=True)
            archive = tx.archive_snapshot(entry["cable"], entry["segment"])
            return {"entry": tx.get_entry(entry_id), "status": "confirmed",
                    "message": "现场数据已确认并入区段接续档案", "archive": archive,
                    "affected_records": self._segment_record_states(tx, entry["cable"], entry["segment"])}

    # ------------------------------------------------------------------
    # 只读
    # ------------------------------------------------------------------
    def segment_archive(self, actor: Actor, cable: str, segment: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        cable = text({"cable": cable}, "cable")
        segment = text({"segment": segment}, "segment")
        return self.repository.segment_archive(cable, segment)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _rollback_siblings(self, tx: Any, cable: str, segment: str, origin_record_id: Optional[int],
                          actor: Actor, invalidate_tested: bool = True) -> None:
        """档案更新后，对同区段在途工单按最新累计损耗联动。"""
        archive = tx.archive_snapshot(cable, segment)
        total = float(archive["total_loss_db"])
        for other in tx.list_segment_records(cable, segment):
            if other["id"] == origin_record_id or other["state"] in {"restored", "cancelled"}:
                continue
            p = dict(other["payload"])
            budget = float(p.get("splice_loss_budget_db", DEFAULT_SPLICE_BUDGET_DB))
            if total > budget and other["state"] in {"spliced", "tested", RECTIFICATION_STATE}:
                p["test_passed"] = False
                p["segment_total_splice_loss_db"] = total
                updated = tx.update_record(other, RECTIFICATION_STATE, p, actor.user_id)
                tx.add_audit(other["id"], "archive_rollback", actor.user_id, updated["version"],
                             {"summary": "区段接续档案累计损耗%s超过预算%s，工单退回待整改" % (total, budget),
                              "segment_total_loss_db": total})
            elif invalidate_tested and other["state"] == "tested":
                # 档案新增接续（晚到记录），原测试结果失效，需复测
                p["test_passed"] = False
                p["segment_total_splice_loss_db"] = total
                p.pop("archive_seq_at_test", None)
                updated = tx.update_record(other, "tested", p, actor.user_id)
                tx.add_audit(other["id"], "test_invalidated", actor.user_id, updated["version"],
                             {"summary": "接续档案新增晚到记录，原测试结果失效，需复测",
                              "segment_total_loss_db": total})

    @staticmethod
    def _segment_record_states(tx: Any, cable: str, segment: str) -> List[Dict[str, Any]]:
        return [{"id": item["id"], "state": item["state"], "version": item["version"]}
                for item in tx.list_segment_records(cable, segment)]

    def _report_envelope(self, record: Optional[Dict[str, Any]], entry: Dict[str, Any],
                         status: str, message: str, tx: Any) -> Dict[str, Any]:
        envelope = dict(record) if record is not None else {}
        envelope["splice_report"] = {
            "status": status,
            "message": message,
            "entry": entry,
            "archive": tx.archive_snapshot(entry["cable"], entry["segment"]),
        }
        return envelope
