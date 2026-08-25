import argparse
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pillow_heif
from insightface.app import FaceAnalysis


def read_image(file_path):
    if file_path.lower().endswith(('.heic', '.heif')):
        heif_file = pillow_heif.open_heif(file_path)
        image = np.array(heif_file)
        return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    else:
        return cv2.imread(str(file_path))

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


def _init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

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
            directory TEXT NOT NULL
        )
        """
    )

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
    for group_id, sum_embedding_str, count, directory in cursor.execute(
        "SELECT id, sum_embedding, count, directory FROM groups ORDER BY id"
    ):
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

        cursor.execute(
            "INSERT INTO groups(id, sum_embedding, count, directory) VALUES (?, ?, ?, ?)",
            (group.get("id", idx), sum_embedding_str, count, directory),
        )
        group_id = group.get("id", idx)

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
        "--output_file",
        default="face_groups.txt",
        help="Path to the output file",
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
    for img_path in new_files:
        try:
            img = read_image(str(img_path))
            if img is None:
                print(f"Could not read image: {img_path}")
                continue
            
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
                
                face_id = f"{img_path.stem}_{face_idx}"
                output_path = None
                
                if max_sim >= args.threshold:
                    # Check if face was already cropped for this group
                    if face_id in best_group['cropped_faces']:
                        continue
                        
                    best_group['sum_embedding'] += embedding
                    best_group['count'] += 1
                    best_group['image_paths'].add(str(img_path))
                    best_group['cropped_faces'].add(face_id)
                    output_path = best_group['directory'] / f"{face_id}.jpg"
                else:
                    group_number = len(groups) + 1
                    group_dir = output_faces_dir / f"group_{group_number}"
                    group_dir.mkdir(parents=True, exist_ok=True)
                    output_path = group_dir / f"{face_id}.jpg"
                    best_group = {
                        'sum_embedding': embedding.copy(),
                        'count': 1,
                        'image_paths': {str(img_path)},
                        'directory': group_dir,
                        'cropped_faces': {face_id}
                    }
                    groups.append(best_group)

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

    # Write output
    with open(args.output_file, 'w') as f:
        for i, group in enumerate(groups):
            f.write(f"face {i+1} (cropped faces in: {group['directory']}):\n")
            for path in sorted(group['image_paths']):
                f.write(f"{path}\n")
            f.write("\n")

if __name__ == '__main__':
    main()