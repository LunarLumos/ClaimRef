#!/usr/bin/env python3
import argparse, re, logging, getpass
from datetime import datetime
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

TOOL_NAME = "ClaimRef"
TOOL_TAGLINE = "Citation Verification Report"
DEVELOPER = "Aifee Aadil"
GITHUB_URL = "https://github.com/LunarLumos"

PRIMARY_COLOR = colors.HexColor("#1a3c6e")
ACCENT_COLOR = colors.HexColor("#4a86e8")
LIGHT_BG = colors.Color(0.95, 0.95, 0.97)
CLAIM_BOX_BG = colors.Color(0.93, 0.95, 0.98)

def get_checker() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "Unknown"

_ABBREVS = [
    "e.g.", "i.e.", "et al.", "al.", "Fig.", "Figs.", "Eq.", "Eqs.",
    "Ref.", "Refs.", "cf.", "vs.", "viz.", "etc.", "Dr.", "Mr.", "Mrs.",
    "Ms.", "Prof.", "Vol.", "No.", "pp.", "approx.", "Inc.", "Ltd.", "St.",
]

_DOT = "\x00"

def sent_tokenize(text: str) -> List[str]:
    protected = text
    for a in _ABBREVS:
        protected = protected.replace(a, a.replace(".", _DOT))
    protected = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + _DOT + m.group(2), protected)
    protected = re.sub(r"\b([A-Z])\.", lambda m: m.group(1) + _DOT, protected)
    parts = re.split(r"(?<=[.!?])\s+", protected)
    return [p.replace(_DOT, ".").strip() for p in parts if p.strip()]

