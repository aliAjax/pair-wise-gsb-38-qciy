import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


def base_of(cue):
    return {"cue_index": cue["cue_index"], "start_ms": cue["start_ms"],
            "end_ms": cue["end_ms"], "text": cue["text"]}


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.c1 = self.db.save_cue(self.version, "bob",
                                   {"cue_index": 1, "start_ms": 1000, "end_ms": 3000,
                                    "text": "seal 海豹在冰面", "expected_revision": 0})
        self.c2 = self.db.save_cue(self.version, "bob",
                                   {"cue_index": 2, "start_ms": 4000, "end_ms": 6000,
                                    "text": "海豹回到海里", "expected_revision": 1})
        # main revision is now 2
        self.uploader = "dave"

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, baseline, operations, uid="batch-1", actor="dave"):
        return self.db.upload_merge(self.version, actor,
                                    {"batch_uid": uid, "baseline_revision": baseline,
                                     "operations": operations})

    def confirm(self, batch_id, actor="alice", role="owner"):
        return self.db.confirm_merge(batch_id, actor, role)

    def test_field_level_auto_merge_and_revision_billing(self):
        # Offline leaves cue 1 untouched, edits text of cue 2 only.
        ops = [{"op": "upsert", "cue_id": self.c2["id"],
                "base": base_of(self.c2),
                "cue_index": 2, "start_ms": 4000, "end_ms": 6000, "text": "海豹游回大海"}]
        batch, created = self.upload(2, ops)
        self.assertTrue(created)
        self.assertEqual(batch["status"], "pending")
        self.assertEqual(batch["items"], [])
        # Import never writes the main version or bumps revision.
        cues = self.db.list_cues(self.version)
        self.assertEqual(cues[1]["text"], "海豹回到海里")

        # Duplicate upload of the same batch uid is idempotent and not re-billed.
        again, created2 = self.upload(2, ops)
        self.assertFalse(created2)
        self.assertEqual(again["id"], batch["id"])

        merged = self.confirm(batch["id"])
        self.assertEqual(merged["status"], "merged")
        self.assertEqual(merged["revision_before"], 2)
        self.assertEqual(merged["revision_after"], 3)  # one cue actually changed
        cues = self.db.list_cues(self.version)
        self.assertEqual(cues[1]["text"], "海豹游回大海")
        self.assertEqual(cues[0]["text"], "seal 海豹在冰面")
        self.assertEqual(len(merged["snapshot_hash"]), 64)
        manifest = merged["snapshot_manifest"]
        self.assertEqual(manifest["revision"], 3)
        self.assertEqual(manifest["merged_batch_uid"], "batch-1")
        # Re-confirming the same batch never bills twice.
        with self.assertRaisesRegex(DomainError, "已经入库"):
            self.confirm(batch["id"])

    def test_both_changed_keeps_both_values_until_owner_resolves(self):
        # Main changes cue 1 text after the offline baseline was taken.
        self.db.save_cue(self.version, "bob",
                         {"cue_id": self.c1["id"], "cue_index": 1, "start_ms": 1000,
                          "end_ms": 3000, "text": "seal 海豹在薄冰上", "expected_revision": 2})
        offline_text = "seal 海豹趴在冰面"
        ops = [{"op": "upsert", "cue_id": self.c1["id"], "base": base_of(self.c1),
                "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": offline_text}]
        batch, _ = self.upload(3, ops)
        self.assertEqual(len(batch["items"]), 1)
        item = batch["items"][0]
        self.assertEqual(item["conflict_type"], "field")
        self.assertEqual(item["field"], "text")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["details"]["main"]["text"], "seal 海豹在薄冰上")
        self.assertEqual(item["details"]["offline"]["text"], offline_text)
        # Main version is untouched; confirming with an open checklist is refused.
        with self.assertRaisesRegex(DomainError, "未裁决"):
            self.confirm(batch["id"])
        # Non-owner members may read the diff but cannot resolve or confirm.
        self.db.assign(self.version, "alice", {"user": "dave", "role": "translator"}, "owner")
        with self.assertRaisesRegex(DomainError, "负责人"):
            self.db.resolve_merge_item(item["id"], "dave", {"decision": "offline"})
        with self.assertRaisesRegex(DomainError, "负责人"):
            self.confirm(batch["id"], actor="dave", role="translator")
        diff = self.db.get_merge(batch["id"])
        self.assertEqual(diff["pending_count"], 1)

        self.db.resolve_merge_item(item["id"], "alice", {"decision": "offline"}, "owner")
        merged = self.confirm(batch["id"])
        texts = {c["cue_index"]: c["text"] for c in self.db.list_cues(self.version)}
        self.assertEqual(texts[1], offline_text)
        self.assertEqual(merged["revision_after"], 4)

    def test_disjoint_fields_merge_without_checklist(self):
        # Main shortens cue 1 duration; offline rewrites its text on the old base.
        self.db.save_cue(self.version, "bob",
                         {"cue_id": self.c1["id"], "cue_index": 1, "start_ms": 1000,
                          "end_ms": 2500, "text": "seal 海豹在冰面", "expected_revision": 2})
        ops = [{"op": "upsert", "cue_id": self.c1["id"], "base": base_of(self.c1),
                "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal 海豹趴在冰面"}]
        batch, _ = self.upload(3, ops)
        self.assertEqual(batch["items"], [])
        self.confirm(batch["id"])
        cue = next(c for c in self.db.list_cues(self.version) if c["id"] == self.c1["id"])
        self.assertEqual(cue["end_ms"], 2500)  # kept main timing
        self.assertEqual(cue["text"], "seal 海豹趴在冰面")  # offline text

    def test_glossary_and_timeline_checklist_then_atomic_merge(self):
        # Offline rewrite uses the forbidden rendering; the new cue overlaps cue 2.
        ops = [
            {"op": "upsert", "cue_id": self.c1["id"], "base": base_of(self.c1),
             "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "密封在冰面"},
            {"op": "upsert", "client_key": "clip-new", "cue_index": 3,
             "start_ms": 5000, "end_ms": 7000, "text": "新的镜头 海豹"},
        ]
        batch, _ = self.upload(2, ops)
        kinds = {(i["cue_key"], i["conflict_type"]) for i in batch["items"]}
        self.assertIn((str(self.c1["id"]), "glossary"), kinds)
        self.assertIn((str(self.c2["id"]), "timeline"), kinds)
        self.assertIn(("new:clip-new", "timeline"), kinds)
        with self.assertRaisesRegex(DomainError, "未裁决"):
            self.confirm(batch["id"])
        ids = {(i["cue_key"], i["conflict_type"]): i["id"] for i in batch["items"]}
        # Glossary: owner supplies compliant custom text.
        self.db.resolve_merge_item(ids[(str(self.c1["id"]), "glossary")], "alice",
                                   {"decision": "custom", "text": "海豹在冰面"}, "owner")
        # Timeline: keep main cue 2, move the new cue clear of it.
        self.db.resolve_merge_item(ids[(str(self.c2["id"]), "timeline")], "alice",
                                   {"decision": "main"}, "owner")
        self.db.resolve_merge_item(ids[("new:clip-new", "timeline")], "alice",
                                   {"decision": "custom", "start_ms": 6000, "end_ms": 7000}, "owner")
        merged = self.confirm(batch["id"])
        cues = self.db.list_cues(self.version)
        self.assertEqual(len(cues), 3)
        new_cue = next(c for c in cues if c["cue_index"] == 3)
        self.assertEqual((new_cue["start_ms"], new_cue["end_ms"]), (6000, 7000))
        self.assertEqual(merged["revision_after"], 2 + 2)  # text edit + new cue
        # Deterministic snapshot reflects the merged cue list.
        self.assertEqual(len(merged["snapshot_manifest"]["cues"]), 3)

    def test_resolution_that_keeps_violating_is_rejected(self):
        ops = [{"op": "upsert", "cue_id": self.c1["id"], "base": base_of(self.c1),
                "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "密封在冰面"}]
        batch, _ = self.upload(2, ops)
        item = batch["items"][0]
        with self.assertRaisesRegex(DomainError, "违反术语表"):
            self.db.resolve_merge_item(item["id"], "alice",
                                       {"decision": "custom", "text": "密封装置"}, "owner")
        # Picking the offending offline side at confirm time is blocked too.
        self.db.resolve_merge_item(item["id"], "alice", {"decision": "offline"}, "owner")
        with self.assertRaisesRegex(DomainError, "违反术语表"):
            self.confirm(batch["id"])

    def test_failed_import_is_retryable_and_changes_nothing(self):
        bad_ops = [{"op": "upsert", "client_key": "x", "cue_index": 9,
                    "start_ms": 5000, "end_ms": 4000, "text": "时间反了"}]
        with self.assertRaisesRegex(DomainError, "字幕时间"):
            self.upload(2, bad_ops, uid="bad-batch")
        self.assertEqual(self.db.list_merges(self.version), [])
        with self.assertRaisesRegex(DomainError, "基线修订号"):
            self.upload(99, [{"op": "upsert", "client_key": "y", "cue_index": 8,
                             "start_ms": 7000, "end_ms": 8000, "text": "x"}], uid="future")
        # Same uid can be retried with a corrected payload.
        good_ops = [{"op": "upsert", "client_key": "x", "cue_index": 9,
                     "start_ms": 7000, "end_ms": 8000, "text": "海豹补充镜头"}]
        batch, created = self.upload(2, good_ops, uid="bad-batch")
        self.assertTrue(created)
        self.confirm(batch["id"])
        with self.assertRaisesRegex(DomainError, "必须携带基线"):
            self.upload(2, [{"op": "delete", "cue_id": self.c1["id"]}], uid="nobase")

    def test_delete_reanchors_comments_and_bills_once(self):
        # 1) A cue-only comment becomes a time-based comment when its cue is deleted.
        comment = self.db.add_comment(self.version, "bob",
                                      {"cue_id": self.c1["id"], "time_ms": 1500, "body": "锚点测试"})
        ops = [{"op": "delete", "cue_id": self.c1["id"], "base": base_of(self.c1)}]
        batch, _ = self.upload(2, ops, uid="batch-del")
        self.confirm(batch["id"])
        stored = next(c for c in self.db.list_comments(self.version) if c["id"] == comment["id"])
        self.assertIsNone(stored["cue_id"])
        self.assertEqual(stored["time_ms"], 1500)

        # 2) A comment whose time is inside another surviving cue re-anchors to it.
        c3 = self.db.save_cue(self.version, "bob",
                              {"cue_index": 4, "start_ms": 8000, "end_ms": 8100,
                               "text": "海豹三", "expected_revision": 3})
        c4 = self.db.save_cue(self.version, "bob",
                              {"cue_index": 5, "start_ms": 8200, "end_ms": 9000,
                               "text": "覆盖者 海豹", "expected_revision": 4})
        anchored = self.db.add_comment(self.version, "bob",
                                       {"cue_id": c3["id"], "time_ms": 8500, "body": "会重锚"})
        self.assertEqual(anchored["cue_id"], c3["id"])
        ops2 = [{"op": "delete", "cue_id": c3["id"], "base": base_of(c3)}]
        batch2, _ = self.upload(5, ops2, uid="batch-2")
        merged2 = self.confirm(batch2["id"])
        moved = next(c for c in self.db.list_comments(self.version) if c["id"] == anchored["id"])
        self.assertEqual(moved["cue_id"], c4["id"])
        self.assertEqual(merged2["revision_after"], 6)

    def test_offline_edits_cue_main_deleted_requires_identity_decision(self):
        ops = [{"op": "upsert", "cue_id": self.c1["id"], "base": base_of(self.c1),
                "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹离线修改"}]
        # Simulate main deleting the cue while the van was away: overwrite rows directly
        # through a fresh cue lifecycle is not exposed, so use a small raw connection.
        import sqlite3
        conn = sqlite3.connect(self.db.path)
        conn.execute("DELETE FROM cues WHERE id=?", (self.c1["id"],))
        conn.commit()
        conn.close()
        batch, _ = self.upload(2, ops)
        item = next(i for i in batch["items"] if i["conflict_type"] == "identity")
        self.assertEqual(item["reason"], "main_deleted")
        with self.assertRaisesRegex(DomainError, "未裁决"):
            self.confirm(batch["id"])
        self.db.resolve_merge_item(item["id"], "alice", {"decision": "main"}, "owner")
        self.confirm(batch["id"])
        self.assertFalse(any(c["cue_index"] == 1 for c in self.db.list_cues(self.version)))

    def test_non_draft_version_cannot_confirm(self):
        ops = [{"op": "upsert", "cue_id": self.c2["id"], "base": base_of(self.c2),
                "cue_index": 2, "start_ms": 4000, "end_ms": 6000, "text": "海豹回大海"}]
        batch, _ = self.upload(2, ops)
        self.db.submit(self.version, "bob")
        with self.assertRaisesRegex(DomainError, "草稿版本"):
            self.confirm(batch["id"])


if __name__ == "__main__":
    unittest.main()
