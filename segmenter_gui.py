#!/usr/bin/env python3
"""
Herbarium Packet Segmenter
GUI tool for batch-segmenting bryophyte/lichen packet labels from photographs.
"""

import concurrent.futures
import csv
import datetime
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
import cv2
import numpy as np
import requests


# ─── Constants ────────────────────────────────────────────────────────────────

APP_TITLE = "Herbarium Packet Segmenter"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

MANIFEST_FIELDS = [
    "source_image", "output_crop", "packet_index", "detected_foreground",
    "top_crop_px", "bbox_x", "bbox_y", "bbox_w", "bbox_h",
    "crop_x1", "crop_y1", "crop_x2", "crop_y2",
    "contour_area", "aspect", "rectangularity", "deskewed",
]

# ─── Herbarium colour palette ─────────────────────────────────────────────────
_C = {
    "deep_green": "#1B3A24",   # primary text, button hover, strong emphasis
    "mid_green":  "#318738",   # primary buttons, sliders, progress bar
    "soft_green": "#D6E8D3",   # panel / card backgrounds, option menus
    "cream":      "#F7F5F0",   # left-panel background, frame cards
    "warm_white": "#FDFCFA",   # window background, entry fields, log box
    "grey":       "#747676",   # secondary text, oversize flag
    "rule":       "#C8C4BC",   # borders, separators, subtle outlines
    # Error / destructive — kept red so they read as unambiguous alerts
    "err":        "#8B2020",
    "err_hover":  "#6B1515",
}

_FLAG_STYLE = {
    "none":     ("⛔  No packets detected",    _C["err"],  _C["err_hover"]),
    "oversize": ("⚠  Possible merged packets", _C["grey"], _C["deep_green"]),
}

VVGO_SERVER_URL   = "https://vouchervision-go-738307415303.us-central1.run.app/"
VVGO_DEFAULT_PROMPT = "SLTPvM_default.yaml"


# ─── Settings ─────────────────────────────────────────────────────────────────

@dataclass
class SegSettings:
    top_crop_frac: float = 0.0
    foreground: str = "light"       # light | dark | auto
    threshold_mode: str = "otsu"    # otsu | adaptive | canny
    contrast: str = "none"          # none | normalize | clahe | both
    adaptive_block_frac: float = 0.06
    adaptive_c: int = 7
    min_area_frac: float = 0.0005
    max_area_frac: float = 0.95
    min_width_frac: float = 0.04
    min_height_frac: float = 0.04
    padding: int = 30
    morph_frac: float = 0.0015
    rectangularity_min: float = 0.12
    aspect_min: float = 0.20
    aspect_max: float = 5.0
    deskew: bool = True


# ─── Segmentation core ────────────────────────────────────────────────────────

def _to_uint8(img):
    if img.dtype == np.uint8:
        return img
    f = img.astype(np.float32)
    lo, hi = np.percentile(f, 0.5), np.percentile(f, 99.5)
    if hi <= lo:
        return np.zeros(img.shape, dtype=np.uint8)
    return np.clip((f - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)


def _to_bgr8(img):
    img8 = _to_uint8(img)
    if img8.ndim == 2:
        return cv2.cvtColor(img8, cv2.COLOR_GRAY2BGR)
    if img8.shape[2] == 4:
        return cv2.cvtColor(img8, cv2.COLOR_BGRA2BGR)
    return img8


def _odd(v):
    v = max(3, int(round(v)))
    return v if v % 2 else v + 1


def _enhance(gray, mode):
    out = gray.copy()
    if mode in ("normalize", "both"):
        out = cv2.normalize(out, None, 0, 255, cv2.NORM_MINMAX)
    if mode in ("clahe", "both"):
        out = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(out)
    return out


def _raw_mask(gray, fg, mode, block_frac, c):
    h, w = gray.shape[:2]
    flag_light = cv2.THRESH_BINARY
    flag_dark = cv2.THRESH_BINARY_INV
    bflag = flag_light if fg == "light" else flag_dark

    if mode == "otsu":
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        _, mask = cv2.threshold(blur, 0, 255, bflag | cv2.THRESH_OTSU)
        return mask

    if mode == "adaptive":
        bs = max(15, _odd(min(h, w) * block_frac))
        return cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, bflag, bs, c)

    if mode == "canny":
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 40, 120)
        k = np.ones((5, 5), np.uint8)
        mask = cv2.dilate(edges, k, iterations=1)
        return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)

    raise ValueError(f"Unknown threshold mode: {mode}")


def _clean_mask(mask, morph_frac):
    h, w = mask.shape[:2]
    sz = _odd(min(h, w) * morph_frac)
    k = np.ones((sz, sz), np.uint8)
    out = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    return cv2.morphologyEx(out, cv2.MORPH_CLOSE, k)


def _score_detections(dets):
    if not dets:
        return -1.0
    areas = [d["area"] for d in dets]
    med = np.median(areas)
    penalty = sum(10.0 for a in areas if med > 0 and a / med > 5)
    return len(dets) * 10.0 + sum(d["rect"] for d in dets) - penalty


def _sort_reading_order(dets):
    if not dets:
        return []
    for d in dets:
        d["cx"] = d["x"] + d["w"] / 2
        d["cy"] = d["y"] + d["h"] / 2
    tol = max(20, np.median([d["h"] for d in dets]) * 0.45)
    by_cy = sorted(dets, key=lambda d: d["cy"])
    rows = []
    for det in by_cy:
        for row in rows:
            if abs(det["cy"] - np.mean([r["cy"] for r in row])) <= tol:
                row.append(det)
                break
        else:
            rows.append([det])
    rows.sort(key=lambda row: np.mean([r["cy"] for r in row]))
    result = []
    for row in rows:
        result.extend(sorted(row, key=lambda d: d["cx"]))
    return result


def _extract_detections(mask, s: SegSettings):
    h, w = mask.shape[:2]
    total_area = h * w
    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    dets = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        ca = cv2.contourArea(c)
        if not (s.min_area_frac * total_area <= ca <= s.max_area_frac * total_area):
            continue
        if bw < s.min_width_frac * w or bh < s.min_height_frac * h:
            continue
        aspect = bw / float(bh)
        if not (s.aspect_min <= aspect <= s.aspect_max):
            continue
        rect = ca / (bw * bh) if bw * bh > 0 else 0.0
        if rect < s.rectangularity_min:
            continue
        dets.append({
            "contour": c, "x": x, "y": y, "w": bw, "h": bh,
            "area": ca, "aspect": aspect, "rect": rect,
        })
    return _sort_reading_order(dets)


def _detect_packets(work_bgr, s: SegSettings):
    gray = _enhance(cv2.cvtColor(work_bgr, cv2.COLOR_BGR2GRAY), s.contrast)

    if s.foreground == "auto":
        masks = {
            fg: _clean_mask(
                _raw_mask(gray, fg, s.threshold_mode, s.adaptive_block_frac, s.adaptive_c),
                s.morph_frac,
            )
            for fg in ("light", "dark")
        }
        dets_l = _extract_detections(masks["light"], s)
        dets_d = _extract_detections(masks["dark"], s)
        if _score_detections(dets_l) >= _score_detections(dets_d):
            return dets_l, masks["light"], "light"
        return dets_d, masks["dark"], "dark"

    mask = _clean_mask(
        _raw_mask(gray, s.foreground, s.threshold_mode, s.adaptive_block_frac, s.adaptive_c),
        s.morph_frac,
    )
    return _extract_detections(mask, s), mask, s.foreground


def _order_box(pts):
    pts = np.array(pts, dtype="float32")
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1)
    return np.array([
        pts[np.argmin(s)], pts[np.argmin(d)],
        pts[np.argmax(s)], pts[np.argmax(d)],
    ], dtype="float32")


