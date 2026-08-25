import csv
import io
import json
import shutil
import sqlite3
import threading
import time
import traceback
from pathlib import Path

import cv2
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

from face_grouping_v5 import (
    cosine_similarity,
    load_processed_state,
    open_db,
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
        self.cancel_requested = False

    def reset(self):
        with self.lock:
            self.running = True
            self.processed = 0
            self.total = 0
            self.error = None
            self.log = []
            self.cancel_requested = False

    def request_cancel(self):
        with self.lock:
            if self.running:
                self.cancel_requested = True
                return True
            return False

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
                "cancel_requested": self.cancel_requested,
            }


run_state = RunState()

_dialog_lock = threading.Lock()


def pick_native_path(mode, initialdir=None):
    """Open a native folder/file dialog and return the selected path or None."""
    import tkinter as tk
    from tkinter import filedialog

    result = {"path": None}

    def _ask():
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if mode == "folder":
                result["path"] = filedialog.askdirectory(initialdir=initialdir)
            elif mode == "save":
                result["path"] = filedialog.asksaveasfilename(
                    initialdir=initialdir,
                    initialfile="processing_state.db",
                    defaultextension=".db",
                    filetypes=[("Database files", "*.db"), ("All files", "*.*")],
                    confirmoverwrite=False,
                )
            else:
                result["path"] = filedialog.askopenfilename(
                    initialdir=initialdir,
                    filetypes=[("Database files", "*.db"), ("All files", "*.*")],
                )
        finally:
            root.destroy()

    with _dialog_lock:
        thread = threading.Thread(target=_ask)
        thread.start()
        thread.join()
    return result["path"]


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


def record_undo(conn, action, items):
    """Append an undo entry. `items` is a list of per-file detail dicts."""
    conn.execute(
        "INSERT INTO undo_log(action, details, created_at) VALUES (?, ?, ?)",
        (action, json.dumps(items), time.time()),
    )


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


def run_grouping(input_folders, output_file, output_faces_dir, threshold, db_file):
    try:
        log = run_state.add_log
        conn, processed_files, groups = load_processed_state(db_file)
        out_root = Path(output_faces_dir).resolve()
        out_root.mkdir(parents=True, exist_ok=True)

        face_app = get_face_app()

        new_files = []
        total_files = 0
        for input_folder in input_folders:
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
            if run_state.cancel_requested:
                log("Cancelled by user. Saving progress so far...")
                break
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

    raw_folders = data.get("input_folders")
    if raw_folders is None:
        raw_folders = data.get("input_folder") or ""
    if isinstance(raw_folders, str):
        raw_folders = [p for line in raw_folders.splitlines() for p in line.split(",")]
    input_folders = [p.strip() for p in raw_folders if p and p.strip()]
    # de-duplicate while preserving order
    seen = set()
    input_folders = [p for p in input_folders if not (p in seen or seen.add(p))]

    output_file = (data.get("output_file") or "").strip() or "face_groups.txt"
    output_faces = (data.get("output_faces") or "").strip() or "output_faces"
    db_file = (data.get("db_file") or "").strip() or "processing_state.db"
    try:
        threshold = float(data.get("threshold", 0.6))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid threshold"}), 400

    if not input_folders:
        return jsonify({"error": "No input folders provided"}), 400
    missing = [p for p in input_folders if not Path(p).is_dir()]
    if missing:
        return jsonify({"error": f"Folders do not exist: {', '.join(missing)}"}), 400

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
        args=(input_folders, output_file, output_faces, threshold, db_file),
        daemon=True,
    )
    thread.start()
    return jsonify({"started": True})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    if run_state.request_cancel():
        return jsonify({"cancelling": True})
    return jsonify({"error": "No run in progress"}), 409


@app.route("/api/status")
def api_status():
    return jsonify(run_state.snapshot())


@app.route("/api/browse", methods=["POST"])
def api_browse():
    data = request.get_json(force=True)
    mode = data.get("mode")
    if mode not in ("folder", "file", "save"):
        return jsonify({"error": "mode must be 'folder', 'file' or 'save'"}), 400
    initialdir = (data.get("initialdir") or "").strip() or None
    try:
        path = pick_native_path(mode, initialdir)
    except Exception as exc:
        return jsonify({"error": f"Could not open dialog: {exc}"}), 500
    return jsonify({"path": path})


