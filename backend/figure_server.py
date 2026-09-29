#!/usr/bin/env python3
"""Caption-anchored figure extractor with the same HTTP API as figure-extractor.

Why: pdffigures2 often clips figures (for example journals that embed the figure
as one raster image, or figures beside their caption) and misses others. This
server finds each "Figure N" caption, gathers the graphics next to it (images,
vector drawings and short text labels), and renders that region at high
resolution with PyMuPDF.

Tables are hard to find from geometry alone, so they are taken from a
pdffigures2 service when one is reachable (PDFFIGURES2_URL, default
http://localhost:5001). If none is reachable, only figures are returned.
Figures that pdffigures2 finds but this extractor cannot are kept as a fallback.

Endpoints (compatible with what the plugin uses):
  GET  /health            -> {"service": "figure-extractor", ...}
  POST /extract           multipart field "file" (PDF)
  GET  /download/<name>

Run:  python3 figure_server.py [--port 5002]
Test: python3 figure_server.py --cli paper.pdf outdir
"""

import argparse
import io
import json
import os
import re
import sys
import tempfile
import time
import uuid

import fitz  # PyMuPDF

DPI = int(os.environ.get("FIGURE_DPI", "400"))
MAX_PIXELS = 60_000_000  # safety cap per image
PDFFIGURES2_URL = os.environ.get("PDFFIGURES2_URL", "http://localhost:5001").rstrip("/")

VERBISH = {"shows", "show", "showed", "illustrates", "depicts", "presents", "is", "are", "was", "were",
           "and", "or", "also", "displays", "summarizes", "demonstrates", "represents", "provides", "the", "in",
           "of", "to", "for", "with", "as", "has", "have", "can", "may", "highlights", "reveals"}

CAP_RE = re.compile(r"^\s*(fig(?:ure)?s?|table)\s*\.?\s*(S?\d+[A-Za-z]?)\s*([.:|\-–—]?)", re.I)


# ---------------------------------------------------------------- geometry ---

def inflate(r, d):
    return fitz.Rect(r.x0 - d, r.y0 - d, r.x1 + d, r.y1 + d)


def gap(a, b):
    """Distance between two rects (0 if they touch or overlap)."""
    dx = max(a.x0 - b.x1, b.x0 - a.x1, 0)
    dy = max(a.y0 - b.y1, b.y0 - a.y1, 0)
    return max(dx, dy)


def merge_close(rects, dist, blockers=()):
    """Union rects that lie within `dist` of each other. A merge is refused if the
    merged rect would cover a blocker (body text or a caption)."""
    rects = [fitz.Rect(r) for r in rects]
    changed = True
    while changed:
        changed = False
        out = []
        while rects:
            cur = rects.pop()
            grew = True
            while grew:
                grew = False
                rest = []
                for r in rects:
                    if gap(cur, r) <= dist:
                        merged = cur | r
                        if any(_covers(merged, b) for b in blockers):
                            rest.append(r)
                            continue
                        cur = merged
                        grew = True
                        changed = True
                    else:
                        rest.append(r)
                rects = rest
            out.append(cur)
        rects = out
    return rects


def _covers(merged, block):
    inter = merged & block
    if inter.is_empty:
        return False
    return inter.get_area() > 0.25 * block.get_area()


# ------------------------------------------------------------ page analysis ---

def page_lines(page):
    """Return (lines, blocks) where lines are dicts with bbox, text, nwords."""
    d = page.get_text("dict")
    lines = []
    blocks = []
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        bl = []
        for ln in b["lines"]:
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if not txt:
                continue
            bold = any(s["flags"] & 16 or "bold" in s["font"].lower() for s in ln["spans"][:1])
            bl.append({"bbox": fitz.Rect(ln["bbox"]), "text": txt, "n": len(txt.split()), "bold": bold,
                       "size": ln["spans"][0]["size"]})
        if bl:
            lines.extend(bl)
            blocks.append({"bbox": fitz.Rect(b["bbox"]), "lines": bl})
    return lines, blocks


