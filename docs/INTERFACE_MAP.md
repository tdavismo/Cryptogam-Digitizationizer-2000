# Interface Map — Cryptogam Digitizationizer 2000

A shared vocabulary for the app's screens and controls, so bug reports can say
exactly *where* something went wrong ("QC-14 doesn't approve the shift-selected
cards") instead of describing it.

**See the IDs on screen:** Settings (top right) → *Troubleshooting* →
**Show control IDs**. Every control below gets a pink badge with its ID. The
setting is remembered per browser; turn it off the same way.

**Reporting a bug** — include:

1. The control ID(s) involved (e.g. `RD-1`, `RD-8`)
2. What you did, step by step (clicks, keys, which crop)
3. What you expected vs. what happened
4. The folder involved, and whether it was a fresh run (`SET-5`) or reopened
   (`SET-6`), and roughly when it was originally segmented (see
   [Folder generations](#folder-generations))

Notes: rows that repeat (crop cards, submission rows) only badge the **first**
one, but the ID applies to every card/row of that kind. IDs with a letter
suffix (`SET-4c`) live inside a pop-out panel and only show while it is open.

---

## Workflow at a glance

```
01 Session Setup ──run──▶ 02 QC Review ──R / Redraw──▶ 03 Redraw Boundary
   (or reopen a                │ approve / flag              (fix a box, add a
    processed folder)          ▼                              missed packet)
                         04 VVGo Submission ──▶ JSON records in <folder>/vvgo_json
```

---

## Global (always visible)

| ID  | Control | What it does |
|-----|---------|--------------|
| G-1 | Top bar | App name and status line. *Known:* the "localhost:8000 · batch: lichen_2024_b3" text is placeholder and doesn't reflect the real port or folder. |
| G-2 | Settings button | Opens the Settings panel (S-1…S-7). |
| G-3 | Tab bar | Switches between the four screens. A segmentation run keeps going while you look at other tabs. |

### Settings panel

| ID  | Control | What it does |
|-----|---------|--------------|
| S-1 | Skin picker | Blueprint / Vaporwave / Retro '95 look. Cosmetic only. |
| S-2 | Text size | Scales the whole interface 85–130 %. |
| S-3 | High contrast | Stronger borders/text. |
| S-4 | Reduce motion | Turns off animations. |
| S-5 | Legibility font | Switches to Atkinson Hyperlegible. |
| S-6 | Show control IDs | Toggles the badges described on this page. |
| S-7 | Reset accessibility | Resets S-2…S-5 (not the skin). |

---

## 01 · Session Setup (`SET`)

Configure a run, watch detection live, or reopen an earlier folder.

| ID  | Control | What it does |
|-----|---------|--------------|
| SET-1 | Input folder + Browse | Folder of source photos to segment. |
| SET-2 | Output folder + Browse | Where crops go. Blank = auto-dated `<DD-MON-YYYY>-PACKETS` folder inside the input folder. |
| SET-3 | Grid R × C | Expected rows × columns. Only guides the reading-order numbering of packets; it does not limit detection. Remembered between sessions. |
| SET-4 | Advanced ▾ | Opens the processing settings panel (SET-4a…4h). |
| SET-4a | Foreground | Light / Dark / Auto — whether packets are lighter or darker than the background. |
| SET-4b | Threshold | Otsu / Adaptive / Canny detection method. |
| SET-4c | Contrast | None / Normalize / CLAHE / Both pre-processing. |
| SET-4d | Deskew | Straighten tilted packets (deskewed crops use a fixed 30 px pad). |
| SET-4e | Auto-portrait | Rotate landscape photos 90° so packets face up. |
| SET-4f | Top crop % | Ignore this much of the top of each photo (colour chart/ruler strip). |
| SET-4g | Padding px | Margin added around each detected packet. |
| SET-4h | Min area % | Ignore blobs smaller than this share of the photo. |
| SET-5 | Run / Stop | Segments every image in SET-1 into SET-2, writing crops + `packet_manifest.csv`. |
| SET-6 | Open processed… | Reopens a folder segmented earlier (any version) for QC / Redraw / VVGo. **Read-only** — opening never changes files. |
| SET-7 | Live preview | The current photo with detected boxes and packet numbers. |
| SET-8 | Frame navigator | ◀ ▶ through photos processed this run; dots show first/last. |
| SET-9 | Activity log | Per-image results, warnings (possible merges, zero detections) and errors. |

---

## 02 · QC Review (`QC`)

Check every crop, approve the good ones, flag problems.

| ID  | Control | What it does |
|-----|---------|--------------|
| QC-0 | "No batch loaded" card | Shown until a run finishes (SET-5) or a folder is reopened (SET-6). |
| QC-1 | Approve all visible / Approve N selected | Approves every *unreviewed* crop in the current filter, or the shift-selected set. |
| QC-2 | Flag selected / Flag N selected | Flags the current crop or the shift-selected set. |
| QC-3 | Filter chips | All / Approved / Unreviewed / Flagged / Oversize, with counts. |
| QC-4 | Approved counter | "N of M approved". |
| QC-5 | Export Crops | *Not wired yet — does nothing.* |
| QC-6 | Proceed to Submission | *Not wired yet — use the 04 tab.* |
| QC-7 | Thumbnail grid | All crops matching QC-3. |
| QC-8 | Crop card | Click = select · Shift-click = select a range · ring colour = status (green approved, red flagged, gold oversize). |
| QC-9 | Receipt badge (on a card) | VVGo state from disk: ✓ output / ⧖ sent (no JSON yet) / ✗ error. |
| QC-10 | Crop Detail panel | Everything about the selected crop. |
| QC-11 | Detail image | Full-size view of the crop. |
| QC-12 | Flag note | Free-text reason, shown when a crop is flagged. |
| QC-13 | Metadata | Source image, packet index, file name, size. |
| QC-14 | Approve | Approve (press again to un-approve). Acts on the selection if one exists. |
| QC-15 | Flag | Flag (press again to un-flag). Acts on the selection if one exists. |
| QC-16 | Redraw | Opens this crop on 03. Disabled for folders with no manifest. |
| QC-17 | Hotkey hint | **A** approve · **F** flag · **R** redraw · **← / →** move · **Esc** clear selection. |

> **Known limitation:** approve/flag decisions and flag notes are kept in the
> browser only. They survive tab switches but are **lost on page refresh or
> when the app is restarted**; nothing is written to the folder.

---

## 03 · Redraw Boundary (`RD`)

Fix a crop's boundary on the full source photo, or add a packet the run missed.
Accepting rewrites that crop image and its manifest row.

| ID  | Control | What it does |
|-----|---------|--------------|
| RD-0 | Status card | "Loading…", or why Redraw can't open (no batch loaded / could not load manifest / manifest is empty). |
| RD-1 | Editor canvas | Whole source photo. Scroll = zoom · drag = pan. In add mode: left-drag draws, right-drag pans. |
| RD-2 | Active boundary | Dashed box being edited — drag to move, 8 handles to resize. Shows size and "unsaved" / "new packet". |
| RD-3 | Ghost outlines *(no badge)* | Thin grey boxes for the other packets on the same photo (green approved, red flagged in QC). |
| RD-4 | Canvas hint | Current packet number, mode, and zoom level. |
| RD-5 | Source panel | Source photo name and how many packets it has. |
| RD-6 | Prev / Next | Step through crops (also ← / →). |
| RD-7 | Coordinates | X·Y, W·H of the box in source-photo pixels, and padding. |
| RD-8 | Accept Boundary / Create Packet | Saves the edited box (Enter), or creates the new packet in add mode. |
| RD-9 | Reset / Cancel | Reset the box to the detected one (R), or cancel add mode. |
| RD-10 | Add packet… / Exit add-packet mode | Toggle drawing a brand-new packet on this photo. |
| RD-11 | Result / hotkey line | "✓ Crop saved", "✓ New packet created", errors, or the hotkeys. |

---

## 04 · VVGo Submission (`VV`)

Send crops to VoucherVision Go and track which have JSON records.

| ID  | Control | What it does |
|-----|---------|--------------|
| VV-0 | "No batch loaded" card | Same as QC-0. |
| VV-1 | API token + Show + Save | Token for VVGo. Save stores it in `~/.cryptogam_config.json` (same file the old desktop app used). |
| VV-2 | Model | LLM used for transcription. |
| VV-3 | Prompt + Fetch | Prompt template; Fetch loads the list from the VVGo server. |
| VV-4 | Scope chips | Approved / All / **Needs output** (skip crops that already have a JSON). |
| VV-5 | Advanced ▾ | Opens VV-7a…7f. |
| VV-6 | Submit (N) / Cancel | Starts or cancels submission of the crops in scope. |
| VV-7a | JSON output folder | Where records are written. Defaults to `<crop folder>/vvgo_json`. |
| VV-7b | OCR engine | Defaults to the same model as VV-2. |
| VV-7c | Parallel workers | 1–16 simultaneous submissions. |
| VV-7d | WFO validation | Check names against World Flora Online (slower). |
| VV-7e | COP90 elevation | Add elevation from coordinates. |
| VV-7f | OCR only | Skip JSON parsing. |
| VV-8 | Progress card | Headline ("N / M received", or live "Submitting — x / y this run"), progress bar, JSON folder path. |
| VV-9 | Status pills | Received / needs output / errored / not submitted — for the whole folder, read from disk. |
| VV-10 | Crop table | One row per crop in scope. |
| VV-11 | Crop row | Crop name, status pill, output file or error text. |
| VV-12 | View | Opens the JSON record (VV-15). |
| VV-13 | Attempts (number) | Opens the submission history (VV-16). |
| VV-14 | Submit / Resubmit | Sends just this crop. |
| VV-15 | JSON record window | Key fields + raw JSON. |
| VV-16 | Audit window | Every attempt for this crop: time, model, result/error. |

"Received" always means **a JSON file exists on disk** for that crop, whichever
version of the app wrote it.

---

## Files the app reads and writes

Inside a crop folder (e.g. `…/04-JUN-2026-PACKETS/`):

| File | Written by | Notes |
|------|-----------|-------|
| `_<photo>_packet_NN.jpg` | SET-5, RD-8 | The crops. |
| `packet_manifest.csv` | SET-5, RD-8 (rewrites a row), RD-8 in add mode (appends a row) | Box coordinates per crop. Needed for Redraw. Extra/reordered columns from older versions are preserved. |
| `vvgo_json/<photo>_packet_NN.json` | VV-6 / VV-14 | VVGo records. Leading `_` dropped so VoucherVision Editor doesn't hide them. |
| `packet_audit.json` | VV-6 / VV-14 only | Submission history (VV-16). Never written just by opening a folder. |

## Folder generations

Folders made by every earlier version must keep working. The automated check
`tests/compat_check.py` builds one of each and verifies them:

| Generation | How to recognise it | Supported |
|------------|--------------------|-----------|
| Desktop .exe (retired) | JSONs named `_<photo>_packet_NN.json` (underscore kept) | Read as received; new submissions use the current name. |
| `segment_packets_updated.py` | Manifest has `threshold_mode`, `contrast` columns | Read correctly; new rows keep that header. |
| Web app | Stripped JSON names, `packet_audit.json` present | Full support. |
| Very old / no manifest | Crops only | QC + VVGo work; Redraw disabled (QC-16). |
| Moved / copied to another PC | Manifest paths point to the old location | Re-homed by file name; Redraw finds the photo beside the folder. |

Run it after any change to `server.py`:

```powershell
.venv\Scripts\python.exe tests\compat_check.py
```
