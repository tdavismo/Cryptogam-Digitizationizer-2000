#!/usr/bin/env python3
"""
Segment herbarium lichen/bryophyte packet labels from batch images.

Designed for images where packet labels are separated on a neutral background.
The script detects packet-shaped foreground regions, saves each crop, writes a
CSV manifest, and can save debug overlays/masks.

Default settings are tuned to the user's current successful command:

    python segment_packets.py input_images output_packets --debug --top-crop-frac 0 --foreground light --min-area-frac 0.0005 --max-area-frac 0.95 --morph-frac 0.0015

Additional options are included for tighter/low-contrast images:
    --contrast clahe
    --threshold-mode adaptive
    --adaptive-c 3/7/12
"""

from pathlib import Path
import argparse
import csv
import cv2
import numpy as np


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Segment herbarium lichen/bryophyte packet labels from batch images."
    )

    parser.add_argument(
        "input",
        type=Path,
        help="Input image file or folder of images."
    )

    parser.add_argument(
        "output",
        type=Path,
        help="Output folder for cropped packet images and manifest."
    )

    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Search input folder recursively."
    )

    parser.add_argument(
        "--top-crop-frac",
        type=float,
        default=0.0,
        help=(
            "Fraction of image height to remove from top before detecting packets. "
            "Use this to exclude ruler/color checker. Default: 0.0"
        )
    )

    parser.add_argument(
        "--foreground",
        choices=["auto", "light", "dark"],
        default="light",
        help=(
            "Whether packets are lighter or darker than background. "
            "Default: light"
        )
    )

    parser.add_argument(
        "--threshold-mode",
        choices=["otsu", "adaptive", "canny"],
        default="otsu",
        help=(
            "Segmentation method. 'otsu' is simple global thresholding; "
            "'adaptive' is better for uneven lighting/low contrast; "
            "'canny' uses edges. Default: otsu"
        )
    )

    parser.add_argument(
        "--contrast",
        choices=["none", "normalize", "clahe", "both"],
        default="none",
        help=(
            "Contrast enhancement before segmentation. "
            "Use 'clahe' or 'both' for low-contrast/tightly packed images. "
            "Default: none"
        )
    )

    parser.add_argument(
        "--adaptive-block-frac",
        type=float,
        default=0.06,
        help=(
            "Adaptive threshold block size as fraction of smaller image dimension. "
            "Used only with --threshold-mode adaptive. Default: 0.06"
        )
    )

    parser.add_argument(
        "--adaptive-c",
        type=int,
        default=7,
        help=(
            "Adaptive threshold constant. Increase if too much background is detected; "
            "decrease if packets are missed. Used only with --threshold-mode adaptive. "
            "Default: 7"
        )
    )

    parser.add_argument(
        "--min-area-frac",
        type=float,
        default=0.0005,
        help=(
            "Minimum detected packet area as fraction of working image area. "
            "Increase to remove false positives. Default: 0.0005"
        )
    )

    parser.add_argument(
        "--max-area-frac",
        type=float,
        default=0.95,
        help=(
            "Maximum detected packet area as fraction of working image area. "
            "Decrease if large background regions are being detected. Default: 0.95"
        )
    )

    parser.add_argument(
        "--min-width-frac",
        type=float,
        default=0.04,
        help="Minimum packet width as fraction of working image width. Default: 0.04"
    )

    parser.add_argument(
        "--min-height-frac",
        type=float,
        default=0.04,
        help="Minimum packet height as fraction of working image height. Default: 0.04"
    )

    parser.add_argument(
        "--padding",
        type=int,
        default=30,
        help="Padding in pixels added around each detected crop. Default: 30"
    )

    parser.add_argument(
        "--morph-frac",
        type=float,
        default=0.0015,
        help=(
            "Morphological cleanup kernel size as fraction of smaller image dimension. "
            "Increase if labels fragment; decrease if packets merge. Default: 0.0015"
        )
    )

    parser.add_argument(
        "--rectangularity-min",
        type=float,
        default=0.12,
        help=(
            "Minimum contour rectangularity. Lower accepts messier packet outlines; "
            "higher rejects shadows/debris. Default: 0.12"
        )
    )

    parser.add_argument(
        "--aspect-min",
        type=float,
        default=0.20,
        help="Minimum width/height aspect ratio for detections. Default: 0.20"
    )

    parser.add_argument(
        "--aspect-max",
        type=float,
        default=5.0,
        help="Maximum width/height aspect ratio for detections. Default: 5.0"
    )

    parser.add_argument(
        "--deskew",
        action="store_true",
        help="Deskew packets using rotated rectangle extraction."
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save debug images with detected packet boxes and masks."
    )

    return parser.parse_args()