def _deskew_crop(img, contour, padding):
    rect = cv2.minAreaRect(contour)
    box = _order_box(cv2.boxPoints(rect))
    wa = int(max(np.linalg.norm(box[2] - box[3]), np.linalg.norm(box[1] - box[0]))) + padding * 2
    ha = int(max(np.linalg.norm(box[1] - box[2]), np.linalg.norm(box[0] - box[3]))) + padding * 2
    if wa <= 0 or ha <= 0:
        return None
    dst = np.array([
        [padding, padding], [wa - padding - 1, padding],
        [wa - padding - 1, ha - padding - 1], [padding, ha - padding - 1],
    ], dtype="float32")
    M = cv2.getPerspectiveTransform(box, dst)
    return cv2.warpPerspective(img, M, (wa, ha),
                               flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def _padded_crop(img, x, y, w, h, padding):
    ih, iw = img.shape[:2]
    x1, y1 = max(0, x - padding), max(0, y - padding)
    x2, y2 = min(iw, x + w + padding), min(ih, y + h + padding)
    return img[y1:y2, x1:x2], (x1, y1, x2, y2)


def _draw_boxes(bgr, dets):
    out = bgr.copy()
    for i, d in enumerate(dets, 1):
        x, y, w, h = d["x"], d["y"], d["w"], d["h"]
        cv2.rectangle(out, (x, y), (x + w, y + h), (30, 200, 60), 4)
        cv2.putText(out, f"{i:02d}", (x + 8, y + 44),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.3, (30, 30, 220), 3, cv2.LINE_AA)
    return out


def segment_image(image_path: Path, s: SegSettings):
    """
    Detect packets — no files written.
    Returns (detections, debug_bgr, selected_foreground, top_crop_px).
    """
    img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise IOError(f"Cannot read: {image_path.name}")
    bgr8 = _to_bgr8(img)
    top_px = int(img.shape[0] * s.top_crop_frac)
    work = bgr8[top_px:, :]
    if work.size == 0:
        raise ValueError("Top-crop fraction removes the entire image.")
    dets, _mask, fg = _detect_packets(work, s)
    return dets, _draw_boxes(work, dets), fg, top_px


def save_crops(image_path: Path, output_dir: Path,
               s: SegSettings, manifest_rows: list):
    """
    Detect packets, save individual crop files, append rows to manifest_rows.
    Crops are written flat into output_dir (no sub-folders).
    Returns (saved_count, debug_bgr, crop_info) where crop_info is a list of
    (Path, width_px, height_px) for every successfully written crop.
    """
    img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise IOError(f"Cannot read: {image_path.name}")
    bgr8 = _to_bgr8(img)
    top_px = int(img.shape[0] * s.top_crop_frac)
    work8 = bgr8[top_px:, :]
    work_orig = img[top_px:, :]
    if work8.size == 0:
        raise ValueError("Top-crop fraction removes the entire image.")

    dets, _mask, fg = _detect_packets(work8, s)
    stem = image_path.stem

    saved = 0
    crop_info: list[tuple[Path, int, int]] = []
    for idx, d in enumerate(dets, 1):
        x, y, w, h = d["x"], d["y"], d["w"], d["h"]
        if s.deskew:
            crop = _deskew_crop(work_orig, d["contour"], s.padding)
            bx1 = by1 = bx2 = by2 = ""
        else:
            crop, (bx1, by1, bx2, by2) = _padded_crop(
                work_orig, x, y, w, h, s.padding)
        if crop is None or crop.size == 0:
            continue
        out_path = output_dir / f"{stem}_packet_{idx:02d}{image_path.suffix.lower()}"
        if cv2.imwrite(str(out_path), crop):
            saved += 1
            crop_info.append((out_path, crop.shape[1], crop.shape[0]))  # (path, w, h)
            manifest_rows.append({
                "source_image": str(image_path),
                "output_crop": str(out_path),
                "packet_index": idx,
                "detected_foreground": fg,
                "top_crop_px": top_px,
                "bbox_x": x, "bbox_y": y + top_px,
                "bbox_w": w, "bbox_h": h,
                "crop_x1": bx1,
                "crop_y1": "" if by1 == "" else by1 + top_px,
                "crop_x2": bx2,
                "crop_y2": "" if by2 == "" else by2 + top_px,
                "contour_area": round(float(d["area"]), 2),
                "aspect": round(float(d["aspect"]), 3),
                "rectangularity": round(float(d["rect"]), 3),
                "deskewed": s.deskew,
            })

    return saved, _draw_boxes(work8, dets), crop_info


# ─── Result flagging ──────────────────────────────────────────────────────────

@dataclass
class ImageResult:
    path: Path
    count: int
    crop_info: list = field(default_factory=list)  # [(Path, width_px, height_px), ...]
    flag: str = ""         # "" | "none" | "oversize"
    flag_detail: str = ""  # human-readable explanation shown in the review panel


# How many times larger than the median a crop must be to trigger a flag.
# 1.8 means a crop that is 80 % wider OR 80 % taller than the median is flagged.
_OVERSIZE_THRESHOLD = 1.8


def flag_results(results: list) -> list:
    """
    Flag source images whose crops are significantly larger than the batch median.
    An oversized crop most commonly means two or more packets were merged by the
    segmentation step and saved as one file.
    """
    # Collect every crop dimension across the whole batch
    all_dims = [(w, h) for r in results for _, w, h in r.crop_info]

    if all_dims:
        med_w = float(np.median([d[0] for d in all_dims]))
        med_h = float(np.median([d[1] for d in all_dims]))
    else:
        med_w = med_h = 0.0

    for r in results:
        if r.flag:          # already flagged (e.g. read error)
            continue
        if r.count == 0:
            r.flag = "none"
            continue
        if med_w > 0 and med_h > 0:
            oversized = [
                (p, w, h) for p, w, h in r.crop_info
                if w > _OVERSIZE_THRESHOLD * med_w
                or h > _OVERSIZE_THRESHOLD * med_h
            ]
            if oversized:
                max_w = max(w for _, w, h in oversized)
                max_h = max(h for _, w, h in oversized)
                r.flag = "oversize"
                r.flag_detail = (
                    f"{len(oversized)} crop(s) up to {max_w}×{max_h}px"
                    f"  (median {int(med_w)}×{int(med_h)}px)"
                )
    return results


# ─── Preview helpers ─────────────────────────────────────────────────────────

def make_composite(bgr_list: list) -> np.ndarray:
    """
    Arrange a list of BGR images side by side at a common height.
    A thin dark separator is drawn between each crop so the boundary is clear.
    """
    if not bgr_list:
        return np.zeros((100, 100, 3), dtype=np.uint8)
    if len(bgr_list) == 1:
        return bgr_list[0]

    target_h = max(img.shape[0] for img in bgr_list)
    panels = []
    sep = np.full((target_h, 6, 3), [188, 196, 200], dtype=np.uint8)  # Rule #C8C4BC (BGR)

    for i, img in enumerate(bgr_list):
        h, w = img.shape[:2]
        if h != target_h:
            img = cv2.resize(img, (int(w * target_h / h), target_h),
                             interpolation=cv2.INTER_LANCZOS4)
        if i > 0:
            panels.append(sep)
        panels.append(img)

    return np.hstack(panels)


# ─── Crop repair ─────────────────────────────────────────────────────────────

def resegment_or_bisect(crop_path: Path, output_dir: Path,
                        s: SegSettings) -> tuple[list, str]:
    """
    Attempt to split an oversized crop into individual packet crops.

    Strategy
    --------
    1. Re-run the segmenter on the crop image alone.  Because the crop is
       small, relative area thresholds recalculate favourably and the gap
       between two adjacent packets is now a much larger fraction of the
       frame, making detection more reliable.
    2. If segmentation finds ≥ 2 objects: save each as a replacement file.
    3. Otherwise fall back to a geometric bisect along the longest edge —
       reliable when exactly two side-by-side (or stacked) packets are merged.

    The original oversized crop is deleted on success.

    Returns (new_paths: list[Path], description: str).
    Raises on unrecoverable errors.
    """
    img = cv2.imread(str(crop_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise IOError(f"Cannot read crop: {crop_path.name}")

    bgr8 = _to_bgr8(img)
    stem = crop_path.stem          # e.g. "_DSC0022_packet_03"
    suffix = crop_path.suffix

    # ── Re-segmentation attempt ──────────────────────────────────────────────
    # Use the same user settings but no top-crop and auto-foreground so the
    # detector is as open as possible on the unfamiliar sub-image.
    s_sub = SegSettings(
        top_crop_frac=0.0,
        foreground="auto",
        threshold_mode=s.threshold_mode,
        contrast=s.contrast,
        adaptive_block_frac=s.adaptive_block_frac,
        adaptive_c=s.adaptive_c,
        min_area_frac=0.05,   # relative to the small crop; permissive
        max_area_frac=0.80,
        min_width_frac=0.06,
        min_height_frac=0.06,
        padding=s.padding,
        morph_frac=s.morph_frac,
        rectangularity_min=s.rectangularity_min,
        aspect_min=s.aspect_min,
        aspect_max=s.aspect_max,
        deskew=s.deskew,
    )

    try:
        dets, _mask, _fg = _detect_packets(bgr8, s_sub)
    except Exception:
        dets = []

    if len(dets) >= 2:
        new_paths = []
        letters = "abcdefghij"
        for i, d in enumerate(dets):
            x, y, w, h = d["x"], d["y"], d["w"], d["h"]
            crop, _ = _padded_crop(img, x, y, w, h, s.padding)
            if crop is None or crop.size == 0:
                continue
            letter = letters[i] if i < len(letters) else str(i + 1)
            out_path = output_dir / f"{stem}{letter}{suffix}"
            if cv2.imwrite(str(out_path), crop):
                new_paths.append(out_path)

        if len(new_paths) >= 2:
            crop_path.unlink(missing_ok=True)
            return new_paths, f"re-segmented into {len(new_paths)} packets"

    # ── Geometric bisect fallback ────────────────────────────────────────────
    ih, iw = img.shape[:2]
    if iw >= ih:
        halves = [img[:, : iw // 2], img[:, iw // 2 :]]
        axis = "left / right"
    else:
        halves = [img[: ih // 2, :], img[ih // 2 :, :]]
        axis = "top / bottom"

    new_paths = []
    for i, half in enumerate(halves):
        if half.size == 0:
            continue
        out_path = output_dir / f"{stem}{'ab'[i]}{suffix}"
        if cv2.imwrite(str(out_path), half):
            new_paths.append(out_path)

    if len(new_paths) == 2:
        crop_path.unlink(missing_ok=True)
        seg_note = f"(re-segmentation found {len(dets)} packet)" if dets else "(re-segmentation found nothing)"
        return new_paths, f"bisected {axis}  {seg_note}"

    raise RuntimeError("Could not write replacement crops.")


# ─── Utilities ────────────────────────────────────────────────────────────────

def _auto_output_dir(input_path: Path) -> Path:
    """Return a dated subdirectory of the input folder: DD-MON-YYYY-PACKETS."""
    today = datetime.date.today()
    folder_name = today.strftime("%d-%b-%Y").upper() + "-PACKETS"
    return input_path / folder_name


class _ToolTip:
    """Lightweight hover tooltip for any tkinter / CTk widget."""

    _DELAY_MS = 600

    def __init__(self, widget, text: str):
        self._widget = widget
        self._text   = text
        self._tip: tk.Toplevel | None = None
        self._after_id = None
        widget.bind("<Enter>",   self._schedule, add="+")
        widget.bind("<Leave>",   self._cancel,   add="+")
        widget.bind("<Destroy>", self._cancel,   add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self._widget.after(self._DELAY_MS, self._show)

    def _cancel(self, _event=None):
        if self._after_id:
            self._widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None

    def _show(self):
        try:
            x = self._widget.winfo_rootx() + 16
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        except Exception:
            return
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip, text=self._text,
            justify="left",
            background=_C["deep_green"], foreground=_C["warm_white"],
            relief="flat", padx=9, pady=5,
            font=("Segoe UI", 9),
            wraplength=270,
        ).pack()


# ─── Manual crop editor ───────────────────────────────────────────────────────

class ManualCropEditor(ctk.CTkToplevel):
    """
    Modal dialog for drawing bounding boxes on a source image and saving the
    selected regions as individual crops.

    Controls
    --------
    Left-drag       Draw a new box (rubber-band preview while dragging)
    Click inside    Select an existing box (highlighted in mid_green)
    Delete / Back   Remove the selected box
    Scroll          Zoom in / out, centred on the cursor
    Right-drag      Pan the view
    Double-click    Reset to fit-in-window, no pan
    """

    def __init__(self, master, image_path: Path,
                 output_dir: Path, s: SegSettings,
                 overlay_bgr: np.ndarray | None = None,
                 on_save: "callable | None" = None):
        super().__init__(master)
        self.title(f"Manual crop editor — {image_path.name}")
        self.geometry("980x680")
        self.minsize(720, 500)
        self.grab_set()   # modal

        self._image_path = image_path
        self._output_dir = output_dir
        self._s = s
        self._on_save = on_save  # called with no args after a successful save

        # Load source image and apply top-crop
        raw = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
        if raw is None:
            raise ValueError(f"Cannot open image: {image_path}")
        bgr_full = _to_bgr8(raw)
        ih = bgr_full.shape[0]
        top_px = int(ih * s.top_crop_frac)
        clean = bgr_full[top_px:, :]

        # _bgr_display: what is rendered on the canvas (may include overlay boxes)
        # _bgr_save:    what is actually cropped when saving (always clean)
        self._bgr_display = overlay_bgr if overlay_bgr is not None else clean
        self._bgr_save    = clean

        # View state
        self._zoom: float = 1.0
        self._pan_x: int = 0
        self._pan_y: int = 0
        self._pan_start: tuple | None = None   # right-drag origin
        self._draw_start: tuple | None = None  # left-drag origin (canvas coords)
        self._rubber_id = None                 # canvas item for rubber-band rect

        # Box state — list of (x1, y1, x2, y2) in *image* pixel coordinates
        self._boxes: list[tuple[int, int, int, int]] = []
        self._selected: int | None = None

        self._tk_photo = None   # prevent GC of the Tk photo object

        self.configure(fg_color=_C["warm_white"])
        self._build_ui()
        self.after(120, self._render)   # wait for canvas to report its size

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ── Left sidebar ─────────────────────────────────────────────────────
        sidebar = ctk.CTkFrame(self, width=210, corner_radius=0,
                               fg_color=_C["cream"])
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            sidebar, text="Draw crops manually",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=_C["deep_green"],
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(14, 4))

        ctk.CTkLabel(
            sidebar,
            text=(
                "Drag  →  new box\n"
                "Click box  →  select\n"
                "Del  →  remove selected\n\n"
                "Scroll  →  zoom\n"
                "Right-drag  →  pan\n"
                "Dbl-click  →  reset view"
            ),
            font=ctk.CTkFont(size=11),
            text_color=_C["grey"],
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

        ctk.CTkFrame(sidebar, height=1, fg_color=_C["rule"]).grid(
            row=2, column=0, sticky="ew", padx=8, pady=4)

        # Box count label
        self._count_lbl = ctk.CTkLabel(
            sidebar, text="0 boxes drawn",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=_C["deep_green"],
        )
        self._count_lbl.grid(row=3, column=0, sticky="w", padx=12, pady=(6, 4))

        # Remove selected
        self._remove_btn = ctk.CTkButton(
            sidebar, text="Remove selected",
            fg_color=_C["grey"], hover_color=_C["deep_green"],
            font=ctk.CTkFont(size=11),
            state="disabled",
            command=self._remove_selected,
        )
        self._remove_btn.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 2))

        # Clear all
        self._clear_btn = ctk.CTkButton(
            sidebar, text="Clear all",
            fg_color=_C["err"], hover_color=_C["err_hover"],
            font=ctk.CTkFont(size=11),
            state="disabled",
            command=self._clear_all,
        )
        self._clear_btn.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 4))

        ctk.CTkFrame(sidebar, height=1, fg_color=_C["rule"]).grid(
            row=6, column=0, sticky="ew", padx=8, pady=6)

        # Padding control
        ctk.CTkLabel(
            sidebar, text="Padding (px)",
            font=ctk.CTkFont(size=11), text_color=_C["deep_green"],
        ).grid(row=7, column=0, sticky="w", padx=12, pady=(0, 2))

        self._pad_var = tk.IntVar(value=self._s.padding)
        ctk.CTkSlider(
            sidebar, from_=0, to=100, number_of_steps=100,
            variable=self._pad_var,
            button_color=_C["mid_green"], button_hover_color=_C["deep_green"],
            progress_color=_C["mid_green"],
        ).grid(row=8, column=0, sticky="ew", padx=12, pady=(0, 0))

        self._pad_lbl = ctk.CTkLabel(
            sidebar, text=f"{self._s.padding} px",
            font=ctk.CTkFont(size=11), text_color=_C["grey"],
        )
        self._pad_lbl.grid(row=9, column=0, sticky="e", padx=12, pady=(0, 4))
        self._pad_var.trace_add(
            "write", lambda *_: self._pad_lbl.configure(
                text=f"{self._pad_var.get()} px"))

        ctk.CTkFrame(sidebar, height=1, fg_color=_C["rule"]).grid(
            row=10, column=0, sticky="ew", padx=8, pady=6)

        # Save crops
        self._save_btn = ctk.CTkButton(
            sidebar, text="Save crops",
            fg_color=_C["mid_green"], hover_color=_C["deep_green"],
            font=ctk.CTkFont(size=12, weight="bold"),
            state="disabled",
            command=self._save_crops,
        )
        self._save_btn.grid(row=11, column=0, sticky="ew", padx=12, pady=(0, 4))

        ctk.CTkButton(
            sidebar, text="Cancel",
            fg_color="transparent", hover_color=_C["soft_green"],
            text_color=_C["grey"], border_color=_C["rule"], border_width=1,
            font=ctk.CTkFont(size=11),
            command=self.destroy,
        ).grid(row=12, column=0, sticky="ew", padx=12, pady=(0, 14))

        # ── Canvas area ───────────────────────────────────────────────────────
        canvas_fr = ctk.CTkFrame(self, corner_radius=0,
                                 fg_color=_C["deep_green"])
        canvas_fr.grid(row=0, column=1, sticky="nsew")
        canvas_fr.grid_columnconfigure(0, weight=1)
        canvas_fr.grid_rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            canvas_fr, bg=_C["deep_green"],
            highlightthickness=0, cursor="crosshair",
        )
        self._canvas.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)

        # Bind events
        self._canvas.bind("<Configure>",       lambda _: self._render())
        self._canvas.bind("<MouseWheel>",       self._on_scroll)
        self._canvas.bind("<ButtonPress-1>",    self._on_draw_start)
        self._canvas.bind("<B1-Motion>",        self._on_draw_drag)
        self._canvas.bind("<ButtonRelease-1>",  self._on_draw_end)
        self._canvas.bind("<ButtonPress-3>",    self._on_pan_start)
        self._canvas.bind("<B3-Motion>",        self._on_pan_drag)
        self._canvas.bind("<ButtonRelease-3>",  self._on_pan_end)
        self._canvas.bind("<Double-Button-1>",  self._on_reset)
        self.bind("<Delete>",    lambda _: self._remove_selected())
        self.bind("<BackSpace>", lambda _: self._remove_selected())
        self.focus_set()

    # ── Coordinate helpers ────────────────────────────────────────────────────

    def _canvas_origin(self) -> tuple[int, int]:
        """Return (cx, cy): the canvas pixel where the image centre is drawn."""
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        return cw // 2 + self._pan_x, ch // 2 + self._pan_y

    def _display_scale(self) -> float:
        cw = max(self._canvas.winfo_width(), 1)
        ch = max(self._canvas.winfo_height(), 1)
        ih, iw = self._bgr_display.shape[:2]
        base = min(cw / iw, ch / ih)
        return base * self._zoom

    def _canvas_to_img(self, cx: int, cy: int) -> tuple[int, int]:
        """Convert canvas pixel → image pixel coordinates."""
        ox, oy = self._canvas_origin()
        scale = self._display_scale()
        iw = self._bgr_display.shape[1]
        ih = self._bgr_display.shape[0]
        ix = (cx - ox) / scale + iw / 2
        iy = (cy - oy) / scale + ih / 2
        return int(ix), int(iy)

    def _img_to_canvas(self, ix: int, iy: int) -> tuple[int, int]:
        """Convert image pixel → canvas pixel coordinates."""
        ox, oy = self._canvas_origin()
        scale = self._display_scale()
        iw = self._bgr_display.shape[1]
        ih = self._bgr_display.shape[0]
        cx = (ix - iw / 2) * scale + ox
        cy = (iy - ih / 2) * scale + oy
        return int(cx), int(cy)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render(self):
        """Redraw: background image then all boxes."""
        ih, iw = self._bgr_display.shape[:2]
        scale = self._display_scale()
        new_w = max(1, int(iw * scale))
        new_h = max(1, int(ih * scale))

        rgb = cv2.cvtColor(self._bgr_display, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb).resize((new_w, new_h), Image.LANCZOS)
        photo = ImageTk.PhotoImage(pil)

        self._canvas.delete("all")
        self._tk_photo = photo

        ox, oy = self._canvas_origin()
        self._canvas.create_image(ox, oy, anchor="center", image=photo)

        # Draw each box
        for i, (x1, y1, x2, y2) in enumerate(self._boxes):
            bx1, by1 = self._img_to_canvas(x1, y1)
            bx2, by2 = self._img_to_canvas(x2, y2)
            selected = (i == self._selected)
            colour = _C["mid_green"] if selected else _C["warm_white"]
            width  = 3 if selected else 2
            self._canvas.create_rectangle(
                bx1, by1, bx2, by2,
                outline=colour, width=width,
            )
            # Box number label (top-left corner of the rect)
            lx = min(bx1, bx2) + 4
            ly = min(by1, by2) + 2
            self._canvas.create_text(
                lx + 1, ly + 1, text=str(i + 1),
                anchor="nw", fill="#000000",
                font=("Arial", 10, "bold"),
            )
            self._canvas.create_text(
                lx, ly, text=str(i + 1),
                anchor="nw", fill=colour,
                font=("Arial", 10, "bold"),
            )

    # ── Zoom / pan ────────────────────────────────────────────────────────────

    def _on_scroll(self, event):
        factor = 1.15 if event.delta > 0 else (1 / 1.15)
        old_zoom = self._zoom
        self._zoom = max(0.25, min(32.0, self._zoom * factor))

        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        mx = event.x - cw // 2
        my = event.y - ch // 2
        z_ratio = self._zoom / old_zoom
        self._pan_x = int(mx + (self._pan_x - mx) * z_ratio)
        self._pan_y = int(my + (self._pan_y - my) * z_ratio)
        self._render()

    def _on_pan_start(self, event):
        self._pan_start = (event.x, event.y)

    def _on_pan_drag(self, event):
        if self._pan_start is None:
            return
        self._pan_x += event.x - self._pan_start[0]
        self._pan_y += event.y - self._pan_start[1]
        self._pan_start = (event.x, event.y)
        self._render()

    def _on_pan_end(self, _event):
        self._pan_start = None

    def _on_reset(self, _event):
        self._zoom = 1.0
        self._pan_x = 0
        self._pan_y = 0
        self._render()

    # ── Box drawing ───────────────────────────────────────────────────────────

    def _on_draw_start(self, event):
        # Check whether the click is inside an existing box — select it if so
        for i, (x1, y1, x2, y2) in enumerate(self._boxes):
            bx1, by1 = self._img_to_canvas(x1, y1)
            bx2, by2 = self._img_to_canvas(x2, y2)
            if (min(bx1, bx2) <= event.x <= max(bx1, bx2) and
                    min(by1, by2) <= event.y <= max(by1, by2)):
                self._selected = i
                self._update_sidebar()
                self._render()
                return
        # No existing box hit — start a new one
        self._selected = None
        self._draw_start = (event.x, event.y)

    def _on_draw_drag(self, event):
        if self._draw_start is None:
            return
        if self._rubber_id:
            self._canvas.delete(self._rubber_id)
        x0, y0 = self._draw_start
        self._rubber_id = self._canvas.create_rectangle(
            x0, y0, event.x, event.y,
            outline=_C["mid_green"], width=2, dash=(6, 3),
        )

    def _on_draw_end(self, event):
        if self._draw_start is None:
            return
        if self._rubber_id:
            self._canvas.delete(self._rubber_id)
            self._rubber_id = None

        x0, y0 = self._draw_start
        self._draw_start = None

        # Ignore accidental micro-drags
        if abs(event.x - x0) < 5 or abs(event.y - y0) < 5:
            self._render()
            return

        # Convert to image coordinates and clamp to image bounds
        ix1, iy1 = self._canvas_to_img(x0, y0)
        ix2, iy2 = self._canvas_to_img(event.x, event.y)
        ih, iw = self._bgr_display.shape[:2]
        ix1 = max(0, min(iw - 1, ix1))
        ix2 = max(0, min(iw - 1, ix2))
        iy1 = max(0, min(ih - 1, iy1))
        iy2 = max(0, min(ih - 1, iy2))

        box = (min(ix1, ix2), min(iy1, iy2), max(ix1, ix2), max(iy1, iy2))
        self._boxes.append(box)
        self._selected = len(self._boxes) - 1
        self._update_sidebar()
        self._render()

    # ── Box management ────────────────────────────────────────────────────────

    def _remove_selected(self):
        if self._selected is not None and 0 <= self._selected < len(self._boxes):
            self._boxes.pop(self._selected)
            self._selected = None
            self._update_sidebar()
            self._render()

    def _clear_all(self):
        self._boxes.clear()
        self._selected = None
        self._update_sidebar()
        self._render()

    def _update_sidebar(self):
        n = len(self._boxes)
        noun = "box" if n == 1 else "boxes"
        self._count_lbl.configure(text=f"{n} {noun} drawn")
        has_boxes = "normal" if n > 0 else "disabled"
        self._clear_btn.configure(state=has_boxes)
        self._save_btn.configure(state=has_boxes)
        self._remove_btn.configure(
            state="normal" if self._selected is not None else "disabled")

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save_crops(self):
        if not self._boxes:
            return
        stem   = self._image_path.stem
        suffix = self._image_path.suffix.lower()
        if suffix not in IMAGE_EXTS:
            suffix = ".jpg"
        pad = self._pad_var.get()
        ih, iw = self._bgr_display.shape[:2]
        saved = []

        for i, (x1, y1, x2, y2) in enumerate(self._boxes, 1):
            w = x2 - x1
            h = y2 - y1
            crop, _ = _padded_crop(self._bgr_save, x1, y1, w, h, pad)
            if crop is None or crop.size == 0:
                continue
            out_path = self._output_dir / f"{stem}_manual_{i:02d}{suffix}"
            if cv2.imwrite(str(out_path), crop):
                saved.append(out_path)

        if saved:
            messagebox.showinfo(
                "Saved",
                f"Saved {len(saved)} crop(s) to:\n{self._output_dir}",
                parent=self,
            )
            if self._on_save:
                self._on_save()
            self.destroy()
        else:
            messagebox.showerror(
                "Error", "Could not save any crops.", parent=self)


# ─── VoucherVision Go submission dialog ───────────────────────────────────────

class VVGoDialog(ctk.CTkToplevel):
    """
    Submit all crop images in the output folder to the VoucherVision Go API.
    Default LLM model, label collage skipped, WFO validation skipped.
    JSON results are saved alongside the crops (or in a user-chosen sub-folder).
    """

    def __init__(self, master, output_dir: Path):
        super().__init__(master)
        self.title("Submit to VoucherVision Go")
        self.geometry("560x530")
        self.resizable(False, False)
        self.grab_set()

        self._output_dir = output_dir
        self._cancel_event = threading.Event()

        self.configure(fg_color=_C["warm_white"])
        self._build_ui()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="VoucherVision Go",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=_C["deep_green"],
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 2))

        ctk.CTkLabel(
            self,
            text="Submit segmented packets for automated text extraction.\n"
                 "Results are saved as JSON files for import into VVGo Editor.",
            font=ctk.CTkFont(size=11), text_color=_C["grey"], justify="left",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 10))

        ctk.CTkFrame(self, height=1, fg_color=_C["rule"]).grid(
            row=2, column=0, sticky="ew", padx=14)

        # API token row
        ctk.CTkLabel(self, text="API Token",
                     text_color=_C["deep_green"]).grid(
            row=3, column=0, sticky="w", padx=20, pady=(12, 2))

        tok_fr = ctk.CTkFrame(self, fg_color="transparent")
        tok_fr.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 8))
        tok_fr.grid_columnconfigure(0, weight=1)

        self._token_var = tk.StringVar()
        self._token_entry = ctk.CTkEntry(
            tok_fr, textvariable=self._token_var, show="•",
            fg_color=_C["warm_white"], border_color=_C["rule"],
            text_color=_C["deep_green"],
            placeholder_text="Paste your VVGo auth token here…",
        )
        self._token_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self._show_tok = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tok_fr, text="Show", variable=self._show_tok,
            fg_color=_C["mid_green"], hover_color=_C["soft_green"],
            checkmark_color="white", text_color=_C["grey"], width=16,
            command=lambda: self._token_entry.configure(
                show="" if self._show_tok.get() else "•"),
        ).grid(row=0, column=1)

        # JSON output folder
        ctk.CTkLabel(self, text="JSON output folder",
                     text_color=_C["deep_green"]).grid(
            row=5, column=0, sticky="w", padx=20, pady=(4, 2))

        json_fr = ctk.CTkFrame(self, fg_color="transparent")
        json_fr.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 10))
        json_fr.grid_columnconfigure(0, weight=1)

        self._json_dir_var = tk.StringVar(
            value=str(self._output_dir / "vvgo_json"))
        ctk.CTkEntry(
            json_fr, textvariable=self._json_dir_var,
            fg_color=_C["warm_white"], border_color=_C["rule"],
            text_color=_C["deep_green"],
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            json_fr, text="Browse", width=70,
            fg_color=_C["cream"], hover_color=_C["soft_green"],
            text_color=_C["deep_green"], border_color=_C["rule"], border_width=1,
            command=self._browse_json,
        ).grid(row=0, column=1)

        ctk.CTkFrame(self, height=1, fg_color=_C["rule"]).grid(
            row=7, column=0, sticky="ew", padx=14, pady=6)

        # Parallel workers
        wrk_fr = ctk.CTkFrame(self, fg_color="transparent")
        wrk_fr.grid(row=8, column=0, sticky="ew", padx=20, pady=(0, 6))
        wrk_fr.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(wrk_fr, text="Parallel workers",
                     text_color=_C["deep_green"]).grid(
            row=0, column=0, sticky="w")
        self._workers_var = tk.IntVar(value=4)
        self._workers_lbl = ctk.CTkLabel(wrk_fr, text="4", width=26,
                                          text_color=_C["grey"])
        self._workers_lbl.grid(row=0, column=2)
        ctk.CTkSlider(
            wrk_fr, from_=1, to=16, number_of_steps=15,
            variable=self._workers_var,
            button_color=_C["mid_green"], button_hover_color=_C["deep_green"],
            progress_color=_C["mid_green"],
            command=lambda v: self._workers_lbl.configure(text=str(int(float(v)))),
        ).grid(row=0, column=1, sticky="ew", padx=8)

        ctk.CTkFrame(self, height=1, fg_color=_C["rule"]).grid(
            row=9, column=0, sticky="ew", padx=14, pady=4)

        # Progress
        self._prog_bar = ctk.CTkProgressBar(
            self, fg_color=_C["soft_green"], progress_color=_C["mid_green"])
        self._prog_bar.set(0)
        self._prog_bar.grid(row=10, column=0, sticky="ew", padx=20, pady=(4, 0))

        self._prog_lbl = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=11), text_color=_C["grey"])
        self._prog_lbl.grid(row=11, column=0, sticky="w", padx=20, pady=(2, 0))

        self._log_box = ctk.CTkTextbox(
            self, height=80,
            font=ctk.CTkFont(size=10, family="Courier New"),
            fg_color=_C["warm_white"], text_color=_C["deep_green"],
            border_color=_C["rule"], border_width=1,
        )
        self._log_box.grid(row=12, column=0, sticky="ew", padx=16, pady=6)
        self._log_box.configure(state="disabled")

        # Action buttons
        btn_fr = ctk.CTkFrame(self, fg_color="transparent")
        btn_fr.grid(row=13, column=0, sticky="ew", padx=16, pady=(0, 16))
        btn_fr.grid_columnconfigure(0, weight=1)
        btn_fr.grid_columnconfigure(1, weight=1)

        self._submit_btn = ctk.CTkButton(
            btn_fr, text="Submit all crops",
            fg_color=_C["mid_green"], hover_color=_C["deep_green"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._submit,
        )
        self._submit_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        ctk.CTkButton(
            btn_fr, text="Close",
            fg_color="transparent", hover_color=_C["soft_green"],
            text_color=_C["grey"], border_color=_C["rule"], border_width=1,
            command=self.destroy,
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _browse_json(self):
        d = filedialog.askdirectory(
            title="Select folder for JSON outputs", parent=self)
        if d:
            self._json_dir_var.set(d)

    def _vlog(self, msg: str):
        self._log_box.configure(state="normal")
        self._log_box.insert("end", msg + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    # ── Submission ────────────────────────────────────────────────────────────

    def _submit(self):
        token = self._token_var.get().strip()
        if not token:
            messagebox.showerror(
                "Token required",
                "Please enter your VoucherVision Go API token.\n\n"
                "Tokens are available at:\n"
                "https://vouchervision-go-738307415303.us-central1.run.app/login",
                parent=self,
            )
            return

        json_dir = Path(self._json_dir_var.get().strip())
        images = sorted(
            p for p in self._output_dir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )
        if not images:
            messagebox.showinfo(
                "No images", "No image files found in the output folder.",
                parent=self)
            return

        json_dir.mkdir(parents=True, exist_ok=True)
        max_workers = self._workers_var.get()
        total = len(images)
        self._submit_btn.configure(state="disabled", text="Submitting…")
        self._cancel_event.clear()
        self._prog_bar.set(0)
        self._vlog(f"Submitting {total} image(s)…")

        def _process_one(img_path: Path) -> dict:
            if self._cancel_event.is_set():
                return {"name": img_path.name, "ok": False, "error": "cancelled"}
            try:
                with img_path.open("rb") as fh:
                    resp = requests.post(
                        f"{VVGO_SERVER_URL}process",
                        headers={"Authorization": f"Bearer {token}"},
                        files={"file": (img_path.name, fh, "image/jpeg")},
                        data={
                            "prompt": VVGO_DEFAULT_PROMPT,
                            "skip_label_collage": "true",
                            # include_wfo omitted → server default = false
                        },
                        timeout=180,
                    )
                resp.raise_for_status()
                out_path = json_dir / f"{img_path.stem}.json"
                out_path.write_text(
                    json.dumps(resp.json(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                return {"name": img_path.name, "ok": True, "error": ""}
            except Exception as exc:
                return {"name": img_path.name, "ok": False, "error": str(exc)}

        done_count = 0
        error_count = 0

        def worker():
            nonlocal done_count, error_count
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max_workers) as ex:
                futures = {ex.submit(_process_one, p): p for p in images}
                for fut in concurrent.futures.as_completed(futures):
                    res = fut.result()
                    done_count += 1
                    if res["ok"]:
                        msg = f"  ✓  {res['name']}"
                    else:
                        error_count += 1
                        msg = f"  ✗  {res['name']}  — {res['error']}"
                    prog = done_count / total

                    def _upd(m=msg, p=prog, d=done_count):
                        self._vlog(m)
                        self._prog_bar.set(p)
                        self._prog_lbl.configure(text=f"{d} / {total}")
                    self.after(0, _upd)

            def _done():
                ok = done_count - error_count
                self._vlog(f"\nDone.  {ok} / {total} succeeded.")
                self._submit_btn.configure(state="normal", text="Submit all crops")
                if error_count == 0:
                    self._prog_lbl.configure(text=f"✓  All {total} succeeded")
            self.after(0, _done)

        threading.Thread(target=worker, daemon=True).start()


# ─── Crop QC gallery ──────────────────────────────────────────────────────────

class CropGallery(ctk.CTkToplevel):
    """
    Scrollable thumbnail gallery of all segmented crops for final visual QC.
    Click a thumbnail to enlarge it in the right panel.
    Flag individual crops for recapture, or re-open the manual crop editor.
    """

    THUMB_W = 150
    THUMB_H = 120
    COLS    = 4

    def __init__(self, master, output_dir: Path,
                 all_results: list, s: SegSettings):
        super().__init__(master)
        self.title("Crop QC Gallery")
        self.geometry("1100x680")
        self.minsize(800, 500)

        self._output_dir  = output_dir
        self._all_results = all_results
        self._s           = s
        self._app         = master          # used to call _open_manual_editor

        # Map crop file → source ImageResult (for Redraw button)
        self._crop_to_result: dict[Path, "ImageResult"] = {}
        for r in all_results:
            for p, _w, _h in r.crop_info:
                self._crop_to_result[p] = r

        # Collect all crop images in the output folder
        self._all_crops: list[Path] = sorted(
            p for p in output_dir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )

        self._flagged:    set[Path] = set()
        self._selected:   Path | None = None
        self._thumb_refs: dict[Path, ImageTk.PhotoImage] = {}  # prevent GC

        self.configure(fg_color=_C["warm_white"])
        self._build_ui()
        # Load thumbs after window is shown so dimensions are valid
        self.after(150, self._populate_grid)

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_rowconfigure(0, weight=1)

        # ── Left: scrollable thumbnail grid ──────────────────────────────────
        left = ctk.CTkFrame(self, corner_radius=0, fg_color=_C["cream"])
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(left, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        hdr.grid_columnconfigure(0, weight=1)

        self._gallery_lbl = ctk.CTkLabel(
            hdr, text="Loading thumbnails…",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=_C["deep_green"],
        )
        self._gallery_lbl.grid(row=0, column=0, sticky="w")

        # Filter chips
        filt_fr = ctk.CTkFrame(hdr, fg_color="transparent")
        filt_fr.grid(row=0, column=1, sticky="e")
        for label, val in [("All", "all"), ("OK", "ok"), ("Flagged", "flagged")]:
            ctk.CTkButton(
                filt_fr, text=label, width=58,
                fg_color=_C["cream"], hover_color=_C["soft_green"],
                text_color=_C["deep_green"], border_color=_C["rule"],
                border_width=1, font=ctk.CTkFont(size=11),
                command=lambda v=val: self._apply_filter(v),
            ).pack(side="left", padx=2)

        self._scroll = ctk.CTkScrollableFrame(
            left, fg_color=_C["warm_white"],
            scrollbar_button_color=_C["rule"],
            scrollbar_button_hover_color=_C["grey"],
        )
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        for c in range(self.COLS):
            self._scroll.grid_columnconfigure(c, weight=1)

        # ── Right: detail panel ───────────────────────────────────────────────
        right = ctk.CTkFrame(self, width=290, corner_radius=0,
                              fg_color=_C["cream"])
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_propagate(False)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            right, text="Selected crop",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=_C["deep_green"],
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 2))

        self._sel_lbl = ctk.CTkLabel(
            right, text="Click a thumbnail",
            font=ctk.CTkFont(size=10), text_color=_C["grey"], wraplength=260,
        )
        self._sel_lbl.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 4))

        self._prev_canvas = tk.Canvas(
            right, bg=_C["deep_green"], highlightthickness=0,
            width=268, height=230,
        )
        self._prev_canvas.grid(row=2, column=0, sticky="nsew", padx=10, pady=4)
        self._prev_photo = None

        ctk.CTkFrame(right, height=1, fg_color=_C["rule"]).grid(
            row=3, column=0, sticky="ew", padx=8, pady=6)

        self._flag_btn = ctk.CTkButton(
            right, text="🚩  Flag for recapture",
            fg_color=_C["err"], hover_color=_C["err_hover"],
            font=ctk.CTkFont(size=11), state="disabled",
            command=self._toggle_flag,
        )
        self._flag_btn.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 4))

        self._redraw_btn = ctk.CTkButton(
            right, text="✏  Redraw boundary",
            fg_color=_C["deep_green"], hover_color=_C["mid_green"],
            font=ctk.CTkFont(size=11), state="disabled",
            command=self._redraw_selected,
        )
        self._redraw_btn.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 4))

        ctk.CTkFrame(right, height=1, fg_color=_C["rule"]).grid(
            row=6, column=0, sticky="ew", padx=8, pady=6)

        self._export_btn = ctk.CTkButton(
            right, text="Export flagged list",
            fg_color="transparent", hover_color=_C["soft_green"],
            text_color=_C["grey"], border_color=_C["rule"], border_width=1,
            font=ctk.CTkFont(size=11), state="disabled",
            command=self._export_flagged,
        )
        self._export_btn.grid(row=7, column=0, sticky="ew", padx=12, pady=(0, 4))

        ctk.CTkButton(
            right, text="Close",
            fg_color="transparent", hover_color=_C["soft_green"],
            text_color=_C["grey"], border_color=_C["rule"], border_width=1,
            font=ctk.CTkFont(size=11), command=self.destroy,
        ).grid(row=8, column=0, sticky="ew", padx=12, pady=(0, 16))

    # ── Grid population ───────────────────────────────────────────────────────

    def _populate_grid(self, crops: "list[Path] | None" = None):
        for w in self._scroll.winfo_children():
            w.destroy()
        if crops is None:
            crops = self._all_crops
        self._gallery_lbl.configure(
            text=f"All crops — {len(self._all_crops)} images")
        for i, p in enumerate(crops):
            self._make_card(p, i // self.COLS, i % self.COLS)

    def _make_thumb(self, path: Path) -> "ImageTk.PhotoImage | None":
        if path in self._thumb_refs:
            return self._thumb_refs[path]
        try:
            raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if raw is None:
                return None
            bgr = _to_bgr8(raw)
            h, w = bgr.shape[:2]
            scale = min(self.THUMB_W / w, self.THUMB_H / h)
            nw = max(1, int(w * scale))
            nh = max(1, int(h * scale))
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(rgb).resize((nw, nh), Image.LANCZOS)
            photo = ImageTk.PhotoImage(pil)
            self._thumb_refs[path] = photo
            return photo
        except Exception:
            return None

    def _make_card(self, path: Path, row: int, col: int):
        is_flagged = path in self._flagged
        border_col = _C["err"] if is_flagged else _C["rule"]

        card = ctk.CTkFrame(
            self._scroll, fg_color=_C["cream"],
            border_color=border_col, border_width=1,
        )
        card.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(
            card, bg=_C["deep_green"],
            width=self.THUMB_W, height=self.THUMB_H,
            highlightthickness=0,
        )
        canvas.grid(row=0, column=0, padx=4, pady=(4, 2))

        photo = self._make_thumb(path)
        if photo:
            canvas.create_image(
                self.THUMB_W // 2, self.THUMB_H // 2,
                anchor="center", image=photo,
            )

        name = path.name
        if len(name) > 24:
            name = name[:11] + "…" + name[-11:]
        ctk.CTkLabel(
            card, text=name, font=ctk.CTkFont(size=9), text_color=_C["grey"],
        ).grid(row=1, column=0, padx=4, pady=(0, 4))

        for w in (canvas, card):
            w.bind("<Button-1>", lambda _e, p=path: self._select(p))

    # ── Selection & preview ───────────────────────────────────────────────────

    def _select(self, path: Path):
        self._selected = path
        self._sel_lbl.configure(text=path.name)
        self._flag_btn.configure(state="normal")
        self._redraw_btn.configure(state="normal")

        if path in self._flagged:
            self._flag_btn.configure(
                text="🚩  Unflag", fg_color=_C["grey"],
                hover_color=_C["deep_green"])
        else:
            self._flag_btn.configure(
                text="🚩  Flag for recapture", fg_color=_C["err"],
                hover_color=_C["err_hover"])

        # Enlarged preview
        try:
            raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if raw is not None:
                bgr = _to_bgr8(raw)
                cw = self._prev_canvas.winfo_width() or 268
                ch = self._prev_canvas.winfo_height() or 230
                h, w = bgr.shape[:2]
                scale = min(cw / w, ch / h)
                nw = max(1, int(w * scale))
                nh = max(1, int(h * scale))
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                pil = Image.fromarray(rgb).resize((nw, nh), Image.LANCZOS)
                photo = ImageTk.PhotoImage(pil)
                self._prev_canvas.delete("all")
                self._prev_photo = photo
                self._prev_canvas.create_image(
                    cw // 2, ch // 2, anchor="center", image=photo)
        except Exception:
            pass

    # ── Actions ───────────────────────────────────────────────────────────────

    def _toggle_flag(self):
        if self._selected is None:
            return
        if self._selected in self._flagged:
            self._flagged.discard(self._selected)
            self._flag_btn.configure(
                text="🚩  Flag for recapture", fg_color=_C["err"],
                hover_color=_C["err_hover"])
        else:
            self._flagged.add(self._selected)
            self._flag_btn.configure(
                text="🚩  Unflag", fg_color=_C["grey"],
                hover_color=_C["deep_green"])
        # Refresh card border colour
        self._apply_filter("all")
        self._export_btn.configure(
            state="normal" if self._flagged else "disabled")

    def _redraw_selected(self):
        if self._selected is None:
            return
        result = self._crop_to_result.get(self._selected)
        if result is None:
            messagebox.showinfo(
                "Source not found",
                "Cannot find the source image for this crop.\n"
                "It may have been produced by a Fix or manual-edit step.",
                parent=self,
            )
            return
        if hasattr(self._app, "_open_manual_editor"):
            self._app._open_manual_editor(result.path, result=result)

    def _apply_filter(self, mode: str):
        for w in self._scroll.winfo_children():
            w.destroy()
        if mode == "all":
            crops = self._all_crops
        elif mode == "flagged":
            crops = [p for p in self._all_crops if p in self._flagged]
        else:
            crops = [p for p in self._all_crops if p not in self._flagged]
        label = {"all": "All", "flagged": "Flagged", "ok": "OK"}[mode]
        self._gallery_lbl.configure(
            text=f"{label} crops — {len(crops)} of {len(self._all_crops)}")
        for i, p in enumerate(crops):
            self._make_card(p, i // self.COLS, i % self.COLS)

    def _export_flagged(self):
        if not self._flagged:
            return
        out = filedialog.asksaveasfilename(
            title="Save flagged crop list",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            parent=self,
        )
        if out:
            Path(out).write_text(
                "\n".join(str(p) for p in sorted(self._flagged)),
                encoding="utf-8",
            )
            messagebox.showinfo("Saved", f"Flagged list saved:\n{out}", parent=self)


# ─── GUI ─────────────────────────────────────────────────────────────────────

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("green")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1040x700")
        self.minsize(820, 560)

        self._cancel_event = threading.Event()
        self._last_debug: np.ndarray | None = None
        self._tk_photo = None   # keep Tk photo reference alive

        # Zoom / pan state for the preview canvas
        self._zoom: float = 1.0
        self._pan_x: int = 0
        self._pan_y: int = 0
        self._drag_start: tuple | None = None

        # Context kept from the most recent batch (used by Fix / Gallery / VVGo)
        self._last_out_dir:    Path | None = None
        self._last_settings:   SegSettings | None = None
        self._last_all_results: list = []

        self.configure(fg_color=_C["warm_white"])
        self._build_ui()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self, width=310, corner_radius=0,
                             fg_color=_C["cream"])
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.grid_columnconfigure(0, weight=1)
        # Row 8 = review panel; let it absorb spare vertical space
        left.grid_rowconfigure(8, weight=1)

        right = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(2, 0))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=3)
        right.grid_rowconfigure(1, weight=1)

        self._build_left(left)
        self._build_right(right)

    def _build_left(self, p):
        # Title
        ctk.CTkLabel(
            p, text="Herbarium Packet Segmenter",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=_C["deep_green"],
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 6))

        # Input / output folder rows
        self._input_var = tk.StringVar()
        self._output_var = tk.StringVar()
        for grid_row, label, var, cmd in (
            (1, "Input folder",  self._input_var,  self._browse_input),
            (3, "Output folder", self._output_var, self._browse_output),
        ):
            ctk.CTkLabel(p, text=label, font=ctk.CTkFont(size=12),
                         text_color=_C["deep_green"]).grid(
                row=grid_row, column=0, sticky="w", padx=14, pady=(4, 0))
            fr = ctk.CTkFrame(p, fg_color="transparent")
            fr.grid(row=grid_row + 1, column=0, sticky="ew", padx=12, pady=(0, 2))
            fr.grid_columnconfigure(0, weight=1)
            ctk.CTkEntry(
                fr, textvariable=var, placeholder_text="Browse…",
                fg_color=_C["warm_white"], border_color=_C["rule"],
                text_color=_C["deep_green"],
                placeholder_text_color=_C["grey"],
            ).grid(row=0, column=0, sticky="ew")
            ctk.CTkButton(
                fr, text="Browse", width=70, command=cmd,
                fg_color=_C["mid_green"], hover_color=_C["deep_green"],
            ).grid(row=0, column=1, padx=(4, 0))

        # Settings
        self._build_settings(p, row=5)

        # Action buttons
        btn_fr = ctk.CTkFrame(p, fg_color="transparent")
        btn_fr.grid(row=6, column=0, sticky="ew", padx=14, pady=(4, 2))
        btn_fr.grid_columnconfigure(0, weight=1)
        btn_fr.grid_columnconfigure(1, weight=1)
        self._preview_btn = ctk.CTkButton(
            btn_fr, text="Preview one", command=self._run_preview,
            fg_color=_C["soft_green"], hover_color=_C["rule"],
            text_color=_C["deep_green"], border_width=1,
            border_color=_C["rule"])
        self._preview_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._run_btn = ctk.CTkButton(
            btn_fr, text="▶  Run Batch", command=self._run_batch,
            fg_color=_C["mid_green"], hover_color=_C["deep_green"])
        self._run_btn.grid(row=0, column=1, sticky="ew")

        # Progress row (hidden at start)
        prog_fr = ctk.CTkFrame(p, fg_color="transparent")
        prog_fr.grid(row=7, column=0, sticky="ew", padx=14, pady=(2, 0))
        prog_fr.grid_columnconfigure(0, weight=1)
        self._prog_bar = ctk.CTkProgressBar(
            prog_fr, fg_color=_C["soft_green"],
            progress_color=_C["mid_green"])
        self._prog_bar.set(0)
        self._prog_bar.grid(row=0, column=0, sticky="ew")
        self._cancel_btn = ctk.CTkButton(
            prog_fr, text="Cancel", width=70,
            fg_color=_C["err"], hover_color=_C["err_hover"],
            command=self._cancel_batch)
        self._cancel_btn.grid(row=0, column=1, padx=(6, 0))
        self._prog_label = ctk.CTkLabel(
            prog_fr, text="", font=ctk.CTkFont(size=11),
            text_color=_C["grey"])
        self._prog_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._prog_fr = prog_fr
        prog_fr.grid_remove()

        # Review panel (hidden until batch produces flagged images)
        rev_fr = ctk.CTkFrame(p, fg_color=_C["soft_green"],
                               border_color=_C["rule"], border_width=1)
        rev_fr.grid(row=8, column=0, sticky="nsew", padx=14, pady=(6, 4))
        rev_fr.grid_columnconfigure(0, weight=1)
        rev_fr.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            rev_fr, text="Needs review",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=_C["deep_green"],
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(6, 2))
        self._review_list = ctk.CTkScrollableFrame(
            rev_fr, height=120, fg_color=_C["soft_green"],
            scrollbar_button_color=_C["rule"],
            scrollbar_button_hover_color=_C["grey"])
        self._review_list.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 6))
        self._review_list.grid_columnconfigure(0, weight=1)
        self._review_fr = rev_fr
        rev_fr.grid_remove()

        # Post-batch action buttons (hidden / disabled until batch completes)
        post_fr = ctk.CTkFrame(p, fg_color="transparent")
        post_fr.grid(row=9, column=0, sticky="ew", padx=14, pady=(2, 0))
        post_fr.grid_columnconfigure(0, weight=1)
        post_fr.grid_columnconfigure(1, weight=1)

        self._gallery_btn = ctk.CTkButton(
            post_fr, text="🖼  QC Gallery",
            fg_color=_C["deep_green"], hover_color=_C["mid_green"],
            state="disabled", command=self._open_gallery)
        self._gallery_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3))

        self._vvgo_btn = ctk.CTkButton(
            post_fr, text="☁  Submit to VVGo",
            fg_color=_C["deep_green"], hover_color=_C["mid_green"],
            state="disabled", command=self._open_vvgo)
        self._vvgo_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0))

        self._open_btn = ctk.CTkButton(
            p, text="Open Output Folder",
            command=self._open_output, state="disabled",
            fg_color=_C["mid_green"], hover_color=_C["deep_green"])
        self._open_btn.grid(row=10, column=0, sticky="ew", padx=14, pady=(4, 14))

    def _build_settings(self, p, row):
        fr = ctk.CTkFrame(p, fg_color=_C["soft_green"],
                           border_color=_C["rule"], border_width=1)
        fr.grid(row=row, column=0, sticky="ew", padx=14, pady=8)
        fr.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            fr, text="Settings",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=_C["deep_green"],
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 4))

        # Top crop slider
        _tc_lbl = ctk.CTkLabel(fr, text="Top crop",
                               text_color=_C["deep_green"])
        _tc_lbl.grid(row=1, column=0, sticky="w", padx=10)
        _ToolTip(_tc_lbl,
                 "Remove this percentage from the top of every image before "
                 "detection. Use to exclude a fixed ruler, colour card, or "
                 "camera rig that appears at the top edge of every shot.")
        self._crop_var = tk.DoubleVar(value=0.0)
        self._crop_lbl = ctk.CTkLabel(fr, text=" 0%", width=34,
                                       text_color=_C["grey"])
        self._crop_lbl.grid(row=1, column=2, padx=(0, 8))
        _crop_slider = ctk.CTkSlider(
            fr, from_=0, to=30, number_of_steps=30,
            variable=self._crop_var,
            button_color=_C["mid_green"], button_hover_color=_C["deep_green"],
            progress_color=_C["mid_green"], fg_color=_C["rule"],
            command=lambda v: self._crop_lbl.configure(text=f"{int(float(v))}%"),
        )
        _crop_slider.grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        _ToolTip(_crop_slider,
                 "Remove this percentage from the top of every image before "
                 "detection. Use to exclude a fixed ruler, colour card, or "
                 "camera rig that appears at the top edge of every shot.")

        # Foreground mode
        _fg_lbl = ctk.CTkLabel(fr, text="Foreground",
                               text_color=_C["deep_green"])
        _fg_lbl.grid(row=2, column=0, sticky="w", padx=10)
        _ToolTip(_fg_lbl,
                 "Light: packets are lighter than the background (most common "
                 "with a dark mat or table).\nDark: packets are darker than "
                 "the background.\nAuto: tries both and keeps whichever finds "
                 "more packets — slower but useful when conditions vary.")
        self._fg_var = tk.StringVar(value="Light")
        _fg_menu = ctk.CTkOptionMenu(
            fr, values=["Light", "Dark", "Auto (slower)"],
            variable=self._fg_var,
            fg_color=_C["cream"], button_color=_C["mid_green"],
            button_hover_color=_C["deep_green"], text_color=_C["deep_green"],
            dropdown_fg_color=_C["warm_white"],
            dropdown_hover_color=_C["soft_green"],
            dropdown_text_color=_C["deep_green"],
        )
        _fg_menu.grid(row=2, column=1, columnspan=2, sticky="ew", padx=4, pady=4)
        _ToolTip(_fg_menu,
                 "Light: packets are lighter than the background (most common "
                 "with a dark mat or table).\nDark: packets are darker than "
                 "the background.\nAuto: tries both and keeps whichever finds "
                 "more packets — slower but useful when conditions vary.")

        # Advanced toggle
        self._adv_open = False
        self._adv_btn = ctk.CTkButton(
            fr, text="▸ Advanced settings",
            fg_color="transparent", text_color=_C["grey"],
            hover_color=_C["rule"], anchor="w",
            command=self._toggle_advanced)
        self._adv_btn.grid(
            row=3, column=0, columnspan=3, sticky="w", padx=4, pady=(2, 2))

        # Advanced panel (hidden by default)
        self._adv_fr = ctk.CTkFrame(fr, fg_color="transparent")
        self._adv_fr.grid(row=4, column=0, columnspan=3, sticky="ew")
        self._adv_fr.grid_columnconfigure(1, weight=1)
        self._adv_fr.grid_remove()
        self._build_advanced(self._adv_fr)

    def _build_advanced(self, p):
        _om_kw = dict(  # shared option-menu colours
            fg_color=_C["cream"], button_color=_C["mid_green"],
            button_hover_color=_C["deep_green"], text_color=_C["deep_green"],
            dropdown_fg_color=_C["warm_white"],
            dropdown_hover_color=_C["soft_green"],
            dropdown_text_color=_C["deep_green"],
        )
        _sl_kw = dict(  # shared slider colours
            button_color=_C["mid_green"], button_hover_color=_C["deep_green"],
            progress_color=_C["mid_green"], fg_color=_C["rule"],
        )

        _thresh_lbl = ctk.CTkLabel(p, text="Threshold", font=ctk.CTkFont(size=11),
                                   text_color=_C["deep_green"])
        _thresh_lbl.grid(row=0, column=0, sticky="w", padx=10)
        _ToolTip(_thresh_lbl,
                 "Algorithm used to separate packets from the background.\n"
                 "Otsu: automatic global threshold — best for consistent "
                 "lighting.\nAdaptive: local thresholding — handles uneven "
                 "illumination.\nCanny: edge-based — useful when tonal "
                 "contrast is low.")
        self._thresh_var = tk.StringVar(value="Otsu")
        _thresh_menu = ctk.CTkOptionMenu(
            p, values=["Otsu", "Adaptive", "Canny"],
            variable=self._thresh_var, **_om_kw,
        )
        _thresh_menu.grid(row=0, column=1, columnspan=2, sticky="ew", padx=4, pady=2)
        _ToolTip(_thresh_menu,
                 "Algorithm used to separate packets from the background.\n"
                 "Otsu: automatic global threshold — best for consistent "
                 "lighting.\nAdaptive: local thresholding — handles uneven "
                 "illumination.\nCanny: edge-based — useful when tonal "
                 "contrast is low.")

        _cont_lbl = ctk.CTkLabel(p, text="Contrast", font=ctk.CTkFont(size=11),
                                  text_color=_C["deep_green"])
        _cont_lbl.grid(row=1, column=0, sticky="w", padx=10)
        _ToolTip(_cont_lbl,
                 "Pre-processing applied before thresholding.\n"
                 "None: no adjustment.\nNormalize: stretches the histogram "
                 "to the full 0–255 range.\nCLAHE: adaptive histogram "
                 "equalisation — improves local contrast without "
                 "over-brightening.\nBoth: normalise then CLAHE.")
        self._contrast_var = tk.StringVar(value="None")
        _cont_menu = ctk.CTkOptionMenu(
            p, values=["None", "Normalize", "CLAHE", "Both"],
            variable=self._contrast_var, **_om_kw,
        )
        _cont_menu.grid(row=1, column=1, columnspan=2, sticky="ew", padx=4, pady=2)
        _ToolTip(_cont_menu,
                 "Pre-processing applied before thresholding.\n"
                 "None: no adjustment.\nNormalize: stretches the histogram "
                 "to the full 0–255 range.\nCLAHE: adaptive histogram "
                 "equalisation — improves local contrast without "
                 "over-brightening.\nBoth: normalise then CLAHE.")

        _pad_lbl_w = ctk.CTkLabel(p, text="Padding", font=ctk.CTkFont(size=11),
                                   text_color=_C["deep_green"])
        _pad_lbl_w.grid(row=2, column=0, sticky="w", padx=10)
        _ToolTip(_pad_lbl_w,
                 "Extra pixels added on each side of every detected bounding "
                 "box before the crop is saved. Increase if label text is "
                 "being clipped at the edges; decrease if crops include too "
                 "much background.")
        self._pad_var = tk.IntVar(value=30)
        self._pad_lbl = ctk.CTkLabel(p, text="30px", width=40,
                                      text_color=_C["grey"])
        self._pad_lbl.grid(row=2, column=2, padx=(0, 8))
        _pad_slider = ctk.CTkSlider(
            p, from_=0, to=80, number_of_steps=16,
            variable=self._pad_var, **_sl_kw,
            command=lambda v: self._pad_lbl.configure(text=f"{int(float(v))}px"),
        )
        _pad_slider.grid(row=2, column=1, sticky="ew", padx=4, pady=2)
        _ToolTip(_pad_slider,
                 "Extra pixels added on each side of every detected bounding "
                 "box before the crop is saved. Increase if label text is "
                 "being clipped at the edges; decrease if crops include too "
                 "much background.")

        _area_lbl_w = ctk.CTkLabel(p, text="Min area", font=ctk.CTkFont(size=11),
                                    text_color=_C["deep_green"])
        _area_lbl_w.grid(row=3, column=0, sticky="w", padx=10)
        _ToolTip(_area_lbl_w,
                 "Minimum contour area as a percentage of the total image "
                 "area. Detections smaller than this are discarded as noise. "
                 "Increase if small debris is being picked up as packets.")
        self._min_area_var = tk.DoubleVar(value=0.05)
        self._min_area_lbl = ctk.CTkLabel(p, text="0.05%", width=46,
                                           text_color=_C["grey"])
        self._min_area_lbl.grid(row=3, column=2, padx=(0, 8))
        _area_slider = ctk.CTkSlider(
            p, from_=0.01, to=2.0, number_of_steps=40,
            variable=self._min_area_var, **_sl_kw,
            command=lambda v: self._min_area_lbl.configure(text=f"{float(v):.2f}%"),
        )
        _area_slider.grid(row=3, column=1, sticky="ew", padx=4, pady=2)
        _ToolTip(_area_slider,
                 "Minimum contour area as a percentage of the total image "
                 "area. Detections smaller than this are discarded as noise. "
                 "Increase if small debris is being picked up as packets.")

        self._deskew_var = tk.BooleanVar(value=True)
        _deskew_cb = ctk.CTkCheckBox(
            p, text="Deskew packets",
            variable=self._deskew_var,
            fg_color=_C["mid_green"], hover_color=_C["soft_green"],
            checkmark_color="white", text_color=_C["deep_green"],
        )
        _deskew_cb.grid(row=4, column=0, columnspan=3, sticky="w",
                        padx=10, pady=(4, 6))
        _ToolTip(_deskew_cb,
                 "Correct slight rotational skew in each crop using a "
                 "minimum-area rectangle fit. Recommended for packets that "
                 "are placed at a small angle. Adds a little processing time.")

    def _build_right(self, p):
        # Preview area
        pf = ctk.CTkFrame(p, fg_color=_C["cream"],
                           border_color=_C["rule"], border_width=1)
        pf.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(8, 4))
        pf.grid_columnconfigure(0, weight=1)
        pf.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(pf, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hdr, text="Preview",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=_C["deep_green"],
        ).grid(row=0, column=0, sticky="w")
        self._count_lbl = ctk.CTkLabel(hdr, text="", font=ctk.CTkFont(size=11),
                                        text_color=_C["grey"])
        self._count_lbl.grid(row=0, column=1, sticky="e")

        self._canvas = tk.Canvas(pf, bg="#1e1e1e", highlightthickness=0,
                                  cursor="fleur")
        self._canvas.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 6))
        self._canvas.bind("<Configure>",    self._on_canvas_resize)
        self._canvas.bind("<MouseWheel>",   self._on_scroll)
        self._canvas.bind("<ButtonPress-1>",   self._on_drag_start)
        self._canvas.bind("<B1-Motion>",       self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self._canvas.bind("<Double-Button-1>", self._on_reset_view)

        # Log area
        lf = ctk.CTkFrame(p, fg_color=_C["cream"],
                           border_color=_C["rule"], border_width=1)
        lf.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        lf.grid_columnconfigure(0, weight=1)
        lf.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            lf, text="Log",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=_C["deep_green"],
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(6, 2))
        self._log_box = ctk.CTkTextbox(
            lf, height=100,
            font=ctk.CTkFont(size=11, family="Courier New"),
            fg_color=_C["warm_white"], text_color=_C["deep_green"],
            border_color=_C["rule"], border_width=1,
            scrollbar_button_color=_C["rule"],
            scrollbar_button_hover_color=_C["grey"])
        self._log_box.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self._log_box.configure(state="disabled")
        self._log("Ready.  Select folders, then press 'Preview one' or 'Run Batch'.")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _toggle_advanced(self):
        self._adv_open = not self._adv_open
        if self._adv_open:
            self._adv_fr.grid()
            self._adv_btn.configure(text="▾ Advanced settings")
        else:
            self._adv_fr.grid_remove()
            self._adv_btn.configure(text="▸ Advanced settings")

    def _build_settings_obj(self) -> SegSettings:
        return SegSettings(
            top_crop_frac=self._crop_var.get() / 100.0,
            foreground={
                "Light": "light", "Dark": "dark", "Auto (slower)": "auto",
            }[self._fg_var.get()],
            threshold_mode={
                "Otsu": "otsu", "Adaptive": "adaptive", "Canny": "canny",
            }[self._thresh_var.get()],
            contrast={
                "None": "none", "Normalize": "normalize",
                "CLAHE": "clahe", "Both": "both",
            }[self._contrast_var.get()],
            min_area_frac=self._min_area_var.get() / 100.0,
            padding=self._pad_var.get(),
            deskew=self._deskew_var.get(),
        )

    def _browse_input(self):
        d = filedialog.askdirectory(title="Select folder containing source images")
        if d:
            self._input_var.set(d)

    def _browse_output(self):
        d = filedialog.askdirectory(title="Select folder for cropped packet output")
        if d:
            self._output_var.set(d)

    def _log(self, msg: str):
        self._log_box.configure(state="normal")
        self._log_box.insert("end", msg + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _show_preview(self, bgr: np.ndarray, count: int, reset_view: bool = True):
        self._last_debug = bgr
        if reset_view:
            self._zoom = 1.0
            self._pan_x = 0
            self._pan_y = 0
        self._render_to_canvas(bgr)
        noun = "packet" if count == 1 else "packets"
        self._count_lbl.configure(
            text=f"{count} {noun} detected   ·   scroll to zoom  ·  drag to pan  ·  double-click to reset"
        )

    def _render_to_canvas(self, bgr: np.ndarray):
        cw = max(self._canvas.winfo_width(), 120)
        ch = max(self._canvas.winfo_height(), 120)
        ih, iw = bgr.shape[:2]

        # Scale that fits the whole image in the canvas at zoom = 1.0
        base_scale = min(cw / iw, ch / ih)
        display_scale = base_scale * self._zoom

        new_w = max(1, int(iw * display_scale))
        new_h = max(1, int(ih * display_scale))

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb).resize((new_w, new_h), Image.LANCZOS)
        photo = ImageTk.PhotoImage(pil)

        self._canvas.delete("all")
        self._tk_photo = photo   # prevent GC

        # Image is centred in the canvas, offset by pan
        cx = cw // 2 + self._pan_x
        cy = ch // 2 + self._pan_y
        self._canvas.create_image(cx, cy, anchor="center", image=photo)

    def _on_canvas_resize(self, _event):
        if self._last_debug is not None:
            self._render_to_canvas(self._last_debug)

    # ── Zoom / pan event handlers ─────────────────────────────────────────────

    def _on_scroll(self, event):
        """Scroll wheel: zoom centred on the cursor position."""
        if self._last_debug is None:
            return
        factor = 1.15 if event.delta > 0 else (1 / 1.15)
        old_zoom = self._zoom
        self._zoom = max(0.5, min(16.0, self._zoom * factor))

        # Keep the pixel under the cursor fixed while zooming
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        mx = event.x - cw // 2   # cursor position relative to canvas centre
        my = event.y - ch // 2
        z_ratio = self._zoom / old_zoom
        self._pan_x = int(mx + (self._pan_x - mx) * z_ratio)
        self._pan_y = int(my + (self._pan_y - my) * z_ratio)

        self._render_to_canvas(self._last_debug)

    def _on_drag_start(self, event):
        self._drag_start = (event.x, event.y)

    def _on_drag(self, event):
        if self._drag_start is None or self._last_debug is None:
            return
        self._pan_x += event.x - self._drag_start[0]
        self._pan_y += event.y - self._drag_start[1]
        self._drag_start = (event.x, event.y)
        self._render_to_canvas(self._last_debug)

    def _on_drag_end(self, _event):
        self._drag_start = None

    def _on_reset_view(self, _event):
        """Double-click: snap back to fit-in-canvas, no pan."""
        self._zoom = 1.0
        self._pan_x = 0
        self._pan_y = 0
        if self._last_debug is not None:
            self._render_to_canvas(self._last_debug)

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self._preview_btn.configure(state=state)
        self._run_btn.configure(state=state)
        if busy:
            self._prog_fr.grid()
        else:
            self._prog_fr.grid_remove()

    # ── Preview ───────────────────────────────────────────────────────────────

    def _run_preview(self):
        in_p = self._input_var.get().strip()
        if not in_p or not Path(in_p).is_dir():
            messagebox.showerror("Error", "Please select a valid input folder.")
            return
        images = sorted(
            p for p in Path(in_p).iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )
        if not images:
            messagebox.showinfo("No images", "No images found in the selected folder.")
            return

        s = self._build_settings_obj()
        self._set_busy(True)
        self._prog_label.configure(text=f"Previewing {images[0].name} …")
        self._log(f"\nPreviewing '{images[0].name}' …")

        def worker():
            try:
                dets, debug, fg, top_px = segment_image(images[0], s)
                count = len(dets)
                self.after(0, lambda: self._show_preview(debug, count))
                self.after(0, lambda: self._log(
                    f"  {count} packets found  "
                    f"(foreground={fg}, top_crop={top_px}px)"))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._log(f"  Error: {e}"))
            finally:
                self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    # ── Batch ─────────────────────────────────────────────────────────────────

    def _cancel_batch(self):
        self._cancel_event.set()
        self._log("  Cancelling after current image …")

    def _run_batch(self):
        in_p = self._input_var.get().strip()
        out_p = self._output_var.get().strip()

        if not in_p or not Path(in_p).is_dir():
            messagebox.showerror("Error", "Please select a valid input folder.")
            return

        # Auto-generate dated output sub-folder when field is blank
        if not out_p:
            auto = _auto_output_dir(Path(in_p))
            self._output_var.set(str(auto))
            out_p = str(auto)

        images = sorted(
            p for p in Path(in_p).iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )
        if not images:
            messagebox.showinfo("No images", "No images found in the selected folder.")
            return

        out_dir = Path(out_p)
        s = self._build_settings_obj()
        total = len(images)

        self._cancel_event.clear()
        self._set_busy(True)
        self._review_fr.grid_remove()
        self._open_btn.configure(state="disabled")
        self._prog_bar.set(0)
        self._log(f"\nStarting batch — {total} image(s) → {out_dir}")

        def worker():
            manifest_rows: list = []
            results: list[ImageResult] = []
            out_dir.mkdir(parents=True, exist_ok=True)

            for i, img_path in enumerate(images, 1):
                if self._cancel_event.is_set():
                    self.after(0, lambda: self._log("  Cancelled."))
                    break

                name = img_path.name

                def _progress(i=i, name=name):
                    self._prog_bar.set(i / total)
                    self._prog_label.configure(text=f"{i} / {total}   {name}")
                self.after(0, _progress)

                try:
                    count, debug, crop_info = save_crops(img_path, out_dir, s, manifest_rows)
                    results.append(ImageResult(path=img_path, count=count, crop_info=crop_info))

                    def _ok(n=name, c=count, d=debug):
                        self._log(f"  {n}:  {c} packets")
                        self._show_preview(d, c)
                    self.after(0, _ok)

                except Exception as exc:
                    results.append(ImageResult(path=img_path, count=0, flag="none"))

                    def _err(n=name, e=str(exc)):
                        self._log(f"  {n}:  ERROR — {e}")
                    self.after(0, _err)

            # Write manifest
            manifest_path = out_dir / "packet_manifest.csv"
            try:
                with manifest_path.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
                    writer.writeheader()
                    writer.writerows(manifest_rows)
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._log(
                    f"  Warning: could not write manifest — {e}"))

            flagged = [r for r in flag_results(results) if r.flag]
            self.after(0, lambda: self._on_batch_done(
                flagged, results, out_dir, manifest_path, s))

        threading.Thread(target=worker, daemon=True).start()

    def _on_batch_done(self, flagged: list, all_results: list,
                       out_dir: Path, manifest_path: Path, settings: SegSettings):
        self._set_busy(False)
        self._open_btn.configure(state="normal")

        # Store context for Fix / Gallery / VVGo buttons
        self._last_out_dir     = out_dir
        self._last_settings    = settings
        self._last_all_results = all_results

        self._gallery_btn.configure(state="normal")
        self._vvgo_btn.configure(state="normal")

        total = len(all_results)
        ok = total - len(flagged)
        self._log(f"\nDone.  {ok} / {total} images OK.")
        self._log(f"Manifest saved: {manifest_path}")

        # Clear old review rows
        for child in self._review_list.winfo_children():
            child.destroy()

        if flagged:
            self._log(
                f"⚠  {len(flagged)} image(s) need a closer look — see panel below.")
            self._review_fr.grid(
                row=8, column=0, sticky="nsew", padx=14, pady=(6, 4))

            for r in flagged:
                label, color, hover = _FLAG_STYLE.get(
                    r.flag, ("⚠ Review", "#888", "#666"))
                detail = f"\n  {r.flag_detail}" if r.flag_detail else ""

                row_fr = ctk.CTkFrame(self._review_list, fg_color=color,
                                      corner_radius=6)
                row_fr.grid(sticky="ew", pady=3, padx=2)
                row_fr.grid_columnconfigure(0, weight=1)

                # Left side: click to preview
                ctk.CTkButton(
                    row_fr,
                    text=f"{label}{detail}\n  {r.path.name}",
                    fg_color="transparent", hover_color=hover,
                    text_color="white", anchor="w",
                    font=ctk.CTkFont(size=11),
                    command=lambda p=r.path: self._preview_flagged(p),
                ).grid(row=0, column=0, sticky="ew", padx=(4, 0), pady=2)

                # Right side: Fix button (oversize only) + ✏ Edit button (all flags)
                col = 1
                if r.flag == "oversize":
                    fix_btn = ctk.CTkButton(
                        row_fr,
                        text="Fix",
                        width=46,
                        fg_color=_C["mid_green"], hover_color=_C["deep_green"],
                        font=ctk.CTkFont(size=11),
                    )
                    fix_btn.configure(
                        command=lambda r=r, b=fix_btn: self._fix_crop(r, b))
                    fix_btn.grid(row=0, column=col, padx=(4, 2), pady=4)
                    col += 1

                edit_btn = ctk.CTkButton(
                    row_fr,
                    text="✏",
                    width=36,
                    fg_color=_C["deep_green"], hover_color=_C["mid_green"],
                    font=ctk.CTkFont(size=13),
                )

                def _open_edit(r=r, btn=edit_btn):
                    def _mark_done():
                        self.after(0, lambda: btn.configure(
                            text="✓", state="disabled",
                            fg_color=_C["mid_green"],
                            hover_color=_C["mid_green"],
                        ))
                    self._open_manual_editor(r.path, result=r, on_done=_mark_done)

                edit_btn.configure(command=_open_edit)
                edit_btn.grid(row=0, column=col, padx=(0, 6), pady=4)
        else:
            self._log("All images look consistent — no review needed.")
            self._review_fr.grid_remove()

    def _fix_crop(self, result: "ImageResult", fix_btn: ctk.CTkButton):
        """
        For an oversize-flagged source image, attempt to fix every oversized
        crop by re-segmenting it, falling back to a geometric bisect.
        Runs in a background thread; updates the Fix button to show the outcome.
        """
        if self._last_out_dir is None or self._last_settings is None:
            return

        out_dir = self._last_out_dir
        s = self._last_settings
        fix_btn.configure(state="disabled", text="…")

        # Identify which of this image's crops are oversized
        all_info = [(p, w, h) for p, w, h in result.crop_info if p.exists()]
        if not all_info:
            fix_btn.configure(text="Gone", state="disabled")
            return

        all_dims = [(w, h) for _, w, h in all_info]
        med_w = float(np.median([d[0] for d in all_dims]))
        med_h = float(np.median([d[1] for d in all_dims]))
        oversized_paths = [
            p for p, w, h in all_info
            if w > _OVERSIZE_THRESHOLD * med_w or h > _OVERSIZE_THRESHOLD * med_h
        ]

        # If this image only has a small number of crops (e.g. 1–2), the
        # "median" isn't meaningful — treat all crops as candidates.
        if not oversized_paths or len(all_info) <= 2:
            oversized_paths = [p for p, _, _ in all_info]

        def worker():
            outcomes = []
            new_previews = []
            for crop_path in oversized_paths:
                try:
                    new_paths, desc = resegment_or_bisect(crop_path, out_dir, s)
                    outcomes.append(f"  {crop_path.name}  →  {desc}")
                    new_previews.extend(new_paths)
                except Exception as exc:
                    outcomes.append(f"  {crop_path.name}  →  ERROR: {exc}")

            def finish():
                for msg in outcomes:
                    self._log(msg)
                if new_previews:
                    # Build a side-by-side composite so all replacement crops
                    # are visible at once for a quick visual check
                    panels = []
                    for p in new_previews:
                        raw = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
                        if raw is not None:
                            panels.append(_to_bgr8(raw))
                    if panels:
                        self._show_preview(make_composite(panels),
                                           len(panels), reset_view=True)
                    fix_btn.configure(text="✓ Fixed", fg_color=_C["deep_green"],
                                      hover_color=_C["deep_green"], state="disabled")
                else:
                    fix_btn.configure(text="✗ Failed", fg_color=_C["err"],
                                      hover_color=_C["err"], state="disabled")

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _preview_flagged(self, img_path: Path):
        s = self._build_settings_obj()
        self._log(f"\nRe-previewing: {img_path.name}")

        def worker():
            try:
                dets, debug, fg, _ = segment_image(img_path, s)
                count = len(dets)
                self.after(0, lambda: self._show_preview(debug, count))
                self.after(0, lambda: self._log(
                    f"  {count} packets  (foreground={fg})"))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._log(f"  Error: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    # ── Manual editor ─────────────────────────────────────────────────────────

    def _open_manual_editor(self, image_path: Path,
                            result: "ImageResult | None" = None,
                            on_done: "callable | None" = None):
        """
        Open ManualCropEditor for *image_path*.
        Runs segmentation in a thread first so the editor can show a detection
        overlay: green boxes for normal crops, red for oversized ones.
        *on_done* is called (no args) after the user saves crops.
        """
        out_dir = self._last_out_dir
        s = self._last_settings
        if out_dir is None or s is None:
            messagebox.showwarning(
                "No batch yet",
                "Run a batch first so the output folder is known.",
                parent=self,
            )
            return

        def build_overlay():
            try:
                raw = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
                if raw is None:
                    raise ValueError(f"Cannot open: {image_path}")
                bgr_full = _to_bgr8(raw)
                ih = bgr_full.shape[0]
                top_px = int(ih * s.top_crop_frac)
                clean = bgr_full[top_px:, :]

                # Re-detect packets on the cropped source
                try:
                    dets, _, _ = _detect_packets(clean, s)
                except Exception:
                    dets = []

                # Decide which detections are "oversized" relative to the batch
                oversized_idx: set[int] = set()
                if result is not None and result.crop_info:
                    all_dims = [(w, h) for _, w, h in result.crop_info]
                    if all_dims:
                        med_w = float(np.median([d[0] for d in all_dims]))
                        med_h = float(np.median([d[1] for d in all_dims]))
                        for i, d in enumerate(dets):
                            dw = d["w"] + 2 * s.padding
                            dh = d["h"] + 2 * s.padding
                            if (dw > _OVERSIZE_THRESHOLD * med_w or
                                    dh > _OVERSIZE_THRESHOLD * med_h):
                                oversized_idx.add(i)

                # Draw colored overlay on a copy of the clean image
                overlay = clean.copy()
                for i, d in enumerate(dets):
                    x, y, w, h = d["x"], d["y"], d["w"], d["h"]
                    # red for oversized, green for OK
                    colour = (0, 0, 220) if i in oversized_idx else (0, 200, 60)
                    cv2.rectangle(overlay, (x, y), (x + w, y + h), colour, 3)
                    cv2.putText(overlay, str(i + 1),
                                (x + 6, y + 26),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                                colour, 2, cv2.LINE_AA)

                self.after(0, lambda: ManualCropEditor(
                    self, image_path, out_dir, s,
                    overlay_bgr=overlay, on_save=on_done,
                ))
            except Exception as exc:
                self.after(0, lambda e=str(exc): messagebox.showerror(
                    "Error", e, parent=self))

        threading.Thread(target=build_overlay, daemon=True).start()

    # ── Gallery & VVGo launchers ──────────────────────────────────────────────

    def _open_gallery(self):
        if not self._last_out_dir or not self._last_settings:
            return
        CropGallery(self, self._last_out_dir,
                    self._last_all_results, self._last_settings)

    def _open_vvgo(self):
        if not self._last_out_dir:
            return
        VVGoDialog(self, self._last_out_dir)

    # ── Open output ───────────────────────────────────────────────────────────

    def _open_output(self):
        out = self._output_var.get().strip()
        if out and Path(out).exists():
            os.startfile(out)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