def find_captions(blocks, page_rect):
    caps = []
    for b in blocks:
        first = b["lines"][0]
        m = CAP_RE.match(first["text"])
        if not m:
            continue
        kind = "Table" if m.group(1).lower().startswith("table") else "Figure"
        punct = m.group(3)
        rest = first["text"][m.end():].strip()
        # In-text references at the start of a paragraph ("Figure 1 shows ...") are
        # rejected: a caption has punctuation after the number, or is bold, or all caps.
        label_caps = first["text"][: m.end()].isupper()
        nxt = rest.split(" ")[0] if rest else ""
        verbish = nxt.lower() in VERBISH
        titled = bool(nxt) and nxt[0].isupper() and not verbish
        bare = re.fullmatch(r"(?i)(fig(?:ure)?|table)\.?\s*S?\d+[A-Za-z]?\.?", first["text"].strip()) is not None
        if not (punct or first["bold"] or label_caps or titled or bare):
            continue
        if punct and verbish and not first["bold"]:
            continue
        # A caption is a multi-word paragraph; skip lone labels like "Figure 1." only
        # if they have no continuation in the block.
        text = " ".join(l["text"] for l in b["lines"])
        text = re.sub(r"\s+", " ", text)
        if len(text.split()) < 4 and not bare:
            continue
        caps.append({"kind": kind, "name": m.group(2), "bbox": fitz.Rect(b["bbox"]), "text": text})
    return caps


def graphics(page):
    """Rects of images and vector drawings that could belong to a figure."""
    pr = page.rect
    out = []
    try:
        for im in page.get_image_info():
            r = fitz.Rect(im["bbox"])
            if r.width < 25 or r.height < 25:
                continue
            if r.get_area() > 0.85 * pr.get_area():
                continue
            out.append(r)
    except Exception:
        pass
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    for d in drawings:
        r = fitz.Rect(d["rect"])
        if r.is_empty and (r.width < 0.1 and r.height < 0.1):
            continue
        if r.get_area() > 0.85 * pr.get_area():
            continue
        # white, unstroked shapes are backgrounds or masks, not figure content
        fill, stroke = d.get("fill"), d.get("color")
        if stroke is None and fill is not None and all(c > 0.97 for c in fill):
            continue
        thin = r.width < 1.5 or r.height < 1.5
        # long rules that separate text or columns
        if thin and (r.width > 0.3 * pr.width or r.height > 0.3 * pr.height):
            continue
        out.append(r)
    clean = []
    for r in out:
        # off-page junk and shapes lying wholly in the running header or footer
        if r.x1 < pr.x0 or r.x0 > pr.x1 or r.y1 < pr.y0 or r.y0 > pr.y1:
            continue
        r = fitz.Rect(r) & pr
        if r.y1 < pr.y0 + 0.06 * pr.height or r.y0 > pr.y1 - 0.06 * pr.height:
            continue
        clean.append(r)
    out = clean
    return out


