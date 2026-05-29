#!/usr/bin/env python3

from pathlib import Path
import argparse
import csv
import math
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
        default=0.18,
        help=(
            "Fraction of image height to remove from top before detecting packets. "
            "Use this to exclude ruler/color checker. Default: 0.18"
        )
    )

    parser.add_argument(
        "--foreground",
        choices=["auto", "light", "dark"],
        default="auto",
        help=(
            "Whether packets are lighter or darker than background. "
            "Use 'auto' first. Default: auto"
        )
    )

    parser.add_argument(
        "--min-area-frac",
        type=float,
        default=0.002,
        help=(
            "Minimum detected packet area as fraction of working image area. "
            "Increase to remove false positives. Default: 0.002"
        )
    )

    parser.add_argument(
        "--max-area-frac",
        type=float,
        default=0.70,
        help=(
            "Maximum detected packet area as fraction of working image area. "
            "Decrease if large background regions are being detected. Default: 0.70"
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
        default=0.003,
        help=(
            "Morphological cleanup kernel size as fraction of smaller image dimension. "
            "Increase if labels fragment; decrease if packets merge. Default: 0.003"
        )
    )

    parser.add_argument(
        "--deskew",
        action="store_true",
        help="Deskew packets using rotated rectangle extraction."
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save debug images with detected packet boxes."
    )

    return parser.parse_args()


def list_images(input_path: Path, recursive: bool):
    if input_path.is_file():
        if input_path.suffix.lower() in IMAGE_EXTENSIONS:
            return [input_path]
        return []

    if recursive:
        files = input_path.rglob("*")
    else:
        files = input_path.iterdir()

    return sorted(
        p for p in files
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def normalize_to_uint8(img):
    """
    Convert image to uint8 for processing, while preserving contrast.
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


def border_foreground_fraction(mask):
    """
    Estimate how much of the image border is foreground.
    Good segmentation should leave most borders as background.
    """
    h, w = mask.shape[:2]
    border = max(5, int(min(h, w) * 0.02))

    top = mask[:border, :]
    bottom = mask[-border:, :]
    left = mask[:, :border]
    right = mask[:, -border:]

    combined = np.concatenate([
        top.flatten(),
        bottom.flatten(),
        left.flatten(),
        right.flatten()
    ])

    return np.mean(combined > 0)


def choose_auto_threshold(blur):
    """
    Try light-on-dark and dark-on-light masks.
    Pick the one that keeps the image border mostly background.
    """
    _, mask_light = cv2.threshold(
        blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )

    _, mask_dark = cv2.threshold(
        blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )

    light_border = border_foreground_fraction(mask_light)
    dark_border = border_foreground_fraction(mask_dark)

    if light_border < dark_border:
        return mask_light, "light"
    else:
        return mask_dark, "dark"


def make_packet_mask(gray, foreground, morph_frac):
    blur = cv2.GaussianBlur(gray, (5, 5), 0)

    if foreground == "light":
        _, mask = cv2.threshold(
            blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        selected_foreground = "light"

    elif foreground == "dark":
        _, mask = cv2.threshold(
            blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
        selected_foreground = "dark"

    else:
        mask, selected_foreground = choose_auto_threshold(blur)

    h, w = gray.shape[:2]
    kernel_size = make_odd_kernel_size(min(h, w) * morph_frac)
    kernel = np.ones((kernel_size, kernel_size), np.uint8)

    # Remove specks.
    clean = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    # Close small text/crease gaps inside packet areas.
    clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)

    return clean, selected_foreground


def contour_rectangularity(contour, width, height):
    rect_area = width * height
    if rect_area <= 0:
        return 0.0

    contour_area = cv2.contourArea(contour)
    return contour_area / rect_area


def detect_packets(work_bgr, args):
    gray = cv2.cvtColor(work_bgr, cv2.COLOR_BGR2GRAY)

    mask, selected_foreground = make_packet_mask(
        gray,
        foreground=args.foreground,
        morph_frac=args.morph_frac
    )

    h, w = gray.shape[:2]
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
        bbox_area = bw * bh

        if contour_area < min_area:
            continue

        if contour_area > max_area:
            continue

        if bw < min_width or bh < min_height:
            continue

        aspect = bw / float(bh)

        # Packet labels may vary, so keep this permissive.
        if aspect < 0.25 or aspect > 4.0:
            continue

        rectangularity = contour_rectangularity(contour, bw, bh)

        # Very low rectangularity usually means a shadow, ruler mark, or debris.
        if rectangularity < 0.20:
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

    detections = sort_detections_reading_order(detections)

    return detections, mask, selected_foreground


def sort_detections_reading_order(detections):
    """
    Sort detections in rough reading order:
    top-to-bottom, then left-to-right within rows.

    Since the number and placement can vary, this groups detections into rows
    based on vertical center proximity.
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
    This is useful if packets are slightly rotated.

    Note: padding is approximate here because perspective warping works from
    a rotated rectangle rather than an axis-aligned bounding box.
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

    # If OpenCV chooses the long side as height, rotate to landscape only
    # when the object is extremely vertical. Comment this out if unwanted.
    return warped


def save_debug_image(debug_bgr, detections, output_path):
    debug = debug_bgr.copy()

    for i, d in enumerate(detections, start=1):
        x, y, w, h = d["x"], d["y"], d["w"], d["h"]

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

    detections, mask, selected_foreground = detect_packets(work_bgr8, args)

    stem = image_path.stem
    image_output_dir = output_dir / stem
    image_output_dir.mkdir(parents=True, exist_ok=True)

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

        cv2.imwrite(str(out_path), crop)

        # Bounding boxes are reported in original full-image coordinates.
        manifest_writer.writerow({
            "source_image": str(image_path),
            "output_crop": str(out_path),
            "packet_index": packet_index,
            "detected_foreground": selected_foreground,
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

        save_debug_image(
            work_bgr8,
            detections,
            debug_dir / f"{stem}_debug_boxes.jpg"
        )

        cv2.imwrite(
            str(debug_dir / f"{stem}_mask.jpg"),
            mask
        )

    print(f"{image_path.name}: found {len(detections)} packets")


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