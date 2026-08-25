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
- Select an input folder and start grouping with a live progress bar and log
- View all face groups as thumbnail grids
- Rename groups (renames the folder on disk too)
- Remove incorrect/unwanted faces from groups
- Move a face to a different group or into a newly created group

### CLI

Basic usage:
```bash
python face_grouping_v5.py --input_folder /path/to/images
```

Full options:
```bash
python face_grouping_v5.py \
    --input_folder /path/to/images \
    --output_file face_groups.txt \
    --output_faces output_faces \
    --threshold 0.6 \
    --db_file processing_state.db
```

### Parameters

- `--input_folder`: (Required) Path to the folder containing images
- `--output_file`: (Optional) Path to the output file (default: face_groups.txt)
- `--output_faces`: (Optional) Directory to save cropped face images (default: output_faces)
- `--threshold`: (Optional) Cosine similarity threshold for grouping (default: 0.6)
- `--db_file`: (Optional) SQLite database file to track processed images (default: processing_state.db)

## Output

1. `face_groups.txt`: Contains the list of images grouped by detected faces
2. `output_faces/`: Directory containing subdirectories for each face group with cropped face images
3. `processing_state.db`: SQLite database tracking processed files and groups for incremental processing

## Notes

- Higher threshold values (closer to 1.0) will create more strict face grouping
- Lower threshold values (closer to 0.0) will create more lenient face grouping
- The application can be stopped and resumed using the state database
- Processing large image collections may take significant time depending on your hardware 