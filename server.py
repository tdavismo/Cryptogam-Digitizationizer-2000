#!/usr/bin/env python3
"""
server.py  —  Cryptogam Digitizationizer 2000  ·  FastAPI backend
------------------------------------------------------------------
Wraps the core processing operations as HTTP endpoints and serves the
single-page web frontend (the design handoff) from the web/ folder.
Runs locally (localhost:8000) so the frontend reads local files with no
upload/download overhead.

API endpoints
-------------
POST /api/preview   Detect packets in one image; return count + debug PNG
POST /api/batch     Process all images in a directory (SSE progress stream)
POST /api/fix       Re-segment or bisect a single oversized crop
GET  /api/crops     List crop images in an output directory
GET  /api/images    List source images in an input directory
GET  /api/file      Stream a local image file (thumbnails / previews)

Frontend
--------
GET  /              web/index.html + web/app/* (React-via-Babel, three skins)

Run:  python server.py     or     python launch.py   (also opens the browser)
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import csv
import json
import mimetypes
import re
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import requests
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from segmenter_core import (
    IMAGE_EXTS, MANIFEST_FIELDS,
    SegSettings, ImageResult,
    segment_image, save_crops, save_crops_detailed, flag_results,
    resegment_or_bisect, _auto_output_dir, _padded_crop, _to_bgr8,
    _imread_oriented,
)

# Correct MIME types regardless of the host's Windows registry, which can
# mis-map .css/.js to text/html or text/plain (strict browsers then reject).
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".jsx")  # Babel fetches as text


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Cryptogam Digitizationizer 2000",
    description="REST API for batch herbarium packet segmentation.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://localhost:5173",
        "http://127.0.0.1:3000", "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache_frontend(request, call_next):
    """
    Disable browser caching for the SPA assets.  The frontend uses in-browser
    Babel, which fetches the .jsx files by URL; without an explicit no-cache
    header the browser serves stale .jsx/.css heuristically and never picks up
    edits.  API responses are unaffected.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/app/") or path.endswith((".jsx", ".css", ".js", ".html")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SegSettingsIn(BaseModel):
    top_crop_frac:       float = 0.0
    foreground:          str   = "light"
    threshold_mode:      str   = "otsu"
    contrast:            str   = "none"
    adaptive_block_frac: float = 0.06
    adaptive_c:          int   = 7
    min_area_frac:       float = 0.0005
    max_area_frac:       float = 0.95
    min_width_frac:      float = 0.04
    min_height_frac:     float = 0.04
    padding:             int   = 30
    morph_frac:          float = 0.0015
    rectangularity_min:  float = 0.12
    aspect_min:          float = 0.20
    aspect_max:          float = 5.0
    deskew:              bool  = True
    auto_portrait:       bool  = False

    def to_core(self) -> SegSettings:
        return SegSettings(**self.dict())


class PreviewRequest(BaseModel):
    image_path: str
    settings:   SegSettingsIn = Field(default_factory=SegSettingsIn)


class BatchRequest(BaseModel):
    input_dir:  str
    output_dir: Optional[str] = None   # auto-generated if omitted
    settings:   SegSettingsIn = Field(default_factory=SegSettingsIn)


