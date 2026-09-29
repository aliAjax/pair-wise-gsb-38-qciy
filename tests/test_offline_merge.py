import json
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        project = self.db.create_project(
            "alice",
            {"name": "离线合并项目", "source_language": "en", "media_name": "offline.mp4",
             "media_sha256": "c" * 64, "duration_ms": 120000},
            "owner",
        )
        self.project = project["id"]
        version = self.db.create_version(self.project, "alice", {"language": "zh-CN"}, "owner")
        self.version = version["id"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def _cue(self, version, index, start, end, text, revision, cue_id=None):
        payload = {"cue_index": index, "start_ms": start, "end_ms": end, "text": text, "expected_revision": revision}
        if cue_id is not None:
            payload["cue_id"] = cue_id
        return self.db.save_cue(version, "bob", payload)

    def _batch(self, ops, baseline_revision=0, baseline_cues=None):
        body = {"baseline_revision": baseline_revision, "operations": ops}
        if baseline_cues is not None:
            body["baseline_cues"] = baseline_cues
        return body

    def _new_version(self):
        v = self.db.create_version(self.project, "alice", {"language": "zh-CN"}, "owner")["id"]
        self.db.assign(v, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(v, "alice", {"user": "carol", "role": "reviewer"}, "owner")
        return v

    def test_fast_forward_offline_edit(self):
        self._cue(self.version, 1, 1000, 3000, "海豹", 0)
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([{"op": "update", "ref": 1, "text": "海豹在冰面"}], baseline_revision=1),
        )
        self.assertEqual(batch["status"], "pending")
        result = json.loads(batch["merge_result"])
        self.assertEqual(result["actions"]["updated"], [1])
        self.assertEqual(result["conflicts"], [])
        confirmed = self.db.confirm_batch(batch["id"], "alice", {})
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(self.db.list_cues(self.version)[0]["text"], "海豹在冰面")
        self.assertEqual(self.db.list_versions()[0]["revision"], 2)

    def test_three_way_field_merge_keeps_both_sides(self):
        first = self._cue(self.version, 1, 1000, 3000, "seal", 0)
        self._cue(self.version, 1, 1000, 3000, "海豹", 1, cue_id=first["id"])  # main edits text
        baseline_cues = [{"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal"}]
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([{"op": "update", "ref": 1, "start_ms": 1000, "end_ms": 2500}],
                        baseline_revision=0, baseline_cues=baseline_cues),
        )
        result = json.loads(batch["merge_result"])
        self.assertEqual(result["conflicts"], [])
        self.db.confirm_batch(batch["id"], "alice", {})
        cues = self.db.list_cues(self.version)
        self.assertEqual(cues[0]["text"], "海豹")   # main side kept
        self.assertEqual(cues[0]["end_ms"], 2500)    # offline side kept

    def test_field_conflict_pending_list_and_default_resolution(self):
        first = self._cue(self.version, 1, 1000, 3000, "seal", 0)
        self._cue(self.version, 1, 1000, 3000, "海豹", 1, cue_id=first["id"])
        baseline_cues = [{"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal"}]
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([{"op": "update", "ref": 1, "text": "海豹在冰面"}],
                        baseline_revision=0, baseline_cues=baseline_cues),
        )
        result = json.loads(batch["merge_result"])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["kind"], "field")
        self.assertEqual(result["conflicts"][0]["field"], "text")
        self.assertEqual(self.db.list_cues(self.version)[0]["text"], "海豹")  # untouched
        self.db.confirm_batch(batch["id"], "alice", {})  # default keeps main
        self.assertEqual(self.db.list_cues(self.version)[0]["text"], "海豹")

    def test_field_conflict_resolution_offline(self):
        first = self._cue(self.version, 1, 1000, 3000, "seal", 0)
        self._cue(self.version, 1, 1000, 3000, "海豹", 1, cue_id=first["id"])
        baseline_cues = [{"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal"}]
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([{"op": "update", "ref": 1, "text": "海豹在冰面"}],
                        baseline_revision=0, baseline_cues=baseline_cues),
        )
        conflict_id = json.loads(batch["merge_result"])["conflicts"][0]["id"]
        self.db.confirm_batch(batch["id"], "alice", {"resolutions": {conflict_id: "offline"}})
        self.assertEqual(self.db.list_cues(self.version)[0]["text"], "海豹在冰面")

    def test_glossary_and_timeline_conflicts_pending(self):
        self.db.set_glossary(self.project, "alice", {"source_term": "seal", "required_translation": "海豹", "forbidden_terms": ["密封"]}, "owner")
        self._cue(self.version, 1, 1000, 3000, "海豹", 0)
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([
                {"op": "update", "ref": 1, "text": "密封装置"},
                {"op": "add", "ref": 2, "cue_index": 2, "start_ms": 2000, "end_ms": 4000, "text": "另一句"},
            ], baseline_revision=1),
        )
        result = json.loads(batch["merge_result"])
        kinds = {c["kind"] for c in result["conflicts"]}
        self.assertIn("glossary", kinds)
        self.assertIn("timeline", kinds)
        self.assertEqual(self.db.list_cues(self.version)[0]["text"], "海豹")  # untouched
        self.db.confirm_batch(batch["id"], "alice", {})
        cues = self.db.list_cues(self.version)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["text"], "密封装置")

    def test_delete_conflict_default_keeps_main(self):
        first = self._cue(self.version, 1, 1000, 3000, "seal", 0)
        self._cue(self.version, 1, 1000, 3000, "海豹", 1, cue_id=first["id"])
        baseline_cues = [{"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal"}]
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([{"op": "delete", "ref": 1}], baseline_revision=0, baseline_cues=baseline_cues),
        )
        result = json.loads(batch["merge_result"])
        self.assertEqual(result["conflicts"][0]["kind"], "delete")
        self.db.confirm_batch(batch["id"], "alice", {})
        self.assertEqual(len(self.db.list_cues(self.version)), 1)  # main kept

    def test_delete_conflict_resolution_offline(self):
        v = self._new_version()
        first = self._cue(v, 1, 1000, 3000, "seal", 0)
        self._cue(v, 1, 1000, 3000, "海豹", 1, cue_id=first["id"])
        baseline_cues = [{"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal"}]
        batch = self.db.upload_batch(
            v, "bob",
            self._batch([{"op": "delete", "ref": 1}], baseline_revision=0, baseline_cues=baseline_cues),
        )
        conflict_id = json.loads(batch["merge_result"])["conflicts"][0]["id"]
        self.db.confirm_batch(batch["id"], "alice", {"resolutions": {conflict_id: "offline"}})
        self.assertEqual(len(self.db.list_cues(v)), 0)  # offline delete applied

    def test_duplicate_upload_settles_once(self):
        self._cue(self.version, 1, 1000, 3000, "海豹", 0)
        body = self._batch([{"op": "update", "ref": 1, "text": "海豹在冰面"}], baseline_revision=1)
        first = self.db.upload_batch(self.version, "bob", body)
        second = self.db.upload_batch(self.version, "bob", body)
        self.assertEqual(first["id"], second["id"])
        self.db.confirm_batch(first["id"], "alice", {})
        third = self.db.upload_batch(self.version, "bob", body)
        self.assertEqual(third["id"], first["id"])
        self.assertEqual(third["status"], "confirmed")
        self.assertEqual(self.db.list_versions()[0]["revision"], 2)

    def test_failed_import_retry(self):
        self._cue(self.version, 1, 1000, 3000, "海豹", 0)
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([{"op": "update", "ref": 1, "text": "海豹在冰面"}], baseline_revision=1),
        )
        with self.db.connect() as conn:
            conn.execute("UPDATE offline_batches SET status='failed',error='boom' WHERE id=?", (batch["id"],))
        retried = self.db.retry_batch(batch["id"], "bob")
        self.assertEqual(retried["status"], "pending")
        self.assertEqual(retried["error"], "")
        self.db.confirm_batch(batch["id"], "alice", {})
        self.assertEqual(self.db.list_cues(self.version)[0]["text"], "海豹在冰面")

    def test_comment_reposition_on_delete(self):
        cue = self._cue(self.version, 1, 1000, 3000, "海豹", 0)
        comment = self.db.add_comment(self.version, "carol", {"cue_id": cue["id"], "time_ms": 1200, "body": "注意术语"}, "reviewer")
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([{"op": "delete", "ref": 1}], baseline_revision=1),
        )
        self.db.confirm_batch(batch["id"], "alice", {})
        after = self.db.list_comments(self.version)[0]
        self.assertIsNone(after["cue_id"])
        self.assertEqual(after["time_ms"], 1200)

    def test_comment_reposition_on_reindex(self):
        cue = self._cue(self.version, 1, 1000, 3000, "海豹", 0)
        self.db.add_comment(self.version, "carol", {"cue_id": cue["id"], "time_ms": 1200, "body": "注意"}, "reviewer")
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([{"op": "update", "ref": 1, "cue_index": 3}], baseline_revision=1),
        )
        self.db.confirm_batch(batch["id"], "alice", {})
        after = self.db.list_comments(self.version)[0]
        cues = self.db.list_cues(self.version)
        self.assertEqual(cues[0]["cue_index"], 3)
        self.assertEqual(after["cue_id"], cues[0]["id"])

    def test_delivery_snapshot_synced_after_merge(self):
        self._cue(self.version, 1, 1000, 3000, "海豹", 0)
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.db.deliver(self.version, "alice")
        before = self.db.list_deliveries()[0]
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([{"op": "update", "ref": 1, "text": "海豹在冰面"}], baseline_revision=1),
        )
        self.db.confirm_batch(batch["id"], "alice", {})
        after = self.db.list_deliveries()[0]
        self.assertNotEqual(after["snapshot_hash"], before["snapshot_hash"])
        self.assertEqual(json.loads(after["manifest"])["cues"][0]["text"], "海豹在冰面")

    def test_member_can_view_but_not_confirm(self):
        self._cue(self.version, 1, 1000, 3000, "海豹", 0)
        batch = self.db.upload_batch(
            self.version, "bob",
            self._batch([{"op": "update", "ref": 1, "text": "海豹在冰面"}], baseline_revision=1),
        )
        viewed = self.db.get_batch(batch["id"], "carol", "reviewer")
        self.assertEqual(viewed["id"], batch["id"])
        with self.assertRaisesRegex(DomainError, "负责人"):
            self.db.confirm_batch(batch["id"], "carol", {})
        with self.assertRaisesRegex(DomainError, "成员"):
            self.db.get_batch(batch["id"], "dave", "viewer")
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.upload_batch(self.version, "carol", self._batch([], baseline_revision=1), "reviewer")


if __name__ == "__main__":
    unittest.main()
