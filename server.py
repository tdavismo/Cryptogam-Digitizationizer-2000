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
import csv
import json
import mimetypes
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from segmenter_core import (
    IMAGE_EXTS, MANIFEST_FIELDS,
    SegSettings, ImageResult,
    segment_image, save_crops, save_crops_detailed, flag_results,
    resegment_or_bisect, _auto_output_dir,
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

@app.get("/api/crops", summary="List crop images in an output directory", tags=["Gallery"])
async def list_crops(output_dir: str = Query(..., description="Path to crops folder")):
    """Metadata for every image in output_dir, sorted by name. Populates the QC gallery."""
    d = Path(output_dir)
    if not d.is_dir():
        raise HTTPException(404, f"Directory not found: {d}")
    crops = sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    return {
        "output_dir": str(d),
        "count": len(crops),
        "crops": [
            {"path": str(p), "name": p.name, "stem": p.stem, "size_bytes": p.stat().st_size}
            for p in crops
        ],
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

@app.get("/api/file", summary="Serve a local image file", tags=["Gallery"])
async def serve_file(path: str = Query(..., description="Absolute path to an image file")):
    """
    Stream one image file from disk for thumbnails / previews. Restricted to
    image extensions. NOTE: localhost single-user tool; restrict to an output
    root if this ever becomes a hosted multi-user service.
    """
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, f"File not found: {p}")
    if p.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(415, f"Not an image file: {p.name}")
    return FileResponse(str(p))


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
