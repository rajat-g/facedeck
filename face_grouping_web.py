import csv
import io
import itertools
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import threading
import time
import traceback
from pathlib import Path

import cv2
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from pathvalidate import ValidationError, validate_filename

from face_grouping_v5 import (
    cosine_similarity,
    image_face_id,
    load_processed_state,
    open_db,
    read_image,
    record_photo_face,
    reset_photo_faces,
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
        self.groups_version = 0

    def reset(self):
        with self.lock:
            self.running = True
            self.processed = 0
            self.total = 0
            self.error = None
            self.log = []
            self.cancel_requested = False
            self.groups_version = 0

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
                "groups_version": self.groups_version,
            }


run_state = RunState()

# Monotonic suffix so two trash moves in the same millisecond can't collide.
_trash_seq = itertools.count()

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
    # Undo history is single-level (only the latest entry is ever read);
    # keep the table bounded instead of growing forever.
    conn.execute(
        "DELETE FROM undo_log WHERE id NOT IN "
        "(SELECT id FROM undo_log ORDER BY id DESC LIMIT 50)"
    )


def _require_idle():
    """409 response while a grouping run owns the database, else None.

    The runner checkpoints its in-memory state over the tables, which would
    silently discard concurrent curation. Renames and reads stay allowed
    (checkpoints preserve names; nothing else they touch is rewritten).
    """
    with run_state.lock:
        running = run_state.running
    if running:
        return jsonify(
            {"error": "A grouping run is in progress — retry when it finishes"}
        ), 409
    return None


def _group_name_error(name):
    """Error string for an invalid new person/group name, else None.

    Filename rules (per-platform invalid chars, reserved names, length and
    whitespace limits) come from the well-tested `pathvalidate` library;
    only the dot-name special cases need a hand-rolled guard.
    """
    if not name or not name.strip():
        return "Group name cannot be empty"
    if name.strip() in (".", ".."):
        return "Group name is reserved"
    try:
        validate_filename(name, platform="universal")
    except ValidationError as exc:
        return f"Invalid group name: {exc}"
    return None


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


def describe_run_target(db_file, input_folders):
    """Facts about a planned run, used for fresh-DB / folder-mismatch warnings.

    Pure reads only — safe to call from tests and the API without starting
    any processing.
    """
    resolved_db = str(Path(db_file).resolve())
    resolved_folders = sorted(str(Path(p).resolve()) for p in input_folders)
    info = {
        "resolved_db": resolved_db,
        "resolved_folders": resolved_folders,
        "fresh_db": True,
        "existing_people": 0,
        "folder_mismatch": False,
        "last_input_folders": [],
    }
    if not Path(db_file).exists():
        return info
    try:
        mconn = open_db(db_file)
        try:
            info["fresh_db"] = False
            info["existing_people"] = mconn.execute(
                "SELECT COUNT(*) FROM groups"
            ).fetchone()[0]
            row = mconn.execute(
                "SELECT value FROM run_meta WHERE key='last_input_folders'"
            ).fetchone()
            if row and row[0]:
                info["last_input_folders"] = json.loads(row[0])
                info["folder_mismatch"] = (
                    sorted(info["last_input_folders"]) != resolved_folders
                )
        finally:
            mconn.close()
    except Exception:
        pass
    return info


