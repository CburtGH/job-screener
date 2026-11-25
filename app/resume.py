"""Resume storage, file text extraction, and DOCX / PDF / plain-text export."""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

from . import db

UPLOAD_DIR = db.DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


# ---- extraction ----------------------------------------------------------

def extract_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    elif ext == ".docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        lines = []
        for p in d.paragraphs:
            t = p.text.strip()
            if not t:
                lines.append("")
                continue
            style = (p.style.name or "").lower() if p.style is not None else ""
            if style.startswith("heading") or style == "title":
                lines.append("## " + t)
            elif "list" in style:
                lines.append("- " + t)
            else:
                lines.append(t)
        for table in d.tables:
            for row in table.rows:
                lines.append(" | ".join(c.text.strip() for c in row.cells))
        text = "\n".join(lines)
    elif ext in (".txt", ".md", ".markdown"):
        text = data.decode("utf-8", errors="replace")
    else:
        raise ValueError(f"Unsupported file type {ext!r}. Upload PDF, DOCX, TXT or MD.")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---- CRUD ----------------------------------------------------------------

def list_resumes() -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, name, is_master, source_file, created_at, updated_at, length(content) AS chars "
            "FROM resumes ORDER BY is_master DESC, updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_resume(resume_id: int) -> dict | None:
    with db.conn() as c:
        row = c.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
    return dict(row) if row else None


def get_master() -> dict | None:
    with db.conn() as c:
        row = c.execute("SELECT * FROM resumes WHERE is_master=1 ORDER BY updated_at DESC LIMIT 1").fetchone()
        if not row:
            row = c.execute("SELECT * FROM resumes ORDER BY updated_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def create_resume(name: str, content: str, source_file: str | None = None, make_master: bool = False) -> dict:
    ts = db.now_iso()
    with db.conn() as c:
        has_any = c.execute("SELECT COUNT(*) FROM resumes").fetchone()[0]
        master = 1 if (make_master or not has_any) else 0
        if master:
            c.execute("UPDATE resumes SET is_master=0")
        cur = c.execute(
            "INSERT INTO resumes(name, content, is_master, source_file, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (name, content, master, source_file, ts, ts),
        )
        rid = cur.lastrowid
    return get_resume(rid)


def update_resume(resume_id: int, name: str | None, content: str | None, make_master: bool | None) -> dict | None:
    with db.conn() as c:
        if make_master:
            c.execute("UPDATE resumes SET is_master=0")
            c.execute("UPDATE resumes SET is_master=1 WHERE id=?", (resume_id,))
        if name is not None:
            c.execute("UPDATE resumes SET name=? WHERE id=?", (name, resume_id))
        if content is not None:
            c.execute("UPDATE resumes SET content=? WHERE id=?", (content, resume_id))
        c.execute("UPDATE resumes SET updated_at=? WHERE id=?", (db.now_iso(), resume_id))
    return get_resume(resume_id)


def delete_resume(resume_id: int) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM resumes WHERE id=?", (resume_id,))


# ---- export --------------------------------------------------------------

_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*)")


def _add_runs(paragraph, text: str) -> None:
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            paragraph.add_run(part[1:-1]).italic = True
        else:
            paragraph.add_run(part)


