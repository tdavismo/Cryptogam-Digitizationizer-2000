#!/usr/bin/env python3
"""
segmenter_core.py
-----------------
Pure-Python / OpenCV processing logic for the Cryptogam Digitizationizer.

No GUI dependencies — safe to import from both the Tkinter desktop app and
the FastAPI web server.  All public functions listed in __all__ are stable
entry-points; private helpers (prefixed _) may change.
"""

from __future__ import annotations

import csv
import datetime
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

__all__ = [
    "IMAGE_EXTS",
    "MANIFEST_FIELDS",
    "SegSettings",
    "ImageResult",
    "_OVERSIZE_THRESHOLD",
    "segment_image",
    "save_crops",
    "save_crops_detailed",
    "flag_results",
    "make_composite",
    "resegment_or_bisect",
    "_auto_output_dir",
    # semi-public helpers used by the GUI
    "_to_bgr8",
    "_padded_crop",
    "_detect_packets",
]


# ─── Constants ────────────────────────────────────────────────────────────────

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

MANIFEST_FIELDS = [
    "source_image", "output_crop", "packet_index", "detected_foreground",
    "top_crop_px", "bbox_x", "bbox_y", "bbox_w", "bbox_h",
    "crop_x1", "crop_y1", "crop_x2", "crop_y2",
    "contour_area", "aspect", "rectangularity", "deskewed",
]

# Crops this many times wider or taller than the batch median are flagged.
_OVERSIZE_THRESHOLD = 1.8


# ─── Settings ─────────────────────────────────────────────────────────────────

@dataclass
class SegSettings:
    top_crop_frac:      float = 0.0
    foreground:         str   = "light"    # light | dark | auto
    threshold_mode:     str   = "otsu"     # otsu | adaptive | canny
    contrast:           str   = "none"     # none | normalize | clahe | both
    adaptive_block_frac: float = 0.06
    adaptive_c:         int   = 7
    min_area_frac:      float = 0.0005
    max_area_frac:      float = 0.95
    min_width_frac:     float = 0.04
    min_height_frac:    float = 0.04
    padding:            int   = 30
    morph_frac:         float = 0.0015
    rectangularity_min: float = 0.12
    aspect_min:         float = 0.20
    aspect_max:         float = 5.0
    deskew:             bool  = True


# ─── Result model ─────────────────────────────────────────────────────────────

@dataclass
class ImageResult:
    path:        Path
    count:       int
    crop_info:   list = field(default_factory=list)  # [(Path, w_px, h_px), ...]
    flag:        str  = ""         # "" | "none" | "oversize"
    flag_detail: str  = ""


# ─── Internal image helpers ───────────────────────────────────────────────────

def _to_uint8(img: np.ndarray) -> np.ndarray:
    if img.dtype == np.uint8:
        return img
    f = img.astype(np.float32)
    lo, hi = np.percentile(f, 0.5), np.percentile(f, 99.5)
    if hi <= lo:
        return np.zeros(img.shape, dtype=np.uint8)
    return np.clip((f - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)


def _to_bgr8(img: np.ndarray) -> np.ndarray:
    img8 = _to_uint8(img)
    if img8.ndim == 2:
        return cv2.cvtColor(img8, cv2.COLOR_GRAY2BGR)
    if img8.shape[2] == 4:
        return cv2.cvtColor(img8, cv2.COLOR_BGRA2BGR)
    return img8


def _odd(v: float) -> int:
    v = max(3, int(round(v)))
    return v if v % 2 else v + 1


def _enhance(gray: np.ndarray, mode: str) -> np.ndarray:
    out = gray.copy()
    if mode in ("normalize", "both"):
        out = cv2.normalize(out, None, 0, 255, cv2.NORM_MINMAX)
    if mode in ("clahe", "both"):
        out = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(out)
    return out


def _raw_mask(gray: np.ndarray, fg: str, mode: str,
              block_frac: float, c: int) -> np.ndarray:
    h, w = gray.shape[:2]
    flag_light = cv2.THRESH_BINARY
    flag_dark  = cv2.THRESH_BINARY_INV
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
        blur  = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 40, 120)
        k     = np.ones((5, 5), np.uint8)
        mask  = cv2.dilate(edges, k, iterations=1)
        return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)

    raise ValueError(f"Unknown threshold mode: {mode!r}")


def _clean_mask(mask: np.ndarray, morph_frac: float) -> np.ndarray:
    h, w = mask.shape[:2]
    sz   = _odd(min(h, w) * morph_frac)
    k    = np.ones((sz, sz), np.uint8)
    out  = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k)
    return cv2.morphologyEx(out,  cv2.MORPH_CLOSE, k)


