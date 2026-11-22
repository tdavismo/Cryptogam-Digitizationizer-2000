#!/usr/bin/env python3
"""
compat_check.py — backward-compatibility guard for previously processed folders.

Builds synthetic output folders shaped exactly like each generation of this
tool wrote them, then runs the real server endpoint code against them:

  exe       desktop app (segmenter_gui.py, retired): manifest in current column
            order; VVGo JSONs named with the leading underscore KEPT
            (_SRC_packet_01.json); no audit sidecar.
  script    segment_packets_updated.py: manifest with extra threshold_mode /
            contrast columns in a different order.
  web       this web app: JSONs with the leading underscore stripped, plus a
            packet_audit.json sidecar holding attempt history.
  nomanif   very old / partial folder: crops only, no manifest.
  moved     exe-style folder whose manifest records absolute paths from another
            machine (folder copied or moved since segmentation).

Run it before merging any change that touches server.py:

    .venv\\Scripts\\python.exe tests\\compat_check.py

Exits non-zero if any check fails. Uses only synthetic images (no specimens).
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402

V1_FIELDS = ["source_image", "output_crop", "packet_index", "detected_foreground",
             "top_crop_px", "bbox_x", "bbox_y", "bbox_w", "bbox_h",
             "crop_x1", "crop_y1", "crop_x2", "crop_y2",
             "contour_area", "aspect", "rectangularity", "deskewed"]
UPDATED_FIELDS = ["source_image", "output_crop", "packet_index", "detected_foreground",
                  "threshold_mode", "contrast", "top_crop_px",
                  "bbox_x", "bbox_y", "bbox_w", "bbox_h",
                  "crop_x1", "crop_y1", "crop_x2", "crop_y2",
                  "contour_area", "aspect", "rectangularity", "deskewed"]

# Three packets laid out left-to-right on a 1600x1200 source.
BOXES = [(100, 200, 400, 800), (600, 200, 400, 800), (1100, 200, 400, 800)]
PAD = 20

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((bool(ok), label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))


def run(coro):
    return asyncio.run(coro)


def make_source(path: Path) -> np.ndarray:
    img = np.full((1200, 1600, 3), 225, np.uint8)
    for i, (x, y, w, h) in enumerate(BOXES):
        cv2.rectangle(img, (x, y), (x + w, y + h), (50 + 40 * i,) * 3, -1)
    cv2.imwrite(str(path), img)
    return img


def build_folder(root: Path, name: str, fields: list[str], *, write_manifest=True,
                 recorded_root: Path | None = None) -> tuple[Path, Path]:
    """input_dir/_SRC.jpg + input_dir/<name>-PACKETS/ crops (+ manifest).
    recorded_root: if set, the manifest records paths under that (nonexistent)
    root instead of the real one — simulates a folder moved from another PC."""
    in_dir = root / name
    out_dir = in_dir / f"{name}-PACKETS"
    out_dir.mkdir(parents=True)
    src = in_dir / "_SRC.jpg"
    img = make_source(src)
    rec_in = (recorded_root / name) if recorded_root else in_dir
    rec_out = rec_in / out_dir.name
    rows = []
    for i, (x, y, w, h) in enumerate(BOXES, 1):
        crop_name = f"_SRC_packet_{i:02d}.jpg"
        cv2.imwrite(str(out_dir / crop_name), img[y - PAD:y + h + PAD, x - PAD:x + w + PAD])
        row = {k: "" for k in fields}
        row.update({"source_image": str(rec_in / "_SRC.jpg"),
                    "output_crop": str(rec_out / crop_name),
                    "packet_index": i, "detected_foreground": "dark", "top_crop_px": 0,
                    "bbox_x": x, "bbox_y": y, "bbox_w": w, "bbox_h": h,
                    "crop_x1": x - PAD, "crop_y1": y - PAD,
                    "crop_x2": x + w + PAD, "crop_y2": y + h + PAD,
                    "contour_area": w * h, "aspect": round(w / h, 3),
                    "rectangularity": 0.98, "deskewed": False})
        if "threshold_mode" in fields:
            row.update({"threshold_mode": "otsu", "contrast": "clahe"})
        rows.append(row)
    if write_manifest:
        with (out_dir / "packet_manifest.csv").open("w", newline="", encoding="utf-8") as fh:
            wtr = csv.DictWriter(fh, fieldnames=fields)
            wtr.writeheader()
            wtr.writerows(rows)
    return in_dir, out_dir


def snapshot(d: Path) -> dict[str, str]:
    return {str(p.relative_to(d)): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}


def assert_read_only(label: str, before: dict, after: dict) -> None:
    changed = [k for k in before if after.get(k) != before[k]]
    added = sorted(set(after) - set(before))
    check(not changed and not added, f"{label}: opening the folder leaves existing files untouched",
          f"changed={changed} added={added}")


def read_all(out_dir: Path, json_dir: Path | None = None):
    crops = run(server.list_crops(output_dir=str(out_dir)))
    audit = run(server.audit(output_dir=str(out_dir), json_dir=str(json_dir) if json_dir else ""))
    sess = run(server.session_from_folder(output_dir=str(out_dir)))
    run(server.get_review(output_dir=str(out_dir)))      # must also be read-only
    try:
        manifest = run(server.get_manifest(output_dir=str(out_dir)))
    except HTTPException as exc:
        manifest = exc
    return crops, audit, sess, manifest


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="cdz_compat_"))
    try:
        # ── exe-era folder ────────────────────────────────────────────────
        print("\n[exe] desktop-app folder (underscore-kept JSON names)")
        _, out = build_folder(root, "exe", V1_FIELDS)
        jd = out / "vvgo_json"
        jd.mkdir()
        for n in ("_SRC_packet_01", "_SRC_packet_02"):        # 2 of 3 submitted
            (jd / f"{n}.json").write_text(json.dumps({"ok": n}), encoding="utf-8")
        before = snapshot(out)
        crops, audit, sess, mani = read_all(out, jd)
        check(crops["count"] == 3, "crops listed", crops["count"])
        t = audit["totals"]
        check(t["received"] == 2 and t["not_submitted"] == 1,
              "desktop-app JSONs (_SRC_packet_NN.json) count as received", t)
        byname = {c["name"]: c for c in audit["crops"]}
        check(byname["_SRC_packet_01.jpg"]["json_name"] == "_SRC_packet_01.json",
              "audit points at the JSON file that actually exists",
              byname["_SRC_packet_01.jpg"]["json_name"])
        check(byname["_SRC_packet_03.jpg"]["json_name"] == "SRC_packet_03.json",
              "an unsubmitted crop targets the current (underscore-stripped) name",
              byname["_SRC_packet_03.jpg"]["json_name"])
        check(sess["hasManifest"] is True and sess["packets"] == 3, "session rebuilt from manifest", sess)
        check(not isinstance(mani, Exception) and mani["count"] == 3, "manifest readable", mani)
        assert_read_only("exe", before, snapshot(out))

        # ── updated-script folder ─────────────────────────────────────────
        print("\n[script] segment_packets_updated.py folder (extra manifest columns)")
        _, out = build_folder(root, "script", UPDATED_FIELDS)
        before = snapshot(out)
        crops, audit, sess, mani = read_all(out)
        check(not isinstance(mani, Exception) and mani["count"] == 3, "manifest readable", mani)
        ok_boxes = not isinstance(mani, Exception) and all(
            (e["x"], e["y"], e["w"], e["h"]) in BOXES and e["padding"] == PAD
            for e in mani["crops"].values())
        check(ok_boxes, "boxes + padding read correctly from extra-column manifest")
        assert_read_only("script", before, snapshot(out))

        res = run(server.add_packet(server.AddPacketRequest(
            output_dir=str(out), source_path=str(out.parent / "_SRC.jpg"),
            x=50, y=1050, w=300, h=120, padding=10)))
        with (out / "packet_manifest.csv").open(encoding="utf-8", newline="") as fh:
            rdr = csv.DictReader(fh)
            header, rows = rdr.fieldnames, list(rdr)
        check(header == UPDATED_FIELDS, "add-packet keeps the original manifest header", header)
        check(all(r["threshold_mode"] == "otsu" and r["contrast"] == "clahe" for r in rows[:3]),
              "add-packet leaves existing rows intact")
        new = rows[-1] if len(rows) == 4 else {}
        check(new.get("bbox_x") == "50" and new.get("bbox_y") == "1050" and new.get("bbox_w") == "300"
              and new.get("packet_index") == "4" and Path(new.get("output_crop", "")).name == res["name"],
              "add-packet row lands in the right columns", new)
        mani2 = run(server.get_manifest(output_dir=str(out)))
        check(mani2["count"] == 4 and res["output_path"] in mani2["crops"],
              "manifest re-reads cleanly after add-packet", mani2.get("count"))

        # ── web-era folder ────────────────────────────────────────────────
        print("\n[web] web-app folder (stripped JSON names + audit sidecar)")
        _, out = build_folder(root, "web", V1_FIELDS)
        jd = out / "vvgo_json"
        jd.mkdir()
        (jd / "SRC_packet_01.json").write_text("{}", encoding="utf-8")
        (out / "packet_audit.json").write_text(json.dumps({"version": 1, "crops": {
            "_SRC_packet_01.jpg": {"segmented_at": "2026-06-01T10:00:00+00:00", "attempts": [
                {"ts": "2026-06-02T10:00:00+00:00", "model": "m", "prompt": "p", "ok": True,
                 "error": None, "json_name": "SRC_packet_01.json"}]},
            "_SRC_packet_02.jpg": {"segmented_at": "2026-06-01T10:00:00+00:00", "attempts": [
                {"ts": "2026-06-02T10:00:00+00:00", "model": "m", "prompt": "p", "ok": False,
                 "error": "400: too blurry", "json_name": "SRC_packet_02.json"}]},
        }}), encoding="utf-8")
        before = snapshot(out)
        _, audit, _, _ = read_all(out, jd)
        t = audit["totals"]
        check(t["received"] == 1 and t["errored"] == 1 and t["not_submitted"] == 1,
              "web-app receipts + errors read back", t)
        after = snapshot(out)
        check(after == before, "existing audit sidecar not rewritten on open",
              [k for k in before if after.get(k) != before[k]])

        # ── legacy folder without a manifest ──────────────────────────────
        print("\n[nomanif] crops only, no manifest")
        _, out = build_folder(root, "nomanif", V1_FIELDS, write_manifest=False)
        before = snapshot(out)
        crops, audit, sess, mani = read_all(out)
        check(crops["count"] == 3 and audit["totals"]["total"] == 3, "crops + audit work", audit["totals"])
        check(sess["hasManifest"] is False and sess["packets"] == 3, "session flags no manifest", sess)
        check(isinstance(mani, HTTPException) and mani.status_code == 404,
              "manifest endpoint reports 404 (Redraw disabled, not crashed)")
        assert_read_only("nomanif", before, snapshot(out))

        # ── folder moved from another machine ─────────────────────────────
        print("\n[moved] manifest records paths from another machine")
        _, out = build_folder(root, "moved", V1_FIELDS, recorded_root=Path("Z:/elsewhere/old_pc"))
        before = snapshot(out)
        _, _, sess, mani = read_all(out)
        check(sess["hasManifest"] is True and sess["packets"] == 3, "session re-homes crops", sess)
        actual = {str(p) for p in out.iterdir() if p.suffix == ".jpg"}
        keys = set(mani["crops"]) if not isinstance(mani, Exception) else set()
        check(keys == actual, "Redraw manifest keys match the crops' real paths",
              sorted(Path(k).name + " @ " + str(Path(k).parent) for k in keys)[:1])
        srcs = {e["source_path"] for e in mani["crops"].values()} if keys else set()
        check(srcs and all(Path(s).is_file() for s in srcs),
              "Redraw finds the source image next to the moved folder", srcs)
        check(keys and all(e["source_w"] == 1600 and e["source_h"] == 1200
                           for e in mani["crops"].values()),
              "source dimensions resolved for moved folder")
        assert_read_only("moved", before, snapshot(out))

        # ── Redraw persists the new box (moved + extra-column manifest) ──────
        print("\n[redraw] accept writes the new box to the manifest")
        _, out = build_folder(root, "redraw", UPDATED_FIELDS,
                              recorded_root=Path("Z:/elsewhere/old_pc"))
        target = out / "_SRC_packet_02.jpg"
        mtime0 = {c["name"]: c["mtime"] for c in run(server.list_crops(output_dir=str(out)))["crops"]}
        src = run(server.get_manifest(output_dir=str(out)))["crops"][str(target)]["source_path"]
        import time as _t; _t.sleep(0.05)
        res = run(server.redraw(server.RedrawRequest(
            source_path=src, output_path=str(target), x=650, y=250, w=300, h=700, padding=PAD)))
        check(res.get("manifest_updated") is True, "redraw reports the manifest row updated", res)
        with (out / "packet_manifest.csv").open(encoding="utf-8", newline="") as fh:
            rdr = csv.DictReader(fh)
            header, rows = rdr.fieldnames, list(rdr)
        check(header == UPDATED_FIELDS, "redraw keeps the original manifest header", header)
        r2 = rows[1]
        check((r2["bbox_x"], r2["bbox_y"], r2["bbox_w"], r2["bbox_h"]) == ("650", "250", "300", "700")
              and r2["threshold_mode"] == "otsu" and r2["output_crop"].startswith("Z:"),
              "redrawn row updated in place, other columns untouched", r2)
        check(all((int(r["bbox_x"]), int(r["bbox_y"]), int(r["bbox_w"]), int(r["bbox_h"])) == BOXES[i]
                  for i, r in ((0, rows[0]), (2, rows[2]))), "other rows unchanged")
        e = run(server.get_manifest(output_dir=str(out)))["crops"][str(target)]
        check((e["x"], e["y"], e["w"], e["h"], e["padding"]) == (650, 250, 300, 700, PAD),
              "reopening Redraw shows the redrawn box", e)
        mtime1 = {c["name"]: c["mtime"] for c in run(server.list_crops(output_dir=str(out)))["crops"]}
        check(mtime1["_SRC_packet_02.jpg"] != mtime0["_SRC_packet_02.jpg"]
              and mtime1["_SRC_packet_01.jpg"] == mtime0["_SRC_packet_01.jpg"],
              "crop list mtime changes only for the redrawn crop (QC thumbnail refresh)")

        # ── Remove segmentations ──────────────────────────────────────────
        print("\n[remove] remove-packets moves crops aside recoverably")
        _, out = build_folder(root, "remove", V1_FIELDS)
        jd = out / "vvgo_json"
        jd.mkdir()
        (jd / "_SRC_packet_01.json").write_text("{}", encoding="utf-8")   # exe-era name
        (jd / "SRC_packet_02.json").write_text("{}", encoding="utf-8")    # current name
        before = snapshot(out)
        try:
            run(server.remove_packets(server.RemovePacketsRequest(
                output_dir=str(out), paths=[str(out.parent / "_SRC.jpg")])))
            rejected = False
        except HTTPException as exc:
            rejected = exc.status_code == 422
        check(rejected and snapshot(out) == before,
              "refuses files outside the batch folder and changes nothing")
        res = run(server.remove_packets(server.RemovePacketsRequest(
            output_dir=str(out), paths=[str(out / "_SRC_packet_01.jpg"), str(out / "_SRC_packet_03.jpg")])))
        rd = out / "_removed"
        check((rd / "_SRC_packet_01.jpg").is_file() and (rd / "_SRC_packet_03.jpg").is_file()
              and not (out / "_SRC_packet_01.jpg").exists(), "crop images moved to _removed/")
        check(res["manifest_rows_removed"] == 2, "two manifest rows removed", res)
        with (rd / "removed_manifest_rows.csv").open(encoding="utf-8", newline="") as fh:
            log = list(csv.DictReader(fh))
        check([r["packet_index"] for r in log] == ["1", "3"],
              "removed rows kept in _removed/removed_manifest_rows.csv", log)
        check((rd / "vvgo_json" / "_SRC_packet_01.json").is_file()
              and (jd / "SRC_packet_02.json").is_file(),
              "removed crop's JSON moved aside; kept crop's JSON untouched")
        crops, audit, sess, mani = read_all(out, jd)
        check(crops["count"] == 1 and audit["totals"]["total"] == 1 and audit["totals"]["received"] == 1,
              "QC/VVGo no longer list removed crops", (crops["count"], audit["totals"]))
        check(mani["count"] == 1 and sess["packets"] == 1, "Redraw + reopen no longer list them",
              (mani["count"], sess["packets"]))
        # Re-add a packet (takes index 03 again), then remove it: no clobbering.
        add = run(server.add_packet(server.AddPacketRequest(
            output_dir=str(out), source_path=str(out.parent / "_SRC.jpg"),
            x=1100, y=200, w=400, h=800, padding=PAD)))
        run(server.remove_packets(server.RemovePacketsRequest(
            output_dir=str(out), paths=[add["output_path"]])))
        check((rd / "_SRC_packet_03.jpg").is_file() and (rd / "_SRC_packet_03~2.jpg").is_file(),
              "removing the same name twice keeps both copies")

        # ── Saved review: decisions + redraw marks survive a reopen ─────────
        print("\n[review] QC decisions and redraw marks are saved with the folder")
        _, out = build_folder(root, "review", V1_FIELDS, recorded_root=Path("Z:/elsewhere/old_pc"))
        c1, c2, c3 = (f"_SRC_packet_0{i}.jpg" for i in (1, 2, 3))
        res = run(server.post_review(server.ReviewUpdate(output_dir=str(out), updates={
            c1: {"status": "approved", "note": ""},
            c2: {"status": "flagged", "note": "two packets merged"},
            "..\\evil.jpg": {"status": "approved"},
            "nope.jpg": {"status": "approved"},
        })))
        check(sorted(res["saved"]) == [c1, c2] and len(res["skipped"]) == 2,
              "saves real crops, ignores bad / missing names", res)
        src = run(server.get_manifest(output_dir=str(out)))["crops"][str(out / c3)]["source_path"]
        run(server.redraw(server.RedrawRequest(source_path=src, output_path=str(out / c3),
                                               x=1110, y=210, w=380, h=780, padding=PAD)))
        run(server.redraw(server.RedrawRequest(source_path=src, output_path=str(out / c3),
                                               x=1100, y=200, w=400, h=800, padding=PAD)))
        # "Reopen": a fresh read, as the UI does after a restart.
        rv = run(server.get_review(output_dir=str(out)))["crops"]
        check(rv.get(c1, {}).get("status") == "approved" and rv[c1]["path"] == str(out / c1),
              "approval reads back on reopen (keyed to this folder's paths)", rv.get(c1))
        check(rv.get(c2, {}).get("status") == "flagged" and rv[c2].get("note") == "two packets merged",
              "flag + note read back on reopen", rv.get(c2))
        check(rv.get(c3, {}).get("redrawn_at") and rv[c3].get("redraw_count") == 2,
              "redrawn marker + count read back on reopen", rv.get(c3))
        run(server.post_review(server.ReviewUpdate(output_dir=str(out), updates={
            c1: {"status": None, "note": ""}})))
        rv = run(server.get_review(output_dir=str(out)))["crops"]
        check(c1 not in rv, "un-approving clears the saved entry", rv.get(c1))
        add = run(server.add_packet(server.AddPacketRequest(
            output_dir=str(out), source_path=src, x=50, y=1050, w=300, h=120, padding=10)))
        rv = run(server.get_review(output_dir=str(out)))["crops"]
        check(rv.get(add["name"], {}).get("added_at"), "hand-added packet is marked", rv.get(add["name"]))
        # Remove the flagged crop (with an audit entry), then re-add under that name.
        (out / "packet_audit.json").write_text(json.dumps({"version": 1, "crops": {
            c2: {"segmented_at": None, "attempts": [{"ok": False, "error": "x"}]}}}), encoding="utf-8")
        run(server.remove_packets(server.RemovePacketsRequest(output_dir=str(out), paths=[str(out / c2)])))
        recs = json.loads((out / "_removed" / "removed_records.json").read_text(encoding="utf-8"))
        check(recs and recs[0]["name"] == c2 and recs[0]["review"]["status"] == "flagged"
              and recs[0]["audit"]["attempts"], "removed crop's review + history kept in _removed/", recs)
        aud = json.loads((out / "packet_audit.json").read_text(encoding="utf-8"))
        check(c2 not in aud["crops"], "removed crop's history leaves the live audit")
        # The name freed by removal is not reused by add-packet (it takes max+1),
        # so simulate reuse directly: a new crop appears under c2's name.
        shutil.copy(out / c1, out / c2)
        rv = run(server.get_review(output_dir=str(out)))["crops"]
        check(c2 not in rv, "a new crop under a removed crop's name starts unreviewed", rv.get(c2))
        server._review_mark(out, c1, redrawn_at="2026-01-01T00:00:00+00:00")
        server._review_forget(out, [c1])
        check(c1 not in run(server.get_review(output_dir=str(out)))["crops"],
              "re-segmenting a name forgets its old review (batch hook)")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        print("FAILED:\n  - " + "\n  - ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
