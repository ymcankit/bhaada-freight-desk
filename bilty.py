"""
A one-page PDF from lines of text, with no library: the built-in Helvetica fonts, A4, cp1252 text.
Enough for a Lorry Receipt (bilty) draft; ₹ and other characters outside cp1252 are spelled out or dropped.
"""
SWAP = {"₹": "Rs.", "→": "->", "✅": "", "⚠️": "!", "🧾": "", "🚛": "", "📄": ""}

def _pdf_str(s):
    for a, b in SWAP.items():
        s = s.replace(a, b)
    raw = s.encode("cp1252", errors="replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"

def _wrap(text, width):
    out, line = [], ""
    for w in text.split():
        if line and len(line) + 1 + len(w) > width:
            out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    return out + [line] if line else out or [""]

def one_page(rows):
    """rows: (style, text) with style "title", "head", "label: value" pairs as ("kv", (label, value)), "text" or "rule"."""
    ops, y = [], 800
    def put(font, size, x, txt):
        ops.append(b"BT /%s %d Tf %d %d Td " % (font, size, x, y) + _pdf_str(txt) + b" Tj ET")
    for style, val in rows:
        if y < 50:
            break                                       # one page: a draft that runs over is cut, not split
        if style == "title":
            put(b"F2", 18, 50, val); y -= 26
        elif style == "head":
            y -= 6; put(b"F2", 11, 50, val); y -= 16
        elif style == "kv":
            label, value = val
            for i, part in enumerate(_wrap(str(value), 62)):
                if i == 0:
                    put(b"F2", 10, 50, label)
                put(b"F1", 10, 200, part); y -= 14
        elif style == "rule":
            ops.append(b"0.6 w 50 %d m 545 %d l S" % (y + 6, y + 6)); y -= 8
        else:
            for part in _wrap(str(val), 95):
                put(b"F1", 9, 50, part); y -= 12
    stream = b"\n".join(ops)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % x for x in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)