def _score_detections(dets: list) -> float:
    if not dets:
        return -1.0
    areas  = [d["area"] for d in dets]
    med    = float(np.median(areas))
    penalty = sum(10.0 for a in areas if med > 0 and a / med > 5)
    return len(dets) * 10.0 + sum(d["rect"] for d in dets) - penalty


def _sort_reading_order(dets: list) -> list:
    if not dets:
        return []
    for d in dets:
        d["cx"] = d["x"] + d["w"] / 2
        d["cy"] = d["y"] + d["h"] / 2
    tol    = max(20, float(np.median([d["h"] for d in dets])) * 0.45)
    by_cy  = sorted(dets, key=lambda d: d["cy"])
    rows: list[list] = []
    for det in by_cy:
        for row in rows:
            if abs(det["cy"] - float(np.mean([r["cy"] for r in row]))) <= tol:
                row.append(det)
                break
        else:
            rows.append([det])
    rows.sort(key=lambda row: float(np.mean([r["cy"] for r in row])))
    result = []
    for row in rows:
        result.extend(sorted(row, key=lambda d: d["cx"]))
    return result


def _extract_detections(mask: np.ndarray, s: SegSettings) -> list:
    h, w       = mask.shape[:2]
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


def _detect_packets(work_bgr: np.ndarray,
                    s: SegSettings) -> tuple[list, np.ndarray, str]:
    """
    Return (detections, binary_mask, foreground_mode_used).
    When foreground='auto' both polarities are tried and the higher-scoring
    result wins.
    """
    gray = _enhance(cv2.cvtColor(work_bgr, cv2.COLOR_BGR2GRAY), s.contrast)

    if s.foreground == "auto":
        masks = {
            fg: _clean_mask(
                _raw_mask(gray, fg, s.threshold_mode,
                          s.adaptive_block_frac, s.adaptive_c),
                s.morph_frac,
            )
            for fg in ("light", "dark")
        }
        dets_l = _extract_detections(masks["light"], s)
        dets_d = _extract_detections(masks["dark"],  s)
        if _score_detections(dets_l) >= _score_detections(dets_d):
            return dets_l, masks["light"], "light"
        return dets_d, masks["dark"], "dark"

    mask = _clean_mask(
        _raw_mask(gray, s.foreground, s.threshold_mode,
                  s.adaptive_block_frac, s.adaptive_c),
        s.morph_frac,
    )
    return _extract_detections(mask, s), mask, s.foreground


def _order_box(pts: np.ndarray) -> np.ndarray:
    pts = np.array(pts, dtype="float32")
    s   = pts.sum(axis=1)
    d   = np.diff(pts, axis=1)
    return np.array([
        pts[np.argmin(s)], pts[np.argmin(d)],
        pts[np.argmax(s)], pts[np.argmax(d)],
    ], dtype="float32")


