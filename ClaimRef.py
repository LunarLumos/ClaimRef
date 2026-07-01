#!/usr/bin/env python3
r"""
Citation Verification Report generator (LaTeX)
----------------------------------------------
Reads a LaTeX manuscript, resolves \input/\include, finds all
\cite{…} commands, extracts the sentence(s) containing each one (the
"claim"), records the enclosing section, links each citation to its
bibliography entry (from \begin{thebibliography} and/or .bib files),
and generates a "Citation Verification Report" PDF.

Usage:
    python citer.py paper.tex -o report.pdf

Requirements: reportlab, bibtexparser  (pip install reportlab bibtexparser)
"""

import argparse, re, logging, getpass
from pathlib import Path
from typing import Dict, List

try:
    import bibtexparser
except ImportError:
    bibtexparser = None

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    Table, TableStyle, HRFlowable,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("citer")

# ----------------------------------------------------------------------
# Branding
# ----------------------------------------------------------------------
TOOL_NAME = "ClaimRef"
TOOL_TAGLINE = "Citation Verification Report"
DEVELOPER = "Aifee Aadil"          # who built ClaimRef

def get_checker() -> str:
    """The person running the verification = the logged-in system user."""
    try:
        return getpass.getuser()
    except Exception:
        return "Unknown"

# ----------------------------------------------------------------------
# 0. Lightweight sentence tokenizer (no nltk dependency)
# ----------------------------------------------------------------------
_ABBREVS = [
    "e.g.", "i.e.", "et al.", "al.", "Fig.", "Figs.", "Eq.", "Eqs.",
    "Ref.", "Refs.", "cf.", "vs.", "viz.", "etc.", "Dr.", "Mr.", "Mrs.",
    "Ms.", "Prof.", "Vol.", "No.", "pp.", "approx.", "Inc.", "Ltd.", "St.",
]

_DOT = "\x00"  # sentinel standing in for a non-boundary period

def sent_tokenize(text: str) -> List[str]:
    """Split text into sentences, protecting common abbreviations and decimals."""
    protected = text
    for a in _ABBREVS:
        protected = protected.replace(a, a.replace(".", _DOT))
    # protect decimal numbers (3.14) and single-letter initials (J. K.)
    protected = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + _DOT + m.group(2), protected)
    protected = re.sub(r"\b([A-Z])\.", lambda m: m.group(1) + _DOT, protected)
    parts = re.split(r"(?<=[.!?])\s+", protected)
    return [p.replace(_DOT, ".").strip() for p in parts if p.strip()]

# ----------------------------------------------------------------------
# 1. LaTeX pre‑processing
# ----------------------------------------------------------------------
def resolve_input_include(content: str, current_dir: Path, seen: set) -> str:
    """Recursively replace \\input{…} and \\include{…} with the file's content."""
    pattern = re.compile(r"\\(input|include)\{([^}]+)\}")
    def replacer(m):
        fname = m.group(2).strip()
        if not fname.endswith(".tex"):
            fname += ".tex"
        path = (current_dir / fname).resolve()
        if path in seen:
            logger.warning(f"Circular include: {path}")
            return ""
        if not path.exists():
            logger.error(f"File not found: {path}")
            return ""
        seen.add(path)
        sub = path.read_text(encoding="utf-8")
        return resolve_input_include(sub, path.parent, seen)
    return pattern.sub(replacer, content)

def remove_comments(tex: str) -> str:
    """Strip LaTeX comments (lines starting with unescaped %)."""
    return re.sub(r"(?<!\\)%.*$", "", tex, flags=re.MULTILINE)

