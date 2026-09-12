import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from face_grouping_v5 import open_db
from face_grouping_web import app


def make_face_file(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"\xff\xd8fakejpg")
    return path


class WebApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-test-"))
        self.db_file = self.tmp / "state.db"
        self.out_root = self.tmp / "output_faces"

        self.g1 = self.out_root / "group_1"
        self.g2 = self.out_root / "group_2"
        self.g1.mkdir(parents=True)
        self.g2.mkdir(parents=True)

        self.face_a = make_face_file(self.g1, "a_0.jpg")
        self.face_b = make_face_file(self.g1, "b_0.jpg")
        self.face_c = make_face_file(self.g2, "c_0.jpg")

        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (?, '', 1, ?, ?)",
            [
                (1, str(self.g1), None),
                (2, str(self.g2), "Bob"),
            ],
        )
        conn.executemany(
            "INSERT INTO group_image_paths VALUES (?, ?)",
            [(1, "C:/photos/a.jpg"), (1, "C:/photos/b.jpg"), (2, "C:/photos/c.jpg")],
        )
        conn.execute(
            "INSERT INTO group_cropped_faces VALUES (1, 'a_0'), (1, 'b_0'), (2, 'c_0')"
        )
        conn.commit()
        conn.close()

        app.config["TESTING"] = True
        self.client = app.test_client()
        self.q = f"db_file={self.db_file.as_posix()}"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- basic listing ----------

    def test_groups_include_name_paths_and_mtime(self):
        body = self.client.get(f"/api/groups?{self.q}").get_json()
        groups = {g["id"]: g for g in body["groups"]}
        self.assertEqual(groups[1]["name"], "group_1")
        self.assertEqual(groups[2]["name"], "Bob")
        self.assertEqual(
            groups[1]["image_paths"], ["C:/photos/a.jpg", "C:/photos/b.jpg"]
        )
        self.assertGreater(groups[1]["mtime"], 0)

    def test_stats(self):
        stats = self.client.get(f"/api/stats?{self.q}").get_json()
        self.assertEqual(stats["groups"], 2)
        self.assertEqual(stats["faces"], 3, "crops actually on record")
        self.assertEqual(stats["largest"][0]["count"], 2)

    def test_stats_ignores_approved_away_crops(self):
        for name in ("a_0.jpg", "b_0.jpg"):
            resp = self.client.delete(f"/api/groups/1/faces/{name}?{self.q}")
            self.assertEqual(resp.status_code, 200)
        stats = self.client.get(f"/api/stats?{self.q}").get_json()
        self.assertEqual(stats["faces"], 1)

    def test_export_csv_and_json(self):
        csv_resp = self.client.get(f"/api/export?format=csv&{self.q}")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("group_id,name,directory", csv_resp.get_data(as_text=True))

        json_resp = self.client.get(f"/api/export?format=json&{self.q}")
        rows = json_resp.get_json
        data = rows()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[1]["name"], "Bob")

    def test_rename_updates_name_only(self):
        resp = self.client.post(
            f"/api/groups/1/rename",
            json={"name": "Alice", "db_file": str(self.db_file)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue((self.g1 / "a_0.jpg").exists(), "folder must not be renamed")
        name = self.client.get(f"/api/groups?{self.q}").get_json()["groups"][0]["name"]
        self.assertEqual(name, "Alice")

    # ---------- delete + undo ----------

    def test_delete_moves_to_trash_and_undo_restores(self):
        resp = self.client.delete(
            f"/api/groups/1/faces/a_0.jpg?{self.q}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse((self.g1 / "a_0.jpg").exists())
        trash = self.out_root / ".trash"
        trashed = list(trash.glob("*_a_0.jpg"))
        self.assertEqual(len(trashed), 1, "face should be in trash")

        undo = self.client.post("/api/undo", json={"db_file": str(self.db_file)})
        self.assertEqual(undo.status_code, 200)
        self.assertTrue((self.g1 / "a_0.jpg").exists(), "undo must restore the file")
        self.assertEqual(list(trash.glob("*")), [])

    def test_undo_empty_log(self):
        resp = self.client.post("/api/undo", json={"db_file": str(self.db_file)})
        self.assertEqual(resp.status_code, 404)

    # ---------- move + undo ----------

    def test_move_and_undo(self):
        resp = self.client.post(
            "/api/faces/bulk-move",
            json={
                "items": [{"group_id": 1, "filename": "b_0.jpg"}],
                "target_group_id": 2,
                "db_file": str(self.db_file),
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["moved"], 1)
        self.assertFalse((self.g1 / "b_0.jpg").exists())
        self.assertTrue((self.g2 / "b_0.jpg").exists())

        conn = open_db(str(self.db_file))
        mapping = {
            (r["group_id"], r["face_id"])
            for r in conn.execute("SELECT group_id, face_id FROM group_cropped_faces")
        }
        conn.close()
        self.assertNotIn((1, "b_0"), mapping)
        self.assertIn((2, "b_0"), mapping)

        undo = self.client.post("/api/undo", json={"db_file": str(self.db_file)})
        self.assertEqual(undo.status_code, 200)
        self.assertTrue((self.g1 / "b_0.jpg").exists())
        self.assertFalse((self.g2 / "b_0.jpg").exists())

    def test_move_to_new_named_group(self):
        resp = self.client.post(
            "/api/faces/bulk-move",
            json={
                "items": [{"group_id": 1, "filename": "b_0.jpg"}],
                "target_group_id": -1,
                "new_group_name": "Carol",
                "db_file": str(self.db_file),
            },
        )
        self.assertEqual(resp.status_code, 200)
        new_dir = self.out_root / "Carol"
        self.assertTrue((new_dir / "b_0.jpg").exists())

    def test_move_existing_name_conflict(self):
        resp = self.client.post(
            "/api/faces/bulk-move",
            json={
                "items": [{"group_id": 1, "filename": "b_0.jpg"}],
                "target_group_id": -1,
                "new_group_name": "group_2",
                "db_file": str(self.db_file),
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_move_rejects_reserved_and_malformed_names(self):
        for bad in ("..", ".", "a/b", 'a"b', "trailing. "):
            resp = self.client.post(
                "/api/faces/bulk-move",
                json={
                    "items": [{"group_id": 1, "filename": "b_0.jpg"}],
                    "target_group_id": -1,
                    "new_group_name": bad,
                    "db_file": str(self.db_file),
                },
            )
            self.assertEqual(resp.status_code, 400, bad)
        # Nothing moved.
        self.assertTrue((self.g1 / "b_0.jpg").exists())

    def test_move_trims_surrounding_whitespace(self):
        resp = self.client.post(
            "/api/faces/bulk-move",
            json={
                "items": [{"group_id": 1, "filename": "b_0.jpg"}],
                "target_group_id": -1,
                "new_group_name": "  Carol  ",
                "db_file": str(self.db_file),
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue((self.out_root / "Carol" / "b_0.jpg").exists())

    # ---------- bulk ops ----------

    def test_bulk_delete_then_single_undo_reverts_all(self):
        resp = self.client.post(
            "/api/faces/bulk-delete",
            json={
                "items": [
                    {"group_id": 1, "filename": "a_0.jpg"},
                    {"group_id": 1, "filename": "b_0.jpg"},
                    {"group_id": 2, "filename": "c_0.jpg"},
                ],
                "db_file": str(self.db_file),
            },
        )
        body = resp.get_json
        self.assertEqual(resp.status_code, 200, body())
        self.assertFalse((self.g1 / "a_0.jpg").exists())
        self.assertFalse((self.g2 / "c_0.jpg").exists())

        undo = self.client.post("/api/undo", json={"db_file": str(self.db_file)})
        self.assertEqual(undo.status_code, 200)
        self.assertTrue((self.g1 / "a_0.jpg").exists())
        self.assertTrue((self.g1 / "b_0.jpg").exists())
        self.assertTrue((self.g2 / "c_0.jpg").exists())

        second = self.client.post("/api/undo", json={"db_file": str(self.db_file)})
        self.assertEqual(second.status_code, 404, "bulk op is a single undo entry")

    def test_bulk_move_to_existing_group_skips_same_group(self):
        resp = self.client.post(
            "/api/faces/bulk-move",
            json={
                "items": [
                    {"group_id": 1, "filename": "a_0.jpg"},
                    {"group_id": 2, "filename": "c_0.jpg"},
                    {"group_id": 1, "filename": "missing.jpg"},
                ],
                "target_group_id": 2,
                "db_file": str(self.db_file),
            },
        )
        body = resp.get_json
        self.assertEqual(resp.status_code, 200, body())
        self.assertEqual(body()["moved"], 1, "same-group item skipped")
        self.assertEqual(len(body()["errors"]), 1, "missing file reported")
        self.assertTrue((self.g2 / "a_0.jpg").exists())

    def test_bulk_delete_no_items(self):
        resp = self.client.post(
            "/api/faces/bulk-delete",
            json={"items": [], "db_file": str(self.db_file)},
        )
        self.assertEqual(resp.status_code, 400)

    # ---------- guards ----------

    def test_source_image_guard_blocks_unknown_path(self):
        resp = self.client.get(
            f"/api/source-image?{self.q}&group_id=1&path=C:/Windows/win.ini"
        )
        self.assertEqual(resp.status_code, 404)

    def test_face_image_missing_db_is_404_without_creating_file(self):
        ghost = self.tmp / "ghost.db"
        resp = self.client.get(f"/api/groups/1/faces/a.jpg?db_file={ghost.as_posix()}")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(ghost.exists(), "read path must not create a database")

    def test_run_validation_missing_folders(self):
        resp = self.client.post(
            "/api/run",
            json={"input_folders": [str(self.tmp / "nope")]},
        )
        self.assertEqual(resp.status_code, 400)


class PhotoTagsTestCase(unittest.TestCase):
    """Facebook-style face tags: multi-face photos, moves, deletes, approve."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-tags-"))
        self.db_file = self.tmp / "state.db"
        self.out_root = self.tmp / "output_faces"

        self.g1 = self.out_root / "group_1"
        self.g2 = self.out_root / "group_2"
        self.g1.mkdir(parents=True)
        self.g2.mkdir(parents=True)

        for name in ("p1_0.jpg", "p1_2.jpg", "p2_0.jpg"):
            make_face_file(self.g1, name)
        make_face_file(self.g2, "p1_1.jpg")

        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (?, '', 1, ?, ?)",
            [(1, str(self.g1), "Alice"), (2, str(self.g2), "Bob")],
        )
        conn.executemany(
            "INSERT INTO group_image_paths VALUES (?, ?)",
            [
                (1, "C:/photos/p1.jpg"),
                (1, "C:/photos/p2.jpg"),
                (2, "C:/photos/p1.jpg"),
            ],
        )
        conn.executemany(
            "INSERT INTO group_cropped_faces VALUES (?, ?)",
            [(1, "p1_0"), (2, "p1_1"), (1, "p1_2"), (1, "p2_0")],
        )
        # One photo with faces of TWO people + a second photo with one face.
        conn.executemany(
            "INSERT INTO photo_faces(image_path, face_idx, face_id, x1, y1, x2, y2, group_id, detached) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            [
                ("C:/photos/p1.jpg", 0, "p1_0", 0.1, 0.1, 0.3, 0.4, 1),
                ("C:/photos/p1.jpg", 1, "p1_1", 0.4, 0.2, 0.6, 0.5, 2),
                ("C:/photos/p1.jpg", 2, "p1_2", 0.7, 0.1, 0.9, 0.4, 1),
                ("C:/photos/p2.jpg", 0, "p2_0", 0.2, 0.2, 0.5, 0.6, 1),
            ],
        )
        conn.commit()
        conn.close()

        app.config["TESTING"] = True
        self.client = app.test_client()
        self.q = f"db_file={self.db_file.as_posix()}"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get_tags(self, group_id, path):
        return self.client.get(
            f"/api/photo-tags?{self.q}&group_id={group_id}&path={path}"
        )

    def test_multi_face_photo_returns_each_person(self):
        resp = self.get_tags(1, "C:/photos/p1.jpg")
        self.assertEqual(resp.status_code, 200)
        tags = resp.get_json()["tags"]
        self.assertEqual([t["face_id"] for t in tags], ["p1_0", "p1_1", "p1_2"])
        self.assertEqual([t["name"] for t in tags], ["Alice", "Bob", "Alice"])
        self.assertEqual(tags[0]["bbox"], [0.1, 0.1, 0.3, 0.4])
        self.assertEqual(tags[0]["group_id"], 1)
        self.assertEqual(tags[1]["group_id"], 2)

    def test_tags_guard_blocks_unknown_path(self):
        resp = self.get_tags(1, "C:/Windows/win.ini")
        self.assertEqual(resp.status_code, 404)

    def test_tags_follow_rename(self):
        self.client.post(
            "/api/groups/1/rename",
            json={"name": "Alicia", "db_file": str(self.db_file)},
        )
        tags = self.get_tags(1, "C:/photos/p1.jpg").get_json()["tags"]
        self.assertEqual(tags[0]["name"], "Alicia")
        self.assertEqual(tags[2]["name"], "Alicia")
        self.assertEqual(tags[1]["name"], "Bob")

    def test_tags_follow_move(self):
        resp = self.client.post(
            "/api/faces/bulk-move",
            json={
                "items": [{"group_id": 1, "filename": "p1_0.jpg"}],
                "target_group_id": 2,
                "db_file": str(self.db_file),
            },
        )
        self.assertEqual(resp.status_code, 200)
        tags = self.get_tags(2, "C:/photos/p1.jpg").get_json()["tags"]
        by_face = {t["face_id"]: t for t in tags}
        self.assertEqual(by_face["p1_0"]["name"], "Bob")
        self.assertEqual(by_face["p1_0"]["group_id"], 2)

    def test_delete_unlinks_photo_and_undo_restores(self):
        resp = self.client.delete(f"/api/groups/1/faces/p2_0.jpg?{self.q}")
        self.assertEqual(resp.status_code, 200)
        # p2.jpg had no other live faces, so it drops out of the group.
        self.assertEqual(self.get_tags(1, "C:/photos/p2.jpg").status_code, 404)
        groups = {g["id"]: g for g in
                  self.client.get(f"/api/groups?{self.q}").get_json()["groups"]}
        self.assertNotIn("C:/photos/p2.jpg", groups[1]["image_paths"])

        undo = self.client.post("/api/undo", json={"db_file": str(self.db_file)})
        self.assertEqual(undo.status_code, 200)
        tags = self.get_tags(1, "C:/photos/p2.jpg").get_json()["tags"]
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0]["name"], "Alice")

    def test_approve_keeps_tags_via_stored_group(self):
        resp = self.client.post(
            f"/api/groups/1/approve", json={"db_file": str(self.db_file)}
        )
        self.assertEqual(resp.status_code, 200)
        tags = self.get_tags(1, "C:/photos/p1.jpg").get_json()["tags"]
        by_face = {t["face_id"]: t for t in tags}
        # Mappings were wiped by approve, but stored group keeps the labels.
        self.assertEqual(by_face["p1_0"]["name"], "Alice")
        self.assertEqual(by_face["p1_2"]["name"], "Alice")
        self.assertEqual(by_face["p1_1"]["name"], "Bob")

    def test_record_helpers_normalise_and_reset(self):
        from face_grouping_v5 import record_photo_face, reset_photo_faces

        conn = open_db(str(self.db_file))
        record_photo_face(
            conn, "C:/photos/p3.jpg", 0, "p3_0",
            (10.0, 20.0, 110.0, 220.0), (400, 200, 3), 2,
        )
        # Degenerate boxes are ignored.
        record_photo_face(
            conn, "C:/photos/p3.jpg", 1, "p3_1",
            (5.0, 5.0, 5.0, 9.0), (400, 200, 3), 2,
        )
        conn.commit()
        rows = conn.execute(
            "SELECT face_id, x1, y1, x2, y2, group_id FROM photo_faces "
            "WHERE image_path='C:/photos/p3.jpg' ORDER BY face_idx"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["x1"], rows[0]["y1"], rows[0]["x2"], rows[0]["y2"]),
            (0.05, 0.05, 0.55, 0.55),
        )
        reset_photo_faces(conn, "C:/photos/p3.jpg")
        conn.commit()
        left = conn.execute(
            "SELECT COUNT(*) FROM photo_faces WHERE image_path='C:/photos/p3.jpg'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(left, 0)


class RunTargetTestCase(unittest.TestCase):
    """describe_run_target: fresh-DB and folder-mismatch warnings."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-target-"))
        self.folder_a = self.tmp / "a"
        self.folder_b = self.tmp / "b"
        self.folder_a.mkdir()
        self.folder_b.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_db(self):
        from face_grouping_web import describe_run_target

        info = describe_run_target(
            str(self.tmp / "new.db"), [str(self.folder_a)]
        )
        self.assertTrue(info["fresh_db"])
        self.assertEqual(info["existing_people"], 0)
        self.assertFalse(info["folder_mismatch"])
        self.assertEqual(info["resolved_folders"], [str(self.folder_a.resolve())])

    def test_existing_db_reports_people_and_mismatch(self):
        from face_grouping_web import describe_run_target

        db = self.tmp / "state.db"
        conn = open_db(str(db))
        conn.execute(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (1, '', 1, 'C:/x/g1', 'Alice')"
        )
        conn.execute(
            "INSERT INTO run_meta(key, value) VALUES ('last_input_folders', ?)",
            (json.dumps([str(self.folder_a.resolve())]),),
        )
        conn.commit()
        conn.close()

        same = describe_run_target(str(db), [str(self.folder_a)])
        self.assertFalse(same["fresh_db"])
        self.assertEqual(same["existing_people"], 1)
        self.assertFalse(same["folder_mismatch"])

        other = describe_run_target(str(db), [str(self.folder_b)])
        self.assertTrue(other["folder_mismatch"])
        self.assertEqual(other["existing_people"], 1)


class LoadSaveRoundTripTestCase(unittest.TestCase):
    """load_processed_state must return EVERY group (cursor-reuse regression).

    The per-group queries reuse the same cursor as the outer groups query;
    streaming the outer rows while re-executing silently dropped every group
    but the first on each re-run, collapsing the DB down to 1-2 groups.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-roundtrip-"))
        self.db_file = self.tmp / "state.db"
        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (?, '1.0,2.0', 2, ?, ?)",
            [
                (1, "C:/x/group_1", "Alice"),
                (2, "C:/x/group_2", None),
                (3, "C:/x/group_3", "Carol"),
            ],
        )
        conn.executemany(
            "INSERT INTO group_image_paths VALUES (?, ?)",
            [(1, "C:/p/a.jpg"), (2, "C:/p/b.jpg"), (3, "C:/p/c.jpg")],
        )
        conn.executemany(
            "INSERT INTO group_cropped_faces VALUES (?, ?)",
            [(1, "a_0"), (2, "b_0"), (3, "c_0")],
        )
        conn.executemany(
            "INSERT INTO processed_files VALUES (?, ?, ?)",
            [(f"C:/p/{c}.jpg", 1.0, 10) for c in "abc"],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_returns_all_groups(self):
        from face_grouping_v5 import load_processed_state

        conn, processed, groups = load_processed_state(str(self.db_file))
        conn.close()
        self.assertEqual([g["id"] for g in groups], [1, 2, 3])
        by_id = {g["id"]: g for g in groups}
        self.assertEqual(by_id[1]["name"], "Alice")
        self.assertEqual(by_id[2]["name"], None)
        self.assertEqual(by_id[3]["image_paths"], {"C:/p/c.jpg"})
        self.assertEqual(by_id[2]["cropped_faces"], {"b_0"})
        self.assertEqual(len(processed), 3)

    def test_save_then_load_preserves_all_groups(self):
        from face_grouping_v5 import (
            load_processed_state,
            save_processed_state,
        )

        conn, processed, groups = load_processed_state(str(self.db_file))
        save_processed_state(conn, processed, groups)
        conn.close()

        conn2, processed2, groups2 = load_processed_state(str(self.db_file))
        conn2.close()
        self.assertEqual([g["id"] for g in groups2], [1, 2, 3])
        names = {g["id"]: g["name"] for g in groups2}
        self.assertEqual(names, {1: "Alice", 2: None, 3: "Carol"})
        self.assertEqual(len(processed2), 3)


class StreamingTestCase(unittest.TestCase):
    def test_status_exposes_groups_version(self):
        app.config["TESTING"] = True
        body = app.test_client().get("/api/status").get_json()
        self.assertIn("groups_version", body)
        self.assertEqual(body["groups_version"], 0)
        self.assertFalse(body["running"])

    def test_db_has_busy_timeout_for_concurrent_readers(self):
        import tempfile

        tmp = tempfile.mkdtemp(prefix="facedeck-pragma-")
        try:
            db = str(Path(tmp) / "s.db")
            conn = open_db(db)
            try:
                timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(timeout, 10000)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RunGuardTestCase(unittest.TestCase):
    """Mutations are rejected (409) while a grouping run owns the database."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-guard-"))
        self.db_file = self.tmp / "state.db"
        self.g1 = self.tmp / "out" / "group_1"
        self.g1.mkdir(parents=True)
        make_face_file(self.g1, "a_0.jpg")
        conn = open_db(str(self.db_file))
        conn.execute(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (1, '', 1, ?, 'Alice')",
            (str(self.g1),),
        )
        conn.execute("INSERT INTO group_cropped_faces VALUES (1, 'a_0')")
        conn.commit()
        conn.close()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.q = f"db_file={self.db_file.as_posix()}"
        from face_grouping_web import run_state

        self._run_state = run_state
        run_state.reset()  # simulate an active run

    def tearDown(self):
        with self._run_state.lock:
            self._run_state.running = False
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mutations_rejected_while_running(self):
        db = {"db_file": str(self.db_file)}
        self.assertEqual(
            self.client.post("/api/faces/bulk-delete",
                             json={"items": [{"group_id": 1, "filename": "a_0.jpg"}], **db}).status_code,
            409,
        )
        self.assertEqual(
            self.client.post("/api/faces/bulk-move",
                             json={"items": [{"group_id": 1, "filename": "a_0.jpg"}],
                                   "target_group_id": -1, **db}).status_code,
            409,
        )
        self.assertEqual(
            self.client.delete(f"/api/groups/1/faces/a_0.jpg?{self.q}").status_code,
            409,
        )
        self.assertEqual(
            self.client.post("/api/groups/1/approve", json=db).status_code, 409
        )
        self.assertEqual(
            self.client.post("/api/groups/approve-all", json=db).status_code, 409
        )
        self.assertEqual(
            self.client.post("/api/undo", json=db).status_code, 409
        )
        self.assertEqual(
            self.client.delete(f"/api/groups/1?{self.q}").status_code,
            409,
        )
        self.assertEqual(
            self.client.post("/api/photos/ungroup",
                             json={"paths": ["C:/p.jpg"], **db}).status_code,
            409,
        )
        self.assertEqual(
            self.client.post("/api/rejected/clear",
                             json={"paths": ["C:/p.jpg"], **db}).status_code,
            409,
        )
        self.assertEqual(
            self.client.post("/api/photos/ungroup",
                             json={"paths": ["C:/p.jpg"], **db}).status_code,
            409,
        )
        # The face is untouched and renames (safe) still work.
        self.assertTrue((self.g1 / "a_0.jpg").exists())
        self.assertEqual(
            self.client.post("/api/groups/1/rename",
                             json={"name": "Alicia", **db}).status_code,
            200,
        )

    def test_mutations_allowed_when_idle(self):
        with self._run_state.lock:
            self._run_state.running = False
        db = {"db_file": str(self.db_file)}
        self.assertEqual(
            self.client.post("/api/faces/bulk-delete",
                             json={"items": [{"group_id": 1, "filename": "a_0.jpg"}], **db}).status_code,
            200,
        )


class PhotoSearchEscapeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-like-"))
        self.db_file = self.tmp / "state.db"
        conn = open_db(str(self.db_file))
        conn.execute(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (1, '', 1, 'C:/x/g1', 'Alice')"
        )
        conn.executemany(
            "INSERT INTO group_image_paths VALUES (1, ?)",
            [("C:/p/a%b.jpg",), ("C:/p/a_b.jpg",), ("C:/p/axb.jpg",)],
        )
        conn.commit()
        conn.close()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.q = f"db_file={self.db_file.as_posix()}"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def photos(self, q):
        return self.client.get(
            f"/api/groups/1/photos?{self.q}&q={q}").get_json()["photos"]

    def test_percent_is_literal(self):
        self.assertEqual(self.photos("a%b"), ["C:/p/a%b.jpg"])

    def test_underscore_is_literal(self):
        self.assertEqual(self.photos("a_b"), ["C:/p/a_b.jpg"])


class TrashAndUndoEdgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-edge-"))
        self.db_file = self.tmp / "state.db"
        self.out_root = self.tmp / "out"
        self.g1 = self.out_root / "group_1"
        self.g2 = self.out_root / "group_2"
        self.g1.mkdir(parents=True)
        self.g2.mkdir(parents=True)
        for name in ("a_0.jpg", "b_0.jpg"):
            make_face_file(self.g1, name)
        make_face_file(self.g2, "c_0.jpg")
        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (?, '', 1, ?, ?)",
            [(1, str(self.g1), "Alice"), (2, str(self.g2), "Bob")],
        )
        conn.executemany(
            "INSERT INTO group_cropped_faces VALUES (?, ?)",
            [(1, "a_0"), (1, "b_0"), (2, "c_0")],
        )
        conn.commit()
        conn.close()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.q = f"db_file={self.db_file.as_posix()}"
        self.db = {"db_file": str(self.db_file)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rapid_deletes_keep_distinct_trash_files(self):
        resp = self.client.post(
            "/api/faces/bulk-delete",
            json={"items": [{"group_id": 1, "filename": "a_0.jpg"},
                            {"group_id": 1, "filename": "b_0.jpg"}],
                  **self.db},
        )
        self.assertEqual(resp.status_code, 200)
        trashed = list((self.out_root / ".trash").glob("*"))
        self.assertEqual(len(trashed), 2, "same-ms deletes must not collide")
        undo = self.client.post("/api/undo", json=self.db)
        self.assertEqual(undo.status_code, 200)
        self.assertTrue((self.g1 / "a_0.jpg").exists())
        self.assertTrue((self.g1 / "b_0.jpg").exists())

    def test_move_undo_recreates_missing_source_dir(self):
        resp = self.client.post(
            "/api/faces/bulk-move",
            json={"items": [{"group_id": 1, "filename": "a_0.jpg"}],
                  "target_group_id": 2, **self.db},
        )
        self.assertEqual(resp.status_code, 200)
        shutil.rmtree(self.g1)  # e.g. approved away after the move
        undo = self.client.post("/api/undo", json=self.db)
        self.assertEqual(undo.status_code, 200)
        self.assertTrue((self.g1 / "a_0.jpg").exists())


class EmptyGroupDeleteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-empty-"))
        self.db_file = self.tmp / "state.db"
        self.out_root = self.tmp / "out"
        self.g1 = self.out_root / "group_1"
        self.g1.mkdir(parents=True)
        make_face_file(self.g1, "a_0.jpg")
        self.g2 = self.out_root / "group_2"
        self.g2.mkdir(parents=True)  # exists but empty
        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (?, '', ?, ?, ?)",
            [(1, 1, str(self.g1), "Alice"), (2, 0, str(self.g2), None)],
        )
        conn.execute("INSERT INTO group_image_paths VALUES (1, 'C:/p/a.jpg')")
        conn.execute("INSERT INTO group_cropped_faces VALUES (1, 'a_0')")
        # Stale tag fallback pointing at the empty group (approved long ago).
        conn.execute(
            "INSERT INTO photo_faces(image_path, face_idx, face_id, x1, y1, x2, y2, group_id, detached) "
            "VALUES ('C:/p/a.jpg', 7, 'zzz_0', 0.1, 0.1, 0.2, 0.2, 2, 0)"
        )
        conn.commit()
        conn.close()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.q = f"db_file={self.db_file.as_posix()}"
        self.db = {"db_file": str(self.db_file)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_empty_group_removes_row_and_dir(self):
        resp = self.client.delete(f"/api/groups/2?{self.q}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["dir_removed"])
        self.assertFalse(self.g2.exists())
        groups = self.client.get(f"/api/groups?{self.q}").get_json()["groups"]
        self.assertEqual([g["id"] for g in groups], [1])
        # Stale fallback tags cleaned up.
        left = open_db(str(self.db_file)).execute(
            "SELECT COUNT(*) FROM photo_faces WHERE face_id='zzz_0'").fetchone()[0]
        self.assertEqual(left, 0)

    def test_delete_then_undo_restores_group(self):
        self.assertEqual(self.client.delete(f"/api/groups/2?{self.q}").status_code, 200)
        undo = self.client.post("/api/undo", json=self.db)
        self.assertEqual(undo.status_code, 200)
        groups = {g["id"]: g for g in
                  self.client.get(f"/api/groups?{self.q}").get_json()["groups"]}
        self.assertEqual(groups[2]["name"], "group_2")
        self.assertTrue(self.g2.is_dir())
        left = open_db(str(self.db_file)).execute(
            "SELECT COUNT(*) FROM photo_faces WHERE face_id='zzz_0'").fetchone()[0]
        self.assertEqual(left, 1)

    def test_delete_refuses_nonempty_and_missing(self):
        self.assertEqual(self.client.delete(f"/api/groups/1?{self.q}").status_code, 400)
        self.assertEqual(self.client.delete(f"/api/groups/99?{self.q}").status_code, 404)
        # Photos alone (approved-like) also refuse.
        conn = open_db(str(self.db_file))
        conn.execute("INSERT INTO group_image_paths VALUES (2, 'C:/p/z.jpg')")
        conn.commit()
        conn.close()
        self.assertEqual(self.client.delete(f"/api/groups/2?{self.q}").status_code, 400)


class PhotoFollowTestCase(unittest.TestCase):
    """Moving/deleting a crop re-links its source photo via face tags."""

    P1 = "C:/photos/p1.jpg"
    P2 = "C:/photos/p2.jpg"
    P3 = "C:/photos/p3.jpg"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-follow-"))
        self.db_file = self.tmp / "state.db"
        self.out_root = self.tmp / "out"
        self.g1 = self.out_root / "group_1"
        self.g2 = self.out_root / "group_2"
        self.g1.mkdir(parents=True)
        self.g2.mkdir(parents=True)
        for name in ("p1_0.jpg", "p1_1.jpg", "p2_0.jpg"):
            make_face_file(self.g1, name)
        make_face_file(self.g2, "p3_0.jpg")
        make_face_file(self.g1, "ghost_0.jpg")
        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (?, '', 1, ?, ?)",
            [(1, str(self.g1), "Alice"), (2, str(self.g2), "Bob")],
        )
        conn.executemany(
            "INSERT INTO group_image_paths VALUES (?, ?)",
            [(1, self.P1), (1, self.P2), (2, self.P3)],
        )
        conn.executemany(
            "INSERT INTO group_cropped_faces VALUES (?, ?)",
            [(1, "p1_0"), (1, "p1_1"), (1, "p2_0"), (2, "p3_0"),
             (1, "ghost_0")],
        )
        conn.executemany(
            "INSERT INTO photo_faces(image_path, face_idx, face_id, x1, y1, x2, y2, group_id, detached) "
            "VALUES (?, ?, ?, 0.1, 0.1, 0.2, 0.2, ?, 0)",
            [(self.P1, 0, "p1_0", 1), (self.P1, 1, "p1_1", 1),
             (self.P2, 0, "p2_0", 1), (self.P3, 0, "p3_0", 2)],
        )
        conn.commit()
        conn.close()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.q = f"db_file={self.db_file.as_posix()}"
        self.db = {"db_file": str(self.db_file)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def paths_of(self, group_id):
        groups = {g["id"]: g for g in
                  self.client.get(f"/api/groups?{self.q}").get_json()["groups"]}
        return sorted(groups[group_id]["image_paths"])

    def test_move_shares_photo_when_faces_remain(self):
        resp = self.client.post(
            "/api/faces/bulk-move",
            json={"items": [{"group_id": 1, "filename": "p1_1.jpg"}],
                  "target_group_id": 2, **self.db},
        )
        self.assertEqual(resp.status_code, 200)
        # P1 now belongs to both groups; the other photos are untouched.
        self.assertEqual(self.paths_of(1), [self.P1, self.P2])
        self.assertEqual(self.paths_of(2), [self.P1, self.P3])
        photos = resp.get_json()["photos"]
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0]["photo"], self.P1)
        self.assertFalse(photos[0]["unlinked_source"])
        self.assertTrue((self.g2 / "p1_1.jpg").exists())

    def test_move_transfers_photo_of_last_face(self):
        resp = self.client.post(
            "/api/faces/bulk-move",
            json={"items": [{"group_id": 1, "filename": "p2_0.jpg"}],
                  "target_group_id": 2, **self.db},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.paths_of(1), [self.P1])
        self.assertEqual(self.paths_of(2), [self.P2, self.P3])
        photos = resp.get_json()["photos"]
        self.assertTrue(photos[0]["unlinked_source"])

    def test_move_without_tags_leaves_photos_alone(self):
        resp = self.client.post(
            "/api/faces/bulk-move",
            json={"items": [{"group_id": 1, "filename": "ghost_0.jpg"}],
                  "target_group_id": 2, **self.db},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["photos"], [])
        self.assertEqual(self.paths_of(1), [self.P1, self.P2])
        self.assertEqual(self.paths_of(2), [self.P3])

    def test_undo_move_restores_photo_links(self):
        self.client.post(
            "/api/faces/bulk-move",
            json={"items": [{"group_id": 1, "filename": "p2_0.jpg"}],
                  "target_group_id": 2, **self.db},
        )
        self.assertEqual(self.client.post("/api/undo", json=self.db).status_code, 200)
        self.assertEqual(self.paths_of(1), [self.P1, self.P2])
        self.assertEqual(self.paths_of(2), [self.P3])

    def test_approve_keeps_photos(self):
        self.assertEqual(
            self.client.post("/api/groups/2/approve", json=self.db).status_code, 200)
        self.assertEqual(self.paths_of(2), [self.P3])


class DuplicatesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-dup-"))
        self.db_file = self.tmp / "state.db"
        self.out_root = self.tmp / "out"
        self.g1 = self.out_root / "group_1"
        self.g1.mkdir(parents=True)
        make_face_file(self.g1, "a_0.jpg")
        conn = open_db(str(self.db_file))
        conn.execute(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (1, '', 1, ?, 'Alice')",
            (str(self.g1),),
        )
        conn.execute("INSERT INTO group_image_paths VALUES (1, 'C:/p/orig.jpg')")
        conn.execute("INSERT INTO group_cropped_faces VALUES (1, 'a_0')")
        conn.execute(
            "INSERT INTO photo_faces(image_path, face_idx, face_id, x1, y1, x2, y2, group_id, detached) "
            "VALUES ('C:/p/orig.jpg', 0, 'a_0', 0.1, 0.1, 0.2, 0.2, 1, 0)"
        )
        # orig.jpg <-> copy.jpg are bit-identical; copy is the alias.
        conn.execute(
            "INSERT INTO file_hashes(path, mtime, size, sha256, phash) VALUES "
            "('C:/p/orig.jpg', 1.0, 10, 'SAMESHA', '0000000000000000'), "
            "('C:/p/copy.jpg', 1.0, 10, 'SAMESHA', '0000000000000000')"
        )
        conn.execute(
            "INSERT INTO duplicates(dup_path, canonical_path) VALUES ('C:/p/copy.jpg', 'C:/p/orig.jpg')"
        )
        # near-dupe candidates with known distances (1 vs 63 bits).
        conn.execute(
            "INSERT INTO file_hashes(path, mtime, size, sha256, phash) VALUES "
            "('C:/s/a.jpg', 1.0, 10, 'shaA', '0000000000000000'), "
            "('C:/s/b.jpg', 1.0, 10, 'shaB', '0000000000000001'), "
            "('C:/s/c.jpg', 1.0, 10, 'shaC', 'ffffffffffffffff')"
        )
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (self.tmp / name).write_bytes(b"x")
        conn.commit()
        conn.close()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.q = f"db_file={self.db_file.as_posix()}"
        self.db = {"db_file": str(self.db_file)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_union_listing_counts_and_aliases(self):
        body = self.client.get(f"/api/groups?{self.q}").get_json()["groups"]
        self.assertEqual(body[0]["photo_count"], 2)
        self.assertEqual(body[0]["image_paths"],
                         ["C:/p/copy.jpg", "C:/p/orig.jpg"])
        page = self.client.get(f"/api/groups/1/photos?{self.q}").get_json()
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["aliases"], {"C:/p/copy.jpg": "C:/p/orig.jpg"})

    def test_tags_and_guards_follow_alias(self):
        real = self.tmp / "copy.jpg"
        real.write_bytes(b"\xff\xd8fakejpg")
        conn = open_db(str(self.db_file))
        conn.execute("UPDATE duplicates SET dup_path=? WHERE dup_path='C:/p/copy.jpg'",
                     (real.as_posix(),))
        conn.commit()
        conn.close()
        tags = self.client.get(
            f"/api/photo-tags?{self.q}&group_id=1&path={real.as_posix()}").get_json()
        self.assertEqual(len(tags["tags"]), 1)
        self.assertEqual(tags["tags"][0]["face_id"], "a_0")
        self.assertEqual(tags["duplicate_of"], "C:/p/orig.jpg")
        resp = self.client.get(
            f"/api/source-image?{self.q}&group_id=1&path={real.as_posix()}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("image/jpeg", resp.content_type)

    def test_delete_alias_recycles_and_keeps_canonical(self):
        from unittest import mock

        target = self.tmp / "copy.jpg"
        target.write_bytes(b"same-bytes")
        conn = open_db(str(self.db_file))
        conn.execute("UPDATE duplicates SET dup_path=? WHERE dup_path='C:/p/copy.jpg'",
                     (str(target),))
        conn.execute("UPDATE file_hashes SET path=? WHERE path='C:/p/copy.jpg'",
                     (str(target),))
        conn.commit()
        conn.close()
        with mock.patch("face_grouping_web.send2trash") as st:
            st.side_effect = lambda p: Path(p).unlink()
            resp = self.client.delete(
                "/api/duplicates",
                json={"paths": [str(target)], **self.db},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["deleted"], [str(target)])
        st.assert_called_once_with(str(target))
        self.assertFalse(target.exists())
        groups = self.client.get(f"/api/groups?{self.q}").get_json()["groups"]
        self.assertEqual(groups[0]["photo_count"], 1)  # canonical alone again

    def test_delete_rejects_canonical(self):
        resp = self.client.delete(
            "/api/duplicates", json={"paths": ["C:/p/orig.jpg"], **self.db})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["deleted"], [])
        self.assertTrue(body["errors"])

    def test_link_unlink_round_trip(self):
        link = self.client.post(
            "/api/duplicates/link",
            json={"dup": "C:/s/b.jpg", "canonical": "C:/p/orig.jpg", **self.db})
        self.assertEqual(link.status_code, 200)
        tags = self.client.get(
            f"/api/photo-tags?{self.q}&group_id=1&path=C:/s/b.jpg").get_json()
        self.assertEqual(tags["duplicate_of"], "C:/p/orig.jpg")
        self.assertEqual(len(tags["tags"]), 1)
        groups = {g["id"]: g for g in
                  self.client.get(f"/api/groups?{self.q}").get_json()["groups"]}
        self.assertIn("C:/s/b.jpg", groups[1]["image_paths"])
        undo = self.client.post(
            "/api/duplicates/link",
            json={"unlink": True, "dup": "C:/s/b.jpg", **self.db})
        self.assertEqual(undo.status_code, 200)
        groups = {g["id"]: g for g in
                  self.client.get(f"/api/groups?{self.q}").get_json()["groups"]}
        self.assertNotIn("C:/s/b.jpg", groups[1]["image_paths"])
        # b has no tag rows of its own, so it drops out entirely again.
        self.assertEqual(
            self.client.get(f"/api/photo-tags?{self.q}&group_id=1&path=C:/s/b.jpg").status_code,
            404)

    def test_scan_finds_near_not_exact_nor_dismissed(self):
        # Real files with crafted perceptual hashes (distances 1 vs 63).
        pa, pb, pc = (str(self.tmp / n) for n in ("na.jpg", "nb.jpg", "nc.jpg"))
        for p in (pa, pb, pc):
            Path(p).write_bytes(b"x")
        conn = open_db(str(self.db_file))
        conn.execute("DELETE FROM file_hashes WHERE path LIKE 'C:/s/%'")
        conn.executemany(
            "INSERT INTO file_hashes(path, mtime, size, sha256, phash) VALUES (?, 1.0, 10, ?, ?)",
            [(pa, "shaA", "0000000000000000"),
             (pb, "shaB", "0000000000000001"),
             (pc, "shaC", "ffffffffffffffff")],
        )
        conn.commit()
        conn.close()
        scan = self.client.post(
            "/api/duplicates/scan", json={**self.db, "threshold": 10})
        self.assertEqual(scan.status_code, 200)
        body = scan.get_json()
        self.assertEqual(body["scanned"], 3)
        self.assertEqual(len(body["sets"]), 1)
        members = sorted(p["path"] for p in body["sets"][0]["photos"])
        self.assertEqual(members, sorted((pa, pb)))
        dist = body["sets"][0]["pairs"][0][2]
        self.assertEqual(dist, 1)
        # Match percentage rides along: (64 - 1) / 64 * 100 = 98.4%.
        self.assertEqual(body["sets"][0]["pairs"][0][3], 98.4)
        self.assertEqual(body["sets"][0]["best_pct"], 98.4)
        # Dismiss hides it; undismiss brings it back.
        self.client.post("/api/duplicates/dismiss",
                         json={"pairs": [[pa, pb]], **self.db})
        again = self.client.post(
            "/api/duplicates/scan", json={**self.db, "threshold": 10}).get_json()
        self.assertEqual(again["sets"], [])
        self.client.post("/api/duplicates/dismiss",
                         json={"pairs": [[pa, pb]], "undismiss": True, **self.db})
        back = self.client.post(
            "/api/duplicates/scan", json={**self.db, "threshold": 10}).get_json()
        self.assertEqual(len(back["sets"]), 1)

    def test_pipeline_skips_and_links_exact_duplicate(self):
        import numpy as np

        calls = []

        class FakeFace:
            def __init__(self):
                self.embedding = np.ones(512, dtype=np.float32)
                self.bbox = np.array([10.0, 10.0, 50.0, 50.0])

        class FakeApp:
            def get(self, img):
                calls.append(1)
                return [FakeFace()]

        from PIL import Image

        import face_grouping_web as webmod

        d1, d2 = self.tmp / "w1", self.tmp / "w2"
        d1.mkdir()
        d2.mkdir()
        Image.new("RGB", (100, 100), (10, 200, 30)).save(d1 / "same.jpg", "JPEG")
        shutil.copy(d1 / "same.jpg", d2 / "same.jpg")
        old = webmod.get_face_app
        webmod.get_face_app = lambda *a, **k: FakeApp()
        try:
            db2 = self.tmp / "pipe.db"
            out2 = self.tmp / "pout"
            r = self.client.post("/api/run", json={
                "input_folders": [str(d1), str(d2)],
                "output_faces": str(out2),
                "db_file": str(db2), "threshold": 0.6})
            self.assertEqual(r.status_code, 200)
            for _ in range(90):
                time.sleep(1)
                if not self.client.get("/api/status").get_json()["running"]:
                    break
            self.assertEqual(len(calls), 1, "duplicate must skip detection")
            groups = self.client.get(
                f"/api/groups?db_file={db2.as_posix()}").get_json()["groups"]
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0]["image_paths"]), 2)
            dupes = self.client.get(
                f"/api/duplicates?db_file={db2.as_posix()}").get_json()["sets"]
            self.assertEqual(len(dupes), 1)
            self.assertEqual(len(dupes[0]["duplicates"]), 1)
        finally:
            webmod.get_face_app = old

    def test_dhash_helpers(self):
        from PIL import Image, ImageDraw

        from face_grouping_v5 import dhash_of, sha256_of

        def split(path, left, right):
            img = Image.new("RGB", (64, 64), left)
            ImageDraw.Draw(img).rectangle([32, 0, 64, 64], fill=right)
            img.save(path, "PNG")

        a = self.tmp / "h1.png"
        b = self.tmp / "h2.png"
        split(a, (0, 0, 0), (255, 255, 255))
        shutil.copy(a, b)
        self.assertEqual(sha256_of(str(a)), sha256_of(str(b)))
        self.assertEqual(dhash_of(str(a)), dhash_of(str(b)))
        split(b, (255, 255, 255), (0, 0, 0))  # inverted edge structure
        self.assertNotEqual(sha256_of(str(a)), sha256_of(str(b)))
        self.assertNotEqual(dhash_of(str(a)), dhash_of(str(b)))
        bad = self.tmp / "bad.jpg"
        bad.write_bytes(b"not-an-image-at-all")
        self.assertIsNone(dhash_of(str(bad)))
        self.assertIsNone(dhash_of(str(self.tmp / "missing.jpg")))


class FacelessTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-faceless-"))
        self.db_file = self.tmp / "state.db"
        self.faced = self.tmp / "faced.jpg"
        self.lonely = self.tmp / "lonely.jpg"
        self.alias = self.tmp / "alias.jpg"
        self.alias2 = self.tmp / "alias2.jpg"
        for p in (self.faced, self.lonely, self.alias, self.alias2):
            p.write_bytes(b"\xff\xd8fakejpg")
        # ghost.jpg was processed but the file is gone.
        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT INTO processed_files(file_path, mtime, size) VALUES (?, 1.0, 10)",
            [(str(self.faced),), (str(self.lonely),), (str(self.alias),),
             (str(self.alias2),), (str(self.tmp / "ghost.jpg"),)],
        )
        conn.execute(
            "INSERT INTO photo_faces(image_path, face_idx, face_id, x1, y1, x2, y2, group_id, detached) "
            "VALUES (?, 0, 'f_0', 0.1, 0.1, 0.2, 0.2, 1, 0)",
            (str(self.faced),),
        )
        # alias.jpg is a skipped duplicate of faced.jpg (inherits faces);
        # alias2.jpg aliases lonely.jpg (no faces anywhere).
        conn.execute(
            "INSERT INTO duplicates(dup_path, canonical_path) VALUES (?, ?)",
            (str(self.alias), str(self.faced)),
        )
        conn.execute(
            "INSERT INTO duplicates(dup_path, canonical_path) VALUES (?, ?)",
            (str(self.alias2), str(self.lonely)),
        )
        conn.commit()
        conn.close()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.q = f"db_file={self.db_file.as_posix()}"
        self.db = {"db_file": str(self.db_file)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lists_only_truly_faceless(self):
        body = self.client.get(f"/api/faceless?{self.q}").get_json()
        paths = {p["path"] for p in body["photos"]}
        self.assertEqual(
            paths,
            {str(self.lonely), str(self.alias2), str(self.tmp / "ghost.jpg")},
        )
        flags = {p["path"]: p["exists"] for p in body["photos"]}
        self.assertTrue(flags[str(self.lonely)])
        self.assertFalse(flags[str(self.tmp / "ghost.jpg")])

    def test_faceless_missing_db_is_empty(self):
        body = self.client.get(
            "/api/faceless?db_file=" + str(self.tmp / "nope.db")).get_json()
        self.assertEqual(body, {"photos": []})

    def test_faceless_image_is_constrained_to_list(self):
        # NOTE: DB-consistent separators (the UI round-trips DB paths verbatim,
        # same convention as photo_in_group).
        ok = self.client.get(
            f"/api/faceless-image?{self.q}&path={str(self.lonely)}")
        self.assertEqual(ok.status_code, 200)
        # faced.jpg has faces -> only servable via its group, not here.
        self.assertEqual(
            self.client.get(
                f"/api/faceless-image?{self.q}&path={str(self.faced)}").status_code,
            404)
        self.assertEqual(
            self.client.get(
                f"/api/faceless-image?{self.q}&path={self.tmp / 'nope.jpg'}").status_code,
            404)

    def test_reveal_allows_faceless_without_group(self):
        from unittest import mock

        with mock.patch("face_grouping_web.subprocess.Popen"):
            allowed = self.client.post(
                "/api/reveal",
                json={"group_id": None, "path": str(self.lonely), **self.db})
            self.assertEqual(allowed.status_code, 200)
            denied = self.client.post(
                "/api/reveal",
                json={"group_id": None, "path": str(self.tmp / "stranger.jpg"),
                      **self.db})
            self.assertEqual(denied.status_code, 404)

    def test_faceless_in_folders_filters(self):
        from face_grouping_v5 import faceless_in_folders

        d1 = self.tmp / "lib1"
        d2 = self.tmp / "lib2"
        d1.mkdir()
        d2.mkdir()
        p1 = d1 / "a.jpg"
        p2 = d2 / "b.jpg"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT INTO processed_files(file_path, mtime, size) VALUES (?, 1.0, 1)",
            [(str(p1.resolve()),), (str(p2.resolve()),), (str(d1 / "gone.jpg"),)],
        )
        conn.commit()
        try:
            self.assertEqual(faceless_in_folders(conn, [str(d1)]), [p1.resolve()])
            both = faceless_in_folders(conn, [str(d1), str(d2)])
            self.assertEqual(sorted(both), sorted([p1.resolve(), p2.resolve()]))
        finally:
            conn.close()

    def test_run_rejects_bad_det_thresh(self):
        r = self.client.post("/api/run", json={
            "input_folders": [str(self.tmp)], "db_file": str(self.db_file),
            "det_thresh": 99})
        self.assertEqual(r.status_code, 400)

    def test_run_only_faceless_needs_targets(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        r = self.client.post("/api/run", json={
            "input_folders": [str(empty)], "db_file": str(self.db_file),
            "only_faceless": True})
        self.assertEqual(r.status_code, 400)
        r2 = self.client.post("/api/run", json={
            "input_folders": [str(self.tmp)],
            "db_file": str(self.tmp / "nope.db"), "only_faceless": True})
        self.assertEqual(r2.status_code, 400)

    def test_run_only_faceless_refuses_already_scanned(self):
        # lonely.jpg + alias2.jpg are faceless; record them scanned at 0.3.
        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT OR REPLACE INTO scan_det(path, det_thresh) VALUES (?, ?)",
            [(str(self.lonely), 0.3), (str(self.alias2), 0.3)],
        )
        conn.commit()
        conn.close()
        # Same threshold again -> refused with guidance, no run started.
        r = self.client.post("/api/run", json={
            "input_folders": [str(self.tmp)], "db_file": str(self.db_file),
            "only_faceless": True, "det_thresh": 0.3})
        self.assertEqual(r.status_code, 400)
        self.assertIn("already scanned", r.get_json()["error"])
        self.assertFalse(
            self.client.get("/api/status").get_json()["running"])

    def test_faceless_in_folders_respects_recorded_threshold(self):
        from face_grouping_v5 import faceless_in_folders

        d1 = self.tmp / "tlib1"
        d2 = self.tmp / "tlib2"
        d1.mkdir()
        d2.mkdir()
        p1 = d1 / "a.jpg"
        p2 = d2 / "b.jpg"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT INTO processed_files(file_path, mtime, size) VALUES (?, 1.0, 1)",
            [(str(p1.resolve()),), (str(p2.resolve()),)],
        )
        conn.execute(
            "INSERT OR REPLACE INTO scan_det(path, det_thresh) VALUES (?, 0.5)",
            (str(p1.resolve()),))
        conn.execute(
            "INSERT OR REPLACE INTO scan_det(path, det_thresh) VALUES (?, 0.2)",
            (str(p2.resolve()),))
        conn.commit()
        try:
            # Unfiltered: both. At 0.3: only p1 (scanned 0.5 > 0.3).
            self.assertEqual(
                sorted(faceless_in_folders(conn, [str(d1), str(d2)])),
                sorted([p1.resolve(), p2.resolve()]))
            self.assertEqual(
                faceless_in_folders(conn, [str(d1), str(d2)], 0.3),
                [p1.resolve()])
            self.assertEqual(
                sorted(faceless_in_folders(conn, [str(d1), str(d2)], 0.1)),
                sorted([p1.resolve(), p2.resolve()]))
            self.assertEqual(
                faceless_in_folders(conn, [str(d1), str(d2)], 0.5), [])
        finally:
            conn.close()

    def test_faceless_lists_last_scan_det(self):
        conn = open_db(str(self.db_file))
        conn.execute(
            "INSERT OR REPLACE INTO scan_det(path, det_thresh) VALUES (?, 0.3)",
            (str(self.lonely),))
        conn.commit()
        conn.close()
        body = self.client.get(f"/api/faceless?{self.q}").get_json()
        by_path = {p["path"]: p for p in body["photos"]}
        self.assertEqual(by_path[str(self.lonely)]["last_det"], 0.3)
        self.assertIsNone(by_path[str(self.alias2)]["last_det"])

    def test_get_face_app_caches_per_det_thresh(self):
        from unittest import mock

        import face_grouping_web as webmod

        old = dict(webmod._face_apps)
        webmod._face_apps.clear()
        try:
            with mock.patch("insightface.app.FaceAnalysis") as FA:
                made = []

                def _make(*a, **k):
                    m = mock.MagicMock()
                    made.append(m)
                    return m

                FA.side_effect = _make
                a1 = webmod.get_face_app(0.5)
                a2 = webmod.get_face_app(0.5)
                a3 = webmod.get_face_app(0.3)
                self.assertIs(a1, a2)
                self.assertIsNot(a1, a3)
                self.assertEqual(len(made), 2)  # one load per threshold
                used = sorted(
                    c.kwargs.get("det_thresh")
                    for m in made for c in m.prepare.call_args_list
                )
                self.assertEqual(used, [0.3, 0.5])
        finally:
            webmod._face_apps.clear()
            webmod._face_apps.update(old)