class FixRequest(BaseModel):
    crop_path:  str
    output_dir: str
    settings:   SegSettingsIn = Field(default_factory=SegSettingsIn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bgr_to_b64(bgr: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return base64.b64encode(buf.tobytes()).decode()


def _result_to_dict(r: ImageResult) -> dict:
    return {
        "path":        str(r.path),
        "name":        r.path.name,
        "count":       r.count,
        "flag":        r.flag,
        "flag_detail": r.flag_detail,
        "crop_info": [
            {"path": str(p), "name": p.name, "w": w, "h": h}
            for p, w, h in r.crop_info
        ],
    }


# ---------------------------------------------------------------------------
# Endpoint 1: POST /api/preview
# ---------------------------------------------------------------------------

@app.post("/api/preview", summary="Detect packets in one image", tags=["Segmentation"])
async def preview(req: PreviewRequest):
    """Run detection on one local image; return count + base64 debug PNG. No files written."""
    path = Path(req.image_path)
    if not path.is_file():
        raise HTTPException(404, f"Image not found: {path}")
    try:
        dets, debug_bgr, fg, top_px = await asyncio.to_thread(
            segment_image, path, req.settings.to_core())
    except Exception as exc:
        raise HTTPException(422, str(exc))
    return {
        "count":       len(dets),
        "foreground":  fg,
        "top_crop_px": top_px,
        "debug_b64":   _bgr_to_b64(debug_bgr),
    }


# ---------------------------------------------------------------------------
# Endpoint 2: POST /api/batch  (Server-Sent Events stream)
# ---------------------------------------------------------------------------

@app.post("/api/batch", summary="Process all images in a directory (SSE)", tags=["Segmentation"])
async def batch(req: BatchRequest):
    """
    Segment every image in input_dir, write crops to output_dir (auto-dated
    sub-folder if omitted), apply QC flagging. Streams SSE events:
      start | progress | error | done
    """
    in_dir = Path(req.input_dir)
    if not in_dir.is_dir():
        raise HTTPException(404, f"Input directory not found: {in_dir}")

    out_dir = Path(req.output_dir) if req.output_dir else _auto_output_dir(in_dir)
    s = req.settings.to_core()

    images = sorted(
        p for p in in_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not images:
        raise HTTPException(422, "No image files found in input directory.")

    async def event_stream():
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest_rows: list[dict] = []
        results: list[ImageResult] = []
        total = len(images)

        yield f"data: {json.dumps({'type':'start','total':total,'output_dir':str(out_dir)})}\n\n"

        for i, img_path in enumerate(images, 1):
            name = img_path.name
            try:
                count, _debug, crop_info, boxes, iw, ih = await asyncio.to_thread(
                    save_crops_detailed, img_path, out_dir, s, manifest_rows)
                results.append(ImageResult(path=img_path, count=count, crop_info=crop_info))
                # path/iw/ih/boxes let the frontend overlay live bounding boxes
                # on the actual source image as each one is processed
                evt = {"type": "progress", "i": i, "total": total, "name": name,
                       "count": count, "path": str(img_path),
                       "iw": iw, "ih": ih, "boxes": boxes}
            except Exception as exc:
                results.append(ImageResult(path=img_path, count=0, flag="none"))
                evt = {"type": "error", "i": i, "total": total, "name": name, "error": str(exc)}
            yield f"data: {json.dumps(evt)}\n\n"

        manifest_path = out_dir / "packet_manifest.csv"
        try:
            with manifest_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
                writer.writeheader()
                writer.writerows(manifest_rows)
        except Exception:
            manifest_path = Path("")

        # Stamp segmented_at into the audit sidecar for every crop produced.
        try:
            crop_names = [p.name for r in results for p, _w, _h in r.crop_info]
            if crop_names:
                _audit_record_segmented(out_dir, crop_names)
                # Fresh crops under these names: any earlier approval / flag /
                # redraw mark (from a previous run into this folder) no longer
                # describes them.
                _review_forget(out_dir, crop_names)
        except Exception:
            pass

        flagged = [r for r in flag_results(results) if r.flag]
        ok = len(results) - len(flagged)
        done = {
            "type": "done", "ok": ok, "total": total,
            "manifest": str(manifest_path),
            "flagged": [_result_to_dict(r) for r in flagged],
        }
        yield f"data: {json.dumps(done)}\n\n"

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Endpoint 3: POST /api/fix
# ---------------------------------------------------------------------------

@app.post("/api/fix", summary="Re-segment or bisect an oversized crop", tags=["Segmentation"])
async def fix(req: FixRequest):
    """Split a merged-packet crop. Tries re-segmentation, falls back to bisect. Deletes original on success."""
    crop_path  = Path(req.crop_path)
    output_dir = Path(req.output_dir)
    if not crop_path.is_file():
        raise HTTPException(404, f"Crop not found: {crop_path}")
    if not output_dir.is_dir():
        raise HTTPException(404, f"Output directory not found: {output_dir}")
    try:
        new_paths, description = await asyncio.to_thread(
            resegment_or_bisect, crop_path, output_dir, req.settings.to_core())
    except Exception as exc:
        raise HTTPException(422, str(exc))
    return {
        "new_paths": [str(p) for p in new_paths],
        "new_names": [p.name for p in new_paths],
        "description": description,
    }


# ---------------------------------------------------------------------------
# Endpoint 4: GET /api/crops
# ---------------------------------------------------------------------------

def _is_crop_file(p: Path) -> bool:
    """
    True for files this tool produced as packet crops, so QC never ingests a
    stray raw source image that happens to share the output folder.

    Batch crops are named  <stem>_packet_NN.ext
    Re-segment / bisect outputs keep that base and append a letter:
                           <stem>_packet_NNa.ext  (regex tolerant of suffix)
    """
    return bool(re.search(r"_packet_\d+", p.stem))


# ---------------------------------------------------------------------------
# Audit trail — a packet_audit.json sidecar written into each output folder.
# It is the persistent, portable record of what happened to each crop:
# when it was segmented, every VVGo submission attempt (timestamp, model,
# prompt, ok/error), and where the JSON landed. The actual JSON file on disk
# remains the authoritative "received" signal — the audit is history, not
# ground truth, so a deleted JSON correctly reads as needing resubmission.
# Shape:
#   { "version": 1,
#     "crops": {
#       "<crop filename>": {
#         "segmented_at": "ISO8601" | null,
#         "attempts": [ {ts, model, prompt, ok, error, json_name} ... ]
#       }, ...
#     } }
# ---------------------------------------------------------------------------

_AUDIT_NAME = "packet_audit.json"
# Serialise audit writes per-folder so concurrent submit workers don't clobber
# each other's read-modify-write of the sidecar.
_AUDIT_LOCKS: dict = {}
import threading as _threading


def _audit_lock(folder: Path) -> "_threading.Lock":
    key = str(folder)
    lk = _AUDIT_LOCKS.get(key)
    if lk is None:
        lk = _AUDIT_LOCKS[key] = _threading.Lock()
    return lk


def _audit_load(folder: Path) -> dict:
    p = folder / _AUDIT_NAME
    if not p.is_file():
        return {"version": 1, "crops": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "crops" not in data:
            return {"version": 1, "crops": {}}
        return data
    except Exception:
        return {"version": 1, "crops": {}}


def _audit_save(folder: Path, data: dict) -> None:
    try:
        (folder / _AUDIT_NAME).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _audit_record_segmented(folder: Path, crop_names: list) -> None:
    """Stamp segmented_at for crops that don't have one yet."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _audit_lock(folder):
        data = _audit_load(folder)
        crops = data.setdefault("crops", {})
        for name in crop_names:
            entry = crops.setdefault(name, {"segmented_at": None, "attempts": []})
            if not entry.get("segmented_at"):
                entry["segmented_at"] = now
        _audit_save(folder, data)


def _audit_record_attempt(folder: Path, crop_name: str, attempt: dict) -> None:
    """Append one submission attempt to a crop's history."""
    with _audit_lock(folder):
        data = _audit_load(folder)
        crops = data.setdefault("crops", {})
        entry = crops.setdefault(crop_name, {"segmented_at": None, "attempts": []})
        entry.setdefault("attempts", []).append(attempt)
        _audit_save(folder, data)


# ---------------------------------------------------------------------------
# Review sidecar — packet_review.json in each output folder.
#
# Holds what a volunteer decided and changed, so reopening a folder shows it:
#   { "version": 1,
#     "crops": { "<crop file name>": {
#         "status":      "approved" | "flagged",     # QC decision (absent = unreviewed)
#         "note":        "free text",                # flag reason
#         "reviewed_at": iso,                        # last status/note change
#         "redrawn_at":  iso, "redraw_count": n,     # boundary redrawn in Redraw
#         "added_at":    iso } } }                   # packet hand-added in Redraw
#
# Keyed by file NAME (not path) so a folder moved/copied to another PC keeps
# its review. Written only on user actions — opening a folder never writes it.
# Shares the per-folder audit lock.
# ---------------------------------------------------------------------------

_REVIEW_NAME = "packet_review.json"
_REVIEW_STATUSES = {"approved", "flagged"}


def _review_load(folder: Path) -> dict:
    p = folder / _REVIEW_NAME
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("crops"), dict):
                return data
        except Exception:
            pass
    return {"version": 1, "crops": {}}


def _review_save(folder: Path, data: dict) -> None:
    tmp = folder / (_REVIEW_NAME + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / _REVIEW_NAME)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _review_mark(folder: Path, name: str, **fields) -> None:
    """Merge fields into one crop's review entry (redrawn_at, added_at…)."""
    with _audit_lock(folder):
        data = _review_load(folder)
        entry = data["crops"].setdefault(name, {})
        if fields.pop("bump_redraw", False):
            entry["redraw_count"] = int(entry.get("redraw_count") or 0) + 1
        entry.update(fields)
        _review_save(folder, data)


def _review_forget(folder: Path, names) -> dict:
    """Drop review entries for *names*; returns what was dropped. Used when a
    crop is removed or re-segmented, so a later crop reusing the same file name
    doesn't inherit an old approval."""
    names = set(names)
    if not (folder / _REVIEW_NAME).is_file():
        return {}
    with _audit_lock(folder):
        data = _review_load(folder)
        dropped = {n: data["crops"].pop(n) for n in list(data["crops"]) if n in names}
        if dropped:
            _review_save(folder, data)
    return dropped


@app.get("/api/review", summary="Saved QC decisions + redraw marks for a folder",
         tags=["Gallery"])
async def get_review(output_dir: str = Query(..., description="PACKETS output folder")):
    """Read-only. Returns entries only for crops that exist in the folder now,
    each with its full path so the UI can key by path like /api/crops does."""
    d = Path(output_dir)
    if not d.is_dir():
        raise HTTPException(404, f"Directory not found: {d}")
    data = _review_load(d)
    out = {}
    for name, entry in data["crops"].items():
        p = d / name
        if isinstance(entry, dict) and p.is_file():
            out[name] = {**entry, "path": str(p)}
    return {"output_dir": str(d), "crops": out}


class ReviewUpdate(BaseModel):
    output_dir: str
    # { "<crop name>": { "status": "approved"|"flagged"|null, "note": str|null } }
    updates: dict


@app.post("/api/review", summary="Save QC decisions (approve / flag / note)",
          tags=["Gallery"])
async def post_review(req: ReviewUpdate):
    d = Path(req.output_dir)
    if not d.is_dir():
        raise HTTPException(404, f"Directory not found: {d}")

    def _do() -> dict:
        saved, skipped = [], []
        with _audit_lock(d):
            data = _review_load(d)
            crops = data["crops"]
            for name, upd in (req.updates or {}).items():
                # Bare file names of crops that exist here — nothing else.
                if (not isinstance(name, str) or not isinstance(upd, dict)
                        or name != _crop_basename(name) or not (d / name).is_file()):
                    skipped.append(name)
                    continue
                entry = crops.get(name, {})
                if "status" in upd:
                    st = upd["status"]
                    if st in _REVIEW_STATUSES:
                        entry["status"] = st
                    else:
                        entry.pop("status", None)
                if "note" in upd:
                    note = (upd["note"] or "").strip()
                    if note:
                        entry["note"] = upd["note"]
                    else:
                        entry.pop("note", None)
                entry["reviewed_at"] = _now_iso()
                # Drop entries that no longer carry anything worth keeping.
                if set(entry) <= {"reviewed_at"}:
                    crops.pop(name, None)
                else:
                    crops[name] = entry
                saved.append(name)
            if saved:
                _review_save(d, data)
        return {"saved": saved, "skipped": skipped}

    try:
        res = await asyncio.to_thread(_do)
    except Exception as exc:
        raise HTTPException(500, f"Could not save review: {exc}")
    return {"ok": True, **res}


@app.get("/api/crops", summary="List crop images in an output directory", tags=["Gallery"])
async def list_crops(output_dir: str = Query(..., description="Path to crops folder")):
    """
    Metadata for every *packet crop* in output_dir, sorted by name. Raw source
    images are excluded (see _is_crop_file) so they can't leak into QC review.
    """
    d = Path(output_dir)
    if not d.is_dir():
        raise HTTPException(404, f"Directory not found: {d}")
    crops = sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and _is_crop_file(p)
    )
    return {
        "output_dir": str(d),
        "count": len(crops),
        "crops": [
            # mtime lets the UI cache-bust thumbnails after Redraw rewrites a
            # crop in place (same path, new pixels).
            {"path": str(p), "name": p.name, "stem": p.stem,
             "size_bytes": (st := p.stat()).st_size, "mtime": st.st_mtime_ns}
            for p in crops
        ],
    }


def _json_name_for_crop(crop_stem: str) -> str:
    """The JSON filename a crop's submission produces (leading-_ stripped)."""
    return (crop_stem.lstrip("_") or crop_stem) + ".json"


def _json_name_candidates(crop_stem: str) -> list[str]:
    """Every JSON filename that counts as this crop's VVGo output.

    The current naming strips the leading underscore (VV Editor ignores
    _-prefixed files). The retired desktop app wrote <crop stem>.json with the
    underscore KEPT, so folders submitted through it must still read as
    received. Current name first, so it wins when both exist."""
    current = _json_name_for_crop(crop_stem)
    legacy = crop_stem + ".json"
    return [current] if legacy == current else [current, legacy]


@app.get("/api/audit",
         summary="Per-crop submission/receipt audit for an output folder",
         tags=["VVGo"])
async def audit(
    output_dir: str = Query(..., description="PACKETS output folder (holds crops)"),
    json_dir: str = Query("", description="Folder holding VVGo JSON outputs "
                          "(defaults to <output_dir>/vvgo_json)"),
):
    """
    Cross-reference every crop in *output_dir* against:
      • the packet_audit.json sidecar (submission history), and
      • the actual JSON files on disk in *json_dir* (authoritative 'received').

    Per crop returns:
      { stem, name, json_name, segmented_at,
        received (json file exists), submitted (>=1 attempt),
        last_ok, last_error, last_model, last_prompt,
        status: "received" | "needs_output" | "errored" | "not_submitted",
        attempts: [ {ts, model, prompt, ok, error, json_name} ... ] }

    And batch totals:
      { total, received, needs_output, errored, not_submitted }

    'received' is derived from the JSON file actually being present, so the
    view self-heals if outputs are deleted or were never produced.
    """
    d = Path(output_dir)
    if not d.is_dir():
        raise HTTPException(404, f"Directory not found: {d}")
    jd = Path(json_dir) if json_dir else (d / "vvgo_json")

    def _build():
        crops = sorted(
            p for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS and _is_crop_file(p))

        sidecar = _audit_load(d)
        scrops = sidecar.get("crops", {})

        # Opening a folder is strictly READ-ONLY: crops never stamped (older
        # folders) get segmented_at derived from the file's mtime in memory.
        # Only real events (segmentation, submission, add-packet) write the
        # sidecar, so viewing a previously processed folder never alters it.
        from datetime import datetime, timezone

        def _mtime_iso(p: Path):
            try:
                return datetime.fromtimestamp(
                    p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            except Exception:
                return None

        out_crops = []
        totals = {"total": 0, "received": 0, "needs_output": 0,
                  "errored": 0, "not_submitted": 0}
        for p in crops:
            # Either naming generation counts as received (see
            # _json_name_candidates); report the file that actually exists.
            cands = _json_name_candidates(p.stem)
            found = next((n for n in cands if (jd / n).is_file()), None)
            received = found is not None
            jname = found or cands[0]
            entry = dict(scrops.get(p.name) or {"segmented_at": None, "attempts": []})
            if not entry.get("segmented_at"):
                entry["segmented_at"] = _mtime_iso(p)
            attempts = entry.get("attempts", [])
            last = attempts[-1] if attempts else None
            submitted = len(attempts) > 0

            if received:
                status = "received"
            elif last and not last.get("ok"):
                status = "errored"
            elif submitted:
                status = "needs_output"   # attempted but no JSON on disk
            else:
                status = "not_submitted"

            totals["total"] += 1
            totals[status] = totals.get(status, 0) + 1

            out_crops.append({
                "stem": p.stem, "name": p.name, "json_name": jname,
                "path": str(p),
                "segmented_at": entry.get("segmented_at"),
                "received": received,
                "submitted": submitted,
                "last_ok": bool(last and last.get("ok")),
                "last_error": (last or {}).get("error"),
                "last_model": (last or {}).get("model"),
                "last_prompt": (last or {}).get("prompt"),
                "status": status,
                "attempts": attempts,
            })
        return out_crops, totals

    try:
        out_crops, totals = await asyncio.to_thread(_build)
    except Exception as exc:
        raise HTTPException(500, f"Could not build audit: {exc}")

    return {"output_dir": str(d), "json_dir": str(jd),
            "totals": totals, "crops": out_crops}


@app.get("/api/session-from-folder",
         summary="Reconstruct a review session from a processed output folder",
         tags=["Gallery"])
async def session_from_folder(output_dir: str = Query(..., description="Existing PACKETS output folder")):
    """
    Rebuild the session object the frontend normally gets from a fresh batch,
    so a user can re-open a previously-segmented folder and go straight to QC /
    Redraw / VVGo without re-running segmentation.

    Reads packet_manifest.csv to recover per-source crop dimensions, re-derives
    the oversize / zero-detection flags with the same flag_results() logic the
    batch uses, and returns:
      { outputDir, sources, packets, flagged: [...], gridRows, gridCols,
        hasManifest }
    The flagged[] entries match the batch SSE 'done' event shape exactly.
    """
    d = Path(output_dir)
    if not d.is_dir():
        raise HTTPException(404, f"Directory not found: {d}")

    crops = [p for p in d.iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTS and _is_crop_file(p)]
    if not crops:
        raise HTTPException(422, "No packet crops found in that folder.")

    mf = d / "packet_manifest.csv"

    def _build():
        # Group crop dimensions by source image. Prefer the manifest (exact
        # original dims); fall back to reading each crop file if absent.
        by_source: dict = {}          # source_name → list[(Path, w, h)]
        have_manifest = mf.is_file()
        if have_manifest:
            with mf.open("r", encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    out_crop = Path(row.get("output_crop", ""))
                    # Manifest may reference an old absolute path; re-home onto
                    # this folder by filename so a moved folder still works.
                    local = d / out_crop.name
                    if not local.is_file():
                        continue
                    src_name = Path(row.get("source_image", out_crop.stem)).name
                    try:
                        w = int(row["bbox_w"]); h = int(row["bbox_h"])
                    except Exception:
                        # No dims in manifest row — read the crop file.
                        im = cv2.imread(str(local))
                        if im is None:
                            continue
                        h, w = im.shape[:2]
                    by_source.setdefault(src_name, []).append((local, w, h))
        else:
            # No manifest: infer the source group from the crop stem prefix
            # (everything before _packet_) and read dims off each file.
            for p in crops:
                m = re.match(r"^(.*)_packet_\d+", p.stem)
                src_name = (m.group(1) if m else p.stem) + ".src"
                im = cv2.imread(str(p))
                if im is None:
                    continue
                h, w = im.shape[:2]
                by_source.setdefault(src_name, []).append((p, w, h))

        results = [
            ImageResult(path=Path(src), count=len(infos), crop_info=infos)
            for src, infos in by_source.items()
        ]
        flagged = [r for r in flag_results(results) if r.flag]
        packets = sum(len(infos) for infos in by_source.values())
        return have_manifest, len(by_source), packets, flagged

    try:
        have_manifest, sources, packets, flagged = await asyncio.to_thread(_build)
    except Exception as exc:
        raise HTTPException(500, f"Could not read folder: {exc}")

    return {
        "outputDir": str(d),
        "sources": sources,
        "packets": packets,
        "hasManifest": have_manifest,
        "flagged": [_result_to_dict(r) for r in flagged],
    }


# ---------------------------------------------------------------------------
# Endpoint 5: GET /api/images
# ---------------------------------------------------------------------------

@app.get("/api/images", summary="List source images in an input directory", tags=["Segmentation"])
async def list_images(input_dir: str = Query(..., description="Path to source image folder")):
    """Metadata for every image in input_dir. Used to pick the preview image and count a batch."""
    d = Path(input_dir)
    if not d.is_dir():
        raise HTTPException(404, f"Directory not found: {d}")
    images = sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    return {
        "input_dir": str(d),
        "count": len(images),
        "images": [{"path": str(p), "name": p.name, "stem": p.stem} for p in images],
    }


# ---------------------------------------------------------------------------
# Endpoint 6: GET /api/file  — stream a local image file to the browser
# ---------------------------------------------------------------------------

# Encoded-bytes cache for the EXIF-oriented /api/file path, keyed by
# (path, mtime_ns, auto_portrait). Bounded in the handler.
_ORIENTED_CACHE: dict = {}


@app.get("/api/file", summary="Serve a local image file", tags=["Gallery"])
async def serve_file(
    path: str = Query(..., description="Absolute path to an image file"),
    oriented: int = Query(
        1, description="1 = bake EXIF orientation into bytes before serving "
                       "(default); 0 = serve raw file"),
    auto_portrait: int = Query(
        0, description="1 = also rotate landscape→portrait"),
    request_if_none_match: str = Header(None, alias="If-None-Match"),
):
    """
    Stream one image file from disk for thumbnails / previews.

    By default the bytes are EXIF-normalised before sending so the browser
    displays the same pixel orientation that our segmentation operated on —
    SVG overlays (bounding boxes, dim masks) line up with the image.

    Set oriented=0 to serve the raw file (faster path; safe for output crops
    written by cv2.imwrite, which never have EXIF). NOTE: localhost
    single-user tool; restrict to an output root if this ever becomes a
    hosted multi-user service.
    """
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, f"File not found: {p}")
    if p.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(415, f"Not an image file: {p.name}")

    if not oriented:
        return FileResponse(str(p))

    # Cache the EXIF-normalised encode keyed by (path, mtime, flags). The
    # Redraw editor reloads the same multi-MB source every time the user steps
    # between crops of one scan; without this each switch re-decoded + re-
    # encoded it, which was the visible lag. The mtime in the key means an
    # edited source still busts the cache.
    mtime = p.stat().st_mtime_ns
    ckey = (str(p), mtime, bool(auto_portrait))
    data = _ORIENTED_CACHE.get(ckey)
    if data is None:
        def _encode() -> bytes:
            bgr = _imread_oriented(p, auto_portrait=bool(auto_portrait))
            # Match the source's compression style: JPEG for jpeg, PNG otherwise.
            ext = ".jpg" if p.suffix.lower() in (".jpg", ".jpeg") else ".png"
            ok, buf = cv2.imencode(ext, bgr,
                [int(cv2.IMWRITE_JPEG_QUALITY), 88] if ext == ".jpg" else [])
            if not ok:
                raise RuntimeError("encode failed")
            return buf.tobytes()
        try:
            data = await asyncio.to_thread(_encode)
        except Exception:
            # Fall back to raw bytes — better to show something than 500
            return FileResponse(str(p))
        # Bounded cache: drop oldest when full so a huge batch can't grow it
        # without limit (sources are a few MB each; keep ~24).
        if len(_ORIENTED_CACHE) >= 24:
            _ORIENTED_CACHE.pop(next(iter(_ORIENTED_CACHE)))
        _ORIENTED_CACHE[ckey] = data

    media = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    # ETag lets the browser skip re-downloading on subsequent loads of the
    # same crop; the encode itself is already cached above for server-side hits.
    etag = f'"{abs(hash(ckey))}"'
    if request_if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag,
                        "Cache-Control": "private, max-age=300"})
    return Response(content=data, media_type=media,
                    headers={"ETag": etag,
                             "Cache-Control": "private, max-age=300"})


@app.get("/api/json", summary="Read a JSON file from disk", tags=["Gallery"])
async def serve_json(path: str = Query(..., description="Absolute path to a .json file")):
    """
    Return the parsed contents of a local .json file for the in-app previewer.
    Used by the VVGo submission table's "View" action so the user sees the
    transcription record inside the app instead of the browser trying to render
    a JSON file as an image.
    """
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, f"File not found: {p}")
    if p.suffix.lower() != ".json":
        raise HTTPException(415, f"Not a JSON file: {p.name}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(422, f"Could not parse JSON: {exc}")
    return {"path": str(p), "name": p.name, "data": data}


# ---------------------------------------------------------------------------
# Endpoint 7: GET /api/pick-folder  — open a native folder dialog
# ---------------------------------------------------------------------------

@app.get("/api/pick-folder", summary="Open a native folder picker", tags=["Local"])
async def pick_folder(title: str = Query("Choose folder", description="Dialog title"),
                      initial: str = Query("", description="Initial directory")):
    """
    Open the OS folder-picker dialog and return the chosen absolute path.

    Tkinter runs the dialog on the server (= the user's own machine for the
    intended localhost deployment). The dialog must run on the main thread on
    Windows, so we spawn a hidden Tk root, modal-lock it, and tear it down
    immediately. Returns {"path": ""} when the user cancels.
    """
    def _open():
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            initdir = initial if initial and Path(initial).is_dir() else None
            chosen = filedialog.askdirectory(
                title=title, initialdir=initdir, parent=root, mustexist=True)
        finally:
            root.destroy()
        return chosen or ""

    try:
        chosen = await asyncio.to_thread(_open)
    except Exception as exc:
        raise HTTPException(500, f"Folder picker failed: {exc}")
    return {"path": chosen}


# ---------------------------------------------------------------------------
# Endpoints 8 & 9 — Redraw Boundary support
# ---------------------------------------------------------------------------
#
# /api/manifest  reads packet_manifest.csv and returns the per-crop detection
#                box (in full-source-image pixel coords — bbox_y is already
#                offset past the top_crop strip) plus the source image path
#                and dimensions, so the editor can render the source and the
#                box overlaid in the correct location.
#
# /api/redraw    crops a region of a source image with optional padding and
#                writes the result to an output path (typically overwriting an
#                existing crop produced by the batch run).

@app.get("/api/manifest",
         summary="Read packet_manifest.csv as a crop_path → box map",
         tags=["Redraw"])
async def get_manifest(output_dir: str = Query(..., description="Batch output folder")):
    """
    Returns:
      {
        "output_dir": "...",
        "count": N,
        "crops": {
          "<absolute crop path>": {
            "source_path": "...",
            "source_w": int, "source_h": int,
            "x": int, "y": int, "w": int, "h": int,    # bbox in full-source coords
            "padding": int | null,                      # recovered when deskewed=False
            "deskewed": bool, "packet_index": int
          }, ...
        }
      }
    """
    d = Path(output_dir)
    mf = d / "packet_manifest.csv"
    if not mf.is_file():
        raise HTTPException(404, f"Manifest not found: {mf}")

    def _basename(s: str) -> str:
        # Manifests may carry Windows paths from another machine; split on
        # both separators so the filename is recovered on any OS.
        return re.split(r"[\\/]", s or "")[-1]

    def _read():
        out: dict = {}
        with mf.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                # Re-home onto THIS folder by filename. The manifest records
                # absolute paths from segmentation time, which are wrong once a
                # folder is moved or copied to another machine. Keys must equal
                # the real crop paths (what QC hands Redraw), and rows whose crop
                # no longer exists (e.g. replaced by a re-segment) are skipped.
                rec_crop = row.get("output_crop", "")
                local = d / _basename(rec_crop)
                if local.is_file():
                    crop = str(local)
                elif rec_crop and Path(rec_crop).is_file():
                    crop = rec_crop
                else:
                    continue
                src = Path(row.get("source_image", ""))
                if not src.is_file():
                    # Default batches put the output folder inside the source
                    # folder, so look beside / inside this folder for the image.
                    sname = _basename(row.get("source_image", ""))
                    for cand in (d.parent / sname, d / sname):
                        if sname and cand.is_file():
                            src = cand
                            break
                try:
                    bx, by, bw, bh = (int(row["bbox_x"]), int(row["bbox_y"]),
                                      int(row["bbox_w"]), int(row["bbox_h"]))
                except Exception:
                    continue
                deskewed = str(row.get("deskewed", "")).strip().lower() in ("true", "1", "yes")
                padding = None
                try:
                    if not deskewed and row.get("crop_x1") not in (None, ""):
                        padding = max(0, bx - int(row["crop_x1"]))
                except Exception:
                    pass
                # Source dimensions — must use the same EXIF orientation as
                # /api/file (and the segmenter) so the Redraw bbox math
                # operates in the same coord space the browser displays.
                if src not in _MANIFEST_DIM_CACHE:
                    try:
                        img = _imread_oriented(src)
                        _MANIFEST_DIM_CACHE[src] = (img.shape[1], img.shape[0])
                    except Exception:
                        _MANIFEST_DIM_CACHE[src] = (0, 0)
                sw, sh = _MANIFEST_DIM_CACHE[src]
                out[crop] = {
                    "source_path": str(src),
                    "source_w": sw, "source_h": sh,
                    "x": bx, "y": by, "w": bw, "h": bh,
                    "padding": padding, "deskewed": deskewed,
                    "packet_index": int(row.get("packet_index") or 0),
                }
        return out

    try:
        crops = await asyncio.to_thread(_read)
    except Exception as exc:
        raise HTTPException(500, f"Could not read manifest: {exc}")
    return {"output_dir": str(d), "count": len(crops), "crops": crops}


# Module-level cache keyed by source path — avoids reading the same multi-MB
# image off disk for every row when a batch has dozens of crops per source.
_MANIFEST_DIM_CACHE: dict = {}


# ---------------------------------------------------------------------------
# Manifest editing — shared by Redraw (update a row), add-packet (append) and
# remove-packets (drop rows). All edits keep the file's OWN header so older
# manifest generations (extra / reordered columns) are never corrupted, and go
# through one lock so concurrent requests can't interleave writes.
# ---------------------------------------------------------------------------

_MANIFEST_LOCK = _threading.Lock()


def _crop_basename(s: str) -> str:
    """Filename from a manifest path recorded on any OS."""
    return re.split(r"[\\/]", s or "")[-1]


def _manifest_read(mf: Path) -> tuple[list[str], list[dict]]:
    with mf.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        return list(reader.fieldnames or MANIFEST_FIELDS), rows


def _manifest_write(mf: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """Write atomically (temp file + replace) so a crash can't truncate it."""
    tmp = mf.with_name(mf.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, restval="", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(mf)


class RedrawRequest(BaseModel):
    source_path: str
    output_path: str
    x: int
    y: int
    w: int
    h: int
    padding: int = 0


@app.post("/api/redraw",
          summary="Crop a region of a source image and write the result",
          tags=["Redraw"])
async def redraw(req: RedrawRequest):
    """
    Re-crop *source_path* at the supplied bounding box (full-image coords)
    with optional extra padding on each side, write the result to
    *output_path*. Typically called to overwrite an existing crop file
    produced by the batch.
    """
    src = Path(req.source_path)
    out = Path(req.output_path)
    if not src.is_file():
        raise HTTPException(404, f"Source not found: {src}")

    def _do() -> dict:
        # Use the EXIF-normalised reader so the box coords from the editor
        # (which sees /api/file's normalised bytes) map correctly here.
        img = _imread_oriented(src)
        ih, iw = img.shape[:2]
        if not (0 <= req.x < iw and 0 <= req.y < ih and req.w > 0 and req.h > 0):
            raise ValueError(
                f"Box {req.x},{req.y} {req.w}x{req.h} is outside the source "
                f"({iw}x{ih}).")
        # Reuse the same padded-crop helper the desktop / batch path uses
        crop, (cx1, cy1, cx2, cy2) = _padded_crop(
            img, req.x, req.y, req.w, req.h, max(0, int(req.padding)))
        if crop is None or crop.size == 0:
            raise ValueError("Crop produced an empty image.")
        out.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out), crop):
            raise IOError(f"Could not write: {out}")

        # Persist the new box to the manifest. Without this the crop image
        # changed but Redraw (and its ghost outlines) re-read the OLD box the
        # next time the folder or tab was opened.
        manifest_updated = False
        mf = out.parent / "packet_manifest.csv"
        if mf.is_file():
            with _MANIFEST_LOCK:
                fieldnames, rows = _manifest_read(mf)
                for row in rows:
                    if _crop_basename(row.get("output_crop", "")) == out.name:
                        row.update({
                            "bbox_x": req.x, "bbox_y": req.y,
                            "bbox_w": req.w, "bbox_h": req.h,
                            "crop_x1": cx1, "crop_y1": cy1,
                            "crop_x2": cx2, "crop_y2": cy2,
                            # The redrawn crop is a plain axis-aligned cut.
                            "deskewed": False,
                        })
                        manifest_updated = True
                if manifest_updated:
                    _manifest_write(mf, fieldnames, rows)

        # Remember it was redrawn — QC shows a marker on the card.
        try:
            _review_mark(out.parent, out.name, redrawn_at=_now_iso(), bump_redraw=True)
        except Exception:
            pass

        return {"out_w": int(crop.shape[1]), "out_h": int(crop.shape[0]),
                "crop_x1": int(cx1), "crop_y1": int(cy1),
                "crop_x2": int(cx2), "crop_y2": int(cy2),
                "manifest_updated": manifest_updated}

    try:
        info = await asyncio.to_thread(_do)
    except (ValueError, IOError) as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Redraw failed: {exc}")
    return {"ok": True, "output_path": str(out), **info}


class AddPacketRequest(BaseModel):
    output_dir: str
    source_path: str
    x: int
    y: int
    w: int
    h: int
    padding: int = 0


@app.post("/api/add-packet",
          summary="Create a NEW packet crop from a hand-drawn box on a source image",
          tags=["Redraw"])
async def add_packet(req: AddPacketRequest):
    """
    Crop *source_path* at a user-drawn box and write it as a brand-new packet
    crop in *output_dir* — for when a source image holds two packets but only
    one was detected by the batch. Mirrors the batch's file naming
    (<source_stem>_packet_NN.<ext>, next free index) and appends a row to
    packet_manifest.csv so the new crop participates in Redraw / QC / audit
    exactly like a detected one.

    Returns the crop path plus a manifest entry in the same shape /api/manifest
    yields, so the frontend can splice it into its in-memory manifest.
    """
    src = Path(req.source_path)
    out_dir = Path(req.output_dir)
    if not src.is_file():
        raise HTTPException(404, f"Source not found: {src}")
    if not out_dir.is_dir():
        raise HTTPException(404, f"Output directory not found: {out_dir}")

    def _do() -> dict:
        img = _imread_oriented(src)
        ih, iw = img.shape[:2]
        if not (0 <= req.x < iw and 0 <= req.y < ih and req.w > 0 and req.h > 0):
            raise ValueError(
                f"Box {req.x},{req.y} {req.w}x{req.h} is outside the source "
                f"({iw}x{ih}).")

        # Next free packet index for THIS source: highest existing
        # <stem>_packet_NN in the folder, plus one (so we never collide with a
        # detected crop or a previously hand-added one).
        stem = src.stem
        suffix = src.suffix.lower()
        max_idx = 0
        pat = re.compile(re.escape(stem) + r"_packet_(\d+)")
        for p in out_dir.iterdir():
            if not p.is_file():
                continue
            m = pat.match(p.stem)
            if m:
                try:
                    max_idx = max(max_idx, int(m.group(1)))
                except ValueError:
                    pass
        idx = max_idx + 1
        out = out_dir / f"{stem}_packet_{idx:02d}{suffix}"

        crop, (cx1, cy1, cx2, cy2) = _padded_crop(
            img, req.x, req.y, req.w, req.h, max(0, int(req.padding)))
        if crop is None or crop.size == 0:
            raise ValueError("Crop produced an empty image.")
        if not cv2.imwrite(str(out), crop):
            raise IOError(f"Could not write: {out}")

        # Append a manifest row mirroring save_crops_detailed (manual packet:
        # no detector stats; top_crop_px=0 since coords are already full-image).
        row = {k: "" for k in MANIFEST_FIELDS}
        row.update({
            "source_image": str(src), "output_crop": str(out),
            "packet_index": idx, "detected_foreground": "manual",
            "top_crop_px": 0,
            "bbox_x": req.x, "bbox_y": req.y, "bbox_w": req.w, "bbox_h": req.h,
            "crop_x1": cx1, "crop_y1": cy1, "crop_x2": cx2, "crop_y2": cy2,
            "deskewed": False,
        })
        mf = out_dir / "packet_manifest.csv"
        with _MANIFEST_LOCK:
            existed = mf.is_file()
            # Append using the manifest's OWN header. Older generations used a
            # different column set/order (segment_packets_updated.py inserts
            # threshold_mode + contrast), so writing with MANIFEST_FIELDS would
            # shift every value into the wrong column and corrupt the file.
            fieldnames = MANIFEST_FIELDS
            if existed:
                with mf.open("r", encoding="utf-8", newline="") as fh:
                    existing = next(csv.reader(fh), None)
                if existing:
                    fieldnames = existing
            with mf.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames,
                                        restval="", extrasaction="ignore")
                if not existed:
                    writer.writeheader()
                writer.writerow(row)

        try:
            _audit_record_segmented(out_dir, [out.name])
        except Exception:
            pass
        try:
            # Clear anything left under this name by a removed crop, then mark
            # it as hand-added so QC can show it.
            _review_forget(out_dir, [out.name])
            _review_mark(out_dir, out.name, added_at=_now_iso())
        except Exception:
            pass

        return {
            "output_path": str(out), "name": out.name,
            "entry": {
                "source_path": str(src), "source_w": int(iw), "source_h": int(ih),
                "x": int(req.x), "y": int(req.y), "w": int(req.w), "h": int(req.h),
                "padding": max(0, int(req.padding)), "deskewed": False,
                "packet_index": idx,
            },
        }

    try:
        info = await asyncio.to_thread(_do)
    except (ValueError, IOError) as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Add packet failed: {exc}")
    return {"ok": True, **info}