@app.route("/api/groups")
def api_groups():
    db_file = request.args.get("db_file", "processing_state.db")
    if not Path(db_file).exists():
        return jsonify({"groups": []})

    conn = open_db(db_file)
    result = []
    for row in conn.execute(
        "SELECT id, directory, name FROM groups ORDER BY id"
    ):
        directory = Path(row["directory"])
        faces = []
        if directory.is_dir():
            faces = sorted(
                p.name
                for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in FACE_EXTENSIONS
            )
        image_paths = sorted(
            r[0]
            for r in conn.execute(
                "SELECT image_path FROM group_image_paths WHERE group_id=?",
                (row["id"],),
            )
        )
        result.append(
            {
                "id": row["id"],
                "name": row["name"] or directory.name,
                "directory": str(directory),
                "faces": faces,
                "image_paths": image_paths,
                "mtime": directory.stat().st_mtime if directory.is_dir() else 0,
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

    conn = open_db(db_file)
    row = conn.execute(
        "SELECT id FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "Group not found"}), 404

    conn.execute("UPDATE groups SET name=? WHERE id=?", (new_name, group_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "name": new_name})


def trash_face(conn, group_id, filename):
    """Move a face crop to the trash folder. Returns an undo detail dict."""
    row = conn.execute(
        "SELECT directory FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    if row is None:
        raise LookupError("Group not found")

    src = Path(row["directory"]) / filename
    if not src.exists():
        raise FileNotFoundError("Face file not found")

    trash_dir = Path(row["directory"]).parent / ".trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    trash_path = trash_dir / f"{int(time.time() * 1000)}_{src.name}"
    shutil.move(str(src), str(trash_path))

    conn.execute(
        "DELETE FROM group_cropped_faces WHERE group_id=? AND face_id=?",
        (group_id, src.stem),
    )
    return {
        "face_path": str(src),
        "trash_path": str(trash_path),
        "group_id": group_id,
        "face_id": src.stem,
    }


@app.route("/api/groups/<int:group_id>/faces/<path:filename>", methods=["DELETE"])
def api_delete_face(group_id, filename):
    db_file = request.args.get("db_file", "processing_state.db")
    conn = open_db(db_file)
    try:
        item = trash_face(conn, group_id, filename)
        record_undo(conn, "delete", [item])
        conn.commit()
        return jsonify({"ok": True})
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except OSError as exc:
        conn.rollback()
        return jsonify({"error": f"Delete failed: {exc}"}), 500
    finally:
        conn.close()


@app.route("/api/faces/bulk-delete", methods=["POST"])
def api_bulk_delete():
    data = request.get_json(force=True)
    db_file = data.get("db_file", "processing_state.db")
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "No faces selected"}), 400

    conn = open_db(db_file)
    deleted, errors = [], []
    try:
        for item in items:
            try:
                group_id = int(item["group_id"])
                filename = Path(item["filename"]).name
                deleted.append(trash_face(conn, group_id, filename))
            except (LookupError, FileNotFoundError) as exc:
                errors.append(f"{item.get('filename')}: {exc}")
            except OSError as exc:
                errors.append(f"{item.get('filename')}: {exc}")

        if deleted:
            record_undo(conn, "delete", deleted)
            conn.commit()
        else:
            conn.rollback()
            return jsonify({"error": "; ".join(errors)}), 400
        return jsonify({"ok": True, "deleted": len(deleted), "errors": errors})
    finally:
        conn.close()


@app.route("/api/faces/bulk-move", methods=["POST"])
def api_bulk_move():
    data = request.get_json(force=True)
    db_file = data.get("db_file", "processing_state.db")
    items = data.get("items") or []
    target_id = data.get("target_group_id")
    new_group_name = (data.get("new_group_name") or "").strip()
    if not items:
        return jsonify({"error": "No faces selected"}), 400

    conn = open_db(db_file)
    moved, errors = [], []
    try:
        # Resolve/create the destination directory once
        dst_dir = None
        dst_group_id = None
        if target_id in (None, "", -1):
            first_src = conn.execute(
                "SELECT directory FROM groups WHERE id=?", (int(items[0]["group_id"]),)
            ).fetchone()
            if first_src is None:
                return jsonify({"error": "Source group not found"}), 404
            parent = Path(first_src["directory"]).parent
            name = new_group_name
            if not name:
                number = next_group_number(conn, parent)
                name = f"group_{number}"
                while (parent / name).exists():
                    number += 1
                    name = f"group_{number}"
            dst_dir = parent / name
            if dst_dir.exists():
                return jsonify({"error": "A folder with this name already exists"}), 400
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst_group_id = next_group_id(conn)
            conn.execute(
                "INSERT INTO groups(id, sum_embedding, count, directory) "
                "VALUES (?, '', 0, ?)",
                (dst_group_id, str(dst_dir)),
            )
        else:
            dst_row = conn.execute(
                "SELECT directory FROM groups WHERE id=?", (int(target_id),)
            ).fetchone()
            if dst_row is None:
                return jsonify({"error": "Target group not found"}), 404
            dst_dir = Path(dst_row["directory"])
            dst_group_id = int(target_id)

        for item in items:
            try:
                group_id = int(item["group_id"])
                filename = Path(item["filename"]).name
                if group_id == dst_group_id:
                    continue
                undo_item = move_single_face(
                    conn, group_id, filename, dst_dir, dst_group_id
                )
                if undo_item:
                    moved.append(undo_item)
            except (LookupError, FileNotFoundError) as exc:
                errors.append(f"{item.get('filename')}: {exc}")
            except OSError as exc:
                errors.append(f"{item.get('filename')}: {exc}")

        if not moved:
            conn.rollback()
            msg = "; ".join(errors) or "Nothing to move"
            return jsonify({"error": msg}), 400

        record_undo(conn, "move", moved)
        conn.commit()
        return jsonify(
            {
                "ok": True,
                "moved": len(moved),
                "target_group_id": dst_group_id,
                "errors": errors,
            }
        )
    finally:
        conn.close()


def move_single_face(conn, group_id, filename, dst_dir, dst_group_id):
    """Move one face crop into dst_dir; returns an undo detail dict."""
    src_row = conn.execute(
        "SELECT directory FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    if src_row is None:
        raise LookupError("Source group not found")

    src_path = Path(src_row["directory"]) / filename
    if not src_path.exists():
        raise FileNotFoundError("Face file not found")

    dst_path = dst_dir / src_path.name
    if dst_path.exists():
        raise FileExistsError("A file with this name already exists in target")

    shutil.move(str(src_path), str(dst_path))
    face_id = src_path.stem
    conn.execute(
        "DELETE FROM group_cropped_faces WHERE group_id=? AND face_id=?",
        (group_id, face_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO group_cropped_faces(group_id, face_id) VALUES (?, ?)",
        (dst_group_id, face_id),
    )
    return {
        "src_path": str(src_path),
        "dst_path": str(dst_path),
        "src_group_id": group_id,
        "dst_group_id": dst_group_id,
        "face_id": face_id,
    }


@app.route("/api/undo", methods=["POST"])
def api_undo():
    data = request.get_json(force=True)
    db_file = data.get("db_file", "processing_state.db")

    conn = open_db(db_file)
    try:
        entry = conn.execute(
            "SELECT id, action, details FROM undo_log "
            "WHERE undone=0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if entry is None:
            return jsonify({"error": "Nothing to undo"}), 404

        details = json.loads(entry["details"])
        descriptions = []
        try:
            for item in details:
                descriptions.append(undo_item(conn, item))
            conn.execute(
                "UPDATE undo_log SET undone=1 WHERE id=?", (entry["id"],)
            )
            conn.commit()
        except (OSError, sqlite3.Error) as exc:
            conn.rollback()
            return jsonify({"error": f"Undo failed: {exc}"}), 500

        label = entry["action"]
        return jsonify({"ok": True, "action": label, "details": descriptions})
    finally:
        conn.close()


def undo_item(conn, item):
    """Reverse a single delete/move item. Returns a short description."""
    if "trash_path" in item:
        # Undo of a delete: restore from trash and re-register the face
        trash_path = Path(item["trash_path"])
        face_path = Path(item["face_path"])
        if trash_path.exists():
            face_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(trash_path), str(face_path))
        conn.execute(
            "INSERT OR IGNORE INTO group_cropped_faces(group_id, face_id) "
            "VALUES (?, ?)",
            (item["group_id"], item["face_id"]),
        )
        return f"restored {face_path.name}"

    # Undo of a move: put the file back into the source group
    dst_path = Path(item["dst_path"])
    src_path = Path(item["src_path"])
    if dst_path.exists():
        shutil.move(str(dst_path), str(src_path))
    conn.execute(
        "DELETE FROM group_cropped_faces WHERE group_id=? AND face_id=?",
        (item["dst_group_id"], item["face_id"]),
    )
    conn.execute(
        "INSERT OR IGNORE INTO group_cropped_faces(group_id, face_id) VALUES (?, ?)",
        (item["src_group_id"], item["face_id"]),
    )
    return f"moved {src_path.name} back"


@app.route("/api/faces/move", methods=["POST"])
def api_move_face():
    data = request.get_json(force=True)
    db_file = data.get("db_file", "processing_state.db")
    group_id = int(data.get("group_id"))
    filename = Path(data.get("filename")).name
    target_id = data.get("target_group_id")
    new_group_name = (data.get("new_group_name") or "").strip()

    conn = open_db(db_file)
    try:
        src = conn.execute(
            "SELECT directory FROM groups WHERE id=?", (group_id,)
        ).fetchone()
        if src is None:
            return jsonify({"error": "Source group not found"}), 404

        if target_id in (None, "", -1):
            if any(ch in new_group_name for ch in '\\/:*?"<>|'):
                return jsonify({"error": "New group name has invalid characters"}), 400
            parent = Path(src["directory"]).parent
            if new_group_name:
                if (parent / new_group_name).exists():
                    return jsonify(
                        {"error": "A folder with this name already exists"}
                    ), 400
                name = new_group_name
            else:
                number = next_group_number(conn, parent)
                name = f"group_{number}"
                while (parent / name).exists():
                    number += 1
                    name = f"group_{number}"
            dst_dir = parent / name
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
                return jsonify({"error": "Target group not found"}), 404
            dst_dir = Path(dst_row["directory"])

        try:
            undo_item = move_single_face(conn, group_id, filename, dst_dir, target_id)
        except (LookupError, FileNotFoundError) as exc:
            conn.rollback()
            return jsonify({"error": str(exc)}), 404
        except FileExistsError as exc:
            conn.rollback()
            return jsonify({"error": str(exc)}), 400
        except OSError as exc:
            conn.rollback()
            return jsonify({"error": f"Move failed: {exc}"}), 500

        record_undo(conn, "move", [undo_item])
        conn.commit()
        return jsonify({"ok": True, "target_group_id": target_id})
    finally:
        conn.close()


@app.route("/api/stats")
def api_stats():
    db_file = request.args.get("db_file", "processing_state.db")
    if not Path(db_file).exists():
        return jsonify({"groups": 0, "faces": 0, "photos": 0, "largest": []})

    conn = open_db(db_file)
    total_groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    total_faces = conn.execute(
        "SELECT COALESCE(SUM(count), 0) FROM groups"
    ).fetchone()[0]
    photos = conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0]
    largest = [
        {"id": r[0], "name": r[1] or Path(r[2]).name, "count": r[3]}
        for r in conn.execute(
            """
            SELECT g.id, g.name, g.directory, COUNT(c.face_id) AS face_count
            FROM groups g
            LEFT JOIN group_cropped_faces c ON c.group_id = g.id
            GROUP BY g.id ORDER BY face_count DESC, g.id LIMIT 10
            """
        )
    ]
    conn.close()
    return jsonify(
        {
            "groups": total_groups,
            "faces": total_faces,
            "photos": photos,
            "largest": largest,
        }
    )


@app.route("/api/export")
def api_export():
    fmt = request.args.get("format", "csv")
    db_file = request.args.get("db_file", "processing_state.db")
    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404

    conn = open_db(db_file)
    rows = []
    for row in conn.execute(
        "SELECT id, directory, name FROM groups ORDER BY id"
    ):
        directory = Path(row["directory"])
        faces = []
        if directory.is_dir():
            faces = sorted(
                p.name
                for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in FACE_EXTENSIONS
            )
        sources = sorted(
            r[0]
            for r in conn.execute(
                "SELECT image_path FROM group_image_paths WHERE group_id=?",
                (row["id"],),
            )
        )
        rows.append(
            {
                "id": row["id"],
                "name": row["name"] or directory.name,
                "directory": str(directory),
                "face_count": len(faces),
                "faces": faces,
                "source_photos": sources,
            }
        )
    conn.close()

    if fmt == "json":
        return send_file(
            io.BytesIO(json.dumps(rows, indent=2).encode("utf-8")),
            mimetype="application/json",
            as_attachment=True,
            download_name="face_groups.json",
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["group_id", "name", "directory", "face_count", "faces", "source_photos"])
    for r in rows:
        writer.writerow(
            [r["id"], r["name"], r["directory"], r["face_count"],
             ";".join(r["faces"]), ";".join(r["source_photos"])]
        )
    return send_file(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name="face_groups.csv",
    )


@app.route("/api/source-image")
def api_source_image():
    """Serve an original photo, only if it belongs to the given group."""
    db_file = request.args.get("db_file", "processing_state.db")
    try:
        group_id = int(request.args.get("group_id", ""))
    except ValueError:
        return jsonify({"error": "Invalid group id"}), 400
    path = (request.args.get("path") or "").strip()

    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404

    conn = open_db(db_file)
    allowed = conn.execute(
        "SELECT 1 FROM group_image_paths WHERE group_id=? AND image_path=?",
        (group_id, path),
    ).fetchone()
    conn.close()

    if not allowed or not Path(path).is_file():
        return jsonify({"error": "Image not found"}), 404
    return send_file(path)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
