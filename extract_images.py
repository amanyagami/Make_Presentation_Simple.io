import re
import sys
import json
import os
import warnings
import logging
from pathlib import Path

import pdfplumber
import fitz
from PIL import Image, ImageDraw

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
logging.getLogger("ppocr").setLevel(logging.ERROR)
logging.getLogger("paddle").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OUTPUT_BASE_DIR       = Path("uploads/figures")
DEFAULT_PROXIMITY     = 20
DEFAULT_MIN_AREA      = 2000
DEFAULT_CAPTION_SCAN  = 30   # pts — typical figure-to-caption gap (was 4, far too small)
THIN_STROKE_THRESHOLD = 0.5
RENDER_SCALE          = 6.0          # 432 dpi
HORIZ_OVERLAP_THRESHOLD  = 0.30
TEXT_ONLY_MAX_VECTOR_PATHS  = 5
TEXT_ONLY_MIN_OCR_COVERAGE  = 0.10
CHART_PATH_DENSITY_THRESHOLD = 0.005
CHART_MIN_PATH_COUNT         = 15
SIDE_MARGIN   = 8
BOTTOM_MARGIN = 8

CAPTION_RE = re.compile(
    r"^\s*(figure\s*\d+|fig\.?\s*\d+|\([a-zA-Z0-9]\)|[a-zA-Z]\)|table\s*\d+|\d+\.\s)",
    re.IGNORECASE,
)

_OCR = None


# ---------------------------------------------------------------------------
# OCR singleton
# ---------------------------------------------------------------------------
def get_ocr():
    global _OCR
    if _OCR is not None:
        return _OCR
    try:
        from paddleocr import PaddleOCR
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _OCR = PaddleOCR(lang="en", use_textline_orientation=True)
    except Exception:
        try:
            from paddleocr import PaddleOCR
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                _OCR = PaddleOCR(use_angle_cls=True, lang="en")
        except Exception as e:
            print(f"  [OCR] Init failed: {str(e)[:100]}")
            _OCR = "DISABLED"
    return _OCR


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def bbox_dict(x0, y0, x1, y1):
    return {"left": round(x0, 2), "top": round(y0, 2),
            "width": round(x1 - x0, 2), "height": round(y1 - y0, 2)}

def to_rect(b):
    return b["left"], b["top"], b["left"] + b["width"], b["top"] + b["height"]

def area(b):
    return b["width"] * b["height"]

def union_bbox(bboxes):
    return bbox_dict(
        min(b["left"] for b in bboxes),
        min(b["top"]  for b in bboxes),
        max(b["left"] + b["width"]  for b in bboxes),
        max(b["top"]  + b["height"] for b in bboxes),
    )

def boxes_are_close(a, b, threshold):
    ax0, ay0, ax1, ay1 = to_rect(a)
    bx0, by0, bx1, by1 = to_rect(b)
    return max(0.0, max(ax0, bx0) - min(ax1, bx1)) <= threshold \
       and max(0.0, max(ay0, by0) - min(ay1, by1)) <= threshold


