"""区段接续档案：累计预算、并发去重、晚到记录失效、旧单补历史项。"""
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}


def _advance_to(service, record, upto, times):
    flow = [
        ('approve', 'repair_manager', {'repair_manager': 'RM-2'}),
        ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}),
        ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}),
    ]
    for action, role, data in flow:
        if upto == action:
            break
        record = service.act(Actor('op', role), record['id'], record['version'], action, data)
        times[action] = times.get(action, 0)
    return record


class SpliceArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / 'test.db'))

    def tearDown(self):
        self.temp.cleanup()

    def _to_surveyed(self, ref='CABLE-30001', budget=None, data=None):
        create = dict(CREATE_DATA if data is None else data)
        if budget is not None:
            create['splice_loss_budget_db'] = budget
        record = self.service.create(Actor('creator', 'noc_operator'), ref, create)
        record = self.service.act(Actor('op', 'repair_manager'), record['id'], record['version'], 'approve', {'repair_manager': 'RM-2'})
        record = self.service.act(Actor('op', 'vessel_master'), record['id'], record['version'], 'mobilize',
                                  {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'})
        record = self.service.act(Actor('op', 'cable_engineer'), record['id'], record['version'], 'survey',
                                  {'survey_complete': True, 'fault_location_km': 128})
        return record

    def test_cumulative_loss_over_budget_returns_to_rectification(self):
        # 预算0.3：两次抢修各报0.18，第二条使累计0.36超预算
        first = self._to_surveyed('C-1', budget=0.3)
        first = self.service.act(Actor('eng-a', 'cable_engineer'), first['id'], first['version'], 'splice',
                                 {'occurred_at': '2026-09-01T10:00:00Z', 'splice_loss_db': 0.18,
                                  'spare_used_km': 16, 'report_id': 'R-1'})
        self.assertEqual(first['state'], 'spliced')
        first = self.service.act(Actor('op', 'noc_operator'), first['id'], first['version'], 'test',
                                 {'end_to_end_loss_db': 0.3})
        self.assertEqual(first['state'], 'tested')
        first = self.service.act(Actor('op', 'noc_operator'), first['id'], first['version'], 'restore',
                                 {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertEqual(first['state'], 'restored')

        second = self._to_surveyed('C-2', budget=0.3)
        second = self.service.act(Actor('eng-b', 'cable_engineer'), second['id'], second['version'], 'splice',
                                  {'occurred_at': '2026-09-02T10:00:00Z', 'splice_loss_db': 0.18,
                                   'spare_used_km': 16, 'report_id': 'R-2'})
        # 区段累计0.36 > 0.3预算：直接退回待整改
        self.assertEqual(second['state'], 'rectification')
        archive = self.service.segment_archive(Actor('eng-a', 'cable_engineer'), 'SEA-1', 'S3')
        self.assertEqual(archive['total_loss_db'], 0.36)
        self.assertEqual(archive['confirmed_count'], 2)
        # 按现场时刻排序
        self.assertEqual([e['occurred_at'] for e in archive['entries']],
                         ['2026-09-01T10:00:00+00:00', '2026-09-02T10:00:00+00:00'])

    def test_concurrent_submissions_only_one_confirmed(self):
        record = self._to_surveyed('C-1')
        barrier = threading.Barrier(2)
        results = [None, None]

        def submit(idx, engineer):
            barrier.wait()
            try:
                # 两名工程师基于同一版本同时上报同一现场时刻的接续结果
                results[idx] = self.service.act(
                    Actor(engineer, 'cable_engineer'), record['id'], record['version'], 'splice',
                    {'occurred_at': '2026-09-03T08:00:00Z', 'splice_loss_db': 0.1, 'spare_used_km': 16,
                     'report_id': 'R-%s' % engineer})
            except Exception as exc:  # noqa: BLE001
                results[idx] = exc

        t1 = threading.Thread(target=submit, args=(0, 'eng-a'))
        t2 = threading.Thread(target=submit, args=(1, 'eng-b'))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertFalse(any(isinstance(r, Exception) for r in results), results)
        statuses = sorted(r['splice_report']['status'] for r in results)
        self.assertEqual(statuses, ['confirmed', 'pending'])
        next(r for r in results if r['splice_report']['status'] == 'confirmed')
        pending = next(r for r in results if r['splice_report']['status'] == 'pending')
        # 只累计一条
        archive = self.service.segment_archive(Actor('eng-a', 'cable_engineer'), 'SEA-1', 'S3')
        self.assertEqual(archive['confirmed_count'], 1)
        self.assertEqual(archive['total_loss_db'], 0.1)
        pending_entries = [e for e in archive['entries'] if e['status'] == 'pending']
        self.assertEqual(len(pending_entries), 1)
        # 工单只被推进一次（版本+1）
        refreshed = self.service.get_record(Actor('eng-a', 'cable_engineer'), record['id'])
        self.assertEqual(refreshed['version'], record['version'] + 1)
        self.assertEqual(refreshed['state'], 'spliced')

        # 败者复核：同一物理接续，判为重复后仍只累计一条
        pending_id = pending['splice_report']['entry']['id']
        with self.assertRaises(Conflict):
            self.service.confirm_entry(Actor('eng-c', 'cable_engineer'), pending_id)
        resolved = self.service.confirm_entry(Actor('eng-c', 'cable_engineer'), pending_id, decision='duplicate')
        self.assertEqual(resolved['status'], 'duplicate')
        archive = self.service.segment_archive(Actor('eng-a', 'cable_engineer'), 'SEA-1', 'S3')
        self.assertEqual(archive['confirmed_count'], 1)
        self.assertEqual(archive['total_loss_db'], 0.1)

        # 另一条不同时刻的暂存现场数据（晚到补报）确认后才入档案
        late = self.service.act(Actor('eng-b', 'cable_engineer'), record['id'], refreshed['version'], 'splice',
                                {'occurred_at': '2026-09-03T07:30:00Z', 'splice_loss_db': 0.05,
                                 'spare_used_km': 16, 'report_id': 'R-LATE'})
        self.assertEqual(late['splice_report']['status'], 'pending')
        out = self.service.confirm_entry(Actor('eng-c', 'cable_engineer'),
                                         late['splice_report']['entry']['id'])
        self.assertEqual(out['status'], 'confirmed')
        archive = self.service.segment_archive(Actor('eng-a', 'cable_engineer'), 'SEA-1', 'S3')
        self.assertEqual(archive['confirmed_count'], 2)
        self.assertEqual(archive['total_loss_db'], 0.15)

    def test_retry_with_same_report_id_does_not_double_accumulate(self):
        record = self._to_surveyed('C-1')
        payload = {'occurred_at': '2026-09-04T08:00:00Z', 'splice_loss_db': 0.1,
                   'spare_used_km': 16, 'report_id': 'R-X', 'idempotency_key': 'IDEM-X'}
        first = self.service.act(Actor('eng-a', 'cable_engineer'), record['id'], record['version'], 'splice', payload)
        self.assertEqual(first['splice_report']['status'], 'confirmed')
        # 第一次成功后客户端重试（版本号已过期）：回放，不报错不累计
        retry = self.service.act(Actor('eng-a', 'cable_engineer'), record['id'], record['version'], 'splice', payload)
        self.assertEqual(retry['splice_report']['status'], 'replayed')
        archive = self.service.segment_archive(Actor('eng-a', 'cable_engineer'), 'SEA-1', 'S3')
        self.assertEqual(archive['confirmed_count'], 1)
        self.assertEqual(archive['total_loss_db'], 0.1)
        # 幂等键独立生效：换一张工单再用同键提交，仍回放既有条目
        first = self.service.act(Actor('op', 'noc_operator'), first['id'], first['version'], 'test',
                                 {'end_to_end_loss_db': 0.3})
        first = self.service.act(Actor('op', 'repair_manager'), first['id'], first['version'], 'restore',
                                 {'traffic_restored': True, 'restore_capacity_gbps': 400})
        other = self._to_surveyed('C-2')
        again = self.service.act(Actor('eng-b', 'cable_engineer'), other['id'], other['version'], 'splice',
                                 {'occurred_at': '2026-09-05T08:00:00Z', 'splice_loss_db': 0.11,
                                  'spare_used_km': 16, 'idempotency_key': 'IDEM-X'})
        self.assertEqual(again['splice_report']['status'], 'replayed')
        self.assertEqual(
            self.service.segment_archive(Actor('eng-a', 'cable_engineer'), 'SEA-1', 'S3')['confirmed_count'], 1)

    def test_late_record_invalidates_test_and_blocks_restore(self):
        first = self._to_surveyed('C-1')
        first = self.service.act(Actor('eng-a', 'cable_engineer'), first['id'], first['version'], 'splice',
                                 {'occurred_at': '2026-09-01T10:00:00Z', 'splice_loss_db': 0.1,
                                  'spare_used_km': 16, 'report_id': 'R-1'})
        first = self.service.act(Actor('op', 'noc_operator'), first['id'], first['version'], 'test',
                                 {'end_to_end_loss_db': 0.3})
        self.assertTrue(first['payload']['test_passed'])
        # 晚到的接续记录：工单已tested，作为现场数据留存
        late = self.service.act(Actor('eng-b', 'cable_engineer'), first['id'], first['version'], 'splice',
                                {'occurred_at': '2026-09-01T09:30:00Z', 'splice_loss_db': 0.08,
                                 'spare_used_km': 16, 'report_id': 'R-LATE'})
        self.assertEqual(late['splice_report']['status'], 'pending')
        # 直接恢复流量应被拒：档案尚未确认晚到数据（seq未变）——此时仍可恢复；确认后则需复测
        pending_id = late['splice_report']['entry']['id']
        out = self.service.confirm_entry(Actor('eng-c', 'cable_engineer'), pending_id)
        self.assertEqual(out['status'], 'confirmed')
        refreshed = self.service.get_record(Actor('op', 'noc_operator'), first['id'])
        self.assertFalse(refreshed['payload']['test_passed'])  # 原测试结果已失效
        self.assertEqual(refreshed['state'], 'tested')
        with self.assertRaises(Conflict):
            self.service.act(Actor('op', 'noc_operator'), refreshed['id'], refreshed['version'], 'restore',
                             {'traffic_restored': True, 'restore_capacity_gbps': 400})
        # 复测通过后档案seq一致，可恢复
        refreshed = self.service.act(Actor('op', 'noc_operator'), refreshed['id'], refreshed['version'], 'test',
                                     {'end_to_end_loss_db': 0.4})
        restored = self.service.act(Actor('op', 'noc_operator'), refreshed['id'], refreshed['version'], 'restore',
                                    {'traffic_restored': True, 'restore_capacity_gbps': 400})
        self.assertEqual(restored['state'], 'restored')

    def test_late_record_over_budget_rolls_tested_back_to_rectification(self):
        first = self._to_surveyed('C-1', budget=0.2)
        first = self.service.act(Actor('eng-a', 'cable_engineer'), first['id'], first['version'], 'splice',
                                 {'occurred_at': '2026-09-01T10:00:00Z', 'splice_loss_db': 0.12,
                                  'spare_used_km': 16, 'report_id': 'R-1'})
        first = self.service.act(Actor('op', 'noc_operator'), first['id'], first['version'], 'test',
                                 {'end_to_end_loss_db': 0.2})
        # 晚到且损耗大：0.12+0.1=0.22 超预算
        late = self.service.act(Actor('eng-b', 'cable_engineer'), first['id'], first['version'], 'splice',
                                {'occurred_at': '2026-09-01T09:00:00Z', 'splice_loss_db': 0.1,
                                 'spare_used_km': 16, 'report_id': 'R-LATE'})
        self.service.confirm_entry(Actor('eng-c', 'cable_engineer'), late['splice_report']['entry']['id'])
        refreshed = self.service.get_record(Actor('op', 'noc_operator'), first['id'])
        self.assertEqual(refreshed['state'], 'rectification')
        self.assertFalse(refreshed['payload']['test_passed'])

    def test_legacy_record_must_backfill_before_test(self):
        # 模拟旧单：档案里没有任何接续条目，却已走到spliced
        record = self._to_surveyed('C-1')
        record = self.service.act(Actor('eng-a', 'cable_engineer'), record['id'], record['version'], 'splice',
                                  {'occurred_at': '2026-09-01T10:00:00Z', 'splice_loss_db': 0.1,
                                   'spare_used_km': 16, 'report_id': 'R-1'})
        # 清空档案模拟“接续损耗只留在故障单里”的旧数据
        import sqlite3
        con = sqlite3.connect(str(Path(self.temp.name) / 'test.db'))
        con.execute('DELETE FROM splice_entries')
        con.commit(); con.close()
        record = self.service.get_record(Actor('eng-a', 'cable_engineer'), record['id'])

        with self.assertRaises(Conflict):
            self.service.act(Actor('op', 'noc_operator'), record['id'], record['version'], 'test',
                             {'end_to_end_loss_db': 0.3})
        # 补齐历史项前不能继续后续动作；补录后可以
        filled = self.service.act(Actor('eng-a', 'cable_engineer'), record['id'], record['version'], 'backfill',
                                  {'items': [
                                      {'occurred_at': '2026-08-30T08:00:00Z', 'splice_loss_db': 0.09, 'engineer_id': 'eng-old'},
                                      {'occurred_at': '2026-08-31T08:00:00Z', 'splice_loss_db': 0.08, 'engineer_id': 'eng-old'},
                                  ]})
        self.assertEqual(filled['state'], 'spliced')
        self.assertEqual(filled['payload']['segment_splice_count'], 2)
        tested = self.service.act(Actor('op', 'noc_operator'), filled['id'], filled['version'], 'test',
                                  {'end_to_end_loss_db': 0.3})
        self.assertEqual(tested['state'], 'tested')

    def test_backfill_over_budget_returns_to_rectification(self):
        record = self._to_surveyed('C-1', budget=0.15)
        record = self.service.act(Actor('eng-a', 'cable_engineer'), record['id'], record['version'], 'splice',
                                  {'occurred_at': '2026-09-01T10:00:00Z', 'splice_loss_db': 0.1,
                                   'spare_used_km': 16, 'report_id': 'R-1'})
        import sqlite3
        con = sqlite3.connect(str(Path(self.temp.name) / 'test.db'))
        con.execute('DELETE FROM splice_entries')
        con.commit(); con.close()
        record = self.service.get_record(Actor('eng-a', 'cable_engineer'), record['id'])
        filled = self.service.act(Actor('eng-a', 'cable_engineer'), record['id'], record['version'], 'backfill',
                                  {'items': [{'occurred_at': '2026-08-30T08:00:00Z', 'splice_loss_db': 0.18}]})
        self.assertEqual(filled['state'], 'rectification')

    def test_single_splice_threshold_still_enforced(self):
        record = self._to_surveyed('C-1')
        with self.assertRaises(ValidationError):
            self.service.act(Actor('eng-a', 'cable_engineer'), record['id'], record['version'], 'splice',
                             {'occurred_at': '2026-09-01T10:00:00Z', 'splice_loss_db': 0.3,
                              'spare_used_km': 16, 'report_id': 'R-BAD'})
        archive = self.service.segment_archive(Actor('eng-a', 'cable_engineer'), 'SEA-1', 'S3')
        self.assertEqual(archive['confirmed_count'], 0)


if __name__ == '__main__':
    unittest.main()