def figure_regions(page):
    """Return list of (caption, region_rect) for figure captions on this page."""
    lines, blocks = page_lines(page)
    caps = [c for c in find_captions(blocks, page.rect) if c["kind"] == "Figure"]
    if not caps:
        return []
    pr = page.rect

    cap_rects = [c["bbox"] for c in caps]
    body = [l["bbox"] for l in lines if l["n"] > 12]           # body-text lines
    # do not treat the caption's own lines as body blockers
    body = [b for b in body if not any(b.intersects(c) for c in cap_rects)]
    blockers = [fitz.Rect(b) for b in body] + [fitz.Rect(c) for c in cap_rects]

    gr = graphics(page)
    # remove graphics that lie inside a caption or inside body text
    gr = [g for g in gr if not any(_covers(g, b) and b.get_area() > 0.6 * g.get_area() for b in blockers)]

    # short text lines (axis labels, panel letters, legends) near graphics
    cap_sizes = [b["lines"][0]["size"] for b in blocks if any(b["bbox"] == c for c in cap_rects)]
    short = [l["bbox"] for l in lines if l["n"] <= 12 and not any(l["bbox"].intersects(c) for c in cap_rects)
             and not any(abs(l["size"] - z) < 0.3 for z in cap_sizes)
             and l["bbox"].width < 0.45 * pr.width and l["bbox"].height < 0.15 * pr.height
             and l["bbox"].x0 > pr.x0 + 0.03 * pr.width and l["bbox"].x1 < pr.x1 - 0.03 * pr.width
             and pr.y0 + 0.05 * pr.height < l["bbox"].y0 and l["bbox"].y1 < pr.y1 - 0.05 * pr.height]

    clusters = merge_close(gr, 12, blockers)
    # absorb short labels that touch a cluster, then re-merge with a wider gap
    grown = []
    for c in clusters:
        cur = fitz.Rect(c)
        for _ in range(2):
            for s in short:
                if gap(cur, s) <= 14 and s.get_area() < 0.5 * pr.get_area():
                    cur = cur | s
        grown.append(cur)
    clusters = merge_close(grown, 40, blockers)
    clusters = [c for c in clusters if c.width >= 40 and c.height >= 30]
    if not clusters:
        return []

    results = []
    used = set()
    for cap in caps:
        cb = cap["bbox"]
        best, best_d = None, 1e9
        for i, c in enumerate(clusters):
            if c.intersects(cb) and (c & cb).get_area() > 0.3 * cb.get_area():
                continue
            hover = min(c.x1, cb.x1) - max(c.x0, cb.x0)   # horizontal overlap
            vover = min(c.y1, cb.y1) - max(c.y0, cb.y0)   # vertical overlap
            if c.y1 <= cb.y0 + 3 and hover > -20:
                d = cb.y0 - c.y1            # cluster above caption
            elif c.y0 >= cb.y1 - 3 and hover > -20:
                d = (c.y0 - cb.y1) + 15     # below caption (slightly less likely)
            elif vover > 0:
                d = gap(c, cb) + 5          # beside caption
            else:
                d = gap(c, cb) + 60
            d -= 0.02 * c.get_area() ** 0.5   # slight preference for larger graphics
            if d < best_d:
                best, best_d = i, d
        if best is None:
            continue
        reg = fitz.Rect(clusters[best])
        # pull in unclaimed neighbours (sibling panels), never crossing another caption or body text
        others = [b for b in blockers if b != cb]
        claimed = {i for _, _, i in results} | {best}
        grew = True
        while grew:
            grew = False
            for j, c2 in enumerate(clusters):
                if j in claimed or gap(reg, c2) > 60:
                    continue
                merged = reg | c2
                if any(_covers(merged, b) for b in others):
                    continue
                reg, grew = merged, True
                claimed.add(j)
        results.append((cap, reg, best))
        used.add(best)

    # several captions must not claim the same cluster: keep the closest
    seen = {}
    final = []
    for cap, rect, idx in results:
        if idx in seen:
            prev = seen[idx]
            d_prev = gap(prev[1], prev[0]["bbox"])
            d_new = gap(rect, cap["bbox"])
            if d_new < d_prev:
                final.remove(prev)
                seen[idx] = (cap, rect)
                final.append((cap, rect))
        else:
            seen[idx] = (cap, rect)
            final.append((cap, rect))
    return [(c, r) for c, r in final]


# ---------------------------------------------------------------- rendering ---

def render(page, rect, path, dpi=DPI, cap=None):
    pad = 4
    clip = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad) & page.rect
    if cap is not None:  # never include the caption's own lines
        cb = cap["bbox"]
        if rect.y0 >= cb.y1 - 3:
            clip.y0 = max(clip.y0, cb.y1 + 1)
        elif rect.y1 <= cb.y0 + 3:
            clip.y1 = min(clip.y1, cb.y0 - 1)
    scale = dpi / 72.0
    # cap total pixels
    while clip.width * scale * clip.height * scale > MAX_PIXELS and scale > 1:
        scale *= 0.8
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
    pix.save(path)
    return pix.width, pix.height, clip


def name_key(n):
    m = re.search(r"\d+", n)
    return (1 if n.upper().startswith("S") else 0, int(m.group()) if m else 0, n)


def extract_pdf(pdf_path, outdir, stem=None):
    """Return the same structure the figure-extractor 'data' object has."""
    stem = stem or os.path.splitext(os.path.basename(pdf_path))[0]
    os.makedirs(outdir, exist_ok=True)
    doc = fitz.open(pdf_path)
    figures = []
    seen_names = set()
    for pno in range(len(doc)):
        page = doc[pno]
        try:
            regs = figure_regions(page)
        except Exception as e:  # never fail the whole document on one page
            sys.stderr.write(f"page {pno + 1}: {e}\n")
            continue
        for cap, rect in regs:
            key = cap["name"].upper()
            if key in seen_names:
                continue
            seen_names.add(key)
            fname = f"{stem}-Figure{cap['name']}-1.png"
            w, h, clip = render(page, rect, os.path.join(outdir, fname), cap=cap)
            figures.append({
                "caption": cap["text"],
                "captionBoundary": {"x1": cap["bbox"].x0, "y1": cap["bbox"].y0, "x2": cap["bbox"].x1, "y2": cap["bbox"].y1},
                "figType": "Figure",
                "imageText": [],
                "name": cap["name"],
                "page": pno,
                "regionBoundary": {"x1": clip.x0, "y1": clip.y0, "x2": clip.x1, "y2": clip.y1},
                "renderDpi": DPI,
                "renderURL": fname,
                "source": "caption-anchored",
                "pixelSize": [w, h],
            })
    figures.sort(key=lambda f: name_key(f["name"]))
    doc.close()
    return figures