class BulkDbRoutingTestCase(unittest.TestCase):
    """Regression: bulk move/delete must honor an explicit non-default db_file.

    The UI once omitted db_file on these two calls, so with any database but
    the default selected, deletes/moves silently hit processing_state.db —
    surfacing as "Face file not found" for files that plainly exist.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-routing-"))
        self.default_db = self.tmp / "processing_state.db"
        self.selected_db = self.tmp / "sample.db"
        self.default_dir = self.tmp / "default_faces" / "group_1"
        self.selected_dir = self.tmp / "sample_faces" / "group_1"
        self.selected_dir2 = self.tmp / "sample_faces" / "group_2"
        for d in (self.default_dir, self.selected_dir, self.selected_dir2):
            d.mkdir(parents=True)
        make_face_file(self.default_dir, "d_0.jpg")
        make_face_file(self.selected_dir, "s_0.jpg")
        conn = open_db(str(self.default_db))
        conn.execute(
            "INSERT INTO groups(id, sum_embedding, count, directory) "
            "VALUES (1, '', 1, ?)", (str(self.default_dir),))
        conn.execute("INSERT INTO group_cropped_faces VALUES (1, 'd_0')")
        conn.commit()
        conn.close()
        conn = open_db(str(self.selected_db))
        conn.executemany(
            "INSERT INTO groups(id, sum_embedding, count, directory) VALUES (?, '', 1, ?)",
            [(1, str(self.selected_dir)), (2, str(self.selected_dir2))],
        )
        conn.execute("INSERT INTO group_cropped_faces VALUES (1, 's_0')")
        conn.execute(
            "INSERT INTO photo_faces(image_path, face_idx, face_id, x1, y1, x2, y2, group_id, detached) "
            "VALUES ('C:/p.jpg', 0, 's_0', 0.1, 0.1, 0.2, 0.2, 1, 0)"
        )
        conn.commit()
        conn.close()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.sel = {"db_file": str(self.selected_db)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _faces(self, db, group_id):
        conn = open_db(str(db))
        try:
            return {r[0] for r in conn.execute(
                "SELECT face_id FROM group_cropped_faces WHERE group_id=?", (group_id,))}
        finally:
            conn.close()

    def test_bulk_delete_hits_selected_db(self):
        r = self.client.post("/api/faces/bulk-delete", json={
            **self.sel, "items": [{"group_id": 1, "filename": "s_0.jpg"}]})
        self.assertEqual(r.status_code, 200)
        # Selected DB: face gone from folder and registry...
        self.assertFalse((self.selected_dir / "s_0.jpg").exists())
        self.assertEqual(self._faces(self.selected_db, 1), set())
        # ...default DB completely untouched.
        self.assertTrue((self.default_dir / "d_0.jpg").exists())
        self.assertEqual(self._faces(self.default_db, 1), {"d_0"})

    def test_bulk_delete_accepts_db_file_from_query(self):
        r = self.client.post(
            f"/api/faces/bulk-delete?db_file={self.selected_db.as_posix()}",
            json={"items": [{"group_id": 1, "filename": "s_0.jpg"}]})
        self.assertEqual(r.status_code, 200)
        self.assertFalse((self.selected_dir / "s_0.jpg").exists())
        self.assertTrue((self.default_dir / "d_0.jpg").exists())

    def test_bulk_move_hits_selected_db(self):
        r = self.client.post("/api/faces/bulk-move", json={
            **self.sel,
            "items": [{"group_id": 1, "filename": "s_0.jpg"}],
            "target_group_id": 2})
        self.assertEqual(r.status_code, 200)
        self.assertTrue((self.selected_dir2 / "s_0.jpg").exists())
        self.assertFalse((self.selected_dir / "s_0.jpg").exists())
        self.assertEqual(self._faces(self.selected_db, 2), {"s_0"})
        # Default DB untouched: its group 1 still has its face on disk and on record.
        self.assertTrue((self.default_dir / "d_0.jpg").exists())
        self.assertEqual(self._faces(self.default_db, 1), {"d_0"})

    def test_frontend_sends_db_file_on_bulk_ops(self):
        src = (Path(__file__).resolve().parent.parent
               / "static" / "js" / "ops.js").read_text(encoding="utf-8")
        for fn in ("deleteFaces", "moveFaces"):
            m = re.search(r"export async function " + fn + r"\b(.*?)\n}\n", src, re.S)
            self.assertIsNotNone(m, f"{fn} not found in ops.js")
            self.assertIn(
                "db_file", m.group(1),
                f"{fn} must send db_file (wrong-database regression)")


class UngroupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="facedeck-ungroup-"))
        self.db_file = self.tmp / "state.db"
        out = self.tmp / "out"
        self.g1 = out / "group_1"
        self.g2 = out / "group_2"
        self.g3 = out / "group_3"
        for d in (self.g1, self.g2, self.g3):
            d.mkdir(parents=True)
        self.photoA = str(self.tmp / "a.jpg")  # 2 live faces, groups 1+2, crops on disk
        self.photoB = str(self.tmp / "b.jpg")  # approved-style: live row, no crop file
        self.photoC = str(self.tmp / "c.jpg")  # dup alias of photoA
        make_face_file(self.g1, "aA_0.jpg")
        make_face_file(self.g2, "aA_1.jpg")
        (self.tmp / "a.jpg").write_bytes(b"x")
        (self.tmp / "b.jpg").write_bytes(b"x")
        (self.tmp / "c.jpg").write_bytes(b"x")
        conn = open_db(str(self.db_file))
        conn.executemany(
            "INSERT INTO groups(id, sum_embedding, count, directory) VALUES (?, '', 1, ?)",
            [(1, str(self.g1)), (2, str(self.g2)), (3, str(self.g3))],
        )
        conn.execute("INSERT INTO processed_files(file_path, mtime, size) VALUES (?, 1.0, 1), (?, 1.0, 1), (?, 1.0, 1)",
                     (self.photoA, self.photoB, self.photoC))
        conn.executemany(
            "INSERT INTO photo_faces(image_path, face_idx, face_id, x1, y1, x2, y2, group_id, detached) "
            "VALUES (?, ?, ?, 0.1, 0.1, 0.4, 0.4, ?, 0)",
            [(self.photoA, 0, "aA_0", 1), (self.photoA, 1, "aA_1", 2),
             (self.photoB, 0, "bB_0", 3)],
        )
        conn.execute("INSERT INTO group_cropped_faces VALUES (1, 'aA_0'), (2, 'aA_1')")
        conn.executemany(
            "INSERT INTO group_image_paths VALUES (?, ?)",
            [(1, self.photoA), (2, self.photoA), (3, self.photoB)],
        )
        conn.execute("INSERT INTO duplicates(dup_path, canonical_path) VALUES (?, ?)",
                     (self.photoC, self.photoA))
        conn.commit()
        conn.close()
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.db = {"db_file": str(self.db_file)}
        self.q = f"db_file={self.db_file.as_posix()}"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _detached(self, path):
        conn = open_db(str(self.db_file))
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT face_id, detached FROM photo_faces WHERE image_path=? ORDER BY face_idx",
                (path,))]
        finally:
            conn.close()

    def test_ungroup_returns_photo_to_faceless(self):
        r = self.client.post("/api/photos/ungroup",
                             json={**self.db, "paths": [self.photoA]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["ungrouped"][0]["faces"], 2)
        # Crops trashed, registries cleared, tags detached.
        self.assertFalse((self.g1 / "aA_0.jpg").exists())
        self.assertFalse((self.g2 / "aA_1.jpg").exists())
        self.assertTrue(any((self.tmp / "out" / ".trash").glob("*.jpg")))
        conn = open_db(str(self.db_file))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM group_cropped_faces").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM group_image_paths WHERE image_path=?", (self.photoA,)).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM rejected_faces WHERE image_path=?", (self.photoA,)).fetchone()[0], 2)
        finally:
            conn.close()
        self.assertEqual(self._detached(self.photoA), [("aA_0", 1), ("aA_1", 1)])
        # ...and the photo is back in No-faces.
        faceless = {p["path"] for p in
                    self.client.get(f"/api/faceless?{self.q}").get_json()["photos"]}
        self.assertIn(self.photoA, faceless)

    def test_ungroup_undo_restores_everything(self):
        self.client.post("/api/photos/ungroup", json={**self.db, "paths": [self.photoA]})
        u = self.client.post("/api/undo", json=self.db)
        self.assertEqual(u.status_code, 200)
        self.assertTrue((self.g1 / "aA_0.jpg").exists())
        self.assertTrue((self.g2 / "aA_1.jpg").exists())
        self.assertEqual(self._detached(self.photoA), [("aA_0", 0), ("aA_1", 0)])
        conn = open_db(str(self.db_file))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM group_cropped_faces").fetchone()[0], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM group_image_paths WHERE image_path=?", (self.photoA,)).fetchone()[0], 2)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM rejected_faces WHERE image_path=?", (self.photoA,)).fetchone()[0], 0)
        finally:
            conn.close()
        faceless = {p["path"] for p in
                    self.client.get(f"/api/faceless?{self.q}").get_json()["photos"]}
        self.assertNotIn(self.photoA, faceless)

    def test_ungroup_undo_survives_intervening_rescan(self):
        # Ungroup, then simulate a rescan: it wipes tag rows (reset) and, with
        # suppression active, adds none back. Undo must still fully restore.
        self.client.post("/api/photos/ungroup", json={**self.db, "paths": [self.photoA]})
        conn = open_db(str(self.db_file))
        conn.execute("DELETE FROM photo_faces WHERE image_path=?", (self.photoA,))
        conn.execute("INSERT OR REPLACE INTO scan_det(path, det_thresh) VALUES (?, 0.2)",
                     (self.photoA,))
        conn.commit()
        conn.close()
        u = self.client.post("/api/undo", json=self.db)
        self.assertEqual(u.status_code, 200)
        self.assertTrue((self.g1 / "aA_0.jpg").exists())
        self.assertTrue((self.g2 / "aA_1.jpg").exists())
        self.assertEqual(self._detached(self.photoA), [("aA_0", 0), ("aA_1", 0)])
        conn = open_db(str(self.db_file))
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM group_image_paths WHERE image_path=?",
                (self.photoA,)).fetchone()[0], 2)
        finally:
            conn.close()
        faceless = {p["path"] for p in
                    self.client.get(f"/api/faceless?{self.q}").get_json()["photos"]}
        self.assertNotIn(self.photoA, faceless)

    def test_suppression_helpers(self):
        from face_grouping_v5 import is_suppressed

        shape = (100, 200, 3)
        # Identical box -> suppressed; far box -> kept; degenerate image -> kept.
        self.assertTrue(is_suppressed((10, 10, 50, 50), shape, [(0.05, 0.1, 0.25, 0.5)]))
        self.assertFalse(is_suppressed((150, 10, 190, 50), shape, [(0.05, 0.1, 0.25, 0.5)]))
        self.assertFalse(is_suppressed((10, 10, 50, 50), (0, 0, 3), [(0.05, 0.1, 0.25, 0.5)]))
        self.assertFalse(is_suppressed((10, 10, 50, 50), shape, []))

    def test_ungroup_alias_unlinks_only(self):
        r = self.client.post("/api/photos/ungroup",
                             json={**self.db, "paths": [self.photoC]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["ungrouped"][0]["unlinked"], self.photoA)
        conn = open_db(str(self.db_file))
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM duplicates WHERE dup_path=?", (self.photoC,)).fetchone())
            # Canonical untouched: rows live, crops on disk.
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM photo_faces WHERE image_path=? AND detached=0",
                (self.photoA,)).fetchone()[0], 2)
        finally:
            conn.close()
        self.assertTrue((self.g1 / "aA_0.jpg").exists())
        faceless = {p["path"] for p in
                    self.client.get(f"/api/faceless?{self.q}").get_json()["photos"]}
        self.assertIn(self.photoC, faceless)
        # Undo re-links the alias.
        self.assertEqual(self.client.post("/api/undo", json=self.db).status_code, 200)
        conn = open_db(str(self.db_file))
        try:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM duplicates WHERE dup_path=?", (self.photoC,)).fetchone())
        finally:
            conn.close()

    def test_ungroup_approved_without_files(self):
        r = self.client.post("/api/photos/ungroup",
                             json={**self.db, "paths": [self.photoB]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._detached(self.photoB), [("bB_0", 1)])
        faceless = {p["path"] for p in
                    self.client.get(f"/api/faceless?{self.q}").get_json()["photos"]}
        self.assertIn(self.photoB, faceless)
        self.assertEqual(self.client.post("/api/undo", json=self.db).status_code, 200)
        self.assertEqual(self._detached(self.photoB), [("bB_0", 0)])

    def test_rejected_clear_rearms_photo(self):
        self.client.post("/api/photos/ungroup", json={**self.db, "paths": [self.photoA]})
        conn = open_db(str(self.db_file))
        conn.execute("INSERT OR REPLACE INTO scan_det(path, det_thresh) VALUES (?, 0.3)",
                     (self.photoA,))
        conn.commit()
        conn.close()
        before = {p["path"]: p for p in
                  self.client.get(f"/api/faceless?{self.q}").get_json()["photos"]}
        self.assertEqual(before[self.photoA]["rejected"], 2)
        r = self.client.post("/api/rejected/clear",
                             json={**self.db, "paths": [self.photoA]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["cleared"],
                         [{"photo": self.photoA, "faces": 2}])
        after = {p["path"]: p for p in
                 self.client.get(f"/api/faceless?{self.q}").get_json()["photos"]}
        self.assertEqual(after[self.photoA]["rejected"], 0)
        # Still faceless (no live faces) — but the scan record is gone, so the
        # next rescan will reconsider it at any threshold.
        self.assertIn(self.photoA, after)
        conn = open_db(str(self.db_file))
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM scan_det WHERE path=?", (self.photoA,)).fetchone())
        finally:
            conn.close()

    def test_rejected_clear_needs_paths(self):
        self.assertEqual(
            self.client.post("/api/rejected/clear", json=self.db).status_code, 400)

    def test_ungroup_nothing_to_do(self):
        r = self.client.post("/api/photos/ungroup",
                             json={**self.db, "paths": [str(self.tmp / "ghost.jpg")]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/photos/ungroup", json=self.db)
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()

