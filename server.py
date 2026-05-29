#!/usr/bin/env python3
"""
server.py  —  Cryptogam Digitizationizer 2000  ·  FastAPI backend
------------------------------------------------------------------
Wraps the four core processing operations as HTTP endpoints.
Designed to run locally (localhost:8000) so the frontend accesses
local files without any upload/download overhead.

Endpoints
---------
POST /api/preview       Detect packets in one image; return count + debug PNG
POST /api/batch         Process all images in a directory (SSE stream)
POST /api/fix           Re-segment or bisect a single oversized crop
GET  /api/crops         List crop images in an output directory

Run directly:
    python server.py
    python launch.py        # also opens the browser
"""

from __future__ import annotations

import asyncio
import base64
import csv
import json
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from segmenter_core import (
    IMAGE_EXTS, MANIFEST_FIELDS, _OVERSIZE_THRESHOLD,
    SegSettings, ImageResult,
    _to_bgr8,
    segment_image, save_crops, flag_results,
    resegment_or_bisect, _auto_output_dir,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Cryptogam Digitizationizer 2000",
    description="REST API for batch herbarium packet segmentation.",
    version="1.0.0",
)

# Allow the dev frontend (Vite default: 5173, CRA default: 3000) to call us.
# In production, lock this down to the actual frontend origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SegSettingsIn(BaseModel):
    """Mirror of SegSettings as a Pydantic model for JSON request bodies."""
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
# Helper: encode a BGR numpy array as a base64 PNG string
# ---------------------------------------------------------------------------