class RemovePacketsRequest(BaseModel):
    output_dir: str
    paths: list[str] = Field(..., min_length=1)
    json_dir: str = ""


REMOVED_DIRNAME = "_removed"


@app.post("/api/remove-packets",
          summary="Remove packet crops from a batch (recoverably)",
          tags=["Redraw"])
async def remove_packets(req: RemovePacketsRequest):
    """
    Take bad segmentations out of a batch WITHOUT destroying anything:

      • each crop image moves to <output_dir>/_removed/
      • its packet_manifest.csv row moves to _removed/removed_manifest_rows.csv
        (same header, so a row can be pasted back to restore it)
      • any VVGo JSON already produced for it moves to _removed/vvgo_json/, so
        the record of a discarded crop doesn't linger beside the good ones

    Everything that lists crops only looks at files directly in output_dir, so
    the _removed subfolder is invisible to QC / Redraw / VVGo. The submission
    history in packet_audit.json is left as-is.
    """
    out_dir = Path(req.output_dir)
    if not out_dir.is_dir():
        raise HTTPException(404, f"Output directory not found: {out_dir}")
    json_dir = Path(req.json_dir) if req.json_dir else out_dir / "vvgo_json"

    def _free(dest: Path) -> Path:
        """dest, or dest with a numeric suffix if something is already there
        (e.g. a re-added packet_05 removed a second time)."""
        if not dest.exists():
            return dest
        n = 2
        while True:
            cand = dest.with_name(f"{dest.stem}~{n}{dest.suffix}")
            if not cand.exists():
                return cand
            n += 1

    def _do() -> dict:
        targets: list[Path] = []
        for s in req.paths:
            p = Path(s)
            # Only crops that live directly in this batch folder — never let a
            # request move arbitrary files from elsewhere on disk.
            try:
                same_dir = p.parent.resolve() == out_dir.resolve()
            except Exception:
                same_dir = False
            if not same_dir or not p.is_file() or p.suffix.lower() not in IMAGE_EXTS \
                    or not _is_crop_file(p):
                raise ValueError(f"Not a crop in this folder: {p}")
            targets.append(p)

        rdir = out_dir / REMOVED_DIRNAME
        rdir.mkdir(exist_ok=True)
        names = {p.name for p in targets}

        # 1. Manifest rows out first: if anything later fails, the worst case
        #    is a crop with no manifest row (Redraw just skips it), never a
        #    manifest row pointing at a crop that has vanished mid-move.
        removed_rows = 0
        mf = out_dir / "packet_manifest.csv"
        if mf.is_file():
            with _MANIFEST_LOCK:
                fieldnames, rows = _manifest_read(mf)
                keep = [r for r in rows if _crop_basename(r.get("output_crop", "")) not in names]
                gone = [r for r in rows if _crop_basename(r.get("output_crop", "")) in names]
                if gone:
                    log = rdir / "removed_manifest_rows.csv"
                    new_log = not log.is_file()
                    log_fields = fieldnames
                    if not new_log:
                        with log.open("r", encoding="utf-8", newline="") as fh:
                            log_fields = next(csv.reader(fh), None) or fieldnames
                    with log.open("a", encoding="utf-8", newline="") as fh:
                        w = csv.DictWriter(fh, fieldnames=log_fields,
                                           restval="", extrasaction="ignore")
                        if new_log:
                            w.writeheader()
                        w.writerows(gone)
                    _manifest_write(mf, fieldnames, keep)
                    removed_rows = len(gone)

        # 2. Move the crops (and any JSON outputs) aside.
        moved, jsons = [], []
        for p in targets:
            dest = _free(rdir / p.name)
            p.replace(dest)
            moved.append({"from": str(p), "to": str(dest)})
            for jn in _json_name_candidates(p.stem):
                jp = json_dir / jn
                if jp.is_file():
                    jdest_dir = rdir / "vvgo_json"
                    jdest_dir.mkdir(exist_ok=True)
                    jdest = _free(jdest_dir / jn)
                    jp.replace(jdest)
                    jsons.append(str(jdest))

        # 3. Review + submission-history entries go with them, so a packet
        #    later added under the same file name starts clean instead of
        #    inheriting this one's approval or VVGo attempts.
        records = {}
        for name, entry in _review_forget(out_dir, names).items():
            records.setdefault(name, {})["review"] = entry
        popped: dict = {}
        if (out_dir / _AUDIT_NAME).is_file():
            with _audit_lock(out_dir):
                data = _audit_load(out_dir)
                acrops = data.get("crops", {})
                popped = {n: acrops.pop(n) for n in list(acrops) if n in names}
                if popped:
                    _audit_save(out_dir, data)
            for name, entry in popped.items():
                records.setdefault(name, {})["audit"] = entry
        if records:
            rec_path = rdir / "removed_records.json"
            try:
                log = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.is_file() else []
            except Exception:
                log = []
            when = _now_iso()
            for name, rec in records.items():
                log.append({"name": name, "removed_at": when, **rec})
            rec_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")

        return {"removed": moved, "manifest_rows_removed": removed_rows,
                "json_moved": jsons, "removed_dir": str(rdir)}

    try:
        info = await asyncio.to_thread(_do)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Remove failed: {exc}")
    return {"ok": True, **info}


