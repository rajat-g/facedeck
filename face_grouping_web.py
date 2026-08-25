import shutil
import sqlite3
import threading
import traceback
from pathlib import Path

import cv2
from flask import Flask, jsonify, render_template, request, send_from_directory

from face_grouping_v5 import (
    cosine_similarity,
    load_processed_state,
    read_image,
    save_processed_state,
)

app = Flask(__name__)

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "bmp", "tiff", "webp", "heic", "heif"}
FACE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}

_face_app = None
_face_app_lock = threading.Lock()


class RunState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.processed = 0
        self.total = 0
        self.error = None
        self.log = []

    def reset(self):
        with self.lock:
            self.running = True
            self.processed = 0
            self.total = 0
            self.error = None
            self.log = []

    def add_log(self, message):
        print(message)
        with self.lock:
            self.log.append(message)
            if len(self.log) > 300:
                del self.log[0]

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "processed": self.processed,
                "total": self.total,
                "error": self.error,
                "log": list(self.log[-30:]),
            }


run_state = RunState()


def get_face_app():
    global _face_app
    with _face_app_lock:
        if _face_app is None:
            from insightface.app import FaceAnalysis

            run_state.add_log("Loading InsightFace model (buffalo_l)...")
            _face_app = FaceAnalysis(
                name="buffalo_l",
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            )
            _face_app.prepare(ctx_id=0, det_size=(640, 640))
    return _face_app