def run_grouping(input_folders, output_faces_dir, threshold, db_file):
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
        log(f"Database: {Path(db_file).resolve()} — "
            f"resuming with {len(groups)} existing people.")
        try:
            prev_row = conn.execute(
                "SELECT value FROM run_meta WHERE key='last_input_folders'"
            ).fetchone()
            if prev_row and prev_row[0]:
                prev_folders = sorted(json.loads(prev_row[0]))
                cur_folders = sorted(
                    str(Path(p).resolve()) for p in input_folders
                )
                if prev_folders and prev_folders != cur_folders:
                    log("WARNING: input folders differ from the last run on "
                        "this database. Groups from other folders stay, but "
                        "only the folders above will be scanned.")
        except Exception as exc:
            log(f"Could not check last input folders: {exc}")

        matchable = [
            g for g in groups if g["sum_embedding"] is not None and g["count"] > 0
        ]
        id_counter = max(
            (g["id"] for g in groups if isinstance(g.get("id"), int)), default=0
        )

        # Streaming checkpoints: persist every 100 images or 20s so the UI
        # can show people as they are found instead of only at the end.
        last_checkpoint = time.time()
        since_checkpoint = 0

        for img_path in new_files:
            if run_state.cancel_requested:
                log("Cancelled by user. Saving progress so far...")
                break
            try:
                img = read_image(str(img_path))
                if img is None:
                    log(f"Could not read image: {img_path}")
                    continue

                reset_photo_faces(conn, str(img_path))
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

                    face_id = image_face_id(img_path, face_idx)
                    output_path = None

                    if max_sim >= threshold and best_group is not None:
                        if face_id in best_group["cropped_faces"]:
                            record_photo_face(
                                conn, str(img_path), face_idx, face_id,
                                face.bbox, img.shape, best_group["id"],
                            )
                            conn.execute(
                                "INSERT OR IGNORE INTO group_image_paths(group_id, image_path) "
                                "VALUES (?, ?)",
                                (best_group["id"], str(img_path)),
                            )
                            continue
                        best_group["sum_embedding"] += embedding
                        best_group["count"] += 1
                        best_group["image_paths"].add(str(img_path))
                        best_group["cropped_faces"].add(face_id)
                        output_path = best_group["directory"] / f"{face_id}.jpg"
                        record_photo_face(
                            conn, str(img_path), face_idx, face_id,
                            face.bbox, img.shape, best_group["id"],
                        )
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
                        record_photo_face(
                            conn, str(img_path), face_idx, face_id,
                            face.bbox, img.shape, id_counter,
                        )

                    if output_path and not output_path.exists():
                        output_path.parent.mkdir(parents=True, exist_ok=True)
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
                since_checkpoint += 1
                now = time.time()
                if since_checkpoint >= 100 or now - last_checkpoint >= 20.0:
                    try:
                        save_processed_state(conn, processed_files, groups)
                        last_checkpoint = now
                        since_checkpoint = 0
                        with run_state.lock:
                            run_state.groups_version += 1
                        log(f"Checkpoint saved — {len(groups)} people so far.")
                    except Exception as exc:
                        log(f"Checkpoint save failed: {exc}")

        save_processed_state(conn, processed_files, groups)
        conn.execute(
            "INSERT OR REPLACE INTO run_meta(key, value) VALUES ('last_input_folders', ?)",
            (json.dumps(sorted(str(Path(p).resolve()) for p in input_folders)),),
        )
        conn.commit()
        conn.close()

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

    parent = Path(db_file).parent
    if str(parent):
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return jsonify({"error": f"Cannot create folder for {db_file}: {exc}"}), 400

    snap = run_state.snapshot()
    if snap["running"]:
        return jsonify({"error": "Processing is already running"}), 409

    target = describe_run_target(db_file, input_folders)

    run_state.reset()
    thread = threading.Thread(
        target=run_grouping,
        args=(input_folders, output_faces, threshold, db_file),
        daemon=True,
    )
    thread.start()
    return jsonify(
        {
            "started": True,
            "fresh_db": target["fresh_db"],
            "existing_people": target["existing_people"],
            "folder_mismatch": target["folder_mismatch"],
            "resolved_db": target["resolved_db"],
            "resolved_folders": target["resolved_folders"],
        }
    )


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

    # summary=1 returns counts only (fast, no file lists) for large libraries.
    summary = request.args.get("summary", "") in ("1", "true", "yes")
    if request.args.get("light", "") in ("1", "true", "yes"):
        summary = True

    conn = open_db(db_file)
    result = []
    for row in conn.execute(
        "SELECT id, directory, name FROM groups ORDER BY id"
    ):
        directory = Path(row["directory"])
        mtime = directory.stat().st_mtime if directory.is_dir() else 0
        name = row["name"] or directory.name
        if summary:
            photo_count = conn.execute(
                "SELECT COUNT(*) FROM group_image_paths WHERE group_id=?",
                (row["id"],),
            ).fetchone()[0]
            face_count = 0
            if directory.is_dir():
                try:
                    face_count = sum(
                        1
                        for p in directory.iterdir()
                        if p.is_file() and p.suffix.lower() in FACE_EXTENSIONS
                    )
                except OSError:
                    face_count = 0
            result.append(
                {
                    "id": row["id"],
                    "name": name,
                    "directory": str(directory),
                    "face_count": face_count,
                    "photo_count": photo_count,
                    "mtime": mtime,
                }
            )
            continue
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
                "name": name,
                "directory": str(directory),
                "faces": faces,
                "image_paths": image_paths,
                # counts included so new UI can use either shape
                "face_count": len(faces),
                "photo_count": len(image_paths),
                "mtime": mtime,
            }
        )
    conn.close()
    return jsonify({"groups": result})