def text_stats(text: str) -> tuple:
    """Return (word_count, char_count) of readable text (LaTeX commands stripped)."""
    t = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?(?:\{[^}]*\})?", " ", text)  # drop \cmd{...}
    t = re.sub(r"[{}]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return len(t.split()), len(t)

def resolve_input_include(content: str, current_dir: Path, seen: set) -> str:
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
    return re.sub(r"(?<!\\)%.*$", "", tex, flags=re.MULTILINE)

def find_matching_brace(text: str, start: int) -> int:
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
    for env in envs:
        tex = re.sub(rf"\\begin\{{{env}\}}.*?\\end\{{{env}\}}",
                     "", tex, flags=re.DOTALL | re.IGNORECASE)
    return tex

def clean_tex_for_sentence(tex: str) -> str:
    tex = remove_comments(tex)
    tex = re.sub(r"\$[^$]*\$", "", tex)
    tex = re.sub(r"\\\[.*?\\\]", "", tex, flags=re.DOTALL)
    tex = re.sub(r"\\\(.*?\\\)", "", tex, flags=re.DOTALL)
    tex = remove_environments(tex, [
        "equation", "equation*", "align", "align*",
        "figure", "figure*", "table", "table*",
        "appendices", "thebibliography"
    ])
    for c in ["footnote", "label", "ref"]:
        tex = strip_cmd_remove_content(tex, c)
    for c in ["textit", "textbf", "emph", "texttt", "textsf", "textsc"]:
        tex = strip_cmd_keep_content(tex, c)
    for c in ["small", "large", "Large", "LARGE", "huge", "Huge"]:
        tex = re.sub(rf"\\{c}\b", "", tex)
    tex = strip_cmd_remove_content(tex, "citeauthor")
    tex = strip_cmd_remove_content(tex, "citeyear")
    tex = re.sub(r"\\(begin|end)\{[^}]*\}(\[[^\]]*\])?(\{[^}]*\})?", "", tex)
    for esc, rep in [(r"\%", "%"), (r"\&", "&"), (r"\_", "_"),
                     (r"\#", "#"), (r"\$", "$"), (r"~", " ")]:
        tex = tex.replace(esc, rep)
    tex = tex.replace("---", "—").replace("--", "–")
    tex = re.sub(r"\\\\", " ", tex)
    tex = re.sub(r"\s+", " ", tex)
    return tex

def parse_thebibliography(tex: str) -> Dict[str, str]:
    env = re.search(r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}", tex, re.DOTALL)
    if not env:
        logger.warning("No thebibliography environment found.")
        return {}
    bib = env.group(0)
    bib = re.sub(r"\\begin\{thebibliography\}.*?(\{.*?\})?", "", bib, count=1)
    bib = re.sub(r"\\end\{thebibliography\}", "", bib, count=1)
    entries = {}
    for m in re.finditer(r"\\bibitem\s*\{([^}]+)\}\s*(.*?)(?=\\bibitem|$)", bib, re.DOTALL):
        key = m.group(1).strip()
        ref_text = m.group(2).strip()
        ref_text = clean_tex_for_sentence(ref_text)
        entries[key] = ref_text
    return entries

def _format_bibtex_entry(e: dict) -> str:
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
    ref = re.sub(r"\s+", " ", ref).strip().rstrip(",")
    if not ref.endswith("."):
        ref += "."
    if e.get("doi"):
        ref += f" doi:{e['doi'].strip()}"
    elif e.get("url"):
        ref += f" URL:{e['url'].strip()}"
    return ref

def load_bibtex(full_tex: str, base_dir: Path) -> Dict[str, str]:
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
        logger.warning(".bib files referenced but bibtexparser not installed.")
        return {}
    entries = {}
    for f in files:
        p = base_dir / f
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

_SECTION_LEVELS = {"section": 1, "subsection": 2, "subsubsection": 3}
_MARKER_RE = re.compile(r"__(SEC|CITE)_(\d+)__")

CITE_CMDS = ["cite", "citep", "citet", "citealp"]

def extract_citations(tex: str) -> List[dict]:
    cmd_alt = "|".join(CITE_CMDS)
    pat = re.compile(r"\\(" + cmd_alt + r")[a-zA-Z]*(?:\s*\[[^\]]*\])?\s*\{([^}]+)\}")
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
    events = ([{"type": "CITE", **c} for c in citations] +
              [{"type": "SEC", **s} for s in sections])
    events.sort(key=lambda e: e["start"])

    text = cleaned_tex
    registry = {}
    for i, ev in enumerate(events):
        registry[i] = ev
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
                if len(claim.split()) < 10:
                    if i > 0:
                        claim = f"{clean_sents[i-1]} {claim}"
                    if len(claim.split()) < 10 and i + 1 < len(sentences):
                        claim = f"{claim} {clean_sents[i+1]}"
                    claim = " ".join(claim.split())
                section_name = " > ".join(hierarchy[l] for l in sorted(hierarchy)) or "(no section)"
                rec = {
                    "index": cid,
                    "claim": claim,
                    "keys": ev["keys"],
                    "raw_command": ev["command"],
                    "section": section_name,
                }
                if ev.get("page") is not None:
                    rec["page"] = ev["page"]
                records.append(rec)
    return records

try:
    import fitz
except ImportError:
    fitz = None

_PDF_CITE_RE = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")
_PDF_SEC_RE = re.compile(
    r"\b([IVXLC]+)\.\s+([A-Z][A-Z][A-Z \-]{1,40}?)(?=\s+[A-Z][a-z])")
_PDF_SUBSEC_RE = re.compile(
    r"\b([A-Z])\.\s+([A-Z][a-z][A-Za-z \-]{1,40}?)(?=\s+[A-Z][a-z])")

def _expand_ref_range(s: str) -> List[str]:
    keys = []
    for part in s.split(","):
        part = part.strip()
        rng = re.match(r"(\d+)\s*[–-]\s*(\d+)$", part)
        if rng:
            a, b = int(rng.group(1)), int(rng.group(2))
            if a <= b <= a + 60:
                keys.extend(str(x) for x in range(a, b + 1))
                continue
        if part.isdigit():
            keys.append(part)
    return keys

def extract_pdf(path: Path):
    if fitz is None:
        raise RuntimeError("PDF input requires PyMuPDF – run: pip install pymupdf")
    doc = fitz.open(str(path))
    parts, raw_parts, page_spans, off = [], [], [], 0
    for pno in range(len(doc)):
        raw = doc[pno].get_text("text")
        raw = re.sub(r"-\n", "", raw)
        raw_parts.append(raw)
        t = re.sub(r"\s+", " ", raw).strip()
        if t:
            t += " "
        parts.append(t)
        page_spans.append((off, off + len(t), pno + 1))
        off += len(t)
    n = len(doc)
    doc.close()
    return "".join(parts), page_spans, n, "\n".join(raw_parts)

def _page_for(offset: int, page_spans) -> int:
    for s, e, p in page_spans:
        if s <= offset < e:
            return p
    return page_spans[-1][2] if page_spans else 1

def split_pdf_references(text: str):
    heads = list(re.finditer(r"\b(References|REFERENCES|Bibliography|BIBLIOGRAPHY)\b", text))
    if not heads:
        logger.warning("No 'References' section found in PDF.")
        return text, {}
    m = heads[-1]
    body, ref_text = text[:m.start()], text[m.end():]
    matches = list(re.finditer(r"\[(\d+)\]", ref_text))
    refs = {}
    for i, mm in enumerate(matches):
        start = mm.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(ref_text)
        entry = re.sub(r"\s+", " ", ref_text[start:end]).strip()
        if entry:
            refs[mm.group(1)] = entry
    return body, refs

def extract_pdf_citations(text: str, page_spans) -> List[dict]:
    cites = []
    for m in _PDF_CITE_RE.finditer(text):
        keys = _expand_ref_range(m.group(1))
        if not keys:
            continue
        cites.append({
            "keys": keys,
            "command": m.group(0),
            "start": m.start(),
            "end": m.end(),
            "page": _page_for(m.start(), page_spans),
        })
    return cites

_AY_PAREN = re.compile(r"\(([^()]*(?:19|20)\d{2}[a-z]?[^()]*)\)")
_AY_NARR = re.compile(
    r"([A-Z][A-Za-z'’\-]+(?:\s+et al\.?)?(?:\s+(?:and|&)\s+[A-Z][A-Za-z'’\-]+)?)"
    r"\s+\(((?:19|20)\d{2})[a-z]?\)")

def _raw_reference_block(raw_text: str) -> str:
    heads = list(re.finditer(r"\b(References|REFERENCES|Bibliography|BIBLIOGRAPHY)\b", raw_text))
    return raw_text[heads[-1].end():] if heads else ""

def build_authoryear_refs(raw_ref_block: str):
    lines = [l.strip() for l in raw_ref_block.splitlines() if l.strip()]
    entries, cur = [], ""
    for l in lines:
        looks_new = re.match(r"[A-Z][A-Za-z'’\-]+,\s+[A-Z]\.", l) and re.search(r"(19|20)\d{2}", cur)
        if looks_new and cur:
            entries.append(cur.strip())
            cur = l
        else:
            cur = (cur + " " + l).strip()
    if cur:
        entries.append(cur.strip())

    bib, index = {}, {}
    for e in entries:
        e = re.sub(r"\s+", " ", e).strip()
        ym = re.search(r"(19|20)\d{2}", e)
        sm = re.match(r"([A-Z][A-Za-z'’\-]+)", e)
        if not (ym and sm):
            continue
        surname, year = sm.group(1), ym.group(0)
        key, base, n = f"{surname}{year}", f"{surname}{year}", 1
        while key in bib and bib[key] != e:
            n += 1
            key = f"{base}{chr(96 + n)}"
        bib[key] = e
        index.setdefault((surname.lower(), year), key)
    return bib, index

def _resolve_ay(surname: str, year: str, index: dict) -> str:
    return index.get((surname.lower(), year), f"{surname}{year}")

def _ay_pieces(inner: str):
    out = []
    for part in re.split(r";", inner):
        ym = re.search(r"(19|20)\d{2}", part)
        if not ym:
            continue
        head = re.sub(r"\bet al\.?", "", part[:ym.start()])
        sm = re.search(r"[A-Z][A-Za-z'’\-]+", head)
        if sm:
            out.append((sm.group(0), ym.group(0)))
    return out

def extract_pdf_citations_authoryear(text: str, page_spans, index: dict) -> List[dict]:
    seen_spans, cites = set(), []
    def add(start, end, command, pairs):
        keys = [_resolve_ay(s, y, index) for s, y in pairs]
        if keys:
            cites.append({"keys": keys, "command": command,
                          "start": start, "end": end,
                          "page": _page_for(start, page_spans)})
    for m in _AY_NARR.finditer(text):
        add(m.start(), m.end(), m.group(0),
            [(re.match(r"[A-Z][A-Za-z'’\-]+", m.group(1)).group(0), m.group(2))])
        seen_spans.add((m.start(), m.end()))
    for m in _AY_PAREN.finditer(text):
        if any(s <= m.start() < e for s, e in seen_spans):
            continue
        add(m.start(), m.end(), m.group(0), _ay_pieces(m.group(1)))
    cites.sort(key=lambda c: c["start"])
    return cites

def extract_pdf_sections(text: str) -> List[dict]:
    sections = []
    for m in _PDF_SEC_RE.finditer(text):
        sections.append({"level": 1, "title": m.group(2).strip().title(),
                         "start": m.start(), "end": m.end()})
    for m in _PDF_SUBSEC_RE.finditer(text):
        sections.append({"level": 2, "title": m.group(2).strip(),
                         "start": m.start(), "end": m.end()})
    return sections

_ENUM_PREFIX = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.|[A-Z]\.)\s+")