# ---------------------------------------------------------------------------
# VoucherVision Go — constants, persisted config, prompts, submit SSE
# ---------------------------------------------------------------------------
#
# The browser does NOT talk to VVGo directly: the backend proxies the calls so
# the API token stays on the user's machine and the frontend doesn't need CORS
# access to the cloud API.  The config file (.cryptogam_config.json) is the
# same one the retired desktop app used, so saved tokens carry over.

VVGO_SERVER_URL     = "https://vouchervision-go-738307415303.us-central1.run.app/"
VVGO_DEFAULT_PROMPT = "SLTPvM_default.yaml"


def _vvgo_auth_headers(token: str) -> dict:
    """
    Build the auth header VVGo expects.  The official VVGo client distinguishes
    two credential types (VoucherVision.py):
      * a Firebase ID token — a JWT, which contains dots and is long
        (>100 chars) — sent as  Authorization: Bearer <token>
      * a plain API key — sent as  X-API-Key: <token>
    Sending Bearer for an API key returns 401, which is the bug we hit on
    /process. /prompts happened to accept either, masking the issue.
    """
    token = (token or "").strip()
    if "." in token and len(token) > 100:
        return {"Authorization": f"Bearer {token}"}
    return {"X-API-Key": token}
VVGO_MODELS = [
    "gemini-3.1-flash-lite-preview",   # fast, unlimited — default
    "gemini-3-flash-preview",          # fast, good quality
    "gemini-3.1-pro-preview",          # highest quality, rate-limited
]
VVGO_MODEL_TIPS = {
    "gemini-3.1-flash-lite-preview": "Fast · Unlimited usage · Recommended for large batches",
    "gemini-3-flash-preview":         "Fast · Good quality",
    "gemini-3.1-pro-preview":         "Highest quality · Rate-limited — use for spot-checks",
}