def _list_face_names(directory: Path):
    """Sorted face-crop filenames in a group directory."""
    if not directory.is_dir():
        return []
    try:
        return sorted(
            p.name
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in FACE_EXTENSIONS
        )
    except OSError:
        return []


@app.route("/api/groups/<int:group_id>/faces")
def api_group_faces(group_id):
    """Paginated face-crop filenames. ?page=1&per_page=48"""
    db_file = request.args.get("db_file", "processing_state.db")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", 48))
    except (TypeError, ValueError):
        per_page = 48
    per_page = max(1, min(per_page, 200))

    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404
    conn = open_db(db_file)
    row = conn.execute(
        "SELECT directory FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "Group not found"}), 404
    names = _list_face_names(Path(row["directory"]))
    total = len(names)
    start = (page - 1) * per_page
    return jsonify(
        {
            "faces": names[start:start + per_page],
            "total": total,
            "page": page,
            "per_page": per_page,
        }
    )


@app.route("/api/groups/<int:group_id>/photos")
def api_group_photos(group_id):
    """Paginated source-photo paths. ?page=1&per_page=50&q=filter"""
    db_file = request.args.get("db_file", "processing_state.db")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", 50))
    except (TypeError, ValueError):
        per_page = 50
    per_page = max(1, min(per_page, 200))
    q = (request.args.get("q") or "").strip()

    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404
    conn = open_db(db_file)
    exists = conn.execute(
        "SELECT 1 FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    if exists is None:
        conn.close()
        return jsonify({"error": "Group not found"}), 404
    if q:
        like = f"%{_like_escape(q)}%"
        total = conn.execute(
            "SELECT COUNT(*) FROM group_image_paths WHERE group_id=? AND image_path LIKE ? ESCAPE '\\'",
            (group_id, like),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT image_path FROM group_image_paths WHERE group_id=? AND image_path LIKE ? ESCAPE '\\' "
            "ORDER BY image_path LIMIT ? OFFSET ?",
            (group_id, like, per_page, (page - 1) * per_page),
        ).fetchall()
    else:
        total = conn.execute(
            "SELECT COUNT(*) FROM group_image_paths WHERE group_id=?",
            (group_id,),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT image_path FROM group_image_paths WHERE group_id=? "
            "ORDER BY image_path LIMIT ? OFFSET ?",
            (group_id, per_page, (page - 1) * per_page),
        ).fetchall()
    conn.close()
    return jsonify(
        {
            "photos": [r[0] for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
        }
    )


def _like_escape(text):
    """Escape LIKE wildcards so filename filters match literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _reveal_path_on_server(target: Path):
    """Open file manager on the server for a file or folder."""
    system = platform.system()
    if system == "Windows":
        if target.is_file():
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            subprocess.Popen(["explorer", str(target)])
    elif system == "Darwin":
        if target.is_file():
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["open", str(target)])
    else:
        folder = str(target if target.is_dir() else target.parent)
        for cmd in (["xdg-open", folder], ["gio", "open", folder]):
            try:
                subprocess.Popen(cmd)
                break
            except FileNotFoundError:
                continue
        else:
            raise OSError("No file manager found to reveal folder")


@app.route("/api/groups/<int:group_id>/reveal-folder", methods=["POST"])
def api_reveal_group_folder(group_id):
    data = {}
    try:
        data = request.get_json(force=True) if request.data else {}
    except Exception:
        data = {}
    db_file = (data.get("db_file") or request.args.get("db_file") or "processing_state.db").strip() or "processing_state.db"
    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404
    conn = open_db(db_file)
    row = conn.execute(
        "SELECT directory FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "Group not found"}), 404
    directory = Path(row["directory"])
    if not directory.is_dir():
        return jsonify({"error": "Group folder does not exist on server"}), 404
    try:
        _reveal_path_on_server(directory)
        return jsonify({"ok": True, "path": str(directory)})
    except Exception as exc:
        return jsonify({"error": f"Reveal failed: {exc}"}), 500


@app.route("/api/groups/<int:group_id>/faces/<path:filename>")
def api_face_image(group_id, filename):
    db_file = request.args.get("db_file", "processing_state.db")
    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404
    conn = open_db(db_file)
    try:
        row = conn.execute(
            "SELECT directory FROM groups WHERE id=?", (group_id,)
        ).fetchone()
        if row is None:
            return jsonify({"error": "Group not found"}), 404
        directory = Path(row["directory"])
        if not directory.is_dir():
            return jsonify({"error": "Group directory missing"}), 404
        return send_from_directory(directory, filename)
    finally:
        conn.close()


@app.route("/api/groups/<int:group_id>/rename", methods=["POST"])
def api_rename_group(group_id):
    data = {}
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}
    # Support db_file from JSON body or query string for robustness
    db_file = (data.get("db_file") or request.args.get("db_file") or "processing_state.db").strip() or "processing_state.db"
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "Name cannot be empty"}), 400

    if not Path(db_file).exists():
        # Try to give helpful error if DB not found; fallback may be relative vs absolute mismatch
        alt = Path(db_file).resolve()
        if alt.exists():
            db_file = str(alt)
        else:
            return jsonify({"error": f"Database file does not exist: {db_file}"}), 404

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
    trash_path = trash_dir / f"{int(time.time() * 1000)}_{next(_trash_seq)}_{src.name}"
    shutil.move(str(src), str(trash_path))

    conn.execute(
        "DELETE FROM group_cropped_faces WHERE group_id=? AND face_id=?",
        (group_id, src.stem),
    )
    # Detach any face tags so the removed face stops being labelled.
    conn.execute(
        "UPDATE photo_faces SET detached=1 WHERE face_id=?",
        (src.stem,),
    )
    # Recompute the source photo's links: it drops out of this group when
    # none of its faces remain here.
    trashed_photos = []
    for photo in photo_paths_for_face(conn, src.stem):
        if relink_photo(conn, photo):
            still = conn.execute(
                "SELECT 1 FROM group_image_paths WHERE group_id=? AND image_path=?",
                (group_id, photo),
            ).fetchone()
            trashed_photos.append({"filename": filename, "photo": photo,
                                   "unlinked_source": not still})
    return {
        "face_path": str(src),
        "trash_path": str(trash_path),
        "group_id": group_id,
        "face_id": src.stem,
        "photos": trashed_photos,
    }


@app.route("/api/groups/<int:group_id>/faces/<path:filename>", methods=["DELETE"])
def api_delete_face(group_id, filename):
    busy = _require_idle()
    if busy:
        return busy
    db_file = request.args.get("db_file", "processing_state.db")
    conn = open_db(db_file)
    try:
        item = trash_face(conn, group_id, filename)
        photos = item.pop("photos", [])
        record_undo(conn, "delete", [item])
        conn.commit()
        return jsonify({"ok": True, "photos": photos})
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except OSError as exc:
        conn.rollback()
        return jsonify({"error": f"Delete failed: {exc}"}), 500
    finally:
        conn.close()


@app.route("/api/groups/<int:group_id>", methods=["DELETE"])
def api_delete_group(group_id):
    """Delete an EMPTY group (no faces, no photos). Refuses otherwise."""
    busy = _require_idle()
    if busy:
        return busy
    db_file = request.args.get("db_file", "processing_state.db")
    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404
    conn = open_db(db_file)
    try:
        row = conn.execute(
            "SELECT directory, name FROM groups WHERE id=?", (group_id,)
        ).fetchone()
        if row is None:
            return jsonify({"error": "Group not found"}), 404
        faces = conn.execute(
            "SELECT COUNT(*) FROM group_cropped_faces WHERE group_id=?",
            (group_id,),
        ).fetchone()[0]
        photos = conn.execute(
            "SELECT COUNT(*) FROM group_image_paths WHERE group_id=?",
            (group_id,),
        ).fetchone()[0]
        directory = Path(row["directory"])
        stray = []
        if directory.is_dir():
            stray = [
                p.name for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in FACE_EXTENSIONS
            ]
        if faces or photos or stray:
            return jsonify(
                {"error": "Group is not empty — move, delete or approve its contents first"}
            ), 400
        grow = conn.execute(
            "SELECT sum_embedding, count, directory, name FROM groups WHERE id=?",
            (group_id,),
        ).fetchone()
        stale_tags = [
            dict(r)
            for r in conn.execute(
                "SELECT image_path, face_idx, face_id, x1, y1, x2, y2, group_id, detached "
                "FROM photo_faces WHERE group_id=? AND face_id NOT IN "
                "(SELECT face_id FROM group_cropped_faces)",
                (group_id,),
            )
        ]
        conn.execute("DELETE FROM group_image_paths WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM group_cropped_faces WHERE group_id=?", (group_id,))
        if stale_tags:
            conn.execute(
                "DELETE FROM photo_faces WHERE group_id=? AND face_id NOT IN "
                "(SELECT face_id FROM group_cropped_faces)",
                (group_id,),
            )
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
        dir_removed = False
        if directory.is_dir():
            try:
                if not any(directory.iterdir()):
                    directory.rmdir()
                    dir_removed = True
            except OSError:
                pass
        record_undo(conn, "delete-group", [{
            "group": {
                "id": group_id,
                "sum_embedding": grow["sum_embedding"],
                "count": grow["count"],
                "directory": grow["directory"],
                "name": grow["name"],
            },
            "tags": stale_tags,
        }])
        conn.commit()
        return jsonify({"ok": True, "dir_removed": dir_removed})
    finally:
        conn.close()


@app.route("/api/faces/bulk-delete", methods=["POST"])
def api_bulk_delete():
    busy = _require_idle()
    if busy:
        return busy
    data = request.get_json(force=True)
    db_file = data.get("db_file", "processing_state.db")
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "No faces selected"}), 400

    conn = open_db(db_file)
    deleted, errors, photos = [], [], []
    try:
        for item in items:
            try:
                group_id = int(item["group_id"])
                filename = Path(item["filename"]).name
                trashed = trash_face(conn, group_id, filename)
                deleted.append(trashed)
                photos.extend(trashed.pop("photos", []))
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
        return jsonify({"ok": True, "deleted": len(deleted), "errors": errors,
                        "photos": photos})
    finally:
        conn.close()


@app.route("/api/faces/bulk-move", methods=["POST"])
def api_bulk_move():
    busy = _require_idle()
    if busy:
        return busy
    data = request.get_json(force=True)
    db_file = data.get("db_file", "processing_state.db")
    items = data.get("items") or []
    target_id = data.get("target_group_id")
    new_group_name = (data.get("new_group_name") or "").strip()
    if not items:
        return jsonify({"error": "No faces selected"}), 400

    conn = open_db(db_file)
    moved, errors, photos = [], [], []
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
            if name:
                name_error = _group_name_error(name)
                if name_error:
                    return jsonify({"error": name_error}), 400
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
                    photos.extend(undo_item.pop("photos", []))
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
                "photos": photos,
                "photos": photos,
            }
        )
    finally:
        conn.close()


def photo_paths_for_face(conn, face_id):
    """Source photo(s) recorded for a face crop. Empty when unknown
    (e.g. data processed before face tags existed)."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT image_path FROM photo_faces WHERE face_id=?",
            (face_id,),
        )
    ]


def relink_photo(conn, image_path):
    """Link a photo to exactly the groups owning its live faces.

    A photo stays linked to a group while at least one non-detached face
    belongs to it (approved groups resolve through their stored group, so
    approving never unlinks photos). Returns True when links were
    recomputed, False when the photo has no tag rows and was left alone.
    """
    if not conn.execute(
        "SELECT 1 FROM photo_faces WHERE image_path=?", (image_path,)
    ).fetchone():
        return False
    rows = conn.execute(
        "SELECT DISTINCT COALESCE(cf.group_id, pf.group_id) AS gid "
        "FROM photo_faces pf "
        "LEFT JOIN group_cropped_faces cf ON cf.face_id = pf.face_id "
        "WHERE pf.image_path=? AND pf.detached=0",
        (image_path,),
    ).fetchall()
    gids = set()
    for (gid,) in rows:
        if gid is not None and conn.execute(
            "SELECT 1 FROM groups WHERE id=?", (gid,)
        ).fetchone():
            gids.add(gid)
    conn.execute("DELETE FROM group_image_paths WHERE image_path=?", (image_path,))
    for gid in gids:
        conn.execute(
            "INSERT OR IGNORE INTO group_image_paths(group_id, image_path) VALUES (?, ?)",
            (gid, image_path),
        )
    return True


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
    # Follow the move so tags label the face with its new person.
    conn.execute(
        "UPDATE photo_faces SET group_id=?, detached=0 WHERE face_id=?",
        (dst_group_id, face_id),
    )
    # The source photo follows its face: link it to the new group, and drop
    # it from the old one only when none of its faces remain there.
    moved_photos = []
    for photo in photo_paths_for_face(conn, face_id):
        if relink_photo(conn, photo):
            still = conn.execute(
                "SELECT 1 FROM group_image_paths WHERE group_id=? AND image_path=?",
                (group_id, photo),
            ).fetchone()
            moved_photos.append({"filename": filename, "photo": photo,
                                 "unlinked_source": not still})
    return {
        "src_path": str(src_path),
        "dst_path": str(dst_path),
        "src_group_id": group_id,
        "dst_group_id": dst_group_id,
        "face_id": face_id,
        "photos": moved_photos,
    }


@app.route("/api/undo", methods=["POST"])
def api_undo():
    busy = _require_idle()
    if busy:
        return busy
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
    """Reverse a single delete/move/group-delete item. Returns a short description."""
    if "group" in item:
        # Undo of an empty-group delete: re-create the row and its tags.
        g = item["group"]
        Path(g["directory"]).mkdir(parents=True, exist_ok=True)
        try:
            conn.execute(
                "INSERT INTO groups(id, sum_embedding, count, directory, name) "
                "VALUES (?, ?, ?, ?, ?)",
                (g["id"], g["sum_embedding"], g["count"], g["directory"], g["name"]),
            )
            group_id = g["id"]
        except sqlite3.IntegrityError:
            group_id = next_group_id(conn)
            conn.execute(
                "INSERT INTO groups(id, sum_embedding, count, directory, name) "
                "VALUES (?, ?, ?, ?, ?)",
                (group_id, g["sum_embedding"], g["count"], g["directory"], g["name"]),
            )
        for t in item.get("tags", []):
            conn.execute(
                "INSERT OR IGNORE INTO photo_faces(image_path, face_idx, face_id, "
                "x1, y1, x2, y2, group_id, detached) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t["image_path"], t["face_idx"], t["face_id"], t["x1"], t["y1"],
                 t["x2"], t["y2"], group_id, t.get("detached", 0)),
            )
        return f"restored group {g['name']}"
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
        conn.execute(
            "UPDATE photo_faces SET detached=0 WHERE face_id=?",
            (item["face_id"],),
        )
        for photo in photo_paths_for_face(conn, item["face_id"]):
            relink_photo(conn, photo)
        return f"restored {face_path.name}"

    # Undo of a move: put the file back into the source group
    dst_path = Path(item["dst_path"])
    src_path = Path(item["src_path"])
    if dst_path.exists():
        src_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst_path), str(src_path))
    conn.execute(
        "DELETE FROM group_cropped_faces WHERE group_id=? AND face_id=?",
        (item["dst_group_id"], item["face_id"]),
    )
    conn.execute(
        "INSERT OR IGNORE INTO group_cropped_faces(group_id, face_id) VALUES (?, ?)",
        (item["src_group_id"], item["face_id"]),
    )
    conn.execute(
        "UPDATE photo_faces SET group_id=? WHERE face_id=?",
        (item["src_group_id"], item["face_id"]),
    )
    for photo in photo_paths_for_face(conn, item["face_id"]):
        relink_photo(conn, photo)
    return f"moved {src_path.name} back"


