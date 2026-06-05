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
            {"path": str(p), "name": p.name, "stem": p.stem, "size_bytes": p.stat().st_size}
            for p in crops
        ],
    }


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

    def _read():
        out: dict = {}
        with mf.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                src = Path(row["source_image"])
                crop = row["output_crop"]
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
        return {"out_w": int(crop.shape[1]), "out_h": int(crop.shape[0]),
                "crop_x1": int(cx1), "crop_y1": int(cy1),
                "crop_x2": int(cx2), "crop_y2": int(cy2)}

    try:
        info = await asyncio.to_thread(_do)
    except (ValueError, IOError) as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Redraw failed: {exc}")
    return {"ok": True, "output_path": str(out), **info}


# ---------------------------------------------------------------------------
# VoucherVision Go — constants, persisted config, prompts, submit SSE
# ---------------------------------------------------------------------------
#
# The browser does NOT talk to VVGo directly: the backend proxies the calls so
# the API token stays on the user's machine (same trust model as the desktop
# dialog) and the frontend doesn't need CORS access to the cloud API.  The
# constants below mirror the values already used by segmenter_gui.py's
# VVGoDialog so both interfaces share one config file.

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

    def _process_one(p: Path) -> dict:
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
            # VoucherVision Editor ignores files whose names start with "_"
            # (it treats leading-underscore entries as hidden), and our crops
            # are named "_DSC0002_packet_01". Strip leading underscores from
            # the JSON filename so the Editor lists the folder correctly.
            json_stem = p.stem.lstrip("_") or p.stem
            out_path = json_dir / f"{json_stem}.json"
            out_path.write_text(
                json.dumps(r.json(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return {"name": p.name, "path": str(p), "ok": True,
                    "json_path": str(out_path)}
        except Exception as exc:
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