def find_matching_brace(text: str, start: int) -> int:
    """Given index of '{', return index of matching '}'."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("Unbalanced braces")

def strip_cmd_keep_content(tex: str, cmd: str) -> str:
    """Remove \\cmd{…} but leave its inner text (e.g., \\textbf{abc} → abc)."""
    pat = re.compile(rf"\\{cmd}\s*\{{")
    parts = []
    pos = 0
    for m in pat.finditer(tex):
        parts.append(tex[pos:m.start()])
        brace_start = m.end() - 1
        brace_end = find_matching_brace(tex, brace_start)
        parts.append(tex[brace_start+1:brace_end])
        pos = brace_end + 1
    parts.append(tex[pos:])
    return "".join(parts)

def strip_cmd_remove_content(tex: str, cmd: str) -> str:
    """Remove \\cmd{…} entirely."""
    pat = re.compile(rf"\\{cmd}\s*\{{")
    parts = []
    pos = 0
    for m in pat.finditer(tex):
        parts.append(tex[pos:m.start()])
        brace_start = m.end() - 1
        brace_end = find_matching_brace(tex, brace_start)
        pos = brace_end + 1
    parts.append(tex[pos:])
    return "".join(parts)

def remove_environments(tex: str, envs: List[str]) -> str:
    """Delete whole environments and their contents."""
    for env in envs:
        tex = re.sub(rf"\\begin\{{{env}\}}.*?\\end\{{{env}\}}",
                     "", tex, flags=re.DOTALL | re.IGNORECASE)
    return tex

def clean_tex_for_sentence(tex: str) -> str:
    """Remove LaTeX markup that does not belong to a readable sentence,
    but keep \\cite, \\citep, \\citet, \\citealp commands."""
    tex = remove_comments(tex)
    # math
    tex = re.sub(r"\$[^$]*\$", "", tex)
    tex = re.sub(r"\\\[.*?\\\]", "", tex, flags=re.DOTALL)
    tex = re.sub(r"\\\(.*?\\\)", "", tex, flags=re.DOTALL)
    # environments to drop (including thebibliography – we parse that separately)
    tex = remove_environments(tex, [
        "equation", "equation*", "align", "align*",
        "figure", "figure*", "table", "table*",
        "appendices", "thebibliography"
    ])
    # commands that take arguments: remove
    for c in ["footnote", "label", "ref"]:
        tex = strip_cmd_remove_content(tex, c)
    # formatting commands: keep content
    for c in ["textit", "textbf", "emph", "texttt", "textsf", "textsc"]:
        tex = strip_cmd_keep_content(tex, c)
    # simple size commands
    for c in ["small", "large", "Large", "LARGE", "huge", "Huge"]:
        tex = re.sub(rf"\\{c}\b", "", tex)
    # remove citeauthor and citeyear (we don't need them)
    tex = strip_cmd_remove_content(tex, "citeauthor")
    tex = strip_cmd_remove_content(tex, "citeyear")
    # drop leftover \begin{env}[..]{..} / \end{env} tags (but NOT section headings)
    tex = re.sub(r"\\(begin|end)\{[^}]*\}(\[[^\]]*\])?(\{[^}]*\})?", "", tex)
    # unescape common escaped characters and normalize dashes / ties
    for esc, rep in [(r"\%", "%"), (r"\&", "&"), (r"\_", "_"),
                     (r"\#", "#"), (r"\$", "$"), (r"~", " ")]:
        tex = tex.replace(esc, rep)
    tex = tex.replace("---", "—").replace("--", "–")
    tex = re.sub(r"\\\\", " ", tex)          # LaTeX line breaks
    # collapse whitespace
    tex = re.sub(r"\s+", " ", tex)
    return tex

# ----------------------------------------------------------------------
# 2. Parse thebibliography (from the ORIGINAL full text)
# ----------------------------------------------------------------------
def parse_thebibliography(tex: str) -> Dict[str, str]:
    """Extract all \\bibitem{key}… entries into a {key: reference_text} dict."""
    env = re.search(r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}", tex, re.DOTALL)
    if not env:
        logger.warning("No thebibliography environment found.")
        return {}
    bib = env.group(0)
    # remove the begin/end tags
    bib = re.sub(r"\\begin\{thebibliography\}.*?(\{.*?\})?", "", bib, count=1)
    bib = re.sub(r"\\end\{thebibliography\}", "", bib, count=1)
    entries = {}
    # split by \bibitem{key}
    for m in re.finditer(r"\\bibitem\s*\{([^}]+)\}\s*(.*?)(?=\\bibitem|$)", bib, re.DOTALL):
        key = m.group(1).strip()
        ref_text = m.group(2).strip()
        # clean the reference text (keep content, remove markup)
        ref_text = clean_tex_for_sentence(ref_text)
        entries[key] = ref_text
    return entries

# ----------------------------------------------------------------------
# 2b. Parse .bib files referenced by \bibliography / \addbibresource
# ----------------------------------------------------------------------
def _format_bibtex_entry(e: dict) -> str:
    """Turn a bibtexparser entry dict into a readable single-line reference."""
    authors = e.get("author", "").replace("\n", " ").strip()
    if authors:
        authors = "; ".join(a.strip() for a in re.split(r"\s+and\s+", authors))
    parts = []
    if authors:
        parts.append(authors + ".")
    title = e.get("title", "").strip()
    if title:
        parts.append(title.rstrip(".") + ".")
    venue = (e.get("journal") or e.get("booktitle") or e.get("publisher") or "").strip()
    if venue:
        parts.append(venue + ",")
    if e.get("volume"):
        parts.append(f"vol. {e['volume'].strip()},")
    if e.get("number"):
        parts.append(f"no. {e['number'].strip()},")
    if e.get("pages"):
        parts.append(f"pp. {e['pages'].strip().replace('--', '–')},")
    if e.get("year"):
        parts.append(f"{e['year'].strip()}.")
    ref = " ".join(parts)
    ref = re.sub(r"[{}]", "", ref)
    ref = re.sub(r"\s+", " ", ref).strip().rstrip(",") + ("" if ref.endswith(".") else "")
    return ref

def load_bibtex(full_tex: str, base_dir: Path) -> Dict[str, str]:
    """Load .bib files named by \\bibliography{…} or \\addbibresource{…}."""
    files = set()
    for m in re.finditer(r"\\bibliography\{([^}]+)\}", full_tex):
        for n in m.group(1).split(","):
            n = n.strip()
            if n:
                files.add(n if n.endswith(".bib") else n + ".bib")
    for m in re.finditer(r"\\addbibresource\{([^}]+)\}", full_tex):
        n = m.group(1).strip()
        if n:
            files.add(n if n.endswith(".bib") else n + ".bib")
    if not files:
        return {}
    if bibtexparser is None:
        logger.warning(".bib files referenced but bibtexparser is not installed (pip install bibtexparser).")
        return {}
    entries = {}
    for f in files:
        p = (base_dir / f)
        if not p.exists():
            logger.warning(f".bib file not found: {p}")
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                db = bibtexparser.load(fh)
        except Exception as exc:
            logger.error(f"Failed to parse {p}: {exc}")
            continue
        for e in db.entries:
            key = e.get("ID", "").strip()
            if key:
                entries[key] = _format_bibtex_entry(e)
    return entries

# ----------------------------------------------------------------------
# 3. Extract citations, sections, and their claims (from CLEANED text)
# ----------------------------------------------------------------------
CITE_CMDS = ["cite", "citep", "citet", "citealp"]
_SECTION_LEVELS = {"section": 1, "subsection": 2, "subsubsection": 3}
_MARKER_RE = re.compile(r"__(SEC|CITE)_(\d+)__")

def extract_citations(tex: str) -> List[dict]:
    """Find every citation command in the cleaned text, return list of {keys, command, start, end}."""
    # literal backslash, then one of the commands, then optional letters, then {…}
    cmd_alt = "|".join(CITE_CMDS)
    pat = re.compile(r"\\(" + cmd_alt + r")[a-zA-Z]*\s*\{([^}]+)\}")
    citations = []
    for m in pat.finditer(tex):
        keys = [k.strip() for k in m.group(2).split(",") if k.strip()]
        citations.append({
            "keys": keys,
            "command": m.group(0),
            "start": m.start(),
            "end": m.end(),
        })
    return citations

def extract_sections(tex: str) -> List[dict]:
    """Find \\section/\\subsection/\\subsubsection (and starred) headings with positions."""
    pat = re.compile(r"\\(subsubsection|subsection|section)\*?\s*\{")
    sections = []
    for m in pat.finditer(tex):
        brace_start = m.end() - 1
        try:
            brace_end = find_matching_brace(tex, brace_start)
        except ValueError:
            continue
        title = " ".join(tex[brace_start + 1:brace_end].split())
        sections.append({
            "level": _SECTION_LEVELS[m.group(1)],
            "title": title,
            "start": m.start(),
            "end": brace_end + 1,
        })
    return sections

def _strip_markers(s: str) -> str:
    s = " ".join(_MARKER_RE.sub(" ", s).split())
    return re.sub(r"\s+([.,;:!?])", r"\1", s)

def extract_claims(cleaned_tex: str, citations: List[dict],
                   sections: List[dict]) -> List[dict]:
    """
    Replace citation and section commands with ordered placeholders, tokenize
    into sentences, then walk sentences tracking the current section hierarchy
    and emit one record per citation occurrence (in document order).
    """
    events = ([{"type": "CITE", **c} for c in citations] +
              [{"type": "SEC", **s} for s in sections])
    events.sort(key=lambda e: e["start"])

    text = cleaned_tex
    registry = {}
    for i, ev in enumerate(events):
        registry[i] = ev
    # replace from last to first so offsets stay valid
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        text = text[:ev["start"]] + f" __{ev['type']}_{i}__ " + text[ev["end"]:]

    sentences = sent_tokenize(text)
    clean_sents = [_strip_markers(s) for s in sentences]

    hierarchy: Dict[int, str] = {}
    records = []
    cid = 0
    for i, sent in enumerate(sentences):
        for tok in _MARKER_RE.finditer(sent):
            typ, idx = tok.group(1), int(tok.group(2))
            ev = registry[idx]
            if typ == "SEC":
                hierarchy[ev["level"]] = ev["title"]
                for lvl in [l for l in hierarchy if l > ev["level"]]:
                    del hierarchy[lvl]
            else:
                cid += 1
                claim = clean_sents[i]
                # extend if too short (< 10 words) using neighbouring sentences
                if len(claim.split()) < 10:
                    if i > 0:
                        claim = f"{clean_sents[i-1]} {claim}"
                    if len(claim.split()) < 10 and i + 1 < len(sentences):
                        claim = f"{claim} {clean_sents[i+1]}"
                    claim = " ".join(claim.split())
                section_name = " > ".join(hierarchy[l] for l in sorted(hierarchy)) or "(no section)"
                records.append({
                    "index": cid,
                    "claim": claim,
                    "keys": ev["keys"],
                    "raw_command": ev["command"],
                    "section": section_name,
                })
    return records

# ----------------------------------------------------------------------
# 4. PDF generation
# ----------------------------------------------------------------------
def build_pdf(records: List[dict], bib: Dict[str, str], outpath: Path):
    doc = SimpleDocTemplate(
        str(outpath), pagesize=A4,
        rightMargin=0.75*inch, leftMargin=0.75*inch,
        topMargin=0.75*inch, bottomMargin=0.75*inch
    )
    checker = get_checker()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("ClaimBox", parent=styles["Normal"],
                               fontSize=10, leading=14,
                               leftIndent=12, rightIndent=12,
                               spaceAfter=6,
                               backColor=colors.Color(0.95,0.95,0.95),
                               borderWidth=0.5, borderColor=colors.grey,
                               borderPadding=8))
    styles.add(ParagraphStyle("CitationCode", parent=styles["Code"],
                               fontSize=9,
                               backColor=colors.Color(0.9,0.95,1.0),
                               borderWidth=0.5, borderColor=colors.HexColor("#4a86e8"),
                               borderPadding=6, spaceAfter=12))
    styles.add(ParagraphStyle("RefText", parent=styles["Normal"],
                               fontSize=9, leftIndent=20, rightIndent=10,
                               spaceAfter=12))

    styles.add(ParagraphStyle("SectionLine", parent=styles["Normal"],
                               fontSize=10, textColor=colors.HexColor("#555555"),
                               spaceAfter=6))

    styles.add(ParagraphStyle("BrandTitle", parent=styles["Title"],
                               fontSize=26, spaceAfter=2,
                               textColor=colors.HexColor("#1a3c6e")))
    styles.add(ParagraphStyle("BrandSub", parent=styles["Normal"],
                               fontSize=12, alignment=1,
                               textColor=colors.HexColor("#555555"), spaceAfter=2))

    story = []
    story.append(Paragraph(TOOL_NAME, styles["BrandTitle"]))
    story.append(Paragraph(TOOL_TAGLINE, styles["BrandSub"]))
    story.append(Paragraph(f"Checked by {get_checker()}", styles["BrandSub"]))
    story.append(Paragraph(f"{TOOL_NAME} developed by {DEVELOPER}", styles["BrandSub"]))
    story.append(Spacer(1, 0.15*inch))
    story.append(HRFlowable(width="100%", thickness=1.2,
                            color=colors.HexColor("#1a3c6e")))
    story.append(Spacer(1, 0.25*inch))

    # Statistics
    all_keys = [k for rec in records for k in rec["keys"]]
    unique_keys = set(all_keys)
    missing = sum(1 for k in unique_keys if k not in bib)
    dup_count = len(all_keys) - len(unique_keys)

    stats_data = [
        ["Total citations (cite commands)", str(len(records))],
        ["Unique references", str(len(unique_keys))],
        ["Duplicate citations (same key used)", str(dup_count)],
        ["Missing bibliography entries", str(missing)],
        ["Number of claims extracted", str(len(records))]
    ]
    stat_table = Table(stats_data, colWidths=[3.0*inch, 1.5*inch])
    stat_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.Color(0.85,0.85,0.85)),
        ("TEXTCOLOR", (0,0), (-1,0), colors.black),
        ("ALIGN", (0,0), (-1,-1), "LEFT"),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,0), 8),
        ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.Color(0.97,0.97,0.97)])
    ]))
    story.append(stat_table)
    story.append(PageBreak())

    # Each citation
    for rec in records:
        idx = rec["index"]
        claim = rec["claim"]
        raw_cmd = rec["raw_command"]
        keys = rec["keys"]

        story.append(Paragraph(f"Citation {idx:02d}", styles["Heading2"]))
        story.append(Paragraph(f"<b>Section:</b> {rec.get('section', '(no section)')}",
                               styles["SectionLine"]))
        story.append(Spacer(1, 0.05*inch))

        story.append(Paragraph("Claim", styles["Heading3"]))
        story.append(Paragraph(claim, styles["ClaimBox"]))
        story.append(Spacer(1, 0.1*inch))

        story.append(Paragraph("Citation", styles["Heading3"]))
        story.append(Paragraph(raw_cmd, styles["CitationCode"]))

        story.append(Paragraph("Reference", styles["Heading3"]))
        for key in keys:
            ref = bib.get(key, f"WARNING: Citation key '{key}' not found in thebibliography.")
            story.append(Paragraph(ref, styles["RefText"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
        story.append(Spacer(1, 0.2*inch))

    def decorate_page(canvas, doc):
        canvas.saveState()
        left, right = 0.75 * inch, A4[0] - 0.75 * inch
        # ---- Header: tool name (left) + tagline (right) with a rule ----
        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(colors.HexColor("#1a3c6e"))
        canvas.drawString(left, A4[1] - 0.5 * inch, TOOL_NAME)
        canvas.setFont("Helvetica-Oblique", 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawRightString(right, A4[1] - 0.5 * inch, TOOL_TAGLINE)
        canvas.setStrokeColor(colors.HexColor("#cccccc"))
        canvas.setLineWidth(0.5)
        canvas.line(left, A4[1] - 0.58 * inch, right, A4[1] - 0.58 * inch)
        # ---- Footer: developer credit + checker + page number ----
        canvas.setStrokeColor(colors.HexColor("#cccccc"))
        canvas.line(left, 0.62 * inch, right, 0.62 * inch)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(left, 0.45 * inch, f"{TOOL_NAME} · developed by {DEVELOPER}")
        canvas.drawCentredString(A4[0] / 2, 0.45 * inch, f"Checked by {checker}")
        canvas.drawRightString(right, 0.45 * inch, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)
    logger.info(f"PDF saved to {outpath}")

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Extract claims and references from LaTeX, produce PDF.")
    parser.add_argument("texfile", help="Path to the main .tex file")
    parser.add_argument("-o", "--output", default="claims_report.pdf", help="Output PDF filename")
    args = parser.parse_args()

    tex_path = Path(args.texfile).resolve()
    if not tex_path.exists():
        logger.error(f"File not found: {tex_path}")
        return

    logger.info("Reading and resolving includes…")
    raw = tex_path.read_text(encoding="utf-8")
    full_tex = resolve_input_include(raw, tex_path.parent, {tex_path})

    # Parse bibliography from the original full text (before cleaning).
    # Merge thebibliography with any referenced .bib files (thebibliography wins on conflict).
    logger.info("Parsing bibliography…")
    bib = load_bibtex(full_tex, tex_path.parent)
    bib.update(parse_thebibliography(full_tex))
    logger.info(f"Loaded {len(bib)} bibliography entries.")

    # Clean the text for sentence extraction, keeping citation + section commands
    logger.info("Cleaning LaTeX text…")
    cleaned_tex = clean_tex_for_sentence(full_tex)

    # Extract citations and sections from the cleaned text (positions are consistent)
    logger.info("Extracting citations and sections…")
    citations = extract_citations(cleaned_tex)
    sections = extract_sections(cleaned_tex)
    if not citations:
        logger.warning("No citations found – PDF will be empty.")

    # Extract claims
    logger.info("Extracting claims…")
    records = extract_claims(cleaned_tex, citations, sections)

    # Build PDF
    logger.info("Building PDF…")
    build_pdf(records, bib, Path(args.output))

    logger.info("Done.")

if __name__ == "__main__":
    main()
