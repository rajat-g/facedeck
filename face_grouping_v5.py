import argparse
import hashlib
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pillow_heif
from PIL import Image, ImageOps
from insightface.app import FaceAnalysis
from tqdm import tqdm


def read_image(file_path):
    """Decode an image to BGR numpy array, honouring EXIF orientation.

    Normalising orientation here (instead of letting each viewer rotate)
    keeps detection coordinates, saved crops and served previews in the
    same pixel space, which face-tag overlays rely on.
    """
    if str(file_path).lower().endswith(('.heic', '.heif')):
        heif_file = pillow_heif.open_heif(file_path)
        image = np.array(heif_file)
        return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    try:
        with Image.open(file_path) as pil_img:
            pil_img = ImageOps.exif_transpose(pil_img).convert("RGB")
            return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    except Exception:
        return cv2.imread(str(file_path))


def record_photo_face(conn, image_path, face_idx, face_id, bbox, img_shape, group_id):
    """Upsert one Facebook-style face tag (normalised 0-1 bbox + owning group)."""
    h, w = img_shape[:2]
    if h <= 0 or w <= 0:
        return
    x1, y1, x2, y2 = (float(v) for v in bbox)
    x1 = min(max(x1, 0.0), float(w))
    y1 = min(max(y1, 0.0), float(h))
    x2 = min(max(x2, 0.0), float(w))
    y2 = min(max(y2, 0.0), float(h))
    if x2 <= x1 or y2 <= y1:
        return
    conn.execute(
        "INSERT OR REPLACE INTO photo_faces"
        "(image_path, face_idx, face_id, x1, y1, x2, y2, group_id, detached) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            str(image_path),
            int(face_idx),
            str(face_id),
            x1 / w,
            y1 / h,
            x2 / w,
            y2 / h,
            int(group_id) if group_id is not None else None,
        ),
    )


def reset_photo_faces(conn, image_path):
    """Drop stale tags for an image about to be (re)processed."""
    conn.execute("DELETE FROM photo_faces WHERE image_path=?", (str(image_path),))


def image_face_id(img_path, face_idx):
    """Unique face id: photo stem + hash of its folder + face index.

    The folder hash disambiguates same-named photos in different folders
    (e.g. two `DSC_001.jpg`), which previously shared one identity and
    silently unlinked the second photo. Deterministic per file, so
    incremental runs keep recognising their own crops.
    """
    parent = str(Path(img_path).resolve().parent)
    digest = hashlib.sha1(parent.encode("utf-8")).hexdigest()[:8]
    return f"{Path(img_path).stem}_{digest}_{face_idx}"


def sha256_of(file_path, chunk_size=1024 * 1024):
    """Hex SHA-256 of file bytes, or None when unreadable."""
    digest = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def dhash_of(file_path):
    """64-bit perceptual hash (hex) via ImageHash, or None when undecodable."""
    try:
        import imagehash

        if str(file_path).lower().endswith((".heic", ".heif")):
            arr = read_image(str(file_path))
            if arr is None:
                return None
            img = Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
            return str(imagehash.dhash(img))
        with Image.open(file_path) as img:
            return str(imagehash.dhash(ImageOps.exif_transpose(img)))
    except Exception:
        return None


def resolve_canonical(conn, path):
    """Follow duplicate aliases to the root canonical path (no chains)."""
    seen = set()
    cur = str(path)
    while True:
        if cur in seen:
            return cur
        seen.add(cur)
        row = conn.execute(
            "SELECT canonical_path FROM duplicates WHERE dup_path=?", (cur,)
        ).fetchone()
        if row is None:
            return cur
        cur = row[0]


def check_duplicate(conn, path):
    """Exact-duplicate check for a new/changed file.

    Records content hashes; returns the root canonical path when this file
    is bit-identical to another (and registers the alias), else None.
    Unchanged files never reach here (discovery skips them first).
    """
    key = str(path)
    try:
        st = Path(path).stat()
    except OSError:
        return None
    row = conn.execute(
        "SELECT mtime, size, sha256 FROM file_hashes WHERE path=?", (key,)
    ).fetchone()
    if row and (row[0], row[1]) == (st.st_mtime, st.st_size):
        sha = row[2]
    else:
        sha = sha256_of(key)
        phash = dhash_of(key)
        conn.execute(
            "INSERT OR REPLACE INTO file_hashes(path, mtime, size, sha256, phash) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, st.st_mtime, st.st_size, sha, phash),
        )
    if not sha:
        conn.execute("DELETE FROM duplicates WHERE dup_path=?", (key,))
        return None
    cand = conn.execute(
        "SELECT path FROM file_hashes WHERE sha256=? AND path != ? "
        "ORDER BY path LIMIT 1",
        (sha, key),
    ).fetchone()
    if cand is None:
        conn.execute("DELETE FROM duplicates WHERE dup_path=?", (key,))
        return None
    # Only trust candidates whose file still matches its cached stat.
    try:
        cst = Path(cand[0]).stat()
    except OSError:
        return None
    crow = conn.execute(
        "SELECT mtime, size FROM file_hashes WHERE path=?", (cand[0],)
    ).fetchone()
    if not crow or (crow[0], crow[1]) != (cst.st_mtime, cst.st_size):
        return None
    root = resolve_canonical(conn, cand[0])
    if root == key:
        return None
    conn.execute(
        "INSERT OR REPLACE INTO duplicates(dup_path, canonical_path) VALUES (?, ?)",
        (key, root),
    )
    return root