def list_images(input_path: Path, recursive: bool):
    if input_path.is_file():
        if input_path.suffix.lower() in IMAGE_EXTENSIONS:
            return [input_path]
        return []

    if not input_path.exists():
        return []

    files = input_path.rglob("*") if recursive else input_path.iterdir()

    return sorted(
        p for p in files
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def normalize_to_uint8(img):
    """
    Convert image to uint8 for processing, while preserving useful contrast.
    Useful for 16-bit TIFFs.
    """
    if img.dtype == np.uint8:
        return img

    img_float = img.astype(np.float32)
    min_val = np.percentile(img_float, 0.5)
    max_val = np.percentile(img_float, 99.5)

    if max_val <= min_val:
        return np.zeros(img.shape, dtype=np.uint8)

    img_float = np.clip(img_float, min_val, max_val)
    img_norm = ((img_float - min_val) / (max_val - min_val) * 255.0)
    return img_norm.astype(np.uint8)


def to_bgr_uint8(img):
    """
    Return a BGR uint8 version for processing/debug display.
    """
    img8 = normalize_to_uint8(img)

    if img8.ndim == 2:
        return cv2.cvtColor(img8, cv2.COLOR_GRAY2BGR)

    if img8.shape[2] == 4:
        return cv2.cvtColor(img8, cv2.COLOR_BGRA2BGR)

    if img8.shape[2] == 3:
        return img8

    raise ValueError("Unsupported image format.")


def make_odd_kernel_size(value):
    value = max(3, int(round(value)))
    if value % 2 == 0:
        value += 1
    return value


def enhance_gray(gray, contrast_mode):
    """
    Improve packet/background separation before thresholding.
    """
    enhanced = gray.copy()

    if contrast_mode in ["normalize", "both"]:
        enhanced = cv2.normalize(enhanced, None, 0, 255, cv2.NORM_MINMAX)

    if contrast_mode in ["clahe", "both"]:
        clahe = cv2.createCLAHE(
            clipLimit=2.0,
            tileGridSize=(8, 8)
        )
        enhanced = clahe.apply(enhanced)

    return enhanced


def build_raw_mask(gray, foreground, threshold_mode, adaptive_block_frac, adaptive_c):
    """
    Build an initial foreground mask before morphological cleanup.
    """
    h, w = gray.shape[:2]

    if threshold_mode == "otsu":
        blur = cv2.GaussianBlur(gray, (5, 5), 0)

        if foreground == "light":
            _, mask = cv2.threshold(
                blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
            )
        else:
            _, mask = cv2.threshold(
                blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
            )

        return mask

    if threshold_mode == "adaptive":
        block_size = make_odd_kernel_size(min(h, w) * adaptive_block_frac)
        block_size = max(15, block_size)

        if foreground == "light":
            threshold_type = cv2.THRESH_BINARY
        else:
            threshold_type = cv2.THRESH_BINARY_INV

        mask = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            threshold_type,
            block_size,
            adaptive_c
        )

        return mask

    if threshold_mode == "canny":
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 40, 120)

        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.dilate(edges, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        return mask

    raise ValueError(f"Unsupported threshold mode: {threshold_mode}")


def cleanup_mask(mask, morph_frac):
    h, w = mask.shape[:2]
    kernel_size = make_odd_kernel_size(min(h, w) * morph_frac)
    kernel = np.ones((kernel_size, kernel_size), np.uint8)

    # Remove specks.
    clean = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    # Close small text/crease gaps inside packet areas.
    clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)

    return clean