def open_db(db_file):
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sum_embedding TEXT NOT NULL,
            count INTEGER NOT NULL,
            directory TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS group_cropped_faces (
            group_id INTEGER NOT NULL,
            face_id TEXT NOT NULL,
            PRIMARY KEY (group_id, face_id)
        )
        """
    )
    conn.commit()
    return conn


def next_group_number(conn, parent_dir):
    numbers = []
    for (directory,) in conn.execute("SELECT directory FROM groups"):
        name = Path(directory).name
        if name.startswith("group_"):
            suffix = name.split("_", 1)[1]
            if suffix.isdigit():
                numbers.append(int(suffix))
    if parent_dir is not None and parent_dir.is_dir():
        for child in parent_dir.iterdir():
            if child.is_dir() and child.name.startswith("group_"):
                suffix = child.name.split("_", 1)[1]
                if suffix.isdigit():
                    numbers.append(int(suffix))
    return max(numbers, default=0) + 1


def next_group_id(conn):
    row = conn.execute("SELECT MAX(id) FROM groups").fetchone()
    return (row[0] or 0) + 1


def run_grouping(input_folder, output_file, output_faces_dir, threshold, db_file):
    try:
        log = run_state.add_log
        conn, processed_files, groups = load_processed_state(db_file)
        out_root = Path(output_faces_dir).resolve()
        out_root.mkdir(parents=True, exist_ok=True)

        face_app = get_face_app()

        new_files = []
        total_files = 0
        for path in Path(input_folder).rglob("*"):
            if path.is_file() and path.suffix.lower().lstrip(".") in IMAGE_EXTENSIONS:
                total_files += 1
                abs_path = path.resolve()
                stat = abs_path.stat()
                key = str(abs_path)
                if key not in processed_files or processed_files[key] != (
                    stat.st_mtime,
                    stat.st_size,
                ):
                    new_files.append(abs_path)
                    processed_files[key] = (stat.st_mtime, stat.st_size)

        with run_state.lock:
            run_state.total = len(new_files)

        log(
            f"Found {len(new_files)} new/changed image(s) "
            f"({total_files} image(s) total in folder)"
        )

        matchable = [
            g for g in groups if g["sum_embedding"] is not None and g["count"] > 0
        ]
        id_counter = max(
            (g["id"] for g in groups if isinstance(g.get("id"), int)), default=0
        )

        for img_path in new_files:
            try:
                img = read_image(str(img_path))
                if img is None:
                    log(f"Could not read image: {img_path}")
                    continue

                faces = face_app.get(img)

                for face_idx, face in enumerate(faces):
                    embedding = face.embedding
                    max_sim = -1.0
                    best_group = None

                    for group in matchable:
                        avg_embed = group["sum_embedding"] / group["count"]
                        sim = cosine_similarity(embedding, avg_embed)
                        if sim > max_sim:
                            max_sim = sim
                            best_group = group

                    face_id = f"{img_path.stem}_{face_idx}"
                    output_path = None

                    if max_sim >= threshold and best_group is not None:
                        if face_id in best_group["cropped_faces"]:
                            continue
                        best_group["sum_embedding"] += embedding
                        best_group["count"] += 1
                        best_group["image_paths"].add(str(img_path))
                        best_group["cropped_faces"].add(face_id)
                        output_path = best_group["directory"] / f"{face_id}.jpg"
                    else:
                        number = next_group_number(conn, out_root)
                        while (out_root / f"group_{number}").exists():
                            number += 1
                        group_dir = out_root / f"group_{number}"
                        group_dir.mkdir(parents=True, exist_ok=True)
                        id_counter += 1
                        output_path = group_dir / f"{face_id}.jpg"
                        new_group = {
                            "id": id_counter,
                            "sum_embedding": embedding.copy(),
                            "count": 1,
                            "image_paths": {str(img_path)},
                            "directory": group_dir,
                            "cropped_faces": {face_id},
                        }
                        groups.append(new_group)
                        matchable.append(new_group)

                    if output_path and not output_path.exists():
                        x1, y1, x2, y2 = face.bbox.astype(int)
                        h, w = img.shape[:2]
                        x1, y1 = max(0, x1), max(0, y1)
                        x2, y2 = min(w, x2), min(h, y2)
                        if x1 >= x2 or y1 >= y2:
                            log(f"Invalid bbox in {img_path}, face {face_idx}")
                            continue
                        if not cv2.imwrite(str(output_path), img[y1:y2, x1:x2]):
                            log(f"Failed to save face crop to {output_path}")

            except Exception as exc:
                log(f"Error processing {img_path}: {exc}")
                traceback.print_exc()
            finally:
                with run_state.lock:
                    run_state.processed += 1

        save_processed_state(conn, processed_files, groups)
        conn.close()

        with open(output_file, "w") as f:
            for i, group in enumerate(groups):
                f.write(f"face {i + 1} (cropped faces in: {group['directory']}):\n")
                for path in sorted(group["image_paths"]):
                    f.write(f"{path}\n")
                f.write("\n")

        run_state.add_log("Processing completed.")
    except Exception as exc:
        traceback.print_exc()
        with run_state.lock:
            run_state.error = str(exc)
        run_state.add_log(f"Failed: {exc}")
    finally:
        with run_state.lock:
            run_state.running = False


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True)
    input_folder = (data.get("input_folder") or "").strip()
    output_file = (data.get("output_file") or "").strip() or "face_groups.txt"
    output_faces = (data.get("output_faces") or "").strip() or "output_faces"
    db_file = (data.get("db_file") or "").strip() or "processing_state.db"
    try:
        threshold = float(data.get("threshold", 0.6))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid threshold"}), 400

    if not input_folder or not Path(input_folder).is_dir():
        return jsonify({"error": "Input folder does not exist"}), 400

    for file_path in (db_file, output_file):
        parent = Path(file_path).parent
        if str(parent):
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return jsonify({"error": f"Cannot create folder for {file_path}: {exc}"}), 400

    snap = run_state.snapshot()
    if snap["running"]:
        return jsonify({"error": "Processing is already running"}), 409

    run_state.reset()
    thread = threading.Thread(
        target=run_grouping,
        args=(input_folder, output_file, output_faces, threshold, db_file),
        daemon=True,
    )
    thread.start()
    return jsonify({"started": True})


@app.route("/api/status")
def api_status():
    return jsonify(run_state.snapshot())


@app.route("/api/groups")
def api_groups():
    db_file = request.args.get("db_file", "processing_state.db")
    if not Path(db_file).exists():
        return jsonify({"groups": []})

    conn = open_db(db_file)
    result = []
    for row in conn.execute("SELECT id, directory FROM groups ORDER BY id"):
        directory = Path(row["directory"])
        faces = []
        if directory.is_dir():
            faces = sorted(
                p.name
                for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in FACE_EXTENSIONS
            )
        result.append(
            {
                "id": row["id"],
                "name": directory.name,
                "directory": str(directory),
                "faces": faces,
            }
        )
    conn.close()
    return jsonify({"groups": result})


@app.route("/api/groups/<int:group_id>/faces/<path:filename>")
def api_face_image(group_id, filename):
    db_file = request.args.get("db_file", "processing_state.db")
    conn = open_db(db_file)
    row = conn.execute(
        "SELECT directory FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "Group not found"}), 404
    directory = Path(row["directory"])
    if not directory.is_dir():
        return jsonify({"error": "Group directory missing"}), 404
    return send_from_directory(directory, filename)


@app.route("/api/groups/<int:group_id>/rename", methods=["POST"])
def api_rename_group(group_id):
    data = request.get_json(force=True)
    db_file = data.get("db_file", "processing_state.db")
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "Name cannot be empty"}), 400
    if any(ch in new_name for ch in '\\/:*?"<>|'):
        return jsonify({"error": "Name contains invalid characters"}), 400

    conn = open_db(db_file)
    row = conn.execute(
        "SELECT directory FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "Group not found"}), 404

    old_dir = Path(row["directory"])
    new_dir = old_dir.parent / new_name
    if new_dir.exists():
        conn.close()
        return jsonify({"error": "A folder with this name already exists"}), 400

    try:
        old_dir.rename(new_dir)
    except OSError as exc:
        conn.close()
        return jsonify({"error": f"Rename failed: {exc}"}), 500

    conn.execute(
        "UPDATE groups SET directory=? WHERE id=?", (str(new_dir), group_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "name": new_name})


@app.route("/api/groups/<int:group_id>/faces/<path:filename>", methods=["DELETE"])
def api_delete_face(group_id, filename):
    db_file = request.args.get("db_file", "processing_state.db")
    conn = open_db(db_file)
    row = conn.execute(
        "SELECT directory FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "Group not found"}), 404

    file_path = Path(row["directory"]) / filename
    try:
        if file_path.exists():
            file_path.unlink()
    except OSError as exc:
        conn.close()
        return jsonify({"error": f"Delete failed: {exc}"}), 500

    conn.execute(
        "DELETE FROM group_cropped_faces WHERE group_id=? AND face_id=?",
        (group_id, Path(filename).stem),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/faces/move", methods=["POST"])
def api_move_face():
    data = request.get_json(force=True)
    db_file = data.get("db_file", "processing_state.db")
    group_id = int(data.get("group_id"))
    filename = Path(data.get("filename"))
    target_id = data.get("target_group_id")
    new_group_name = (data.get("new_group_name") or "").strip()

    conn = open_db(db_file)
    src = conn.execute(
        "SELECT directory FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    if src is None:
        conn.close()
        return jsonify({"error": "Source group not found"}), 404

    src_path = Path(src["directory"]) / filename
    if not src_path.exists():
        conn.close()
        return jsonify({"error": "Face file not found"}), 404

    if target_id in (None, "", -1):
        if any(ch in new_group_name for ch in '\\/:*?"<>|'):
            conn.close()
            return jsonify({"error": "New group name has invalid characters"}), 400
        parent = Path(src["directory"]).parent
        if not new_group_name:
            number = next_group_number(conn, parent)
            new_group_name = f"group_{number}"
            while (parent / new_group_name).exists():
                number += 1
                new_group_name = f"group_{number}"
        dst_dir = parent / new_group_name
        if dst_dir.exists():
            conn.close()
            return jsonify({"error": "A folder with this name already exists"}), 400
        dst_dir.mkdir(parents=True, exist_ok=True)
        target_id = next_group_id(conn)
        conn.execute(
            "INSERT INTO groups(id, sum_embedding, count, directory) VALUES (?, '', 0, ?)",
            (target_id, str(dst_dir)),
        )
    else:
        target_id = int(target_id)
        dst_row = conn.execute(
            "SELECT directory FROM groups WHERE id=?", (target_id,)
        ).fetchone()
        if dst_row is None:
            conn.close()
            return jsonify({"error": "Target group not found"}), 404
        dst_dir = Path(dst_row["directory"])

    dst_path = dst_dir / filename.name
    if dst_path.exists():
        conn.close()
        return jsonify({"error": "A file with this name already exists in target"}), 400

    try:
        shutil.move(str(src_path), str(dst_path))
    except OSError as exc:
        conn.close()
        return jsonify({"error": f"Move failed: {exc}"}), 500

    conn.execute(
        "INSERT OR IGNORE INTO group_cropped_faces(group_id, face_id) VALUES (?, ?)",
        (target_id, filename.stem),
    )
    conn.execute(
        "DELETE FROM group_cropped_faces WHERE group_id=? AND face_id=?",
        (group_id, filename.stem),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "target_group_id": target_id})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