_APP_CFG_PATH = Path.home() / ".cryptogam_config.json"

# Keys we expose through /api/config — every other key in the file is ignored
# (some belong to the desktop GUI's settings persistence).
_CFG_KEYS = {
    "vvgo_token", "vvgo_model", "vvgo_prompt", "vvgo_json_dir",
    "vvgo_workers", "vvgo_ocr", "vvgo_wfo", "vvgo_cop90", "vvgo_ocr_only",
    "setup_rows", "setup_cols", "setup_auto_portrait",
    # Session Setup — remembered between app restarts. The OUTPUT folder is
    # deliberately not stored: blank means "new dated folder", and reusing
    # yesterday's folder would make the next run write into it.
    "setup_folder", "setup_foreground", "setup_threshold", "setup_contrast",
    "setup_deskew", "setup_top_crop", "setup_padding", "setup_min_area",
}


def _cfg_load() -> dict:
    try:
        return json.loads(_APP_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cfg_save(updates: dict) -> None:
    cfg = _cfg_load()
    cfg.update(updates)
    try:
        _APP_CFG_PATH.write_text(
            json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


@app.get("/api/config", summary="Read persisted VVGo config", tags=["VVGo"])
async def get_config():
    """
    Return the persisted VVGo settings (token, model, prompt, advanced flags).
    Keys not yet saved are omitted. Localhost-only single-user tool — the
    token is returned to the browser so the field can pre-populate, matching
    the desktop dialog's behaviour.
    """
    cfg = _cfg_load()
    return {k: v for k, v in cfg.items() if k in _CFG_KEYS}


@app.put("/api/config", summary="Persist VVGo config updates", tags=["VVGo"])
async def put_config(updates: dict):
    """Merge whitelisted keys into the persisted config file."""
    safe = {k: v for k, v in (updates or {}).items() if k in _CFG_KEYS}
    if not safe:
        raise HTTPException(400, "No recognised config keys in payload.")
    _cfg_save(safe)
    return {"ok": True, "saved": sorted(safe.keys())}


@app.get("/api/vvgo-prompts",
         summary="Fetch the VVGo prompt list (proxied)", tags=["VVGo"])
async def vvgo_prompts(token: str = Query(..., description="VVGo bearer token")):
    """
    Proxy GET {VVGO_SERVER_URL}prompts with the user's token. The server reply
    shape varies (list vs dict), so we normalise to a flat list of names.
    """
    def _do():
        r = requests.get(
            f"{VVGO_SERVER_URL}prompts",
            headers=_vvgo_auth_headers(token),
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()

        def _name_of(item):
            """Pull a clean prompt name out of whatever shape an item takes."""
            if isinstance(item, str):
                return item
            if isinstance(item, dict):
                for k in ("filename", "name", "prompt", "id", "title", "file"):
                    v = item.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
            return None

        if isinstance(data, list):
            names = [n for n in (_name_of(p) for p in data) if n]
        elif isinstance(data, dict):
            # Either {"prompts": [...]} / {"names": [...]} or a name-keyed map.
            inner = data.get("prompts", data.get("names"))
            if isinstance(inner, list):
                names = [n for n in (_name_of(p) for p in inner) if n]
            elif isinstance(inner, dict):
                names = [str(k) for k in inner.keys()]
            else:
                names = [str(k) for k in data.keys()]
        else:
            names = []
        # De-dup while preserving order
        seen, uniq = set(), []
        for n in names:
            if n not in seen:
                seen.add(n); uniq.append(n)
        return uniq or [VVGO_DEFAULT_PROMPT]

    try:
        names = await asyncio.to_thread(_do)
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code if exc.response else 502,
                            f"VVGo /prompts failed: {exc}")
    except Exception as exc:
        raise HTTPException(502, f"VVGo /prompts failed: {exc}")
    return {"prompts": names}


@app.post("/api/vvgo-submit",
          summary="Submit crops to VoucherVision Go (SSE)", tags=["VVGo"])
async def vvgo_submit(body: dict):
    """
    Submit a list of crop image paths to the VVGo /process endpoint in
    parallel, writing one JSON file per crop into `json_dir`. Streams
    Server-Sent Events:

        {type:"start",    total:N, json_dir:"..."}
        {type:"progress", i, total, name, ok:true,  json_path:"..."}
        {type:"progress", i, total, name, ok:false, error:"..."}
        {type:"done",     ok, total, errors}

    Expected request body:
        {
          token, model, prompt, json_dir, crop_paths: [...],
          ocr_engine (or "" for "Same as LLM"),
          include_wfo (bool), include_cop90 (bool), ocr_only (bool),
          max_workers (int, default 4)
        }
    """
    token       = (body or {}).get("token", "").strip()
    if not token:
        raise HTTPException(400, "Missing VVGo API token.")
    crop_paths  = body.get("crop_paths") or []
    if not crop_paths:
        raise HTTPException(400, "No crop paths supplied.")
    json_dir    = Path(body.get("json_dir") or "")
    if not str(json_dir):
        raise HTTPException(400, "Missing json_dir.")
    json_dir.mkdir(parents=True, exist_ok=True)

    model       = body.get("model")       or VVGO_MODELS[0]
    prompt      = body.get("prompt")      or VVGO_DEFAULT_PROMPT
    ocr_engine  = (body.get("ocr_engine") or "").strip()
    include_wfo = bool(body.get("include_wfo", False))
    include_cop = bool(body.get("include_cop90", False))
    ocr_only    = bool(body.get("ocr_only", False))
    max_workers = max(1, min(16, int(body.get("max_workers") or 4)))

    post_data: dict = {"prompt": prompt, "skip_label_collage": "true"}
    if model != VVGO_MODELS[0]:
        post_data["llm_model"] = model
    if ocr_engine:
        post_data["engines"] = json.dumps([ocr_engine])
    if include_wfo:
        post_data["include_wfo"] = "true"
    if include_cop:
        post_data["include_cop90"] = "true"
    if ocr_only:
        post_data["ocr_only"] = "true"

    paths: list[Path] = [Path(p) for p in crop_paths]
    # Validate up front — much better than discovering mid-stream
    for p in paths:
        if not p.is_file():
            raise HTTPException(404, f"Crop not found: {p}")

    from datetime import datetime, timezone

    def _process_one(p: Path) -> dict:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        crop_folder = p.parent
        # VoucherVision Editor ignores files whose names start with "_"
        # (it treats leading-underscore entries as hidden), and our crops
        # are named "_DSC0002_packet_01". Strip leading underscores from
        # the JSON filename so the Editor lists the folder correctly.
        json_stem = p.stem.lstrip("_") or p.stem
        out_path = json_dir / f"{json_stem}.json"
        try:
            with p.open("rb") as fh:
                r = requests.post(
                    f"{VVGO_SERVER_URL}process",
                    headers=_vvgo_auth_headers(token),
                    files={"file": (p.name, fh, "image/jpeg")},
                    data=post_data,
                    timeout=180,
                )
            r.raise_for_status()
            out_path.write_text(
                json.dumps(r.json(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            _audit_record_attempt(crop_folder, p.name, {
                "ts": ts, "model": model, "prompt": prompt,
                "ok": True, "error": None, "json_name": out_path.name})
            return {"name": p.name, "path": str(p), "ok": True,
                    "json_path": str(out_path)}
        except Exception as exc:
            _audit_record_attempt(crop_folder, p.name, {
                "ts": ts, "model": model, "prompt": prompt,
                "ok": False, "error": str(exc), "json_name": out_path.name})
            return {"name": p.name, "path": str(p), "ok": False,
                    "error": str(exc)}

    total = len(paths)

    async def event_stream():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def runner():
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max_workers) as ex:
                futs = {ex.submit(_process_one, p): p for p in paths}
                for fut in concurrent.futures.as_completed(futs):
                    res = fut.result()
                    loop.call_soon_threadsafe(queue.put_nowait, res)
            loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

        # Kick off the worker pool in a background thread.
        threading_done = asyncio.create_task(asyncio.to_thread(runner))

        yield (f"data: " + json.dumps(
            {"type": "start", "total": total, "json_dir": str(json_dir)})
            + "\n\n")

        i = 0
        errors = 0
        while True:
            res = await queue.get()
            if res is None:
                break
            i += 1
            if not res["ok"]:
                errors += 1
            evt = {"type": "progress", "i": i, "total": total, **res}
            yield "data: " + json.dumps(evt) + "\n\n"

        await threading_done  # propagate any background exception
        yield "data: " + json.dumps(
            {"type": "done", "ok": total - errors,
             "total": total, "errors": errors}) + "\n\n"

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Static frontend  (mounted AFTER all /api routes so it doesn't shadow them)
# ---------------------------------------------------------------------------

_WEB_DIR = Path(__file__).parent / "web"
if _WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="frontend")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True, log_level="info")