def make_packet_mask(gray, foreground, morph_frac, threshold_mode, contrast_mode, adaptive_block_frac, adaptive_c):
    gray = enhance_gray(gray, contrast_mode)

    if foreground == "auto":
        mask_light = build_raw_mask(
            gray,
            foreground="light",
            threshold_mode=threshold_mode,
            adaptive_block_frac=adaptive_block_frac,
            adaptive_c=adaptive_c
        )

        mask_dark = build_raw_mask(
            gray,
            foreground="dark",
            threshold_mode=threshold_mode,
            adaptive_block_frac=adaptive_block_frac,
            adaptive_c=adaptive_c
        )

        return {
            "light": cleanup_mask(mask_light, morph_frac),
            "dark": cleanup_mask(mask_dark, morph_frac)
        }, "auto"

    raw = build_raw_mask(
        gray,
        foreground=foreground,
        threshold_mode=threshold_mode,
        adaptive_block_frac=adaptive_block_frac,
        adaptive_c=adaptive_c
    )

    return cleanup_mask(raw, morph_frac), foreground


def contour_rectangularity(contour, width, height):
    rect_area = width * height
    if rect_area <= 0:
        return 0.0

    contour_area = cv2.contourArea(contour)
    return contour_area / rect_area


def extract_detections_from_mask(mask, args):
    h, w = mask.shape[:2]
    image_area = h * w

    min_area = args.min_area_frac * image_area
    max_area = args.max_area_frac * image_area
    min_width = args.min_width_frac * w
    min_height = args.min_height_frac * h

    contours, _ = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE
    )

    detections = []

    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        contour_area = cv2.contourArea(contour)

        if contour_area < min_area:
            continue

        if contour_area > max_area:
            continue

        if bw < min_width or bh < min_height:
            continue

        aspect = bw / float(bh)

        if aspect < args.aspect_min or aspect > args.aspect_max:
            continue

        rectangularity = contour_rectangularity(contour, bw, bh)

        if rectangularity < args.rectangularity_min:
            continue

        detections.append({
            "contour": contour,
            "x": x,
            "y": y,
            "w": bw,
            "h": bh,
            "area": contour_area,
            "aspect": aspect,
            "rectangularity": rectangularity,
        })

    return sort_detections_reading_order(detections)


def score_detections(detections):
    """
    Prefer masks that produce a reasonable number of packet-like objects,
    not one huge background blob or many tiny fragments.
    """
    if not detections:
        return -1.0

    count_score = len(detections) * 10.0
    rectangularity_score = sum(d["rectangularity"] for d in detections)

    area_values = [d["area"] for d in detections]
    area_median = np.median(area_values)

    size_consistency_penalty = 0.0
    for area in area_values:
        if area_median > 0:
            ratio = area / area_median
            if ratio > 5:
                size_consistency_penalty += 10.0

    return count_score + rectangularity_score - size_consistency_penalty


def detect_packets(work_bgr, args):
    gray = cv2.cvtColor(work_bgr, cv2.COLOR_BGR2GRAY)

    mask_result, selected_foreground = make_packet_mask(
        gray,
        foreground=args.foreground,
        morph_frac=args.morph_frac,
        threshold_mode=args.threshold_mode,
        contrast_mode=args.contrast,
        adaptive_block_frac=args.adaptive_block_frac,
        adaptive_c=args.adaptive_c
    )

    if args.foreground == "auto":
        light_mask = mask_result["light"]
        dark_mask = mask_result["dark"]

        light_detections = extract_detections_from_mask(light_mask, args)
        dark_detections = extract_detections_from_mask(dark_mask, args)

        light_score = score_detections(light_detections)
        dark_score = score_detections(dark_detections)

        if light_score >= dark_score:
            detections = light_detections
            mask = light_mask
            selected_foreground = "light"
        else:
            detections = dark_detections
            mask = dark_mask
            selected_foreground = "dark"
    else:
        mask = mask_result
        detections = extract_detections_from_mask(mask, args)

    return detections, mask, selected_foreground