def _deskew_crop(img: np.ndarray, contour: np.ndarray,
                 padding: int) -> np.ndarray | None:
    rect = cv2.minAreaRect(contour)
    box  = _order_box(cv2.boxPoints(rect))
    wa = int(max(np.linalg.norm(box[2] - box[3]),
                 np.linalg.norm(box[1] - box[0]))) + padding * 2
    ha = int(max(np.linalg.norm(box[1] - box[2]),
                 np.linalg.norm(box[0] - box[3]))) + padding * 2
    if wa <= 0 or ha <= 0:
        return None
    dst = np.array([
        [padding, padding],
        [wa - padding - 1, padding],
        [wa - padding - 1, ha - padding - 1],
        [padding, ha - padding - 1],
    ], dtype="float32")
    M = cv2.getPerspectiveTransform(box, dst)
    return cv2.warpPerspective(img, M, (wa, ha),
                               flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def _padded_crop(img: np.ndarray, x: int, y: int, w: int, h: int,
                 padding: int) -> tuple[np.ndarray, tuple]:
    ih, iw     = img.shape[:2]
    x1, y1     = max(0, x - padding), max(0, y - padding)
    x2, y2     = min(iw, x + w + padding), min(ih, y + h + padding)
    return img[y1:y2, x1:x2], (x1, y1, x2, y2)


def _draw_boxes(bgr: np.ndarray, dets: list) -> np.ndarray:
    out = bgr.copy()
    for i, d in enumerate(dets, 1):
        x, y, w, h = d["x"], d["y"], d["w"], d["h"]
        cv2.rectangle(out, (x, y), (x + w, y + h), (30, 200, 60), 4)
        cv2.putText(out, f"{i:02d}", (x + 8, y + 44),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.3, (30, 30, 220), 3,
                    cv2.LINE_AA)
    return out


# ─── Public API: detection ────────────────────────────────────────────────────

def segment_image(image_path: Path,
                  s: SegSettings) -> tuple[list, np.ndarray, str, int]:
    """
    Detect packets in *image_path* without writing any files.

    Returns
    -------
    detections : list[dict]
    debug_bgr  : np.ndarray   (source image with coloured bounding boxes)
    foreground : str          ('light' | 'dark')
    top_crop_px : int
    """
    img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise IOError(f"Cannot read: {image_path.name}")
    bgr8   = _to_bgr8(img)
    top_px = int(img.shape[0] * s.top_crop_frac)
    work   = bgr8[top_px:, :]
    if work.size == 0:
        raise ValueError("Top-crop fraction removes the entire image.")
    dets, _mask, fg = _detect_packets(work, s)
    return dets, _draw_boxes(work, dets), fg, top_px


# ─── Public API: batch save ───────────────────────────────────────────────────

def save_crops(image_path: Path, output_dir: Path,
               s: SegSettings,
               manifest_rows: list) -> tuple[int, np.ndarray, list]:
    """
    Detect packets, write individual crop files, append rows to *manifest_rows*.
    Crops land flat in *output_dir* (no sub-folders).

    Backward-compatible 3-tuple wrapper around save_crops_detailed() — used by
    the desktop Tkinter app.

    Returns
    -------
    saved_count : int
    debug_bgr   : np.ndarray
    crop_info   : list[(Path, width_px, height_px)]
    """
    saved, debug, crop_info, _boxes, _iw, _ih = save_crops_detailed(
        image_path, output_dir, s, manifest_rows)
    return saved, debug, crop_info


def save_crops_detailed(image_path: Path, output_dir: Path,
                        s: SegSettings,
                        manifest_rows: list) -> tuple[int, np.ndarray, list, list, int, int]:
    """
    Same as save_crops() but additionally returns the detection box geometry
    and the full source-image dimensions, so a caller (e.g. the web server)
    can overlay live bounding boxes on the source image.

    Returns
    -------
    saved_count : int
    debug_bgr   : np.ndarray
    crop_info   : list[(Path, width_px, height_px)]
    boxes       : list[dict]  — {idx, x, y, w, h} in FULL-image pixel coords
                  (y already offset by the top-crop, so boxes map directly
                  onto the unmodified source image)
    iw_full     : int  — full source-image width  (px)
    ih_full     : int  — full source-image height (px)
    """
    img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise IOError(f"Cannot read: {image_path.name}")

    ih_full, iw_full = img.shape[:2]
    bgr8      = _to_bgr8(img)
    top_px    = int(img.shape[0] * s.top_crop_frac)
    work8     = bgr8[top_px:, :]
    work_orig = img[top_px:, :]
    if work8.size == 0:
        raise ValueError("Top-crop fraction removes the entire image.")

    dets, _mask, fg = _detect_packets(work8, s)
    stem = image_path.stem

    saved     = 0
    crop_info: list[tuple[Path, int, int]] = []
    boxes:     list[dict] = []

    for idx, d in enumerate(dets, 1):
        x, y, w, h = d["x"], d["y"], d["w"], d["h"]
        # Box in full-image coords (y shifted past the removed top strip)
        boxes.append({"idx": idx, "x": int(x), "y": int(y + top_px),
                      "w": int(w), "h": int(h)})
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
            crop_info.append((out_path, crop.shape[1], crop.shape[0]))
            manifest_rows.append({
                "source_image":      str(image_path),
                "output_crop":       str(out_path),
                "packet_index":      idx,
                "detected_foreground": fg,
                "top_crop_px":       top_px,
                "bbox_x": x, "bbox_y": y + top_px,
                "bbox_w": w, "bbox_h": h,
                "crop_x1": bx1,
                "crop_y1": "" if by1 == "" else by1 + top_px,
                "crop_x2": bx2,
                "crop_y2": "" if by2 == "" else by2 + top_px,
                "contour_area":  round(float(d["area"]),   2),
                "aspect":        round(float(d["aspect"]),  3),
                "rectangularity": round(float(d["rect"]),  3),
                "deskewed":      s.deskew,
            })

    return saved, _draw_boxes(work8, dets), crop_info, boxes, iw_full, ih_full


# ─── Public API: QC flagging ──────────────────────────────────────────────────

def flag_results(results: list[ImageResult]) -> list[ImageResult]:
    """
    Flag source images whose crops are significantly larger than the batch median.
    An oversized crop most commonly means two or more packets were merged.
    Mutates and returns *results*.
    """
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


# ─── Public API: composite preview ───────────────────────────────────────────

def make_composite(bgr_list: list[np.ndarray]) -> np.ndarray:
    """Stitch a list of BGR images side-by-side at a common height."""
    if not bgr_list:
        return np.zeros((100, 100, 3), dtype=np.uint8)
    if len(bgr_list) == 1:
        return bgr_list[0]

    target_h = max(img.shape[0] for img in bgr_list)
    panels   = []
    sep      = np.full((target_h, 6, 3), [188, 196, 200], dtype=np.uint8)

    for i, img in enumerate(bgr_list):
        h, w = img.shape[:2]
        if h != target_h:
            img = cv2.resize(img, (int(w * target_h / h), target_h),
                             interpolation=cv2.INTER_LANCZOS4)
        if i > 0:
            panels.append(sep)
        panels.append(img)

    return np.hstack(panels)


# ─── Public API: crop repair ──────────────────────────────────────────────────

def resegment_or_bisect(crop_path: Path, output_dir: Path,
                        s: SegSettings) -> tuple[list[Path], str]:
    """
    Attempt to split an oversized crop into individual packet crops.

    Strategy
    --------
    1.  Re-run the segmenter on the crop image alone.  Because the crop is
        small, relative area thresholds recalculate favourably.
    2.  If ≥ 2 detections found: save each as a replacement file.
    3.  Fallback: geometric bisect along the longest edge.

    The original oversized crop is deleted on success.
    Returns (new_paths, description_string).
    Raises RuntimeError on unrecoverable failure.
    """
    img = cv2.imread(str(crop_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise IOError(f"Cannot read crop: {crop_path.name}")

    bgr8   = _to_bgr8(img)
    stem   = crop_path.stem
    suffix = crop_path.suffix

    # Re-segmentation with permissive settings
    s_sub = SegSettings(
        top_crop_frac=0.0, foreground="auto",
        threshold_mode=s.threshold_mode, contrast=s.contrast,
        adaptive_block_frac=s.adaptive_block_frac, adaptive_c=s.adaptive_c,
        min_area_frac=0.05, max_area_frac=0.80,
        min_width_frac=0.06, min_height_frac=0.06,
        padding=s.padding, morph_frac=s.morph_frac,
        rectangularity_min=s.rectangularity_min,
        aspect_min=s.aspect_min, aspect_max=s.aspect_max,
        deskew=s.deskew,
    )
    try:
        dets, _mask, _fg = _detect_packets(bgr8, s_sub)
    except Exception:
        dets = []

    if len(dets) >= 2:
        new_paths: list[Path] = []
        letters = "abcdefghij"
        for i, d in enumerate(dets):
            x, y, w, h = d["x"], d["y"], d["w"], d["h"]
            crop, _    = _padded_crop(img, x, y, w, h, s.padding)
            if crop is None or crop.size == 0:
                continue
            letter   = letters[i] if i < len(letters) else str(i + 1)
            out_path = output_dir / f"{stem}{letter}{suffix}"
            if cv2.imwrite(str(out_path), crop):
                new_paths.append(out_path)
        if len(new_paths) >= 2:
            crop_path.unlink(missing_ok=True)
            return new_paths, f"re-segmented into {len(new_paths)} packets"

    # Geometric bisect fallback
    ih, iw = img.shape[:2]
    if iw >= ih:
        halves = [img[:, : iw // 2], img[:, iw // 2 :]]
        axis   = "left / right"
    else:
        halves = [img[: ih // 2, :], img[ih // 2 :, :]]
        axis   = "top / bottom"

    new_paths = []
    for i, half in enumerate(halves):
        if half.size == 0:
            continue
        out_path = output_dir / f"{stem}{'ab'[i]}{suffix}"
        if cv2.imwrite(str(out_path), half):
            new_paths.append(out_path)

    if len(new_paths) == 2:
        crop_path.unlink(missing_ok=True)
        note = (f"(re-segmentation found {len(dets)} packet)"
                if dets else "(re-segmentation found nothing)")
        return new_paths, f"bisected {axis}  {note}"

    raise RuntimeError("Could not write replacement crops.")


# ─── Utility ─────────────────────────────────────────────────────────────────

def _auto_output_dir(input_path: Path) -> Path:
    """Return a dated sub-directory of the input folder: DD-MON-YYYY-PACKETS."""
    folder_name = datetime.date.today().strftime("%d-%b-%Y").upper() + "-PACKETS"
    return input_path / folder_name