def markdown_to_docx(markdown: str) -> bytes:
    """Small markdown subset -> DOCX: #/##/### headings, -/* bullets, **bold**, *italic*."""
    import docx
    from docx.shared import Pt

    d = docx.Document()
    style = d.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    for raw in unwrap_lines(markdown).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            d.add_heading(m.group(2).strip(), level=0 if level == 1 else level - 1)
            continue
        m = re.match(r"^\s*[-*•]\s+(.*)", line)
        if m:
            _add_runs(d.add_paragraph(style="List Bullet"), m.group(1))
            continue
        _add_runs(d.add_paragraph(), line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ---- plain text and PDF, for uploading to application sites -------------------------------------------

_BLOCK = re.compile(r"^\s*(#{1,3}\s|[-*•]\s)")


def unwrap_lines(markdown: str) -> str:
    """Rejoin plain-text lines that were wrapped mid-sentence, so PDF/Word reflow them as one paragraph.
    A line joins the next when the next starts in lowercase ("...I've kept / order and billing..."), or when it's a
    long line ending in a comma (a wrapped list). Short lines like "Best regards," keep their break, and headings,
    bullets and blank lines are never joined."""
    out: list[str] = []
    for line in markdown.splitlines():
        prev = out[-1] if out else ""
        stripped = line.strip()
        # Candidates: this line is plain text and the previous one is plain text or a bullet (not a heading).
        if prev.strip() and stripped and not _BLOCK.match(line) and not re.match(r"^\s*#", prev):
            p = prev.rstrip()
            if stripped[0].islower() or (p.endswith(",") and len(p) > 60):
                out[-1] = p + " " + stripped
                continue
        out.append(line)
    return "\n".join(out)

_INLINE = re.compile(r"(\*\*[^*]+\*\*|(?<!\*)\*[^*\s][^*]*\*(?!\*))")


def markdown_to_text(markdown: str) -> str:
    """Plain text that pastes and uploads cleanly: no #, **, or * markup; headings in capitals; '- ' bullets."""
    out = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            text = re.sub(r"\*+", "", m.group(2)).strip()
            level = len(m.group(1))
            if level == 2:
                if out and out[-1] != "":
                    out.append("")
                out.append(text.upper())
            else:
                out.append(text)
            continue
        m = re.match(r"^(\s*)[-*•]\s+(.*)", line)
        if m:
            out.append(f"{m.group(1)}- " + re.sub(r"\*\*([^*]+)\*\*|\*([^*]+)\*", lambda x: x.group(1) or x.group(2), m.group(2)))
            continue
        out.append(re.sub(r"\*\*([^*]+)\*\*|(?<!\*)\*([^*\s][^*]*)\*(?!\*)", lambda x: x.group(1) or x.group(2), line))
    text = "\n".join(out).strip() + "\n"
    return re.sub(r"\n{3,}", "\n\n", text).replace("\n", "\r\n")  # Windows line endings for Notepad & co.


def _pdf_fonts(pdf) -> str:
    """Arial from Windows (full Unicode: accents, dashes, smart quotes). Falls back to the built-in Helvetica."""
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    files = {"": "arial.ttf", "B": "arialbd.ttf", "I": "ariali.ttf", "BI": "arialbi.ttf"}
    if all((fonts / f).exists() for f in files.values()):
        for style, f in files.items():
            pdf.add_font("Body", style, str(fonts / f))
        return "Body"
    return "Helvetica"


def markdown_to_pdf(markdown: str, title: str = "") -> bytes:
    """Small markdown subset -> a clean one-column PDF (US Letter): #/##/### headings, bullets, **bold**, *italic*."""
    from fpdf import FPDF

    pdf = FPDF(format="letter", unit="pt")
    pdf.set_margins(54, 50, 54)
    pdf.set_auto_page_break(True, margin=50)
    if title:
        pdf.set_title(title)
    family = _pdf_fonts(pdf)
    unicode_ok = family != "Helvetica"
    pdf.add_page()

    def clean(t: str) -> str:
        if unicode_ok:
            return t
        return (t.replace("\u2014", "-").replace("\u2013", "-").replace("\u2019", "'").replace("\u2018", "'")
                 .replace("\u201c", '"').replace("\u201d", '"').replace("\u2022", "-")
                 .encode("latin-1", "replace").decode("latin-1"))

    def runs(text: str, size: float, base: str = "", lh: float | None = None) -> None:
        lh = lh or size * 1.35
        for part in _INLINE.split(text):
            if not part:
                continue
            style = base
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                part, style = part[2:-2], "B" + base.replace("B", "")
            elif part.startswith("*") and part.endswith("*") and len(part) > 2:
                part, style = part[1:-1], base.replace("I", "") + "I"
            pdf.set_font(family, "".join(sorted(set(style), key="BI".index)), size)
            pdf.write(lh, clean(part))
        pdf.ln(lh)

    left = pdf.l_margin
    for raw in unwrap_lines(markdown).splitlines():
        line = raw.rstrip()
        if not line.strip():
            pdf.ln(5)
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            level, text = len(m.group(1)), re.sub(r"\*+", "", m.group(2)).strip()
            if level == 1:
                runs(text, 18, "B", 24)
            elif level == 2:
                pdf.ln(4)
                runs(text.upper(), 11.5, "B", 16)
                y = pdf.get_y()
                pdf.set_draw_color(150, 150, 150)
                pdf.line(left, y, pdf.w - pdf.r_margin, y)
                pdf.ln(3)
            else:
                pdf.ln(2)
                runs(text, 10.5, "B", 14)
            continue
        m = re.match(r"^(\s*)[-*•]\s+(.*)", line)
        if m:
            indent = 12 + min(len(m.group(1)), 8) * 4
            pdf.set_font(family, "", 10)
            pdf.set_x(left + indent)
            pdf.write(13.5, clean("•") if unicode_ok else "-")
            pdf.set_left_margin(left + indent + 10)
            pdf.set_x(left + indent + 10)
            runs(m.group(2), 10, "", 13.5)
            pdf.set_left_margin(left)
            continue
        runs(line, 10.5, "", 14.5)
    return bytes(pdf.output())
