#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.24"]
# ///
"""
ispy.py – generator for "Jeg ser"-fargeleggingsoppgaver (I Spy / finn-og-tell).

Bruk:
    uv run ispy.py fetch                      # hent ikonpakker (en gang)
    uv run ispy.py list                       # vis tilgjengelige temaer
    uv run ispy.py make paaske                # lag én oppgave + fasit
    uv run ispy.py make halloween jul --copies 3
    uv run ispy.py make all --seed 42 --paper A4 --legend 24

Temaer defineres i themes.toml – legg til egne uten å røre scriptet.
Ikoner refereres som "pakke:navn", f.eks. "tabler:egg", "lucide:rabbit",
"phosphor:flower-tulip", "builtin:egg-stripes" eller "custom:mitt-ikon"
(egne SVG-er i icons/custom/).
"""
from __future__ import annotations

import argparse
import io
import json
import math
import random
import sys
import tarfile
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ICON_DIR = HERE / "icons"
THEMES_FILE = HERE / "themes.toml"
DEFAULT_OUT = HERE / "out"

# --------------------------------------------------------------------------
# Ikonpakker (alle permissive lisenser, vektor/SVG)
# --------------------------------------------------------------------------
PACKS = {
    "tabler": dict(npm="@tabler/icons", version="3.49.0", subdir="icons/outline",
                   license="MIT", label="Tabler Icons"),
    "lucide": dict(npm="lucide-static", version="1.55.0", subdir="icons",
                   license="ISC", label="Lucide"),
    "phosphor": dict(npm="@phosphor-icons/core", version="2.1.1", subdir="assets/regular",
                     license="MIT", label="Phosphor Icons"),
}
STROKE_W = 1.9  # linjetykkelse i 24-enheters ikonboks (tykkere = enklere å fargelegge)

PAPERS = {"A4": (210.0, 297.0), "A3": (297.0, 420.0), "Letter": (215.9, 279.4)}
PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080",
           "#9a6324", "#f032e6", "#808000", "#000075"]

# --------------------------------------------------------------------------
# Innebygde ikoner (egg med mønstre – lager variasjon som i originalen)
# --------------------------------------------------------------------------
EGG = "M12 2.8c-4.2 0-6.6 6.6-6.6 11a6.6 6.6 0 0 0 13.2 0c0-4.4-2.4-11-6.6-11z"


def _egg_polygon(inset: float = 0.86, n: int = 40) -> list[tuple[float, float]]:
    """Konveks polygon som følger eggskallet (innskrudd mot midten).
    Mønstre klippes mot denne i Python, så SVG-en slipper clip-path
    (som ikke alle renderere støtter)."""
    def bez(p0, c1, c2, p3, t):
        u = 1 - t
        return (u**3 * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * p3[1])
    top, left, right = (12.0, 2.8), None, None
    left = [bez(top, (7.8, 2.8), (5.4, 9.4), (5.4, 13.8), i / n) for i in range(n + 1)]
    arc = [(12 + 6.6 * math.cos(math.pi - math.pi * i / n), 13.8 + 6.6 * math.sin(math.pi * i / n))
           for i in range(1, n)]
    rgt = [bez(top, (16.2, 2.8), (18.6, 9.4), (18.6, 13.8), i / n) for i in range(n + 1)]
    poly = left + arc + rgt[::-1][:-1]            # mot klokka i skjermkoordinater
    cx, cy = 12.0, 10.5
    return [(cx + (x - cx) * inset, cy + (y - cy) * inset) for x, y in poly]


_EGG_POLY = None


def _clip_seg(p, q):
    """Cyrus–Beck: klipper segment p→q mot den konvekse egg-polygonen."""
    global _EGG_POLY
    if _EGG_POLY is None:
        _EGG_POLY = _egg_polygon()
    poly = _EGG_POLY
    # sørg for konsistent orientering
    area = sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
               for i in range(len(poly)))
    sgn = 1 if area > 0 else -1
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        ex, ey = b[0] - a[0], b[1] - a[1]
        nx, ny = -ey * sgn, ex * sgn          # innoverrettet normal
        num = (p[0] - a[0]) * nx + (p[1] - a[1]) * ny
        den = dx * nx + dy * ny
        if abs(den) < 1e-12:
            if num < 0:
                return None
            continue
        t = -num / den
        if den > 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return (p[0] + t0 * dx, p[1] + t0 * dy), (p[0] + t1 * dx, p[1] + t1 * dy)


