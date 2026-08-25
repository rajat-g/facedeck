import json
import shutil
import tempfile
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
        self.assertEqual(stats["faces"], 2, "sum of group embedding counts")
        self.assertEqual(stats["largest"][0]["count"], 2)

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
            "/api/faces/move",
            json={
                "group_id": 1,
                "filename": "b_0.jpg",
                "target_group_id": 2,
                "db_file": str(self.db_file),
            },
        )
        self.assertEqual(resp.status_code, 200)
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
            "/api/faces/move",
            json={
                "group_id": 1,
                "filename": "b_0.jpg",
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
            "/api/faces/move",
            json={
                "group_id": 1,
                "filename": "b_0.jpg",
                "target_group_id": -1,
                "new_group_name": "group_2",
                "db_file": str(self.db_file),
            },
        )
        self.assertEqual(resp.status_code, 400)

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

    def test_run_validation_missing_folders(self):
        resp = self.client.post(
            "/api/run",
            json={"input_folders": [str(self.tmp / "nope")]},
        )
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()

