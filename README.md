# Face Grouping Application

This application uses InsightFace to detect and group similar faces from a collection of images. It supports various image formats including HEIC/HEIF and common image formats.

## Features

- Face detection and embedding extraction using InsightFace
- Support for multiple image formats (JPG, JPEG, PNG, BMP, TIFF, WEBP, HEIC, HEIF)
- Automatic face grouping based on similarity
- Cropped face images saved for each group
- State tracking to resume processing
- Progress tracking and incremental processing

## Requirements

- Python 3.10+ (3.12 recommended)
- CUDA-compatible GPU (recommended) or CPU
- Required Python packages:
  - opencv-python
  - numpy
  - insightface
  - pillow-heif
  - flask

## Installation

1. Install the required packages:
```bash
pip install opencv-python numpy insightface pillow-heif flask
```

2. Download the InsightFace model (buffalo_l) - this will be done automatically on first run

## Usage

### Web app (recommended)

```bash
python face_grouping_web.py
```

Then open http://127.0.0.1:5000 in your browser.

The web UI lets you:
- Pick input folders (multiple supported) and DB file with native folder/file dialogs
- Start grouping with a live progress bar, log, and a cancel button
- Watch people stream in during the run (progress checkpoints every 100 photos / 20s)
  — curation (move, delete, approve, undo) pauses during a run so checkpoints
  can't clobber it, and unlocks when the run finishes
- Starting a run states the resolved database path and existing people count,
  and warns about a brand-new database or input folders that differ from the
  database's last run (groups belong to the DB, not the folder)
- Browse people as a fast list (no images loaded up front — safe for thousands of photos)
- Select a person to see counts, then explicitly choose per section:
  - Face crops: click "View on UI" for a paginated, lazy-loaded grid, or keep previews off
  - Source photos: List mode shows filenames with zero images, Thumbs mode loads one page at a time,
    and every photo has Preview (single-image viewer) plus Reveal (opens Explorer on the server);
    "Open folder" reveals the whole group folder without loading anything in the browser
- Name people (stored in the database) shown on group cards
- View a face in a lightbox together with the source photo(s) it came from
- Face tags on source photos: hover boxes with names (Facebook-style), click a tag to open that person
- Remove incorrect/unwanted faces (moved to a `.trash` folder) and undo moves/deletes
- Select multiple faces (Ctrl-click / Shift-click) or drag-and-drop them between groups
- Copy all source photo paths of a group to the clipboard
- See stats (photos processed, faces, largest groups)
- Export groups as CSV or JSON
- Toggle dark/light theme; settings are remembered in localStorage
- Keyboard shortcuts: arrow keys navigate faces, Enter opens lightbox,
  M moves, Delete removes, Esc closes dialogs

### CLI

Basic usage:
```bash
python face_grouping_v5.py --input_folder /path/to/images
```

Full options:
```bash
python face_grouping_v5.py \
    --input_folder /path/to/images \
    --output_faces output_faces \
    --threshold 0.6 \
    --db_file processing_state.db
```

### Parameters

- `--input_folder`: (Required) Path to the folder containing images
- `--output_faces`: (Optional) Directory to save cropped face images (default: output_faces)
- `--threshold`: (Optional) Cosine similarity threshold for grouping (default: 0.6)
- `--db_file`: (Optional) SQLite database file to track processed images (default: processing_state.db)

## Output

1. `output_faces/`: Directory containing subdirectories for each face group with cropped face images
2. `processing_state.db`: SQLite database tracking processed files, groups, and per-photo face-tag boxes for incremental processing (use Export in the web UI for CSV/JSON reports)

Face tags are recorded during grouping (normalised boxes + owning group per face).
Tags follow renames and moves, survive "approve", and are hidden for trashed faces.
Photos processed before tagging show no boxes until re-processed.
EXIF orientation is normalised at detection and serving time so boxes land correctly.

## Notes

- Higher threshold values (closer to 1.0) will create more strict face grouping
- Lower threshold values (closer to 0.0) will create more lenient face grouping
- The application can be stopped and resumed using the state database
- Processing large image collections may take significant time depending on your hardware 