@app.route("/api/stats")
def api_stats():
    db_file = request.args.get("db_file", "processing_state.db")
    if not Path(db_file).exists():
        return jsonify({"groups": 0, "faces": 0, "photos": 0, "largest": []})

    conn = open_db(db_file)
    total_groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    # Count crops actually on record (embedding counts survive Approve,
    # which deletes the crops — summing them would inflate the chip).
    total_faces = conn.execute(
        "SELECT COUNT(*) FROM group_cropped_faces"
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
    """Serve an original photo, only if it belongs to the given group.
    HEIC/HEIF are converted to JPEG on-the-fly so browsers can preview them.
    """
    db_file = request.args.get("db_file", "processing_state.db")
    try:
        group_id = int(request.args.get("group_id", ""))
    except ValueError:
        return jsonify({"error": "Invalid group id"}), 400
    path = (request.args.get("path") or "").strip()

    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404

    conn = open_db(db_file)
    try:
        allowed = conn.execute(
            "SELECT 1 FROM group_image_paths WHERE group_id=? AND image_path=?",
            (group_id, path),
        ).fetchone()

        if not allowed or not Path(path).is_file():
            return jsonify({"error": "Image not found"}), 404

        # HEIC/HEIF need conversion for browser preview (Chrome/Firefox don't render HEIC)
        if path.lower().endswith((".heic", ".heif")):
            try:
                img = read_image(path)
                if img is None:
                    return jsonify({"error": "Could not decode HEIC image"}), 500
                ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                if not ok:
                    return jsonify({"error": "HEIC conversion failed"}), 500
                return send_file(
                    io.BytesIO(buf.tobytes()),
                    mimetype="image/jpeg",
                    as_attachment=False,
                    download_name=Path(path).stem + ".jpg",
                    max_age=3600,
                )
            except Exception as exc:
                traceback.print_exc()
                return jsonify({"error": f"HEIC preview failed: {exc}"}), 500

        # Photos with an EXIF orientation flag are served transposed so the
        # displayed pixels match detection space (face-tag boxes stay correct).
        # Anything else takes the fast path: original bytes, zero overhead.
        try:
            converted = _oriented_jpeg_bytes(path)
        except Exception:
            traceback.print_exc()
            converted = None
        if converted is not None:
            return send_file(
                io.BytesIO(converted),
                mimetype="image/jpeg",
                as_attachment=False,
                download_name=Path(path).stem + ".jpg",
                max_age=3600,
            )

        return send_file(path, max_age=3600)
    finally:
        conn.close()


def _oriented_jpeg_bytes(path):
    """JPEG bytes in display orientation, or None when no rotation is needed.

    Browsers auto-rotate by EXIF but detection runs on the transposed pixels
    (see read_image), so rotated photos must be served transposed for face-tag
    boxes to land correctly. Photos without an orientation flag are served
    untouched via the fast path below.
    """
    from PIL import Image, ImageOps

    with Image.open(path) as img:
        try:
            orientation = (img.getexif().get(0x0112) or 1)
        except Exception:
            return None
        if orientation == 1:
            return None
        img = ImageOps.exif_transpose(img).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        return buf.getvalue()


@app.route("/api/photo-tags")
def api_photo_tags():
    """Facebook-style face tags for one source photo.

    Returns normalised 0-1 boxes with the CURRENT owning group: moves are
    followed through face_id, renames resolve live, approved groups fall back
    to the stored group, and trashed faces are hidden.
    """
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
    if not allowed:
        conn.close()
        return jsonify({"error": "Image not associated with this group"}), 404

    rows = conn.execute(
        "SELECT pf.face_idx, pf.face_id, pf.x1, pf.y1, pf.x2, pf.y2, "
        "COALESCE(cf.group_id, pf.group_id) AS group_id, g.name, g.directory "
        "FROM photo_faces pf "
        "LEFT JOIN group_cropped_faces cf ON cf.face_id = pf.face_id "
        "LEFT JOIN groups g ON g.id = COALESCE(cf.group_id, pf.group_id) "
        "WHERE pf.image_path=? AND pf.detached=0 "
        "ORDER BY pf.face_idx",
        (path,),
    ).fetchall()
    conn.close()

    tags = []
    for r in rows:
        if r["directory"]:
            name = r["name"] or Path(r["directory"]).name
        else:
            name = r["name"] or "Unknown"
        tags.append(
            {
                "face_idx": r["face_idx"],
                "face_id": r["face_id"],
                "bbox": [r["x1"], r["y1"], r["x2"], r["y2"]],
                "group_id": r["group_id"],
                "name": name,
            }
        )
    return jsonify({"tags": tags})


# ---------- Approve (permanent crop deletion) ----------

def _approve_group_crops(conn, group_id):
    """Permanently delete cropped face files for one group. Returns (deleted_count, dir_removed)."""
    row = conn.execute("SELECT directory FROM groups WHERE id=?", (group_id,)).fetchone()
    if row is None:
        raise LookupError("Group not found")
    directory = Path(row["directory"])
    deleted = 0
    dir_removed = False
    did_exist = directory.is_dir()

    if did_exist:
        for p in list(directory.iterdir()):
            if p.is_file() and p.suffix.lower() in FACE_EXTENSIONS:
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    pass
    # count DB entries that may remain even if file missing
    cur = conn.execute("SELECT COUNT(*) FROM group_cropped_faces WHERE group_id=?", (group_id,)).fetchone()
    db_count = cur[0] if cur else 0
    if deleted == 0 and db_count > 0:
        deleted = db_count

    conn.execute("DELETE FROM group_cropped_faces WHERE group_id=?", (group_id,))

    # delete empty dir permanently only if it existed before and is now empty
    if did_exist and directory.is_dir():
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
                dir_removed = True
        except OSError:
            pass

    return deleted, dir_removed


@app.route("/api/groups/<int:group_id>/approve", methods=["POST"])
def api_approve_group(group_id):
    busy = _require_idle()
    if busy:
        return busy
    data = request.get_json(force=True) if request.data else {}
    db_file = (data.get("db_file") or request.args.get("db_file") or "processing_state.db").strip() or "processing_state.db"
    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404
    conn = open_db(db_file)
    try:
        deleted, dir_removed = _approve_group_crops(conn, group_id)
        conn.commit()
        return jsonify({"ok": True, "deleted": deleted, "dir_removed": dir_removed})
    except LookupError as exc:
        conn.rollback()
        return jsonify({"error": str(exc)}), 404
    except OSError as exc:
        conn.rollback()
        return jsonify({"error": f"Approve failed: {exc}"}), 500
    finally:
        conn.close()


@app.route("/api/groups/approve-all", methods=["POST"])
def api_approve_all():
    busy = _require_idle()
    if busy:
        return busy
    data = request.get_json(force=True) if request.data else {}
    db_file = (data.get("db_file") or request.args.get("db_file") or "processing_state.db").strip() or "processing_state.db"
    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404
    conn = open_db(db_file)
    try:
        total_deleted = 0
        dirs_removed = 0
        groups_processed = 0
        for (gid,) in list(conn.execute("SELECT id FROM groups")):
            deleted, dir_removed = _approve_group_crops(conn, gid)
            total_deleted += deleted
            if dir_removed:
                dirs_removed += 1
            groups_processed += 1
        conn.commit()
        return jsonify({
            "ok": True,
            "groups": groups_processed,
            "deleted": total_deleted,
            "dirs_removed": dirs_removed,
        })
    except OSError as exc:
        conn.rollback()
        return jsonify({"error": f"Approve all failed: {exc}"}), 500
    finally:
        conn.close()


@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    data = request.get_json(force=True)
    db_file = (data.get("db_file") or "processing_state.db").strip() or "processing_state.db"
    try:
        group_id = int(data.get("group_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid group id"}), 400
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "Missing path"}), 400
    if not Path(db_file).exists():
        return jsonify({"error": "Database file does not exist"}), 404
    conn = open_db(db_file)
    allowed = conn.execute(
        "SELECT 1 FROM group_image_paths WHERE group_id=? AND image_path=?",
        (group_id, path),
    ).fetchone()
    conn.close()
    if not allowed:
        return jsonify({"error": "Image not associated with this group"}), 404
    file_path = Path(path)
    if not file_path.exists():
        return jsonify({"error": "File does not exist on server"}), 404
    try:
        system = platform.system()
        if system == "Windows":
            # Use explorer /select to highlight file
            subprocess.Popen(["explorer", "/select,", str(file_path)])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", str(file_path)])
        else:
            # Linux: try to open parent folder; no universal select
            folder = str(file_path.parent)
            for cmd in (["xdg-open", folder], ["gio", "open", folder]):
                try:
                    subprocess.Popen(cmd)
                    break
                except FileNotFoundError:
                    continue
            else:
                return jsonify({"error": "No file manager found to reveal folder"}), 500
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": f"Reveal failed: {exc}"}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