def photo_in_group(conn, group_id, image_path):
    """Group membership, direct or inherited through a duplicate alias."""
    if conn.execute(
        "SELECT 1 FROM group_image_paths WHERE group_id=? AND image_path=?",
        (group_id, image_path),
    ).fetchone():
        return True
    return (
        conn.execute(
            "SELECT 1 FROM duplicates d "
            "JOIN group_image_paths g ON g.image_path = d.canonical_path "
            "AND g.group_id=? WHERE d.dup_path=?",
            (group_id, image_path),
        ).fetchone()
        is not None
    )


def group_photos_with_dupes(conn, group_id):
    """Sorted group photos: direct links plus inherited duplicates."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT image_path FROM group_image_paths WHERE group_id=? "
            "UNION "
            "SELECT d.dup_path FROM duplicates d "
            "JOIN group_image_paths g ON g.image_path = d.canonical_path "
            "AND g.group_id=? "
            "ORDER BY 1",
            (group_id, group_id),
        )
    ]


def alias_map_for_photos(conn, photo_paths):
    """{dup_path: canonical_path} for the given photos (empty when none)."""
    paths = list(photo_paths)
    if not paths:
        return {}
    marks = ",".join("?" * len(paths))
    return {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT dup_path, canonical_path FROM duplicates WHERE dup_path IN ({marks})",
            paths,
        )
    }


def canonical_for_tag(conn, image_path):
    """Effective tag source: canonical path for aliases, else the path itself."""
    row = conn.execute(
        "SELECT canonical_path FROM duplicates WHERE dup_path=?", (str(image_path),)
    ).fetchone()
    return row[0] if row else str(image_path)

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


def open_db(db_path: str) -> sqlite3.Connection:
    """Open (and initialise) the database. Shared by web UI, GUI and CLI."""
    return _init_db(db_path)


def _init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Write-ahead logging: readers (web UI) no longer block while a run writes
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    # Readers poll frequently during runs; wait instead of failing busy.
    cursor.execute("PRAGMA busy_timeout=10000")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_files (
            file_path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sum_embedding TEXT NOT NULL,
            count INTEGER NOT NULL,
            directory TEXT NOT NULL,
            name TEXT
        )
        """
    )

    # Migration for databases created before the "name" column existed
    cursor.execute("PRAGMA table_info(groups)")
    cols = {row[1] for row in cursor.fetchall()}
    if "name" not in cols:
        cursor.execute("ALTER TABLE groups ADD COLUMN name TEXT")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS group_image_paths (
            group_id INTEGER NOT NULL,
            image_path TEXT NOT NULL,
            PRIMARY KEY (group_id, image_path)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS group_cropped_faces (
            group_id INTEGER NOT NULL,
            face_id TEXT NOT NULL,
            PRIMARY KEY (group_id, face_id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_faces (
            image_path TEXT NOT NULL,
            face_idx INTEGER NOT NULL,
            face_id TEXT NOT NULL,
            x1 REAL NOT NULL,
            y1 REAL NOT NULL,
            x2 REAL NOT NULL,
            y2 REAL NOT NULL,
            group_id INTEGER,
            detached INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (image_path, face_idx)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS undo_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            undone INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS run_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_image_paths_image_path "
        "ON group_image_paths(image_path)"
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_group_cropped_faces_face_id "
        "ON group_cropped_faces(face_id)"
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_photo_faces_image_path "
        "ON photo_faces(image_path)"
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_photo_faces_face_id "
        "ON photo_faces(face_id)"
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS file_hashes (
            path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            sha256 TEXT,
            phash TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS duplicates (
            dup_path TEXT PRIMARY KEY,
            canonical_path TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS dismissed_pairs (
            path_a TEXT NOT NULL,
            path_b TEXT NOT NULL,
            PRIMARY KEY (path_a, path_b)
        )
        """
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_hashes_sha ON file_hashes(sha256)"
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_duplicates_canonical "
        "ON duplicates(canonical_path)"
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_undo_log_undone ON undo_log(undone, id)"
    )

    conn.commit()
    return conn


def load_processed_state(db_path):
    """
    Load state from SQLite database.

    Returns (conn, processed_files, groups)
    where:
      - processed_files: {file_path: (mtime, size)}
      - groups: list of dicts compatible with the original in‑memory format.
    """
    conn = _init_db(db_path)
    cursor = conn.cursor()

    processed_files = {}
    for file_path, mtime, size in cursor.execute(
        "SELECT file_path, mtime, size FROM processed_files"
    ):
        processed_files[file_path] = (mtime, size)

    groups = []
    # NOTE: buffer the outer rows first — the per-group queries below reuse
    # this cursor, and re-executing on it would discard the outer result set
    # (this once silently dropped every group but the first on every re-run).
    group_rows = cursor.execute(
        "SELECT id, sum_embedding, count, directory, name FROM groups ORDER BY id"
    ).fetchall()
    for group_id, sum_embedding_str, count, directory, name in group_rows:
        if sum_embedding_str:
            sum_embedding = np.fromstring(sum_embedding_str, sep=",", dtype=np.float32)
        else:
            sum_embedding = None

        image_paths = {
            row[0]
            for row in cursor.execute(
                "SELECT image_path FROM group_image_paths WHERE group_id=?",
                (group_id,),
            )
        }
        cropped_faces = {
            row[0]
            for row in cursor.execute(
                "SELECT face_id FROM group_cropped_faces WHERE group_id=?",
                (group_id,),
            )
        }

        groups.append(
            {
                "id": group_id,
                "sum_embedding": sum_embedding,
                "count": count,
                "name": name,
                "image_paths": image_paths,
                "directory": Path(directory),
                "cropped_faces": cropped_faces,
            }
        )

    return conn, processed_files, groups


def save_processed_state(conn, processed_files, groups):
    """
    Persist current state into SQLite database.
    """
    cursor = conn.cursor()

    # Replace processed_files snapshot
    cursor.execute("DELETE FROM processed_files")
    for file_path, (mtime, size) in processed_files.items():
        cursor.execute(
            "INSERT INTO processed_files(file_path, mtime, size) VALUES (?, ?, ?)",
            (file_path, float(mtime), int(size)),
        )

    # Replace groups and their associated paths/faces
    # Preserve person names across the rewrite
    cursor.execute("SELECT id, name FROM groups")
    existing_names = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute("DELETE FROM group_image_paths")
    cursor.execute("DELETE FROM group_cropped_faces")
    cursor.execute("DELETE FROM groups")

    for idx, group in enumerate(groups, start=1):
        sum_embedding = group["sum_embedding"]
        if isinstance(sum_embedding, np.ndarray):
            sum_embedding_str = ",".join(map(str, sum_embedding.tolist()))
        else:
            sum_embedding_str = ""

        directory = str(group["directory"])
        count = int(group["count"])
        group_id = group.get("id", idx)
        name = group.get("name") or existing_names.get(group_id)

        cursor.execute(
            "INSERT INTO groups(id, sum_embedding, count, directory, name) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_id, sum_embedding_str, count, directory, name),
        )

        for image_path in group["image_paths"]:
            cursor.execute(
                "INSERT OR IGNORE INTO group_image_paths(group_id, image_path) "
                "VALUES (?, ?)",
                (group_id, image_path),
            )

        for face_id in group["cropped_faces"]:
            cursor.execute(
                "INSERT OR IGNORE INTO group_cropped_faces(group_id, face_id) "
                "VALUES (?, ?)",
                (group_id, face_id),
            )

    conn.commit()

def main():
    parser = argparse.ArgumentParser(description="Group images by detected faces.")
    parser.add_argument(
        "--input_folder",
        required=True,
        help="Path to the folder containing images",
    )
    parser.add_argument(
        "--output_faces",
        default="output_faces",
        help="Directory to save cropped face images",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.6,
        help="Cosine similarity threshold for grouping",
    )
    parser.add_argument(
        "--db_file",
        default="processing_state.db",
        help="SQLite database file to track processed images",
    )
    args = parser.parse_args()

    # Load existing state from SQLite
    conn, processed_files, groups = load_processed_state(args.db_file)
    output_faces_dir = Path(args.output_faces)
    output_faces_dir.mkdir(parents=True, exist_ok=True)

    # Initialize FaceAnalysis model
    app = FaceAnalysis(name='buffalo_l', providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])
    app.prepare(ctx_id=0, det_size=(640, 640))

    # Find new/changed files using absolute paths
    image_extensions = {'jpg', 'jpeg', 'png', 'bmp', 'tiff', 'webp', 'heic', 'heif'}
    new_files = []
    for path in Path(args.input_folder).rglob('*'):
        if path.is_file() and path.suffix.lower().lstrip('.') in image_extensions:
            abs_path = path.resolve()
            file_stat = abs_path.stat()
            file_key = str(abs_path)
            current_mtime = file_stat.st_mtime
            current_size = file_stat.st_size
            
            if file_key not in processed_files or processed_files[file_key] != (current_mtime, current_size):
                new_files.append(abs_path)
                processed_files[file_key] = (current_mtime, current_size)

    # Process new/changed files
    id_counter = max(
        (g["id"] for g in groups if isinstance(g.get("id"), int)), default=0
    )
    dup_skipped = 0
    for img_path in tqdm(new_files, desc="Grouping photos", unit="photo"):
        try:
            canonical = check_duplicate(conn, str(img_path))
            if canonical is not None:
                dup_skipped += 1
                print(f"Duplicate of {Path(canonical).name} — linked, not rescanned: "
                      f"{img_path.name}")
                continue
            img = read_image(str(img_path))
            if img is None:
                print(f"Could not read image: {img_path}")
                continue

            reset_photo_faces(conn, str(img_path))
            faces = app.get(img)

            for face_idx, face in enumerate(faces):
                embedding = face.embedding
                max_sim = -1
                best_group = None

                # Find best matching group
                for group in groups:
                    avg_embed = group['sum_embedding'] / group['count']
                    sim = cosine_similarity(embedding, avg_embed)
                    if sim > max_sim:
                        max_sim = sim
                        best_group = group

                face_id = image_face_id(img_path, face_idx)
                output_path = None

                if max_sim >= args.threshold:
                    # Check if face was already cropped for this group
                    if face_id in best_group['cropped_faces']:
                        record_photo_face(
                            conn, str(img_path), face_idx, face_id,
                            face.bbox, img.shape, best_group.get("id"),
                        )
                        if best_group.get("id") is not None:
                            conn.execute(
                                "INSERT OR IGNORE INTO group_image_paths(group_id, image_path) "
                                "VALUES (?, ?)",
                                (best_group.get("id"), str(img_path)),
                            )
                        continue

                    best_group['sum_embedding'] += embedding
                    best_group['count'] += 1
                    best_group['image_paths'].add(str(img_path))
                    best_group['cropped_faces'].add(face_id)
                    output_path = best_group['directory'] / f"{face_id}.jpg"
                    record_photo_face(
                        conn, str(img_path), face_idx, face_id,
                        face.bbox, img.shape, best_group.get("id"),
                    )
                else:
                    group_number = len(groups) + 1
                    group_dir = output_faces_dir / f"group_{group_number}"
                    group_dir.mkdir(parents=True, exist_ok=True)
                    output_path = group_dir / f"{face_id}.jpg"
                    id_counter += 1
                    best_group = {
                        'id': id_counter,
                        'sum_embedding': embedding.copy(),
                        'count': 1,
                        'image_paths': {str(img_path)},
                        'directory': group_dir,
                        'cropped_faces': {face_id}
                    }
                    groups.append(best_group)
                    record_photo_face(
                        conn, str(img_path), face_idx, face_id,
                        face.bbox, img.shape, id_counter,
                    )

                # Save face crop if needed
                if output_path and not output_path.exists():
                    x1, y1, x2, y2 = face.bbox.astype(int)
                    h, w = img.shape[:2]
                    x1 = max(0, x1)
                    y1 = max(0, y1)
                    x2 = min(w, x2)
                    y2 = min(h, y2)
                    
                    if x1 >= x2 or y1 >= y2:
                        print(f"Invalid bbox in {img_path}, face {face_idx}")
                        continue

                    face_crop = img[y1:y2, x1:x2]
                    if not cv2.imwrite(str(output_path), face_crop):
                        print(f"Failed to save face crop to {output_path}")

        except Exception as e:
            print(f"Error processing {img_path}: {str(e)}")

    # Save updated state
    save_processed_state(conn, processed_files, groups)
    conn.close()

    print(f"Done. {len(groups)} group(s) in '{output_faces_dir}', state in '{args.db_file}'.")
    print(f"{dup_skipped} duplicate(s) linked, not rescanned.")
    print("Use the web UI export (CSV/JSON) for a portable report.")

if __name__ == '__main__':
    main()