# --------------------------------------------------- tables via pdffigures2 ---

def tables_from_pdffigures2(pdf_path, outdir, stem, have_figures):
    """Ask a pdffigures2 service for tables (and any figures we missed)."""
    import urllib.request
    tables, extra_figs = [], []
    boundary = uuid.uuid4().hex
    with open(pdf_path, "rb") as fh:
        payload = fh.read()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{stem}.pdf\"\r\n"
            f"Content-Type: application/pdf\r\n\r\n").encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(PDFFIGURES2_URL + "/extract", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())["data"]
    except Exception as e:
        sys.stderr.write(f"pdffigures2 unavailable ({e}); returning figures only\n")
        return [], []

    def grab(item, prefix):
        fname = os.path.basename(item["renderURL"])
        newname = f"{stem}-{prefix}-{fname}"
        try:
            with urllib.request.urlopen(f"{PDFFIGURES2_URL}/download/{urllib.parse.quote(fname)}", timeout=120) as r:
                with open(os.path.join(outdir, newname), "wb") as out:
                    out.write(r.read())
        except Exception:
            return None
        item = dict(item)
        item["renderURL"] = newname
        item["source"] = "pdffigures2"
        return item

    import urllib.parse
    for t in data.get("tables", []):
        g = grab(t, "pf2")
        if g:
            tables.append(g)
    for f in data.get("figures", []):
        if f["name"].upper() not in have_figures:
            g = grab(f, "pf2")
            if g:
                extra_figs.append(g)
    return tables, extra_figs


def run(pdf_path, outdir, stem=None):
    t0 = time.time()
    stem = stem or os.path.splitext(os.path.basename(pdf_path))[0]
    figures = extract_pdf(pdf_path, outdir, stem)
    have = {f["name"].upper() for f in figures}
    tables, extra = tables_from_pdffigures2(pdf_path, outdir, stem, have)
    figures += extra
    figures.sort(key=lambda f: name_key(f["name"]))
    meta_name = f"{stem}.json"
    with open(os.path.join(outdir, meta_name), "w") as fh:
        json.dump(figures + tables, fh, indent=1)
    return {
        "document": stem,
        "figures": figures,
        "tables": tables,
        "metadata_file": meta_name,
        "metadata_filename": meta_name,
        "num_figures": len(figures),
        "num_tables": len(tables),
        "time_in_millis": int((time.time() - t0) * 1000),
    }


# ------------------------------------------------------------------- server ---

def make_app(workdir):
    from flask import Flask, jsonify, request, send_from_directory, abort
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
    os.makedirs(workdir, exist_ok=True)

    @app.get("/health")
    def health():
        return jsonify({"service": "figure-extractor", "flavor": "caption-anchored", "status": "healthy",
                        "version": "2.0.0", "timestamp": time.time()})

    @app.post("/extract")
    def extract():
        f = request.files.get("file")
        if not f:
            return jsonify({"success": False, "message": "no file"}), 400
        job = uuid.uuid4().hex[:8]
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.splitext(f.filename or "doc")[0])[:60] or "doc"
        stem = f"{stem}_{job}"
        pdf = os.path.join(workdir, stem + ".pdf")
        f.save(pdf)
        try:
            data = run(pdf, workdir, stem)
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500
        finally:
            try:
                os.remove(pdf)
            except OSError:
                pass
        return jsonify({"success": True, "message": "Figures extracted successfully", "data": data})

    @app.get("/download/<path:name>")
    def download(name):
        name = os.path.basename(name)
        if not os.path.exists(os.path.join(workdir, name)):
            abort(404)
        return send_from_directory(workdir, name)

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5002)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--cli", nargs=2, metavar=("PDF", "OUTDIR"))
    a = ap.parse_args()
    if a.cli:
        res = run(a.cli[0], a.cli[1])
        for f in res["figures"] + res["tables"]:
            print(f["figType"], f["name"], "p", f["page"] + 1, f.get("source"), f["renderURL"], f.get("pixelSize", ""))
        return
    app = make_app(os.path.join(tempfile.gettempdir(), "figure-server-work"))
    app.run(host=a.host, port=a.port, threaded=True)


if __name__ == "__main__":
    main()