def _clipped(polylines, step: float = 0.5) -> str:
    """Tar polylinjer (lister av punkter), deler dem opp og klipper mot egget."""
    d = []
    for pl in polylines:
        pts = []
        for (x0, y0), (x1, y1) in zip(pl, pl[1:]):
            k = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
            pts += [(x0 + (x1 - x0) * i / k, y0 + (y1 - y0) * i / k) for i in range(k)]
        pts.append(pl[-1])
        run = []
        for a, b in zip(pts, pts[1:]):
            seg = _clip_seg(a, b)
            if seg is None:
                if len(run) > 1:
                    d.append("M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in run))
                run = []
                continue
            if not run:
                run = [seg[0]]
            run.append(seg[1])
            if seg[1] != b:                      # forlot egget
                d.append("M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in run))
                run = []
        if len(run) > 1:
            d.append("M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in run))
    return f'<path stroke-width="1.1" d="{"".join(d)}"/>'


def _hlines(ys):
    return [[(0, y), (24, y)] for y in ys]


def _wave(y, amp=1.1, period=3.0):
    pts, x = [], 0.0
    while x <= 24:
        pts.append((x, y + amp * math.sin(2 * math.pi * x / period)))
        x += 0.4
    return pts


def _zig(y, amp=1.1, period=3.0):
    pts, x, up = [], 0.0, True
    while x <= 24 + period:
        pts.append((x, y + (-amp if up else amp)))
        x += period / 2
        up = not up
    return pts


def _dots(rows):
    out = []
    for r, y in enumerate(rows):
        for i in range(6):
            x = 6 + 3.0 * i + (1.5 if r % 2 else 0)
            if _clip_seg((x, y), (x + 0.01, y)) is not None:
                out.append(f'<circle cx="{x:.1f}" cy="{y}" r=".55" fill="#000" stroke="none"/>')
    return "".join(out)


def _outline(extra: str = "") -> str:
    return f'<path d="{EGG}"/>{extra}'


BUILTIN = {
    "egg-plain": lambda: _outline('<path d="M8.6 15a3.6 3.6 0 0 0 2.2 3.1"/>'),
    "egg-stripes": lambda: _outline(_clipped(_hlines([7.5, 10.5, 13.5, 16.5, 19.5]))),
    "egg-dots": lambda: _outline(_dots([7.5, 10.5, 13.5, 16.5, 19.0])),
    "egg-zigzag": lambda: _outline(_clipped([_zig(10.5), _zig(16.0)])),
    "egg-wave": lambda: _outline(_clipped([_wave(10.5), _wave(16.0)])),
    "egg-spots": lambda: _outline(
        '<circle cx="10.5" cy="9.5" r="1"/><circle cx="14" cy="12" r="1.2"/>'
        '<circle cx="10" cy="15" r="1.2"/><circle cx="14" cy="17.2" r=".9"/>'),
    "egg-diagonal": lambda: _outline(_clipped([[(0, 14 + k * 5), (14 + k * 5, 0)] for k in range(-1, 5)])),
    "egg-bands": lambda: _outline(_clipped(_hlines([9.5, 15.5])) + _clipped([[(0, 12.5), (24, 12.5)]]).replace(
        'stroke-width="1.1"', 'stroke-width="1.1" stroke-dasharray="1 1.6"')),
}


# --------------------------------------------------------------------------
# Ikon-lasting
# --------------------------------------------------------------------------
def icon_file(ref: str) -> Path:
    pack, _, name = ref.partition(":")
    return ICON_DIR / pack / f"{name}.svg"


def icon_exists(ref: str) -> bool:
    pack, _, name = ref.partition(":")
    return name in BUILTIN if pack == "builtin" else icon_file(ref).exists()


def icon_group(ref: str, gid: str) -> str:
    """Returnerer <g id=gid> med ikonet normalisert til en 24x24-boks, svart strek."""
    pack, _, name = ref.partition(":")
    stroke = (f'fill="none" stroke="#000" stroke-width="{STROKE_W}" '
              f'stroke-linecap="round" stroke-linejoin="round"')
    if pack == "builtin":
        return f'<g id="{gid}" {stroke}>{BUILTIN[name]()}</g>'

    root = ET.fromstring(icon_file(ref).read_text(encoding="utf-8"))
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]
    for parent in list(root.iter()):
        for ch in list(parent):
            if ch.tag in ("title", "desc", "metadata"):
                parent.remove(ch)
    vb = [float(v) for v in root.get("viewBox", "0 0 24 24").replace(",", " ").split()]
    scale = 24.0 / vb[2]
    inner = "".join(ET.tostring(c, encoding="unicode") for c in root).replace("currentColor", "#000")
    is_stroke = root.get("fill") == "none" or root.get("stroke") not in (None, "none")
    if is_stroke:
        attrs = (f'fill="none" stroke="#000" stroke-width="{STROKE_W / scale:.3f}" '
                 f'stroke-linecap="round" stroke-linejoin="round"')
    else:
        attrs = 'fill="#000" stroke="none"'
    return f'<g id="{gid}"><g transform="translate({-vb[0] * scale:.3f} {-vb[1] * scale:.3f}) scale({scale:.5f})" {attrs}>{inner}</g></g>'


# --------------------------------------------------------------------------
# Tema
# --------------------------------------------------------------------------
def load_themes() -> tuple[dict, dict]:
    with open(THEMES_FILE, "rb") as f:
        data = tomllib.load(f)
    return data.get("defaults", {}), data.get("themes", {})


# --------------------------------------------------------------------------
# Scene / layout
# --------------------------------------------------------------------------
@dataclass
class Cell:
    x: float
    y: float
    rot: float
    size: float
    flip: bool
    ref: str
    target: int | None  # indeks i legend, eller None for blindikon


@dataclass
class Scene:
    W: float
    H: float
    M: float
    gx: float
    gy: float
    cols: int
    rows: int
    cell: float
    legend: list[tuple[str, int]]
    cells: list[Cell] = field(default_factory=list)
    legend_cols: int = 10
    legend_y: float = 0
    seed: int = 0


def build_scene(refs: list[str], seed: int, legend_n: int, cell: float, paper: str) -> Scene:
    rng = random.Random(seed)
    W, H = PAPERS[paper]
    M = 10.0
    title_h = 32.0 * (W / 210.0)
    pool = list(dict.fromkeys(refs))
    min_fillers = 4
    legend_n = min(legend_n, len(pool) - min_fillers)
    if legend_n < 4:
        sys.exit(f"For få ikoner i temaet ({len(pool)}); trenger minst {4 + min_fillers}.")

    legend_cols = max(4, int((W - 2 * M) // 19))
    legend_rows = math.ceil(legend_n / legend_cols)
    row_h = 11.5
    credit_h = 6.0
    legend_y = H - M - credit_h - legend_rows * row_h
    gy = M + title_h
    avail_h = legend_y - 4 - gy
    cols = int((W - 2 * M) // cell)
    rows = int(avail_h // cell)
    gx = (W - cols * cell) / 2
    total = cols * rows

    def concept(r: str) -> str:
        pack, _, name = r.partition(":")
        return r if pack == "builtin" else name.split("-")[0]

    shuffled = rng.sample(pool, len(pool))
    targets, seen = [], set()
    for r in shuffled:                       # først ulike motiver, så resten ved behov
        if concept(r) not in seen:
            targets.append(r); seen.add(concept(r))
        if len(targets) == legend_n:
            break
    for r in shuffled:
        if len(targets) == legend_n:
            break
        if r not in targets:
            targets.append(r)
    fillers = [r for r in pool if r not in targets]
    counts = [rng.choices(range(1, 10), weights=[2, 4, 5, 6, 6, 5, 4, 2, 1])[0] for _ in targets]
    budget = int(total * 0.58)
    while sum(counts) > budget:
        i = rng.choice([i for i, c in enumerate(counts) if c > 1] or [0])
        counts[i] = max(1, counts[i] - 1)
        if all(c == 1 for c in counts):
            break

    # fyll de resterende rutene med blindikoner – alle typer brukes minst én gang
    n_fill = total - sum(counts)
    fill_refs = list(fillers) if n_fill >= len(fillers) else rng.sample(fillers, n_fill)
    fill_refs += rng.choices(fillers, k=n_fill - len(fill_refs))
    assignment: list[tuple[str, int | None]] = (
        [(t, i) for i, (t, c) in enumerate(zip(targets, counts)) for _ in range(c)]
        + [(f, None) for f in fill_refs])
    rng.shuffle(assignment)

    scene = Scene(W, H, M, gx, gy, cols, rows, cell, list(zip(targets, counts)),
                  legend_cols=legend_cols, legend_y=legend_y, seed=seed)
    j = cell * 0.07
    for k, (ref, tgt) in enumerate(assignment):
        r, c = divmod(k, cols)
        scene.cells.append(Cell(
            x=gx + (c + 0.5) * cell + rng.uniform(-j, j),
            y=gy + (r + 0.5) * cell + rng.uniform(-j, j),
            rot=rng.uniform(-14, 14),
            size=cell * rng.uniform(0.78, 0.9),
            flip=rng.random() < 0.5,
            ref=ref, target=tgt))
    return scene


# --------------------------------------------------------------------------
# SVG-rendering
# --------------------------------------------------------------------------
def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_svg(scene: Scene, title: str, subtitle: str, solution: bool = False) -> str:
    s = scene
    used = list(dict.fromkeys([r for r, _ in s.legend] + [c.ref for c in s.cells]))
    ids = {ref: f"i{n}" for n, ref in enumerate(used)}
    defs = "".join(icon_group(ref, ids[ref]) for ref in used)

    def use(ref, x, y, size, rot=0.0, flip=False):
        sc = size / 24.0
        sx = -sc if flip else sc
        return (f'<use xlink:href="#{ids[ref]}" transform="translate({x:.2f} {y:.2f}) '
                f'rotate({rot:.1f}) scale({sx:.4f} {sc:.4f}) translate(-12 -12)"/>')

    font = 'font-family="DejaVu Sans, Helvetica, Arial, sans-serif" font-weight="bold"'
    out: list[str] = []

    # --- tittelbånd
    fs_t, fs_s = 17.0 * s.W / 210, 7.0 * s.W / 210
    tw = len(title) * 0.64 * fs_t
    sw = len(subtitle) * 0.66 * fs_s
    total = tw + 4 + sw
    bw = min(total + 36, s.W - 2 * s.M - 4)
    bh = 22.0 * s.W / 210
    x0, x1 = s.W / 2 - bw / 2, s.W / 2 + bw / 2
    y0 = s.M + 2
    ym, y1 = y0 + bh / 2, y0 + bh
    n = 6
    out.append(f'<polygon points="{x0:.1f},{y0} {x1:.1f},{y0} {x1 - n:.1f},{ym} {x1:.1f},{y1} {x0:.1f},{y1} {x0 + n:.1f},{ym}" '
               f'fill="#fff" stroke="#000" stroke-width="0.7"/>')
    tx = s.W / 2 - total / 2
    out.append(f'<text x="{tx:.2f}" y="{ym + fs_t * 0.36:.2f}" font-size="{fs_t:.1f}" {font}>{esc(title)}</text>')
    out.append(f'<text x="{tx + tw + 4:.2f}" y="{ym + fs_t * 0.36:.2f}" font-size="{fs_s:.1f}" {font}>{esc(subtitle)}</text>')
    if solution:
        out.append(f'<text x="{s.M}" y="{s.M - 2}" font-size="5" {font} fill="#e6194b">FASIT</text>')

    # --- ramme + rutenett
    out.append(f'<rect x="{s.gx}" y="{s.gy}" width="{s.cols * s.cell}" height="{s.rows * s.cell}" '
               f'fill="none" stroke="#000" stroke-width="0.7"/>')
    for c in s.cells:
        out.append(use(c.ref, c.x, c.y, c.size, c.rot, c.flip))
    if solution:
        for c in s.cells:
            if c.target is None:
                continue
            col = PALETTE[c.target % len(PALETTE)]
            r = s.cell * 0.46
            out.append(f'<circle cx="{c.x:.2f}" cy="{c.y:.2f}" r="{r:.2f}" fill="none" stroke="{col}" stroke-width="0.5"/>')
            out.append(f'<text x="{c.x + r * 0.55:.2f}" y="{c.y - r * 0.55:.2f}" font-size="2.6" fill="{col}" '
                       f'{font} text-anchor="middle">{c.target + 1}</text>')

    # --- tellelisten
    ew = (s.W - 2 * s.M) / s.legend_cols
    row_h = 11.5
    for i, (ref, cnt) in enumerate(s.legend):
        r, c = divmod(i, s.legend_cols)
        lx = s.M + c * ew
        ly = s.legend_y + r * row_h + row_h / 2
        out.append(f'<text x="{lx + 1:.2f}" y="{ly + 2.4:.2f}" font-size="7" {font}>{cnt}</text>')
        out.append(use(ref, lx + ew * 0.5 + 2, ly, 8.0))
        if solution:
            col = PALETTE[i % len(PALETTE)]
            out.append(f'<circle cx="{lx + ew * 0.5 + 2:.2f}" cy="{ly:.2f}" r="5" fill="none" stroke="{col}" stroke-width="0.5"/>')
            out.append(f'<text x="{lx + ew * 0.5 + 6.5:.2f}" y="{ly - 3:.2f}" font-size="2.6" fill="{col}" {font}>{i + 1}</text>')

    # --- kreditering
    packs = sorted({r.split(":")[0] for r in used if r.split(":")[0] in PACKS})
    lic = ", ".join(f"{PACKS[p]['label']} ({PACKS[p]['license']})" for p in packs)
    out.append(f'<text x="{s.W / 2}" y="{s.H - s.M + 1}" font-size="2.4" fill="#777" text-anchor="middle" '
               f'font-family="DejaVu Sans, Helvetica, Arial, sans-serif">Ikoner: {esc(lic)}  ·  seed {s.seed}</text>')

    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{s.W}mm" height="{s.H}mm" viewBox="0 0 {s.W} {s.H}">'
            f'<rect width="{s.W}" height="{s.H}" fill="#fff"/>'
            f'<defs>{defs}</defs>{"".join(out)}</svg>')


def write_outputs(svg: str, base: Path, formats: set[str], paper: str) -> list[Path]:
    written = []
    base.parent.mkdir(parents=True, exist_ok=True)
    if "svg" in formats:
        p = base.with_suffix(".svg")
        p.write_text(svg, encoding="utf-8")
        written.append(p)
    if formats & {"pdf", "png"}:
        import pymupdf  # ingen native systembiblioteker nødvendig
        doc = pymupdf.open(stream=svg.encode("utf-8"), filetype="svg")
        if "pdf" in formats:
            p = base.with_suffix(".pdf")
            p.write_bytes(doc.convert_to_pdf())
            written.append(p)
        if "png" in formats:
            p = base.with_suffix(".png")
            doc[0].get_pixmap(dpi=200).save(str(p))
            written.append(p)
    return written


# --------------------------------------------------------------------------
# Kommandoer
# --------------------------------------------------------------------------
def http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def cmd_fetch(args) -> None:
    for pack, cfg in PACKS.items():
        dest = ICON_DIR / pack
        if dest.exists() and any(dest.glob("*.svg")) and not args.force:
            print(f"✓ {pack}: allerede hentet ({len(list(dest.glob('*.svg')))} ikoner)")
            continue
        print(f"↓ {pack}: {cfg['npm']}@{cfg['version']} …")
        meta = json.loads(http_get(f"https://registry.npmjs.org/{cfg['npm']}/{cfg['version']}"))
        data = http_get(meta["dist"]["tarball"])
        dest.mkdir(parents=True, exist_ok=True)
        sub, n = cfg["subdir"] + "/", 0
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            for m in tf.getmembers():
                rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
                if rel.startswith(sub) and rel.endswith(".svg") and "/" not in rel[len(sub):]:
                    (dest / Path(rel).name).write_bytes(tf.extractfile(m).read())
                    n += 1
                elif rel in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
                    (dest / "LICENSE").write_bytes(tf.extractfile(m).read())
        print(f"  {n} ikoner → {dest}")
    (ICON_DIR / "custom").mkdir(parents=True, exist_ok=True)


def cmd_list(args) -> None:
    _, themes = load_themes()
    for key, t in themes.items():
        refs = t["icons"]
        missing = [r for r in refs if not icon_exists(r)]
        flag = f"  ⚠ mangler: {', '.join(missing)}" if missing else ""
        print(f"{key:12} {t.get('subtitle', ''):14} {len(refs):3} ikoner{flag}")


def cmd_make(args) -> None:
    defaults, themes = load_themes()
    keys = list(themes) if "all" in args.themes else args.themes
    for k in keys:
        if k not in themes:
            sys.exit(f"Ukjent tema '{k}'. Tilgjengelig: {', '.join(themes)}")
    formats = {f.strip() for f in args.formats.split(",")}
    out_dir = Path(args.out)
    base_seed = args.seed if args.seed is not None else random.randrange(1000, 9999)

    for k in keys:
        t = themes[k]
        missing = [r for r in t["icons"] if not icon_exists(r)]
        if missing:
            sys.exit(f"Tema '{k}' har ikoner som ikke finnes: {missing}\n"
                     f"Kjør `uv run ispy.py fetch` eller rett themes.toml.")
        title = args.title or t.get("title") or defaults.get("title", "JEG SER")
        subtitle = args.subtitle if args.subtitle is not None else t.get("subtitle", k.capitalize())
        for n in range(args.copies):
            seed = base_seed + n
            scene = build_scene(t["icons"], seed, args.legend, args.cell, args.paper)
            stem = f"{k}-{seed}"
            files = write_outputs(render_svg(scene, title, subtitle), out_dir / stem, formats, args.paper)
            if not args.no_solution:
                files += write_outputs(render_svg(scene, title, subtitle, solution=True),
                                       out_dir / f"{stem}-fasit", formats, args.paper)
            total = sum(c for _, c in scene.legend)
            print(f"{k} (seed {seed}): {scene.cols}×{scene.rows} ruter, {len(scene.legend)} gjenstander, "
                  f"{total} å finne")
            for f in files:
                print(f"  {f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generator for 'Jeg ser'-fargeleggingsoppgaver")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="hent ikonpakker fra npm")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("list", help="vis temaer og sjekk at ikonene finnes")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("make", help="lag oppgaver")
    p.add_argument("themes", nargs="+", help="temanavn fra themes.toml, eller 'all'")
    p.add_argument("--seed", type=int, help="gir reproduserbar oppgave (standard: tilfeldig)")
    p.add_argument("--copies", type=int, default=1, help="antall ulike oppgaver per tema")
    p.add_argument("--legend", type=int, default=24, help="antall gjenstander i tellelisten")
    p.add_argument("--cell", type=float, default=14.0, help="rutestørrelse i mm (mindre = flere ikoner)")
    p.add_argument("--paper", choices=PAPERS, default="A4")
    p.add_argument("--title", help="overstyr tittel (standard: JEG SER)")
    p.add_argument("--subtitle", help="overstyr undertittel")
    p.add_argument("--formats", default="svg,pdf", help="svg,pdf,png")
    p.add_argument("--no-solution", action="store_true", help="ikke lag fasit")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.set_defaults(fn=cmd_make)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