def sort_detections_reading_order(detections):
    """
    Sort detections in rough reading order:
    top-to-bottom, then left-to-right within rows.
    """
    if not detections:
        return []

    for d in detections:
        d["cx"] = d["x"] + d["w"] / 2
        d["cy"] = d["y"] + d["h"] / 2

    median_height = np.median([d["h"] for d in detections])
    row_tolerance = max(20, median_height * 0.45)

    detections_sorted = sorted(detections, key=lambda d: d["cy"])

    rows = []

    for detection in detections_sorted:
        placed = False

        for row in rows:
            row_cy = np.mean([d["cy"] for d in row])
            if abs(detection["cy"] - row_cy) <= row_tolerance:
                row.append(detection)
                placed = True
                break

        if not placed:
            rows.append([detection])

    rows.sort(key=lambda row: np.mean([d["cy"] for d in row]))

    sorted_detections = []
    for row in rows:
        row.sort(key=lambda d: d["cx"])
        sorted_detections.extend(row)

    return sorted_detections


def crop_with_padding(img, x, y, w, h, padding):
    img_h, img_w = img.shape[:2]

    x1 = max(0, x - padding)
    y1 = max(0, y - padding)
    x2 = min(img_w, x + w + padding)
    y2 = min(img_h, y + h + padding)

    return img[y1:y2, x1:x2], (x1, y1, x2, y2)


def order_box_points(points):
    """
    Return points ordered as top-left, top-right, bottom-right, bottom-left.
    """
    points = np.array(points, dtype="float32")

    s = points.sum(axis=1)
    diff = np.diff(points, axis=1)

    top_left = points[np.argmin(s)]
    bottom_right = points[np.argmax(s)]
    top_right = points[np.argmin(diff)]
    bottom_left = points[np.argmax(diff)]

    return np.array([top_left, top_right, bottom_right, bottom_left], dtype="float32")