def _bgr_to_b64(bgr: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return base64.b64encode(buf.tobytes()).decode()


def _result_to_dict(r: ImageResult) -> dict:
    return {
        "path":       str(r.path),
        "name":       r.path.name,
        "count":      r.count,
        "flag":       r.flag,
        "flag_detail": r.flag_detail,
        "crop_info": [
            {"path": str(p), "name": p.name, "w": w, "h": h}
            for p, w, h in r.crop_info
        ],
    }


# ---------------------------------------------------------------------------
# Endpoint 1: POST /api/preview
# ---------------------------------------------------------------------------

@app.post("/api/preview",
          summary="Detect packets in one image",
          tags=["Segmentation"])
async def preview(req: PreviewRequest):
    """
    Run packet detection on a single local image file.

    Returns the detection count, the debug image (bounding boxes drawn)
    as a base64-encoded PNG, and the foreground mode that was used.
    No files are written.
    """
    path = Path(req.image_path)
    if not path.is_file():
        raise HTTPException(404, f"Image not found: {path}")

    try:
        dets, debug_bgr, fg, top_px = await asyncio.to_thread(
            segment_image, path, req.settings.to_core()
        )
    except Exception as exc:
        raise HTTPException(422, str(exc))

    return {
        "count":      len(dets),
        "foreground": fg,
        "top_crop_px": top_px,
        "debug_b64":  _bgr_to_b64(debug_bgr),
    }


# ---------------------------------------------------------------------------
# Endpoint 2: POST /api/batch  (Server-Sent Events stream)
# ---------------------------------------------------------------------------

@app.post("/api/batch",
          summary="Process all images in a directory (SSE)",
          tags=["Segmentation"])
async def batch(req: BatchRequest):
    """
    Segment every image in *input_dir*, write crops to *output_dir*
    (auto-generated dated sub-folder when omitted), and apply QC flagging.

    **Streams Server-Sent Events** so the browser can update a progress bar
    in real time.  Event shapes:

    ```
    { "type": "start",    "total": N, "output_dir": "..." }
    { "type": "progress", "i": N, "total": N, "name": "...", "count": N }
    { "type": "error",    "i": N, "total": N, "name": "...", "error": "..." }
    { "type": "done",     "ok": N, "total": N,
                          "flagged": [...], "manifest": "path/to/csv" }
    ```
    """
    in_dir = Path(req.input_dir)
    if not in_dir.is_dir():
        raise HTTPException(404, f"Input directory not found: {in_dir}")

    out_dir = Path(req.output_dir) if req.output_dir else _auto_output_dir(in_dir)
    s       = req.settings.to_core()

    images = sorted(
        p for p in in_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if not images:
        raise HTTPException(422, "No image files found in input directory.")

    async def event_stream():
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest_rows: list[dict] = []
        results:       list[ImageResult] = []
        total = len(images)

        yield f"data: {json.dumps({'type': 'start', 'total': total, 'output_dir': str(out_dir)})}\n\n"

        for i, img_path in enumerate(images, 1):
            name = img_path.name
            try:
                count, _debug, crop_info = await asyncio.to_thread(
                    save_crops, img_path, out_dir, s, manifest_rows
                )
                results.append(ImageResult(path=img_path, count=count,
                                           crop_info=crop_info))
                event = {
                    "type": "progress",
                    "i": i, "total": total,
                    "name": name,
                    "count": count,
                }
            except Exception as exc:
                results.append(ImageResult(path=img_path, count=0, flag="none"))
                event = {
                    "type": "error",
                    "i": i, "total": total,
                    "name": name,
                    "error": str(exc),
                }

            yield f"data: {json.dumps(event)}\n\n"

        # Write manifest CSV
        manifest_path = out_dir / "packet_manifest.csv"
        try:
            with manifest_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
                writer.writeheader()
                writer.writerows(manifest_rows)
        except Exception:
            manifest_path = Path("")  # non-fatal

        # Flag results
        flagged = [r for r in flag_results(results) if r.flag]
        ok      = len(results) - len(flagged)

        done_event = {
            "type":     "done",
            "ok":        ok,
            "total":     total,
            "manifest":  str(manifest_path),
            "flagged":  [_result_to_dict(r) for r in flagged],
        }
        yield f"data: {json.dumps(done_event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Endpoint 3: POST /api/fix
# ---------------------------------------------------------------------------

@app.post("/api/fix",
          summary="Re-segment or bisect an oversized crop",
          tags=["Segmentation"])
async def fix(req: FixRequest):
    """
    Attempt to split a merged-packet crop into individual outputs.

    Tries re-segmentation first; falls back to a geometric bisect.
    The original crop file is deleted on success.

    Returns the new file paths and a short description of what was done.
    """
    crop_path  = Path(req.crop_path)
    output_dir = Path(req.output_dir)

    if not crop_path.is_file():
        raise HTTPException(404, f"Crop not found: {crop_path}")
    if not output_dir.is_dir():
        raise HTTPException(404, f"Output directory not found: {output_dir}")

    try:
        new_paths, description = await asyncio.to_thread(
            resegment_or_bisect, crop_path, output_dir, req.settings.to_core()
        )
    except Exception as exc:
        raise HTTPException(422, str(exc))

    return {
        "new_paths":   [str(p) for p in new_paths],
        "new_names":   [p.name for p in new_paths],
        "description": description,
    }


# ---------------------------------------------------------------------------
# Endpoint 4: GET /api/crops
# ---------------------------------------------------------------------------

@app.get("/api/crops",
         summary="List crop images in an output directory",
         tags=["Gallery"])
async def list_crops(output_dir: str = Query(..., description="Path to crops folder")):
    """
    Return metadata for every image file in *output_dir*, sorted by name.
    Used by the QC gallery to populate thumbnail cards.
    """
    d = Path(output_dir)
    if not d.is_dir():
        raise HTTPException(404, f"Directory not found: {d}")

    crops = sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    return {
        "output_dir": str(d),
        "count":      len(crops),
        "crops": [
            {
                "path":       str(p),
                "name":       p.name,
                "stem":       p.stem,
                "size_bytes": p.stat().st_size,
            }
            for p in crops
        ],
    }


# ---------------------------------------------------------------------------
# Dev server entry point (also invoked by launch.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True,
                log_level="info")
