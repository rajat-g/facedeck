import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from face_grouping_v5 import load_processed_state, open_db, main as cli_main


class FaceGroupingApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("InsightFace Grouping")
        self.geometry("1000x700")
        self.resizable(True, True)

        self.worker_thread = None
        self.current_db_file: str | None = None
        self.groups = []
        self.selected_group_index: int | None = None
        self._thumb_images = []

        self._build_ui()

    def _build_ui(self) -> None:
        padding = {"padx": 8, "pady": 4}

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=10, pady=10)

        # Input folder
        row0 = ttk.Frame(container)
        row0.pack(fill="x", **padding)
        ttk.Label(row0, text="Input folder:").pack(side="left")
        self.input_var = tk.StringVar()
        ttk.Entry(row0, textvariable=self.input_var, width=60).pack(
            side="left", padx=(6, 6)
        )
        ttk.Button(row0, text="Browse...", command=self._browse_input).pack(side="left")

        # Output faces folder
        row1 = ttk.Frame(container)
        row1.pack(fill="x", **padding)
        ttk.Label(row1, text="Output faces folder:").pack(side="left")
        self.output_faces_var = tk.StringVar(value="output_faces")
        ttk.Entry(row1, textvariable=self.output_faces_var, width=60).pack(
            side="left", padx=(6, 6)
        )
        ttk.Button(row1, text="Browse...", command=self._browse_output_faces).pack(
            side="left"
        )

        # Database file
        row3 = ttk.Frame(container)
        row3.pack(fill="x", **padding)
        ttk.Label(row3, text="Database file:").pack(side="left")
        self.db_file_var = tk.StringVar(value="processing_state.db")
        ttk.Entry(row3, textvariable=self.db_file_var, width=60).pack(
            side="left", padx=(6, 6)
        )
        ttk.Button(row3, text="Browse...", command=self._browse_db_file).pack(
            side="left"
        )

        # Threshold
        row4 = ttk.Frame(container)
        row4.pack(fill="x", **padding)
        ttk.Label(row4, text="Similarity threshold:").pack(side="left")
        self.threshold_var = tk.DoubleVar(value=0.6)
        ttk.Scale(
            row4,
            from_=0.3,
            to=0.9,
            orient="horizontal",
            variable=self.threshold_var,
        ).pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.threshold_label = ttk.Label(row4, text="0.60")
        self.threshold_label.pack(side="left")
        self.threshold_var.trace_add("write", self._update_threshold_label)

        # Run button
        row5 = ttk.Frame(container)
        row5.pack(fill="x", **padding)
        self.run_button = ttk.Button(row5, text="Start grouping", command=self._run)
        self.run_button.pack(side="right")

        # Main content area: groups / faces / log
        paned = ttk.Panedwindow(container, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=(10, 0))

        # Left: groups list and rename
        left_frame = ttk.Frame(paned)
        paned.add(left_frame, weight=1)

        group_frame = ttk.LabelFrame(left_frame, text="Groups")
        group_frame.pack(fill="both", expand=True, padx=4, pady=4)

        self.group_list = tk.Listbox(group_frame, height=15)
        self.group_list.pack(fill="both", expand=True, padx=4, pady=4)
        self.group_list.bind("<<ListboxSelect>>", self._on_group_select)

        rename_group_frame = ttk.Frame(left_frame)
        rename_group_frame.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Label(rename_group_frame, text="Group name:").pack(side="left")
        self.group_name_var = tk.StringVar()
        ttk.Entry(rename_group_frame, textvariable=self.group_name_var, width=20).pack(
            side="left", padx=(4, 4)
        )
        ttk.Button(
            rename_group_frame, text="Rename group", command=self._rename_group
        ).pack(side="left")

        # Right: faces and log
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=3)

        faces_frame = ttk.LabelFrame(right_frame, text="Cropped faces")
        faces_frame.pack(fill="both", expand=True, padx=4, pady=4)

        self.thumb_canvas = tk.Canvas(faces_frame)
        self.thumb_canvas.pack(side="left", fill="both", expand=True)

        thumb_scrollbar = ttk.Scrollbar(
            faces_frame, orient="vertical", command=self.thumb_canvas.yview
        )
        thumb_scrollbar.pack(side="right", fill="y")
        self.thumb_canvas.configure(yscrollcommand=thumb_scrollbar.set)

        self.thumb_inner = ttk.Frame(self.thumb_canvas)
        self.thumb_canvas.create_window((0, 0), window=self.thumb_inner, anchor="nw")

        self.thumb_inner.bind(
            "<Configure>",
            lambda e: self.thumb_canvas.configure(scrollregion=self.thumb_canvas.bbox("all")),
        )

        # Log output
        log_frame = ttk.LabelFrame(right_frame, text="Log")
        log_frame.pack(fill="x", padx=4, pady=(0, 4))

        self.log_text = tk.Text(
            log_frame,
            height=8,
            wrap="word",
            state="disabled",
            background="#111111",
            foreground="#f0f0f0",
        )
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    def _update_threshold_label(self, *_):
        self.threshold_label.config(text=f"{self.threshold_var.get():.2f}")

    def _browse_input(self) -> None:
        folder = filedialog.askdirectory(title="Select input folder")
        if folder:
            self.input_var.set(folder)

    def _browse_output_faces(self) -> None:
        folder = filedialog.askdirectory(title="Select output faces folder")
        if folder:
            self.output_faces_var.set(folder)

    def _browse_db_file(self) -> None:
        file_path = filedialog.asksaveasfilename(
            title="Select database file",
            defaultextension=".db",
            filetypes=[("SQLite DB", "*.db"), ("All files", "*.*")],
        )
        if file_path:
            self.db_file_var.set(file_path)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_thumbnails(self) -> None:
        for child in self.thumb_inner.winfo_children():
            child.destroy()
        self._thumb_images.clear()

    def _show_thumbnails_for_group(self, group_index: int) -> None:
        self._clear_thumbnails()
        if group_index < 0 or group_index >= len(self.groups):
            return

        group = self.groups[group_index]
        group_dir = Path(group["directory"])
        if not group_dir.is_dir():
            self._append_log(f"Group directory missing: {group_dir}")
            return

        exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}
        image_paths = sorted(
            [p for p in group_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]
        )

        for idx, img_path in enumerate(image_paths):
            try:
                img = Image.open(img_path)
                img.thumbnail((120, 120))
                photo = ImageTk.PhotoImage(img)
                self._thumb_images.append(photo)

                lbl = ttk.Label(
                    self.thumb_inner,
                    image=photo,
                    text=img_path.name,
                    compound="top",
                    padding=4,
                )
                lbl.grid(row=idx // 4, column=idx % 4, padx=4, pady=4)
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"Failed to load thumbnail for {img_path}: {exc}")

    def _on_group_select(self, _event=None) -> None:
        if not self.groups:
            return
        selection = self.group_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        self.selected_group_index = index
        group = self.groups[index]
        self.group_name_var.set(group.get("name") or Path(group["directory"]).name)
        self._show_thumbnails_for_group(index)

    def _run(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Running", "Processing is already in progress.")
            return

        input_folder = self.input_var.get().strip()
        if not input_folder:
            messagebox.showerror("Error", "Please select an input folder.")
            return

        if not Path(input_folder).is_dir():
            messagebox.showerror("Error", "Input folder does not exist.")
            return

        output_faces = self.output_faces_var.get().strip() or "output_faces"
        db_file = self.db_file_var.get().strip() or "processing_state.db"
        threshold = self.threshold_var.get()

        self.current_db_file = db_file
        self.run_button.config(state="disabled")
        self._append_log("Starting face grouping...")

        def worker():
            import sys

            args = [
                "face_grouping_v5.py",
                "--input_folder",
                input_folder,
                "--output_faces",
                output_faces,
                "--threshold",
                str(threshold),
                "--db_file",
                db_file,
            ]

            # Build a fake argv for the existing CLI entrypoint
            old_argv = sys.argv
            try:
                sys.argv = args
                cli_main()
                self.after(0, lambda: self._append_log("Processing completed."))
                self.after(0, lambda: self._load_groups_from_db(db_file))
            except Exception as exc:  # noqa: BLE001
                self.after(
                    0,
                    lambda: [
                        self._append_log(f"Error: {exc}"),
                        messagebox.showerror("Error", str(exc)),
                    ],
                )
            finally:
                sys.argv = old_argv
                self.after(0, lambda: self.run_button.config(state="normal"))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _load_groups_from_db(self, db_file: str) -> None:
        try:
            conn, _processed, groups = load_processed_state(db_file)
            conn.close()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"Failed to load groups from DB: {exc}")
            return

        self.groups = groups
        self.group_list.delete(0, "end")
        for idx, g in enumerate(groups, start=1):
            name = g.get("name") or Path(g["directory"]).name
            self.group_list.insert("end", f"{idx}: {name}")
        self._append_log(f"Loaded {len(groups)} groups from database.")

    def _rename_group(self) -> None:
        if self.selected_group_index is None or not self.groups:
            messagebox.showerror("Error", "Please select a group first.")
            return

        new_name = self.group_name_var.get().strip()
        if not new_name:
            messagebox.showerror("Error", "Group name cannot be empty.")
            return

        group = self.groups[self.selected_group_index]

        if not self.current_db_file:
            messagebox.showerror("Error", "No database file selected.")
            return

        try:
            conn = open_db(self.current_db_file)
            row = conn.execute(
                "SELECT id FROM groups WHERE id=?", (group["id"],)
            ).fetchone()
            if row is None:
                conn.close()
                messagebox.showerror("Error", "Group not found in database.")
                return
            conn.execute(
                "UPDATE groups SET name=? WHERE id=?", (new_name, group["id"])
            )
            conn.commit()
            conn.close()
            group["name"] = new_name

            # Update UI list
            self.group_list.delete(self.selected_group_index)
            self.group_list.insert(
                self.selected_group_index,
                f"{self.selected_group_index + 1}: {new_name}",
            )
            self._append_log(f"Renamed person to: {new_name}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Error", f"Failed to rename group: {exc}")


def main() -> None:
    app = FaceGroupingApp()
    app.mainloop()


if __name__ == "__main__":
    main()