def _clean_heading(t: str) -> str:
    t = _ENUM_PREFIX.sub("", t).strip()
    return t.title() if t.isupper() else t

def _heading_level(text: str, size: float, size_ranks: dict) -> int:
    if re.match(r"^\s*\d+\.\d+\.\d+", text) or re.match(r"^\s*[A-Z]\.\d", text):
        return 3
    if re.match(r"^\s*\d+\.\d+(?!\d)", text):
        return 2
    if re.match(r"^\s*[A-Z]\.\s", text):
        return 2
    if re.match(r"^\s*(?:\d+\.?|[IVXLC]+\.)\s", text):
        return 1
    return size_ranks.get(round(size, 1), 1)

def extract_pdf_headings(path: Path) -> List[tuple]:
    if fitz is None:
        return []
    doc = fitz.open(str(path))
    size_chars: Dict[float, int] = {}
    lines = []
    for pno in range(len(doc)):
        for block in doc[pno].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                txt = "".join(s["text"] for s in spans).strip()
                if not txt:
                    continue
                size = round(max(s["size"] for s in spans), 1)
                bold = any((s["flags"] & 16) or "Bold" in s.get("font", "") for s in spans)
                size_chars[size] = size_chars.get(size, 0) + len(txt)
                lines.append((size, bold, txt))
    doc.close()
    if not lines:
        return []

    body_size = max(size_chars, key=size_chars.get)
    bigger = sorted((s for s in size_chars if s >= body_size + 0.5), reverse=True)
    size_ranks = {s: min(i + 1, 3) for i, s in enumerate(bigger)}

    headings = []
    for size, bold, txt in lines:
        if not (3 <= len(txt) <= 70) or len(txt.split()) > 9:
            continue
        if re.match(r"^\d+$", txt):
            continue
        is_big = size >= body_size + 0.5
        numbered = bool(re.match(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.|[A-Z]\.)\s+[A-Z]", txt))
        if not (is_big or (bold and numbered)):
            continue
        if txt.endswith(".") and not numbered:
            continue
        headings.append((_heading_level(txt, size, size_ranks), txt))
    return headings

def build_pdf_sections(body: str, headings: List[tuple]) -> List[dict]:
    sections, cursor = [], 0
    for level, htext in headings:
        norm = re.sub(r"\s+", " ", htext).strip()
        idx = body.find(norm, cursor)
        if idx == -1:
            idx = body.find(norm)
        if idx == -1:
            continue
        sections.append({"level": level, "title": _clean_heading(norm),
                         "start": idx, "end": idx + len(norm)})
        cursor = idx + len(norm)
    return sections

def process_pdf(path: Path):
    logger.info("Extracting text from PDF…")
    text, page_spans, n_pages, raw_text = extract_pdf(path)
    logger.info(f"Read {n_pages} page(s).")

    logger.info("Locating reference list…")
    body, num_bib = split_pdf_references(text)

    num_cites = extract_pdf_citations(body, page_spans)
    if num_cites and num_bib:
        style = "numeric"
        bib, citations = num_bib, num_cites
    else:
        ay_bib, ay_index = build_authoryear_refs(_raw_reference_block(raw_text))
        ay_cites = extract_pdf_citations_authoryear(body, page_spans, ay_index)
        if ay_cites:
            style, bib, citations = "author–year", ay_bib, ay_cites
        else:
            style, bib, citations = "numeric", num_bib, num_cites
    logger.info(f"Detected {style} citation style: "
                f"{len(citations)} citations, {len(bib)} references.")

    sections = build_pdf_sections(body, extract_pdf_headings(path))
    if not sections:
        sections = extract_pdf_sections(body)
    logger.info(f"Detected {len(sections)} section heading(s).")
    if not citations:
        logger.warning("No citations detected. Supported PDF styles: numeric [n] "
                       "(IEEE/ACM) and author–year (Springer/Nature/Elsevier).")
    if not sections:
        logger.warning("No section headings detected.")

    logger.info("Extracting claims…")
    records = extract_claims(body, citations, sections)
    words, chars = text_stats(body)
    meta = {"word_count": words, "char_count": chars, "pages": n_pages}
    logger.info(f"Word count: {words:,} · Character count: {chars:,}")
    return records, bib, meta

def build_pdf(records: List[dict], bib: Dict[str, str], outpath: Path, meta: dict = None):
    checker = get_checker()
    meta = meta or {}
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    doc = SimpleDocTemplate(
        str(outpath), pagesize=A4,
        rightMargin=0.75*inch, leftMargin=0.75*inch,
        topMargin=0.75*inch, bottomMargin=0.75*inch,
    )

    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        "ClickableTitle",
        parent=styles["Title"],
        fontSize=28,
        leading=34,
        spaceBefore=6,
        spaceAfter=10,
        textColor=PRIMARY_COLOR,
        underlineColor=PRIMARY_COLOR,
        underlineWidth=1,
        underlineGap=1,
        alignment=1,
    ))
    styles.add(ParagraphStyle(
        "Tagline",
        parent=styles["Normal"],
        fontSize=12,
        alignment=1,
        textColor=colors.HexColor("#555555"),
        spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        "SubLine",
        parent=styles["Normal"],
        fontSize=9,
        alignment=1,
        textColor=colors.HexColor("#777777"),
        spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        "ClaimBox",
        parent=styles["Normal"],
        fontSize=10, leading=14,
        leftIndent=12, rightIndent=12,
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        "CitationCode",
        parent=styles["Code"],
        fontSize=9,
        leftIndent=12,
        spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        "RefText",
        parent=styles["Normal"],
        fontSize=9, leftIndent=20, rightIndent=10,
        spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        "SectionLine",
        parent=styles["Normal"],
        fontSize=10, textColor=colors.HexColor("#555555"),
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        "Heading2Color",
        parent=styles["Heading2"],
        textColor=PRIMARY_COLOR,
        spaceBefore=14,
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        "Heading3Color",
        parent=styles["Heading3"],
        textColor=ACCENT_COLOR,
        spaceBefore=10,
        spaceAfter=4,
    ))

    story = []

    clickable_title = (
        f'<link href="{GITHUB_URL}" color="{PRIMARY_COLOR}"><u>{TOOL_NAME}</u></link>'
        f' <font size="11" color="#777777">by {DEVELOPER}</font>'
    )
    story.append(Paragraph(clickable_title, styles["ClickableTitle"]))
    story.append(Paragraph(TOOL_TAGLINE, styles["Tagline"]))
    story.append(Paragraph(f"Checked by {checker}", styles["SubLine"]))
    story.append(Paragraph(f"Generated on {generated}", styles["SubLine"]))
    story.append(Spacer(1, 0.15 * inch))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT_COLOR))
    story.append(Spacer(1, 0.35 * inch))

    all_keys = [k for rec in records for k in rec["keys"]]
    unique_keys = set(all_keys)
    missing = sum(1 for k in unique_keys if k not in bib)
    dup_count = len(all_keys) - len(unique_keys)
    word_count = meta.get("word_count", 0)
    char_count = meta.get("char_count", 0)

    stats_data = [
        ["Total citations (cite commands)", str(len(records))],
        ["Unique references", str(len(unique_keys))],
        ["Duplicate citations", str(dup_count)],
        ["Missing bibliography entries", str(missing)],
        ["Number of claims extracted", str(len(records))],
        ["Total word count", f"{word_count:,}"],
        ["Total character count", f"{char_count:,}"],
        ["Report generated", generated],
    ]
    stat_table = Table(stats_data, colWidths=[3.0 * inch, 1.9 * inch])
    stat_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY_COLOR),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BG]),
        ("TOPPADDING", (0, 1), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
    ]))
    story.append(stat_table)
    story.append(PageBreak())

    for rec in records:
        idx = rec["index"]
        claim = rec["claim"]
        raw_cmd = rec["raw_command"]
        keys = rec["keys"]
        section = rec.get("section", "(no section)")

        story.append(Paragraph(f"Citation {idx:02d}", styles["Heading2Color"]))
        meta_line = f'<font color="#555555"><b>Section:</b></font> {section}'
        if rec.get("page") is not None:
            meta_line += f' &nbsp;&nbsp;<font color="#555555"><b>Page:</b></font> {rec["page"]}'
        story.append(Paragraph(meta_line, styles["SectionLine"]))
        story.append(Paragraph("Claim", styles["Heading3Color"]))
        story.append(Paragraph(claim, styles["ClaimBox"]))
        story.append(Paragraph("Citation", styles["Heading3Color"]))
        story.append(Paragraph(raw_cmd, styles["CitationCode"]))
        story.append(Paragraph("Reference", styles["Heading3Color"]))
        for key in keys:
            ref = bib.get(key, f"WARNING: Citation key '{key}' not found in bibliography.")
            story.append(Paragraph(ref, styles["RefText"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.Color(0.8, 0.8, 0.8)))
        story.append(Spacer(1, 0.2 * inch))

    def decorate_page(canvas, doc):
        canvas.saveState()
        left, right = 0.75 * inch, A4[0] - 0.75 * inch

        hy = A4[1] - 0.5 * inch
        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(PRIMARY_COLOR)
        canvas.drawString(left, hy, TOOL_NAME)
        name_w = canvas.stringWidth(TOOL_NAME, "Helvetica-Bold", 9)
        canvas.setFont("Helvetica-Oblique", 7)
        canvas.setFillColor(colors.HexColor("#999999"))
        canvas.drawString(left + name_w + 4, hy, f"by {DEVELOPER}")
        canvas.setFont("Helvetica-Oblique", 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawRightString(right, hy, TOOL_TAGLINE)
        canvas.setStrokeColor(ACCENT_COLOR)
        canvas.setLineWidth(0.8)
        canvas.line(left, A4[1] - 0.58 * inch, right, A4[1] - 0.58 * inch)

        canvas.setStrokeColor(ACCENT_COLOR)
        canvas.line(left, 0.62 * inch, right, 0.62 * inch)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(left, 0.45 * inch, f"{TOOL_NAME} · {GITHUB_URL}")
        canvas.drawCentredString(A4[0] / 2, 0.45 * inch, f"Checked by {checker}")
        canvas.drawRightString(right, 0.45 * inch, f"Page {canvas.getPageNumber()}")

        canvas.restoreState()

    doc.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)
    logger.info(f"PDF saved to {outpath}")

def process_latex(tex_path: Path):
    logger.info("Reading and resolving includes…")
    raw = tex_path.read_text(encoding="utf-8")
    full_tex = resolve_input_include(raw, tex_path.parent, {tex_path})

    logger.info("Parsing bibliography…")
    bib = load_bibtex(full_tex, tex_path.parent)
    bib.update(parse_thebibliography(full_tex))
    logger.info(f"Loaded {len(bib)} bibliography entries.")

    logger.info("Cleaning LaTeX text…")
    cleaned_tex = clean_tex_for_sentence(full_tex)

    logger.info("Extracting citations and sections…")
    citations = extract_citations(cleaned_tex)
    sections = extract_sections(cleaned_tex)

    logger.info("Extracting claims…")
    records = extract_claims(cleaned_tex, citations, sections)
    words, chars = text_stats(cleaned_tex)
    meta = {"word_count": words, "char_count": chars}
    logger.info(f"Word count: {words:,} · Character count: {chars:,}")
    return records, bib, meta

def main():
    parser = argparse.ArgumentParser(
        description="ClaimRef – extract claims and references from a LaTeX (.tex) "
                    "or PDF paper and produce a Citation Verification Report.")
    parser.add_argument("source", help="Path to the paper: .tex (LaTeX) or .pdf")
    parser.add_argument("-o", "--output", default="claims_report.pdf", help="Output PDF filename")
    args = parser.parse_args()

    src = Path(args.source).resolve()
    if not src.exists():
        logger.error(f"File not found: {src}")
        return

    if src.suffix.lower() == ".pdf":
        records, bib, meta = process_pdf(src)
    else:
        records, bib, meta = process_latex(src)

    if not records:
        logger.warning("No citations found – the report will be empty.")

    logger.info("Building PDF…")
    build_pdf(records, bib, Path(args.output), meta)

    logger.info("Done.")

if __name__ == "__main__":
    main()
