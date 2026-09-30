import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400, 'splice_loss_budget_db': 0.5}
ENGINEER = Actor("ce-1", "cable_engineer")
ENGINEER2 = Actor("ce-2", "cable_engineer")
NOC = Actor("noc-1", "noc_operator")
MANAGER = Actor("rm-1", "repair_manager")


class SpliceArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference="CABLE-40001", data=None):
        return self.service.create(Actor("creator", "noc_operator"), reference, data or CREATE_DATA)

    def _advance(self, record, through, splice_data=None):
        steps = [
            ('approve', 'repair_manager', {'repair_manager': 'RM-2'}),
            ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}),
            ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}),
            ('splice', 'cable_engineer', splice_data or {'splice_loss_db': 0.12, 'spare_used_km': 16, 'splice_point_km': 128.0, 'occurred_at': '2026-09-20T08:00:00+00:00', 'engineer_id': 'ce-1'}),
            ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}),
            ('restore', 'noc_operator', {'traffic_restored': True, 'restore_capacity_gbps': 400}),
        ]
        for action, role, data in steps:
            record = self.service.act(Actor("op", role), record["id"], record["version"], action, data)
            if action == through:
                return record
        return record

    def _wipe_archive(self, record_id):
        conn = sqlite3.connect(self.service.repository.db_path)
        conn.execute("DELETE FROM segment_splices WHERE record_id=?", (record_id,))
        conn.commit()
        conn.close()

    # 1. 多次抢修按现场时刻累计，超预算退回待整改
    def test_cumulative_loss_by_occurred_at_exceeds_budget(self):
        record = self._advance(self._create(), 'splice')
        self.assertAlmostEqual(record["payload"]["cumulative_splice_loss_db"], 0.12, places=6)
        result = self.service.report_field_splice(
            ENGINEER, record["id"],
            {'splice_loss_db': 0.2, 'splice_point_km': 130.0, 'occurred_at': '2026-09-19T06:00:00+00:00', 'engineer_id': 'ce-1', 'client_token': 'tok-r2'},
        )
        record = result["record"]
        self.assertAlmostEqual(record["payload"]["cumulative_splice_loss_db"], 0.32, places=6)
        result = self.service.report_field_splice(
            ENGINEER, record["id"],
            {'splice_loss_db': 0.19, 'splice_point_km': 131.0, 'occurred_at': '2026-09-21T09:00:00+00:00', 'engineer_id': 'ce-1', 'client_token': 'tok-r3'},
        )
        record = result["record"]
        self.assertEqual(record["state"], "rectification")
        self.assertAlmostEqual(record["payload"]["cumulative_splice_loss_db"], 0.51, places=6)
        # 档案按现场时刻排序，晚到数据按时刻归位
        archive = self.service.list_splices(ENGINEER, cable="SEA-1", segment="S3")
        times = [item["occurred_at"] for item in archive["items"]]
        self.assertEqual(times, sorted(times))
        self.assertAlmostEqual(archive["cumulative_splice_loss_db"], 0.51, places=6)
        # 整改：预算低于累计值则拒绝
        with self.assertRaises(ValidationError):
            self.service.act(MANAGER, record["id"], record["version"], "rectify",
                             {'rectified': True, 'rectify_note': '重做接头', 'splice_loss_budget_db': 0.5})
        record = self.service.act(MANAGER, record["id"], record["version"], "rectify",
                                  {'rectified': True, 'rectify_note': '重做接头', 'splice_loss_budget_db': 0.6})
        self.assertEqual(record["state"], "spliced")
        self.assertNotIn("budget_exceeded_at", record["payload"])
        self.assertAlmostEqual(record["payload"]["splice_loss_budget_db"], 0.6, places=6)

    # 2. 晚到记录更新后原测试结果失效，需重新测试
    def test_late_splice_invalidates_prior_test(self):
        record = self._advance(self._create(), 'test')
        self.assertTrue(record["payload"]["test_passed"])
        result = self.service.report_field_splice(
            ENGINEER, record["id"],
            {'splice_loss_db': 0.05, 'splice_point_km': 129.0, 'occurred_at': '2026-09-20T20:00:00+00:00', 'engineer_id': 'ce-9', 'client_token': 'tok-late'},
        )
        record = result["record"]
        self.assertEqual(record["state"], "spliced")
        self.assertFalse(record["payload"]["test_passed"])
        self.assertIn("test_invalidated_at", record["payload"])
        self.assertNotIn("end_to_end_loss_db", record["payload"])
        with self.assertRaises(Conflict):
            self.service.act(NOC, record["id"], record["version"], "restore",
                             {'traffic_restored': True, 'restore_capacity_gbps': 400})
        record = self.service.act(NOC, record["id"], record["version"], "test", {'end_to_end_loss_db': 0.4})
        self.assertNotIn("test_invalidated_at", record["payload"])
        record = self.service.act(NOC, record["id"], record["version"], "restore",
                                  {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertEqual(record["state"], "restored")

    # 3. 两名工程师同时提交：一条确认，一条留现场数据待确认
    def test_concurrent_submissions_only_one_confirmed(self):
        record = self._advance(self._create(), 'splice')
        base = {'splice_loss_db': 0.1, 'splice_point_km': 130.5, 'occurred_at': '2026-09-22T05:00:00+00:00'}
        outcomes, errors = [], []
        barrier = threading.Barrier(2)

        def submit(actor, token):
            try:
                barrier.wait()
                outcomes.append(self.service.report_field_splice(
                    actor, record["id"], dict(base, engineer_id=actor.user_id, client_token=token)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=submit, args=(ENGINEER, "tok-a"))
        t2 = threading.Thread(target=submit, args=(ENGINEER2, "tok-b"))
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(errors, [])
        statuses = sorted(item["splice"]["status"] for item in outcomes)
        self.assertEqual(statuses, ["confirmed", "pending_confirmation"])
        record = self.service.get_record(ENGINEER, record["id"])
        self.assertAlmostEqual(record["payload"]["cumulative_splice_loss_db"], 0.22, places=6)
        pending = [item for item in outcomes if item["outcome"] == "pending"][0]
        # 确认待现场数据：替换原确认档案并重算
        resolved = self.service.resolve_pending_splice(MANAGER, pending["splice"]["id"],
                                                       {'resolution': 'confirm', 'note': '采信ce-2'})
        self.assertEqual(resolved["splice"]["status"], "confirmed")
        archive = self.service.list_splices(ENGINEER, cable="SEA-1", segment="S3")
        by_status = {}
        for item in archive["items"]:
            by_status.setdefault(item["status"], []).append(item)
        self.assertEqual(len(by_status["confirmed"]), 2)
        self.assertEqual(len(by_status["superseded"]), 1)
        self.assertAlmostEqual(archive["cumulative_splice_loss_db"], 0.22, places=6)
        # 不能重复裁决
        with self.assertRaises(Conflict):
            self.service.resolve_pending_splice(MANAGER, pending["splice"]["id"],
                                                {'resolution': 'reject', 'note': 'x'})

    # 3b. 另一条待确认数据可拒绝留痕
    def test_pending_rejection(self):
        record = self._advance(self._create(), 'splice')
        base = {'splice_loss_db': 0.08, 'splice_point_km': 131.2, 'occurred_at': '2026-09-22T08:00:00+00:00'}
        self.service.report_field_splice(ENGINEER, record["id"], dict(base, engineer_id='ce-1', client_token='tok-c'))
        r2 = self.service.report_field_splice(ENGINEER2, record["id"], dict(base, engineer_id='ce-2', client_token='tok-d'))
        self.assertEqual(r2["outcome"], "pending")
        rejected = self.service.resolve_pending_splice(MANAGER, r2["splice"]["id"],
                                                       {'resolution': 'reject', 'note': '数据不可信'})
        self.assertEqual(rejected["splice"]["status"], "rejected")
        record = self.service.get_record(ENGINEER, record["id"])
        self.assertAlmostEqual(record["payload"]["cumulative_splice_loss_db"], 0.20, places=6)

    # 3c. tested状态下confirm替换档案同样使原测试失效
    def test_confirm_pending_at_tested_invalidates_test(self):
        record = self._advance(self._create(), 'splice')
        base = {'splice_loss_db': 0.05, 'splice_point_km': 130.5, 'occurred_at': '2026-09-22T05:00:00+00:00'}
        o1 = self.service.report_field_splice(ENGINEER, record["id"], dict(base, engineer_id='ce-1', client_token='a'))
        o2 = self.service.report_field_splice(ENGINEER2, record["id"], dict(base, engineer_id='ce-2', client_token='b'))
        pending_id = [o["splice"]["id"] for o in (o1, o2) if o["outcome"] == "pending"][0]
        record = self.service.get_record(ENGINEER, record["id"])
        record = self.service.act(NOC, record["id"], record["version"], "test", {'end_to_end_loss_db': 0.35})
        self.assertEqual(record["state"], "tested")
        resolved = self.service.resolve_pending_splice(MANAGER, pending_id, {'resolution': 'confirm', 'note': '采信ce-2'})
        self.assertEqual(resolved["record"]["state"], "spliced")
        self.assertFalse(resolved["record"]["payload"]["test_passed"])
        self.assertIn("test_invalidated_at", resolved["record"]["payload"])

    # 4. 写入失败后重试不重复累加
    def test_retry_with_same_token_does_not_double_accumulate(self):
        record = self._advance(self._create(), 'splice')
        data = {'splice_loss_db': 0.1, 'splice_point_km': 130.5, 'occurred_at': '2026-09-22T05:00:00+00:00', 'engineer_id': 'ce-1', 'client_token': 'idem-1'}
        first = self.service.report_field_splice(ENGINEER, record["id"], data)
        second = self.service.report_field_splice(ENGINEER, record["id"], dict(data, splice_loss_db=0.2))
        self.assertEqual(second["outcome"], "duplicate")
        self.assertEqual(second["splice"]["splice_loss_db"], 0.1)
        record = self.service.get_record(ENGINEER, record["id"])
        self.assertAlmostEqual(record["payload"]["cumulative_splice_loss_db"], 0.22, places=6)
        self.assertEqual(len(self.service.list_splices(ENGINEER, cable="SEA-1", segment="S3")["items"]), 2)
        self.assertEqual(second["record"]["version"], first["record"]["version"])

    # 5. 旧单缺接续记录：补齐历史项前不能恢复；补录幂等；补录超预算退整改
    def test_old_ticket_must_backfill_before_restore(self):
        record = self._advance(self._create(), 'splice')
        self._wipe_archive(record["id"])
        record = self.service.repository.get(record["id"])
        record = self.service.act(NOC, record["id"], record["version"], "test", {'end_to_end_loss_db': 0.3})
        with self.assertRaises(Conflict) as ctx:
            self.service.act(NOC, record["id"], record["version"], "restore",
                             {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertIn("补录", str(ctx.exception))
        backfill_payload = {'splices': [
            {'splice_loss_db': 0.15, 'splice_point_km': 128.0, 'occurred_at': '2026-09-15T10:00:00+00:00', 'engineer_id': 'ce-hist'},
        ]}
        result = self.service.backfill(ENGINEER, record["id"], backfill_payload)
        self.assertEqual(result["outcome"], "accepted")
        again = self.service.backfill(ENGINEER, record["id"], backfill_payload)
        self.assertEqual(again["outcome"], "duplicate")
        record = self.service.repository.get(record["id"])
        # 补录发生在tested之后，原测试同样失效，需重测后恢复
        if not record["payload"].get("test_passed"):
            record = self.service.act(NOC, record["id"], record["version"], "test", {'end_to_end_loss_db': 0.35})
        record = self.service.act(NOC, record["id"], record["version"], "restore",
                                  {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertEqual(record["state"], "restored")

    def test_backfill_over_budget_moves_to_rectification(self):
        record = self._advance(self._create("CABLE-40003"), 'splice')
        self._wipe_archive(record["id"])
        record = self.service.repository.get(record["id"])
        result = self.service.backfill(ENGINEER, record["id"], {'splices': [
            {'splice_loss_db': 0.19, 'splice_point_km': 128.0, 'occurred_at': '2026-09-15T10:00:00+00:00', 'engineer_id': 'h'},
            {'splice_loss_db': 0.19, 'splice_point_km': 129.0, 'occurred_at': '2026-09-15T12:00:00+00:00', 'engineer_id': 'h'},
            {'splice_loss_db': 0.13, 'splice_point_km': 130.0, 'occurred_at': '2026-09-15T14:00:00+00:00', 'engineer_id': 'h'},
        ]})
        self.assertEqual(result["record"]["state"], "rectification")
        self.assertAlmostEqual(result["cumulative_splice_loss_db"], 0.51, places=6)

    def test_backfill_single_entry_over_threshold_rejected(self):
        record = self._advance(self._create(), 'splice')
        self._wipe_archive(record["id"])
        with self.assertRaises(ValidationError):
            self.service.backfill(ENGINEER, record["id"], {'splices': [
                {'splice_loss_db': 0.21, 'splice_point_km': 128.0, 'occurred_at': '2026-09-15T10:00:00+00:00', 'engineer_id': 'h'},
            ]})

    # 6. 恢复前复核：待确认数据未处理不能恢复
    def test_restore_blocked_by_pending(self):
        record = self._advance(self._create(), 'splice')
        base = {'splice_loss_db': 0.1, 'splice_point_km': 130.5, 'occurred_at': '2026-09-22T05:00:00+00:00'}
        self.service.report_field_splice(ENGINEER, record["id"], dict(base, engineer_id='ce-1', client_token='p1'))
        self.service.report_field_splice(ENGINEER2, record["id"], dict(base, engineer_id='ce-2', client_token='p2'))
        record = self.service.repository.get(record["id"])
        record = self.service.act(NOC, record["id"], record["version"], "test", {'end_to_end_loss_db': 0.4})
        with self.assertRaises(Conflict) as ctx:
            self.service.act(NOC, record["id"], record["version"], "restore",
                             {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertIn("待确认", str(ctx.exception))

    # 7. 同一区段的累计跨故障单
    def test_budget_accumulates_across_tickets_on_same_segment(self):
        record = self._advance(self._create("CABLE-50001"), 'restore')
        self.assertEqual(record["state"], "restored")
        second = self._create("CABLE-50002")
        self.service.act(Actor("op", "repair_manager"), second["id"], 1, "approve", {'repair_manager': 'RM-2'})
        self.service.act(Actor("op", "vessel_master"), second["id"], 2, "mobilize",
                         {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'})
        self.service.act(ENGINEER, second["id"], 3, "survey", {'survey_complete': True, 'fault_location_km': 130})
        r = self.service.act(ENGINEER, second["id"], 4, "splice",
                             {'splice_loss_db': 0.12, 'spare_used_km': 16, 'splice_point_km': 130.0,
                              'occurred_at': '2026-10-01T08:00:00+00:00', 'engineer_id': 'ce-1'})
        self.assertAlmostEqual(r["payload"]["cumulative_splice_loss_db"], 0.24, places=6)
        self.service.report_field_splice(ENGINEER, second["id"],
                                         {'splice_loss_db': 0.2, 'splice_point_km': 130.6, 'occurred_at': '2026-10-01T10:00:00+00:00', 'engineer_id': 'ce-1', 'client_token': 'x1'})
        over = self.service.report_field_splice(ENGINEER, second["id"],
                                                {'splice_loss_db': 0.1, 'splice_point_km': 130.8, 'occurred_at': '2026-10-01T11:00:00+00:00', 'engineer_id': 'ce-1', 'client_token': 'x2'})
        self.assertEqual(over["record"]["state"], "rectification")

    # 8. 已恢复的单不再接收接续数据
    def test_restored_ticket_rejects_new_splice_data(self):
        record = self._advance(self._create(), 'restore')
        with self.assertRaises(Conflict):
            self.service.report_field_splice(ENGINEER, record["id"],
                                             {'splice_loss_db': 0.05, 'splice_point_km': 129.0, 'occurred_at': '2026-10-02T00:00:00+00:00', 'engineer_id': 'ce-1', 'client_token': 'late-restored'})


if __name__ == "__main__":
    unittest.main()