def deskew_crop(original_work_img, contour, padding):
    """
    Extract a rotated rectangle around the detected packet.
    Useful if packets are slightly rotated.
    """
    rect = cv2.minAreaRect(contour)
    box = cv2.boxPoints(rect)
    box = order_box_points(box)

    width_a = np.linalg.norm(box[2] - box[3])
    width_b = np.linalg.norm(box[1] - box[0])
    max_width = int(max(width_a, width_b)) + padding * 2

    height_a = np.linalg.norm(box[1] - box[2])
    height_b = np.linalg.norm(box[0] - box[3])
    max_height = int(max(height_a, height_b)) + padding * 2

    if max_width <= 0 or max_height <= 0:
        return None

    destination = np.array([
        [padding, padding],
        [max_width - padding - 1, padding],
        [max_width - padding - 1, max_height - padding - 1],
        [padding, max_height - padding - 1],
    ], dtype="float32")

    transform = cv2.getPerspectiveTransform(box, destination)
    warped = cv2.warpPerspective(
        original_work_img,
        transform,
        (max_width, max_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE
    )

    return warped


def save_debug_image(debug_bgr, detections, output_path, y_offset=0):
    debug = debug_bgr.copy()

    for i, d in enumerate(detections, start=1):
        x = d["x"]
        y = d["y"] + y_offset
        w = d["w"]
        h = d["h"]

        cv2.rectangle(debug, (x, y), (x + w, y + h), (0, 255, 0), 4)

        label = f"{i:02d}"
        cv2.putText(
            debug,
            label,
            (x + 10, y + 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.2,
            (0, 0, 255),
            3,
            cv2.LINE_AA
        )

    cv2.imwrite(str(output_path), debug)


def process_image(image_path, output_dir, manifest_writer, args):
    original = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)

    if original is None:
        print(f"Could not read: {image_path}")
        return

    original_bgr8 = to_bgr_uint8(original)

    full_h, full_w = original.shape[:2]
    top_crop_px = int(full_h * args.top_crop_frac)

    # Work image for detection/debug.
    work_bgr8 = original_bgr8[top_crop_px:, :]

    # Work image for actual crop output.
    # This preserves original bit depth/channels where possible.
    original_work = original[top_crop_px:, :]

    if work_bgr8.size == 0:
        print(f"Skipped {image_path.name}: top crop removed entire image")
        return

    detections, mask, selected_foreground = detect_packets(work_bgr8, args)

    stem = image_path.stem
    image_output_dir = output_dir / stem
    image_output_dir.mkdir(parents=True, exist_ok=True)

    saved_count = 0

    for packet_index, d in enumerate(detections, start=1):
        x, y, w, h = d["x"], d["y"], d["w"], d["h"]

        if args.deskew:
            crop = deskew_crop(original_work, d["contour"], args.padding)
            crop_x1 = crop_y1 = crop_x2 = crop_y2 = ""
        else:
            crop, crop_box = crop_with_padding(
                original_work,
                x,
                y,
                w,
                h,
                args.padding
            )
            crop_x1, crop_y1, crop_x2, crop_y2 = crop_box

        if crop is None or crop.size == 0:
            continue

        out_name = f"{stem}_packet_{packet_index:02d}{image_path.suffix.lower()}"
        out_path = image_output_dir / out_name

        ok = cv2.imwrite(str(out_path), crop)
        if not ok:
            print(f"Warning: could not write crop: {out_path}")
            continue

        saved_count += 1

        # Bounding boxes are reported in original full-image coordinates.
        manifest_writer.writerow({
            "source_image": str(image_path),
            "output_crop": str(out_path),
            "packet_index": packet_index,
            "detected_foreground": selected_foreground,
            "threshold_mode": args.threshold_mode,
            "contrast": args.contrast,
            "top_crop_px": top_crop_px,
            "bbox_x": x,
            "bbox_y": y + top_crop_px,
            "bbox_w": w,
            "bbox_h": h,
            "crop_x1": crop_x1,
            "crop_y1": "" if crop_y1 == "" else crop_y1 + top_crop_px,
            "crop_x2": crop_x2,
            "crop_y2": "" if crop_y2 == "" else crop_y2 + top_crop_px,
            "contour_area": round(float(d["area"]), 2),
            "aspect": round(float(d["aspect"]), 3),
            "rectangularity": round(float(d["rectangularity"]), 3),
            "deskewed": args.deskew,
        })

    if args.debug:
        debug_dir = output_dir / "_debug"
        debug_dir.mkdir(exist_ok=True)

        # Debug in cropped work area.
        save_debug_image(
            work_bgr8,
            detections,
            debug_dir / f"{stem}_debug_boxes_work_area.jpg",
            y_offset=0
        )

        # Debug in full original image coordinates.
        save_debug_image(
            original_bgr8,
            detections,
            debug_dir / f"{stem}_debug_boxes_full_image.jpg",
            y_offset=top_crop_px
        )

        cv2.imwrite(str(debug_dir / f"{stem}_mask_work_area.jpg"), mask)

    print(f"{image_path.name}: found {len(detections)} packets, saved {saved_count} crops")


def main():
    args = parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    image_paths = list_images(args.input, args.recursive)

    if not image_paths:
        print("No image files found.")
        return

    manifest_path = args.output / "packet_manifest.csv"

    fieldnames = [
        "source_image",
        "output_crop",
        "packet_index",
        "detected_foreground",
        "threshold_mode",
        "contrast",
        "top_crop_px",
        "bbox_x",
        "bbox_y",
        "bbox_w",
        "bbox_h",
        "crop_x1",
        "crop_y1",
        "crop_x2",
        "crop_y2",
        "contour_area",
        "aspect",
        "rectangularity",
        "deskewed",
    ]

    with manifest_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for image_path in image_paths:
            process_image(image_path, args.output, writer, args)

    print(f"\nDone. Manifest written to: {manifest_path}")


if __name__ == "__main__":
    main()
