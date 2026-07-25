"""Build unhinted fonts from font binaries.

Pipeline (per font):

    font binary  --extractor(doInstructions=False)-->  UFO (in memory)
                 --clear CFF hints-->                  (no hinting info)
                 --fix languagesystem / drop invalid lookups-->
                 --ufo2ft compileTTF-->                {stem}-unhint.ttf

The extraction uses extractor's internal ``extractFontFromOpenType(...,
doInstructions=False)`` so TrueType hinting (``fpgm``/``prep``/``cvt`` and
per-glyph instructions, plus the composite grid-fitting flags) is never
created, and PostScript (CFF Private DICT) hinting values are cleared from
``fontinfo.plist``.  The result is a hint-free UFO for both TTF and OTF inputs.

The UFO is kept in memory and compiled directly; it is only written to disk
when ``--keep-ufo`` is given.

Usage
-----
    python build_unhint.py ttf_ori                         # -> ttf_out/*-unhint.ttf
    python build_unhint.py ttf_ori --keep-ufo              # also keep ufo_ori/*.ufo
    python build_unhint.py ttf_ori --out-dir out --ufo-dir ufo --keep-ufo
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from extractor import extractFormat
from extractor.formats.opentype import extractFontFromOpenType
from ufoLib2 import Font
from ufo2ft import compileTTF

# Font binaries we accept as input.  extractor detects the real format per file.
FONT_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2"}

# PostScript (CFF Private DICT) hinting attributes stored in fontinfo.plist.
# Cleared so the result is hint-free for CFF/OTF inputs too.  No-op for TTF.
CFF_HINT_ATTRS = (
    "postscriptBlueValues",
    "postscriptOtherBlues",
    "postscriptFamilyBlues",
    "postscriptFamilyOtherBlues",
    "postscriptStemSnapH",
    "postscriptStemSnapV",
    "postscriptBlueFuzz",
    "postscriptBlueShift",
    "postscriptBlueScale",
    "postscriptForceBold",
)


# --------------------------------------------------------------------------- #
# UFO feature sanitization
# --------------------------------------------------------------------------- #
def fix_languagesystem_order(ufo: Font) -> None:
    """Ensure ``languagesystem DFLT dflt;`` is the first languagesystem statement.

    feaLib requires that, if present, it comes first and appears at most once.
    """
    if not ufo.features.text:
        return

    lines = ufo.features.text.splitlines()
    ls_indices = [i for i, l in enumerate(lines) if l.strip().startswith("languagesystem")]
    if not ls_indices:
        return

    ls_raw = [lines[i] for i in ls_indices]

    def is_dflt(s: str) -> bool:
        return bool(re.match(r"languagesystem\s+DFLT\s+dflt\s*;\s*$", s.strip()))

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip())

    # Collect DFLT dflt separately; dedupe the rest preserving order.
    seen: set[str] = set()
    rest: list[str] = []
    has_dflt = False
    for s in ls_raw:
        if is_dflt(s):
            has_dflt = True
        else:
            n = norm(s)
            if n not in seen:
                seen.add(n)
                rest.append(s.rstrip())

    if not has_dflt:
        return  # no DFLT dflt statement to fix

    # Already first and no duplicates -> nothing to do.
    if is_dflt(ls_raw[0]) and len(ls_raw) == 1 + len(rest):
        return

    reordered = ["languagesystem DFLT dflt;"] + rest

    # Rebuild file: emit the reordered block in place of the original
    # languagesystem lines, dropping any duplicates beyond the unique set.
    new_lines: list[str] = []
    ls_idx_set = set(ls_indices)
    ri = 0
    for i, line in enumerate(lines):
        if i in ls_idx_set:
            if ri < len(reordered):
                new_lines.append(reordered[ri])
                ri += 1
            # else: drop duplicate
        else:
            new_lines.append(line)

    ufo.features.text = "\n".join(new_lines)
    print("  fixed languagesystem order: 'DFLT dflt' moved to first position")


def clean_invalid_lookups(ufo: Font) -> None:
    """Drop lookups that are referenced but never defined in features.fea."""
    if not ufo.features.text:
        return

    text = ufo.features.text
    lines = text.splitlines()

    defined = set(re.findall(r"lookup\s+(\w+)\s*\{", text))
    referenced = set(re.findall(r"lookup\s+(\w+)\s*;", text))
    invalid = referenced - defined
    if not invalid:
        return

    print(f"  dropping invalid lookups: {', '.join(sorted(invalid))}")
    cleaned = [line for line in lines if not any(name in line for name in invalid)]
    ufo.features.text = "\n".join(cleaned)


# --------------------------------------------------------------------------- #
# Extraction (no-hint fast path)
# --------------------------------------------------------------------------- #
def clear_cff_hints(ufo: Font) -> int:
    """Drop PostScript hinting values from ``ufo.info``. Returns count removed."""
    removed = 0
    for attr in CFF_HINT_ATTRS:
        if getattr(ufo.info, attr, None) is not None:
            setattr(ufo.info, attr, None)
            removed += 1
    return removed


def extract_ufo(src: Path) -> Font:
    """Extract one font binary into a hint-free in-memory UFO."""
    fmt = extractFormat(str(src))
    if fmt != "OTF":
        # doInstructions only exists on the OpenType handler.  WOFF / Type1 /
        # ttx / vfb would need their own handling.
        raise ValueError(
            f"{src.name}: format {fmt!r} is not supported by the no-hint fast "
            f"path (only OpenType TTF/OTF)."
        )

    ufo = Font()
    # Source-level fix: tell extractor not to extract TrueType instructions.
    extractFontFromOpenType(str(src), ufo, doInstructions=False)
    cleared = clear_cff_hints(ufo)
    if cleared:
        print(f"  cleared {cleared} CFF hinting attribute(s)")
    return ufo


# --------------------------------------------------------------------------- #
# Compile
# --------------------------------------------------------------------------- #
def build_unhint_font(
    src: Path,
    out_dir: Path,
    ufo_dir: Path | None,
    keep_ufo: bool,
) -> Path:
    """Extract -> sanitize -> compile one font. Returns the output TTF path."""
    ufo = extract_ufo(src)

    fix_languagesystem_order(ufo)
    clean_invalid_lookups(ufo)

    ttf = compileTTF(ufo)
    out_path = out_dir / f"{src.stem}-unhint.ttf"
    ttf.save(str(out_path))

    if keep_ufo and ufo_dir is not None:
        ufo_dst = ufo_dir / f"{src.stem}.ufo"
        ufo.save(str(ufo_dst), overwrite=True)
        print(f"  kept UFO: {ufo_dst}")

    return out_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build unhinted fonts ({stem}-unhint.ttf) from font binaries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("dir", help="Input directory with font binaries (.ttf/.otf/.woff/.woff2).")
    ap.add_argument("--out-dir", default="./unhint_ttf", help="Output dir for unhinted fonts (default: ./unhint_ttf).")
    ap.add_argument("--ufo-dir", default="./ufo", help="Dir for kept UFOs (default: ./ufo); only used with --keep-ufo.")
    ap.add_argument("--keep-ufo", action="store_true", help="Keep extracted UFO sources after building.")
    args = ap.parse_args(argv)

    src_dir = Path(args.dir)
    if not src_dir.is_dir():
        print(f"Input directory not found: {src_dir}", file=sys.stderr)
        return 1

    fonts = sorted(
        p for p in src_dir.iterdir()
        if p.is_file() and p.suffix.lower() in FONT_SUFFIXES
    )
    if not fonts:
        print(f"No font files found in {src_dir}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ufo_dir: Path | None = None
    if args.keep_ufo:
        ufo_dir = Path(args.ufo_dir)
        ufo_dir.mkdir(parents=True, exist_ok=True)

    print(f"Building {len(fonts)} unhinted font(s): {src_dir} -> {out_dir}"
          + ("  (keeping UFOs)" if args.keep_ufo else ""))

    errors = 0
    for src in fonts:
        print(f"[build] {src.name}")
        try:
            out_path = build_unhint_font(src, out_dir, ufo_dir, args.keep_ufo)
            print(f"  -> {out_path}")
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"  !! failed: {exc}", file=sys.stderr)

    print("Done." + ("  (some fonts failed, see above)" if errors else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