# ---------------------------------------------------------------------------
# Union-Find clustering
# ---------------------------------------------------------------------------
def cluster(elements, threshold):
    n = len(elements)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if boxes_are_close(elements[i], elements[j], threshold):
                parent[find(i)] = find(j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


# ---------------------------------------------------------------------------
# pdfplumber: text blocks + tables
# ---------------------------------------------------------------------------
def get_plumber_data(pdf_path):
    results = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pn = page.page_number
            table_objs   = page.find_tables()
            table_bboxes = [t.bbox for t in table_objs]
            tables       = [
                [[c or "" for c in row] for row in t.extract()]
                for t in table_objs
            ]

            words = page.extract_words()
            lines = {}
            for w in words:
                lines.setdefault(round(w["top"]), []).append(w)

            text_blocks = []
            for key in sorted(lines):
                ws  = lines[key]
                bx0 = min(w["x0"]     for w in ws)
                by0 = min(w["top"]    for w in ws)
                bx1 = max(w["x1"]     for w in ws)
                by1 = max(w["bottom"] for w in ws)
                txt = " ".join(w["text"] for w in ws)
                in_table = any(
                    bx0 >= tx0 and by0 >= ty0 and bx1 <= tx1 and by1 <= ty1
                    for tx0, ty0, tx1, ty1 in table_bboxes
                )
                if not in_table:
                    text_blocks.append({"x0": bx0, "y0": by0,
                                        "x1": bx1, "y1": by1, "text": txt})

            text = " ".join(
                w["text"] for w in words
                if not any(w["x0"] >= tx0 and w["top"] >= ty0
                           and w["x0"] <= tx1 and w["top"] <= ty1
                           for tx0, ty0, tx1, ty1 in table_bboxes)
            ).strip() if table_bboxes else (page.extract_text() or "").strip()

            results[pn] = {"text": text, "tables": tables,
                           "text_blocks": text_blocks, "table_bboxes": table_bboxes}
    return results


# ---------------------------------------------------------------------------
# PyMuPDF element bboxes
# ---------------------------------------------------------------------------
def get_vector_bboxes(page, pw, ph):
    bboxes = []
    for d in page.get_drawings():
        r = d.get("rect")
        if r is None or r.is_empty or r.is_infinite:
            continue
        if (r.width >= pw * 0.85 and r.height < 5) or \
           (r.height >= ph * 0.85 and r.width < 5):
            continue
        sw = d.get("width", 1.0) or 0.0
        if sw <= THIN_STROKE_THRESHOLD and r.width * r.height < 100:
            continue
        bboxes.append(bbox_dict(r.x0, r.y0, r.x1, r.y1))
    return bboxes

def get_raster_bboxes(page):
    bboxes = []
    for img_info in page.get_images(full=True):
        for rect in page.get_image_rects(img_info[0]):
            if not rect.is_empty:
                bboxes.append(bbox_dict(rect.x0, rect.y0, rect.x1, rect.y1))
    return bboxes


# ---------------------------------------------------------------------------
# OCR helper (works with both old and new PaddleOCR APIs)
# ---------------------------------------------------------------------------
def run_ocr(ocr_engine, img_path):
    boxes = []
    if ocr_engine == "DISABLED":
        return boxes

    def parse_quad(quad):
        try:
            xs = [p[0] for p in quad]; ys = [p[1] for p in quad]
            return (min(xs), min(ys), max(xs), max(ys))
        except Exception:
            return None

    if hasattr(ocr_engine, 'predict'):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                results = list(ocr_engine.predict(str(img_path)))
            for res in results:
                if res is None:
                    continue
                polys = (getattr(res, 'dt_polys', None) or
                         getattr(res, 'boxes', None) or
                         getattr(res, 'dt_boxes', None))
                if polys is not None:
                    for poly in polys:
                        pts = list(poly)
                        if len(pts) >= 4 and hasattr(pts[0], '__iter__'):
                            b = parse_quad(pts)
                            if b: boxes.append(b)
            if boxes:
                return boxes
        except Exception:
            pass

    try:
        result = ocr_engine.ocr(str(img_path))
    except TypeError:
        result = ocr_engine.ocr(str(img_path), cls=True)
    if not result:
        return boxes
    page_r = result[0] if (isinstance(result[0], list) and result[0]
                           and isinstance(result[0][0], list)) else result
    for line in (page_r or []):
        try:
            b = parse_quad(line[0])
            if b: boxes.append(b)
        except Exception:
            pass
    return boxes


# ---------------------------------------------------------------------------
# Chart detection
# ---------------------------------------------------------------------------
def is_chart_figure(page, merged_bbox):
    fx0, fy0, fx1, fy1 = to_rect(merged_bbox)
    fig_area = (fx1 - fx0) * (fy1 - fy0)
    if fig_area <= 0:
        return False

    inside = []
    for d in page.get_drawings():
        r = d.get("rect")
        if r is None or r.is_empty or r.is_infinite:
            continue
        ix0, iy0 = max(r.x0, fx0), max(r.y0, fy0)
        ix1, iy1 = min(r.x1, fx1), min(r.y1, fy1)
        if ix1 > ix0 and iy1 > iy0:
            d_area = r.width * r.height
            if d_area > 0 and (ix1 - ix0) * (iy1 - iy0) / d_area > 0.5:
                inside.append(d)

    n = len(inside)
    if n >= CHART_MIN_PATH_COUNT and n / fig_area >= CHART_PATH_DENSITY_THRESHOLD:
        print(f"      [chart-detect] density {n/fig_area:.5f} → chart")
        return True

    h_lines = sum(1 for d in inside if d.get("rect") and d["rect"].height < 2.0
                  and d["rect"].y0 > fy0 + 0.75 * (fy1 - fy0))
    v_lines = sum(1 for d in inside if d.get("rect") and d["rect"].width < 2.0
                  and d["rect"].x1 < fx0 + 0.25 * (fx1 - fx0))
    if h_lines >= 2 or v_lines >= 2:
        print(f"      [chart-detect] axis lines h={h_lines} v={v_lines} → chart")
        return True

    filled_rects = [d for d in inside if d.get("fill") is not None
                    and d.get("rect") and 2 < d["rect"].width < (fx1-fx0)*0.5
                    and d["rect"].height > 5]
    x_centres = set(round((d["rect"].x0 + d["rect"].x1) / 2 / 5) * 5
                    for d in filled_rects)
    if len(x_centres) >= 3:
        print(f"      [chart-detect] {len(x_centres)} bar columns → chart")
        return True

    return False


# ---------------------------------------------------------------------------
# Text-only figure detection
# ---------------------------------------------------------------------------
def is_text_only_figure(page, merged_bbox, has_raster, raw_image_path):
    if has_raster:
        return False
    fx0, fy0, fx1, fy1 = to_rect(merged_bbox)
    fig_area = (fx1 - fx0) * (fy1 - fy0)
    if fig_area <= 0:
        return False

    filled_inside = sum(
        1 for d in page.get_drawings()
        if d.get("rect") and not d["rect"].is_empty and d.get("fill") is not None
        and (r := d["rect"])
        and max(r.x0, fx0) < min(r.x1, fx1) and max(r.y0, fy0) < min(r.y1, fy1)
        and (min(r.x1, fx1) - max(r.x0, fx0)) * (min(r.y1, fy1) - max(r.y0, fy0))
            / (r.width * r.height) > 0.5
    )
    if filled_inside > TEXT_ONLY_MAX_VECTOR_PATHS:
        return False

    ocr = get_ocr()
    if ocr == "DISABLED":
        return False
    try:
        img = Image.open(raw_image_path)
        img_area = img.width * img.height
        if img_area <= 0:
            return False
        boxes = run_ocr(ocr, raw_image_path)
        coverage = sum((x1-x0)*(y1-y0) for x0,y0,x1,y1 in boxes) / img_area
        if coverage >= TEXT_ONLY_MIN_OCR_COVERAGE:
            print(f"      [text-only-detect] coverage={coverage:.2%} → text-only")
            return True
    except Exception as e:
        print(f"      [text-only-detect] failed: {e}")
    return False


# ---------------------------------------------------------------------------
# Caption detection
# ---------------------------------------------------------------------------
def is_caption_line(text):
    return bool(CAPTION_RE.match(text.strip()))


def estimate_body_line_height(text_blocks, fig_y1, page_h):
    """Estimate typical body-text line height from blocks well below the figure."""
    below = [b for b in text_blocks if b["y0"] > fig_y1 + 30]
    if len(below) < 4:
        return None
    heights = sorted(b["y1"] - b["y0"] for b in below)
    # median of middle half
    mid = heights[len(heights)//4 : 3*len(heights)//4]
    return sum(mid) / len(mid) if mid else None


# ---------------------------------------------------------------------------
# Bbox refinement
# ---------------------------------------------------------------------------
def refine_figure_bbox(raw, text_blocks, caption_scan, page_h, skip_hclip=False):
    fx0, fy0, fx1, fy1 = to_rect(raw)
    fig_cx  = (fx0 + fx1) / 2
    fig_w   = fx1 - fx0

    # Estimate body-text line height up-front — used throughout this function
    body_lh = estimate_body_line_height(text_blocks, fy1, page_h)
    ref_lh  = body_lh or 12   # fallback if not enough blocks to estimate

    # 1. Vertical hard clip (top) + above-caption scan
    # Only include content above the figure if it's a confirmed labelled caption
    # (e.g. "Table 1: Results" placed above the table). General text blocks
    # above are always treated as a hard top boundary.
    above = sorted([b for b in text_blocks if b["y1"] <= fy0], key=lambda b: b["y1"])
    if above:
        nearest_above = above[-1]
        gap_above     = fy0 - nearest_above["y1"]
        # Only pull in confirmed labelled captions placed above the figure
        if gap_above <= caption_scan and is_caption_line(nearest_above["text"]):
            print(f"      Above-caption found: gap={gap_above:.1f}  "
                  f"\"{nearest_above['text'][:60]}\"")
            fy0   = nearest_above["y0"]
            above = [b for b in above if b["y1"] <= fy0]

        # Hard clip: snap fy0 to just below the nearest non-caption block above
        clip_top_blocks = [b for b in above if b["y1"] > fy0]
        if clip_top_blocks:
            clip_top = max(b["y1"] for b in clip_top_blocks)
            if clip_top > fy0:
                fy0 = clip_top

    # 2. Horizontal hard clip (skip for charts)
    if not skip_hclip:
        left_clip_x1  = fx0
        right_clip_x0 = fx1
        for b in text_blocks:
            if b["y0"] >= fy1 or b["y1"] <= fy0 or is_caption_line(b["text"]):
                continue
            bx0, bx1 = b["x0"], b["x1"]
            bw = bx1 - bx0
            if bw <= 0:
                continue
            overlap = max(0.0, min(bx1, fx1) - max(bx0, fx0)) / bw
            if overlap >= HORIZ_OVERLAP_THRESHOLD:
                continue
            if (bx0 + bx1) / 2 < fig_cx:
                left_clip_x1 = max(left_clip_x1, bx1)
            else:
                right_clip_x0 = min(right_clip_x0, bx0)
        if left_clip_x1 < right_clip_x0:
            if left_clip_x1 > fx0:
                print(f"      H-clip LEFT  : {fx0:.1f} → {left_clip_x1:.1f}")
            if right_clip_x0 < fx1:
                print(f"      H-clip RIGHT : {fx1:.1f} → {right_clip_x0:.1f}")
            fx0, fx1 = left_clip_x1, right_clip_x0
    else:
        print("      H-clip SKIPPED (chart)")

    # ── 3. Caption scan ───────────────────────────────────────────────────
    #
    # Two caption flavours:
    #   LABELLED   — starts with "Figure N:", "Fig. N:", "(a)", "Table N:" etc.
    #   UNLABELLED — short title/label below a text-box figure (no CAPTION_RE match)
    #
    # Phase A: find the caption's first line within caption_scan pts of fy1.
    # Phase B: greedily consume continuation lines. Stop only when:
    #   (a) gap > 2 × body line height  (paragraph-level spacing)
    #   (b) font size jumps UP by > 1.8× (heading / section title above caption)
    #   (c) a confirmed NEW labelled caption starts (another "Figure N:" trigger)
    #
    # NOTE: We intentionally do NOT break on x0 differences — caption text in
    # PDF papers frequently reflows across lines with different left edges
    # (especially centred or justified captions).

    below = sorted([b for b in text_blocks if b["y0"] >= fy1], key=lambda b: b["y0"])

    new_fy1     = fy1
    in_caption  = False
    prev_y1     = fy1
    caption_lh  = None   # line height of first caption line (font-size reference)

    for blk in below:
        gap   = blk["y0"] - prev_y1
        blk_h = blk["y1"] - blk["y0"]
        text  = blk["text"].strip()

        if not in_caption:
            # Phase A — hunt for the opening caption line
            if gap > caption_scan:
                break   # too far below, no caption exists

            is_labelled   = is_caption_line(text)
            is_unlabelled = (
                blk_h <= ref_lh * 1.8   # not a large heading
                and len(text) > 0
            )
            if is_labelled or is_unlabelled:
                in_caption  = True
                caption_lh  = blk_h
                new_fy1     = blk["y1"]
                prev_y1     = blk["y1"]
                print(f"      Caption start ({'labelled' if is_labelled else 'unlabelled'}): "
                      f"gap={gap:.1f}pt  \"{text[:70]}\"")

        else:
            # Phase B — consume continuation lines
            max_cont_gap = max(ref_lh * 2.0, caption_lh * 2.0 if caption_lh else ref_lh * 2.0)

            # (a) Paragraph-level gap → stop
            if gap > max_cont_gap:
                break

            # (b) Line is significantly TALLER than caption → probably a heading/title
            #     above the next figure; stop.
            if caption_lh and blk_h > caption_lh * 2.0:
                break

            # (c) Another labelled caption starts → this is a new figure's caption
            if is_caption_line(text) and new_fy1 > fy1:
                # We already have content — this new trigger is for the next figure
                break

            new_fy1 = blk["y1"]
            prev_y1 = blk["y1"]

    fy1 = new_fy1

    # ── 4. Hard bottom clip ───────────────────────────────────────────────
    # Prevent the render padding from pulling in body text that begins
    # immediately after the caption. Only fire when the very next wide
    # block starts within (BOTTOM_MARGIN + 4) pts of fy1, meaning it
    # would appear in the padded render crop.
    post_body = [
        b for b in text_blocks
        if b["y0"] > fy1 + 1
        and (b["x1"] - b["x0"]) > fig_w * 0.4
    ]
    if post_body:
        next_top = min(b["y0"] for b in post_body)
        if next_top < fy1 + BOTTOM_MARGIN + 4:
            print(f"      Hard bottom clip: fy1 {fy1:.1f} → {next_top - 1:.1f} "
                  f"(body text at {next_top:.1f})")
            fy1 = next_top - 1

    if fy1 <= fy0 or fx1 <= fx0:
        return None
    return bbox_dict(fx0, fy0, fx1, fy1)


# ---------------------------------------------------------------------------
# OCR whitefill
# ---------------------------------------------------------------------------
def ocr_whitefill(raw_path, cluster_bbox_pdf, crop_origin_pdf, scale,
                  clean_path, is_chart=False, is_text_only=False):
    import shutil
    if is_text_only:
        shutil.copy2(str(raw_path), str(clean_path))
        print("      Text-only: copied as-is")
        return clean_path

    ocr = get_ocr()
    if ocr == "DISABLED":
        shutil.copy2(str(raw_path), str(clean_path))
        return clean_path

    img  = Image.open(raw_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    w_px, h_px = img.size

    ox, oy = crop_origin_pdf
    cx0 = max(0.0, (cluster_bbox_pdf["left"]  - ox) * scale)
    cy0 = max(0.0, (cluster_bbox_pdf["top"]   - oy) * scale)
    cx1 = min(float(w_px), cx0 + cluster_bbox_pdf["width"]  * scale)
    cy1 = min(float(h_px), cy0 + cluster_bbox_pdf["height"] * scale)

    boxes = run_ocr(ocr, raw_path)
    margin_zone_px = w_px * 0.08
    filled = 0
    for (tx0, ty0, tx1, ty1) in boxes:
        tcx, tcy = (tx0 + tx1) / 2, (ty0 + ty1) / 2
        outside = tcx < cx0 or tcx > cx1 or tcy < cy0 or tcy > cy1
        edge_fragment = (is_chart and (tx1 - tx0) < margin_zone_px
                         and (tx1 < margin_zone_px or tx0 > w_px - margin_zone_px))
        if outside or edge_fragment:
            pad = 4
            draw.rectangle([max(0, tx0-pad), max(0, ty0-pad),
                            min(w_px, tx1+pad), min(h_px, ty1+pad)],
                           fill=(255, 255, 255))
            filled += 1

    img.save(str(clean_path), dpi=(432, 432))
    if filled:
        print(f"      OCR whitefilled {filled} region(s)" +
              (" [chart]" if is_chart else ""))
    return clean_path


# ---------------------------------------------------------------------------
# Main figure extraction
# ---------------------------------------------------------------------------
def extract_figures(pdf_path, plumber_data, img_dir, proximity, min_area, caption_scan):
    img_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(RENDER_SCALE, RENDER_SCALE)
    page_figures = {}

    for page_index in range(len(doc)):
        page     = doc[page_index]
        page_num = page_index + 1
        pw, ph   = page.rect.width, page.rect.height
        pd       = plumber_data.get(page_num, {})
        text_blocks  = pd.get("text_blocks", [])
        table_bboxes = pd.get("table_bboxes", [])

        raster_bboxes = get_raster_bboxes(page)
        vector_bboxes = get_vector_bboxes(page, pw, ph)
        all_bboxes    = vector_bboxes + raster_bboxes

        def in_table(b):
            bx0, by0, bx1, by1 = to_rect(b)
            return any(bx0 >= tx0 and by0 >= ty0 and bx1 <= tx1 and by1 <= ty1
                       for tx0, ty0, tx1, ty1 in table_bboxes)

        all_bboxes    = [b for b in all_bboxes if not in_table(b)]
        raster_bboxes = [b for b in raster_bboxes if not in_table(b)]
        if not all_bboxes:
            continue

        groups  = cluster(all_bboxes, threshold=proximity)
        figures = []

        for fig_idx, indices in enumerate(groups):
            group_bboxes = [all_bboxes[i] for i in indices]
            raw_merged   = union_bbox(group_bboxes)
            if area(raw_merged) < min_area:
                continue

            has_raster = any(
                any(boxes_are_close(all_bboxes[i], rb, threshold=proximity)
                    for rb in raster_bboxes)
                for i in indices
            )

            is_chart = is_chart_figure(page, raw_merged)
            if is_chart:
                print(f"      Figure {fig_idx}: CHART — H-clip disabled")

            refined = refine_figure_bbox(raw_merged, text_blocks, caption_scan,
                                         ph, skip_hclip=is_chart)
            if refined is None:
                continue

            rx0 = max(0.0, refined["left"]  - SIDE_MARGIN)
            ry0 = max(0.0, refined["top"]   - SIDE_MARGIN)
            rx1 = min(pw,  refined["left"]  + refined["width"]  + SIDE_MARGIN)
            ry1 = min(ph,  refined["top"]   + refined["height"] + BOTTOM_MARGIN)
            padded = bbox_dict(rx0, ry0, rx1, ry1)

            raw_path   = img_dir / f"raw_page{page_num}_figure{fig_idx:04d}.png"
            clean_path = img_dir / f"page{page_num}_figure{fig_idx:04d}.png"

            page.get_pixmap(matrix=mat, clip=fitz.Rect(rx0, ry0, rx1, ry1)).save(str(raw_path))

            text_only = is_text_only_figure(page, raw_merged, has_raster, raw_path)
            if text_only:
                print(f"      Figure {fig_idx}: TEXT-ONLY — pass-through")

            try:
                final_path = ocr_whitefill(
                    raw_path, cluster_bbox_pdf=refined,
                    crop_origin_pdf=(padded["left"], padded["top"]),
                    scale=RENDER_SCALE, clean_path=clean_path,
                    is_chart=is_chart, is_text_only=text_only,
                )
                raw_path.unlink(missing_ok=True)
            except Exception as e:
                print(f"      OCR whitefill skipped ({e})")
                if raw_path.exists():
                    raw_path.rename(clean_path)
                final_path = clean_path

            figures.append({"path": str(final_path), "bbox": padded,
                            "is_chart": is_chart, "text_only": text_only})

        if figures:
            page_figures[page_num] = figures
            print(f"  Page {page_num}: {len(all_bboxes)} elements → {len(figures)} figure(s)")

    doc.close()
    return page_figures


# ---------------------------------------------------------------------------
# Process a single PDF
# ---------------------------------------------------------------------------
def process_pdf(pdf_path, proximity, min_area, caption_scan):
    pdf_path = str(pdf_path)
    pdf_stem = Path(pdf_path).stem
    out_dir  = OUTPUT_BASE_DIR / pdf_stem
    img_dir  = out_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"Processing   : {pdf_path}")
    print(f"Output dir   : {out_dir}")
    print(f"Proximity    : {proximity} pts  |  Min area: {min_area} pts²")
    print(f"Caption scan : {caption_scan} pts  |  Render DPI: {int(72 * RENDER_SCALE)}")

    print("\n  [1/2] Extracting text & tables (pdfplumber)...")
    plumber_data = get_plumber_data(pdf_path)

    print("\n  [2/2] Extracting & cleaning figures (PyMuPDF + PaddleOCR)...")
    figure_data = extract_figures(pdf_path, plumber_data, img_dir,
                                  proximity, min_area, caption_scan)

    final = {}
    for pn in sorted(set(plumber_data) | set(figure_data)):
        pd_page = plumber_data.get(pn, {})
        final[str(pn)] = {
            "text":   pd_page.get("text", ""),
            "tables": pd_page.get("tables", []),
            "images": figure_data.get(pn, []),
        }

    json_path = out_dir / "output.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)

    print(f"\n  JSON saved → {json_path}")
    print(f"  Done: {pdf_stem}")
    return out_dir


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print("Usage: python get_inidv_image_bounding_boxes.py <pdf_path> [proximity] [min_area] [caption_scan]")
        sys.exit(1)

    pdf_path     = sys.argv[1]
    proximity    = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PROXIMITY
    min_area     = float(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_MIN_AREA
    caption_scan = float(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_CAPTION_SCAN

    target = Path(pdf_path)
    if target.is_dir():
        pdf_files = sorted(target.glob("*.pdf")) + sorted(target.glob("*.PDF"))
        if not pdf_files:
            print(f"No PDF files found in {target}"); sys.exit(1)
        for pdf in pdf_files:
            try:
                process_pdf(str(pdf), proximity, min_area, caption_scan)
            except Exception as e:
                print(f"  ERROR processing {pdf.name}: {e}")
    elif target.is_file():
        process_pdf(str(target), proximity, min_area, caption_scan)
    else:
        print(f"Path not found: {target}"); sys.exit(1)


if __name__ == "__main__":
    main()