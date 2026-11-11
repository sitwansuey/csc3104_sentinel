"""A small Markdown-to-PDF renderer for the instructions, using only the standard library.

It understands the Markdown the instructions use: ATX headings, paragraphs,
bullet and numbered lists, block quotes, pipe tables, fenced code blocks, and
inline code, bold, italic and links. HTML comments are dropped. Text is set in
the PDF standard fonts (Helvetica, Helvetica-Bold, Helvetica-Oblique and
Courier) with WinAnsi encoding, so nothing is embedded; a character outside
Windows-1252 prints as '?' and is reported by render().

Long command lines are wrapped at a space with the shell's own continuation
character (a backtick for PowerShell, a backslash for bash), so a command copied
from the PDF still runs. Output is deterministic: no timestamps, uncompressed
content streams.
"""

from dataclasses import dataclass
import re

PAGE_WIDTH, PAGE_HEIGHT = 595.28, 841.89  # A4 in points
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 56.0, 60.0, 64.0
BODY_WIDTH = PAGE_WIDTH - 2 * MARGIN_X
FONTS = {"regular": ("F1", "Helvetica"), "bold": ("F2", "Helvetica-Bold"), "italic": ("F3", "Helvetica-Oblique"),
         "mono": ("F4", "Courier")}
# Advance widths (1/1000 em) of ASCII 32-126 from the Adobe core-font metrics.
_HELVETICA = (278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, *([556] * 10), 278, 278,
              584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667,
              778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556,
              278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500,
              334, 260, 334, 584)
_HELVETICA_BOLD = (278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, *([556] * 10), 333,
                   333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722,
                   778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611,
                   556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556,
                   778, 556, 556, 500, 389, 280, 389, 584)
_WIDE = {"\u2014": 1000, "\u2022": 350, "\u2018": 222, "\u2019": 222, "\u201c": 333, "\u201d": 333}
GREY, LIGHT, RULE = "0.35", "0.93", "0.7"
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
FENCE = re.compile(r"^ {0,3}(```+|~~~+)(.*)$")
INLINE = re.compile(r"(?P<code>`[^`]+`)|(?P<bold>\*\*.+?\*\*)|(?P<link>!?\[[^\]]*\]\([^)\s]+\))"
                    r"|(?P<italic>(?<![\w*])\*[^*\s][^*]*\*(?!\w))")


def width(text, style, size):
    """Width of text in points for one style at one font size."""
    if style == "mono":
        return 0.6 * size * len(text)
    table = _HELVETICA_BOLD if style == "bold" else _HELVETICA
    total = 0
    for char in text:
        code = ord(char)
        total += table[code - 32] if 32 <= code <= 126 else _WIDE.get(char, 556)
    return total * size / 1000.0


def inline(text):
    """Split Markdown inline text into (style, text) runs."""
    text = re.sub(r"</?(?:br|sup|sub|kbd|span|a)\b[^>]*>", "", text).replace("\\*", "*").replace("\\_", "_")
    runs, position = [], 0
    for match in INLINE.finditer(text):
        if match.start() > position:
            runs.append(("regular", text[position:match.start()]))
        token = match.group(0)
        if match.group("code"):
            runs.append(("mono", token[1:-1]))
        elif match.group("bold"):
            runs.append(("bold", token[2:-2].replace("`", "")))
        elif match.group("italic"):
            runs.append(("italic", token[1:-1]))
        else:
            label, target = re.match(r"!?\[([^\]]*)\]\(([^)\s]+)\)", token).groups()
            runs.append(("regular", label.replace("`", "")))
            if target.startswith(("http://", "https://")) and target != label:
                runs.append(("regular", f" ({target})"))
        position = match.end()
    if position < len(text):
        runs.append(("regular", text[position:]))
    return [run for run in runs if run[1]]


@dataclass
class Block:
    kind: str  # heading, paragraph, item, quote, table, code, rule
    text: str = ""
    level: int = 0
    label: str = ""
    rows: tuple = ()
    language: str = ""


def parse(markdown, describe=None):
    """Turn Markdown into a list of Blocks. describe(info) gives the label printed above a code block."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    result, prose, index = [], [], 0
    while index < len(lines):
        opened = FENCE.match(lines[index])
        if not opened:
            prose.append(lines[index])
            index += 1
            continue
        result.extend(_prose("\n".join(prose)))
        prose = []
        fence, info, body = opened.group(1), opened.group(2).strip(), []
        index += 1
        while index < len(lines) and not re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$",
                                                  lines[index]):
            body.append(lines[index])
            index += 1
        index += 1
        language = info.split()[0].lower() if info else ""
        result.append(Block("code", "\n".join(body), label=describe(info) if describe else "", language=language))
    result.extend(_prose("\n".join(prose)))
    return result


def _prose(text):
    blocks, paragraph, kind = [], [], None

    def flush():
        nonlocal paragraph, kind
        if paragraph:
            blocks.append(Block(kind or "paragraph", " ".join(part.strip() for part in paragraph)))
        paragraph, kind = [], None

    lines = COMMENT.sub("", text).split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        item = re.match(r"^(\s*)(?:[-*+]|(\d+)[.)])\s+(.*)$", line)
        if not stripped:
            flush()
        elif heading:
            flush()
            blocks.append(Block("heading", heading.group(2), level=len(heading.group(1))))
        elif re.match(r"^ {0,3}(-{3,}|\*{3,}|_{3,})\s*$", line):
            flush()
            blocks.append(Block("rule"))
        elif stripped.startswith("|"):
            flush()
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", lines[index].strip().strip("|"))]
                if not all(re.match(r"^:?-{2,}:?$", cell) for cell in cells):
                    rows.append(tuple(cell.replace("\\|", "|") for cell in cells))
                index += 1
            blocks.append(Block("table", rows=tuple(rows)))
            continue
        elif item:
            flush()
            level = len(item.group(1).replace("\t", "  ")) // 2
            marker = (item.group(2) + ".") if item.group(2) else "\u2022"
            paragraph = [item.group(3)]
            index += 1
            while index < len(lines) and lines[index].strip() and not re.match(
                    r"^(\s*)(?:[-*+]|\d+[.)])\s+", lines[index]) and not lines[index].strip().startswith(("|", "#")):
                paragraph.append(lines[index])
                index += 1
            blocks.append(Block("item", " ".join(part.strip() for part in paragraph), level=level, label=marker))
            paragraph = []
            continue
        elif stripped.startswith(">"):
            if kind != "quote":
                flush()
                kind = "quote"
            paragraph.append(re.sub(r"^\s*>\s?", "", line))
        else:
            if kind == "quote":
                flush()
            paragraph.append(line)
        index += 1
    flush()
    return blocks


class Layout:
    """Places text on A4 pages; each page is a list of PDF content operators."""

    def __init__(self):
        self.pages, self.y = [], 0.0
        self.new_page()

    def new_page(self):
        self.pages.append([])
        self.y = PAGE_HEIGHT - MARGIN_TOP

    def ensure(self, height):
        if self.y - height < MARGIN_BOTTOM and self.y < PAGE_HEIGHT - MARGIN_TOP:
            self.new_page()

    def text(self, x, y, style, size, value, colour="0", page=-1):
        name = FONTS[style][0]
        self.pages[page].append(f"{colour} g BT /{name} {size:g} Tf {x:.2f} {y:.2f} Td ({_escape(value)}) Tj ET")

    def rect(self, x, y, w, h, colour):
        self.pages[-1].append(f"{colour} g {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def line(self, x1, y1, x2, y2):
        self.pages[-1].append(f"{RULE} G 0.5 w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def runs(self, x, runs, size, leading, available, colour="0", bar=False):
        for line in wrap(runs, size, available):
            self.ensure(leading)
            self.y -= leading
            if bar:
                self.rect(x - 10, self.y - 3, 2.5, leading, GREY)
            cursor = x
            for style, value in line:
                self.text(cursor, self.y, style, size, value, colour)
                cursor += width(value, style, size)


def wrap(runs, size, available):
    """Greedy word wrap of styled runs into lines of (style, text) that fit 'available' points."""
    words = []
    for style, text in runs:
        for part in re.split(r"(\s+)", text):
            if part:
                words.append((style, " " if part.isspace() else part))
    lines, current, used = [], [], 0.0
    for style, word in words:
        if word == " ":
            if current:
                current.append((style, word))
                used += width(word, style, size)
            continue
        size_word = width(word, style, size)
        while size_word > available:
            # A single word wider than the line (a long path or URL) is split by characters.
            cut = max(1, int(len(word) * available / size_word) - 1)
            if current:
                lines.append(_trim(current))
                current, used = [], 0.0
            lines.append([(style, word[:cut])])
            word = word[cut:]
            size_word = width(word, style, size)
        if used + size_word > available and current:
            lines.append(_trim(current))
            current, used = [], 0.0
        current.append((style, word))
        used += size_word
    if current:
        lines.append(_trim(current))
    return [line for line in lines if line]


def _trim(line):
    while line and line[-1][1] == " ":
        line = line[:-1]
    merged = []
    for style, text in line:
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + text)
        else:
            merged.append((style, text))
    return merged


def wrap_code(line, columns, language):
    """Split a long code line at spaces, ending each piece with the shell's continuation character."""
    continuation = {"powershell": " `", "bash": " \\"}.get(language)
    if len(line) <= columns:
        return [line]
    pieces, rest, indent = [], line, " " * (len(line) - len(line.lstrip()) + 4)
    while len(rest) > columns:
        limit = columns - (len(continuation) if continuation else 0)
        cut = _split_point(rest, len(indent) if pieces else 1, limit) if continuation else -1
        if cut <= 0:
            pieces.append(rest[:columns])
            rest = rest[columns:]
            continue
        pieces.append(rest[:cut] + continuation)
        rest = indent + rest[cut + 1:]
    pieces.append(rest)
    return pieces


def _split_point(text, start, limit):
    """The last space in text[start:limit + 1] that is outside quotes, or -1."""
    for index in range(min(limit, len(text) - 1), start - 1, -1):
        if text[index] == " " and text.count("'", 0, index) % 2 == 0 and text.count('"', 0, index) % 2 == 0:
            return index
    return -1


def _escape(text):
    encoded = text.encode("cp1252", errors="replace")
    out = []
    for byte in encoded:
        if byte in (0x28, 0x29, 0x5C):
            out.append("\\" + chr(byte))
        elif byte < 32 or byte > 126:
            out.append(f"\\{byte:03o}")
        else:
            out.append(chr(byte))
    return "".join(out)


HEADING_SIZE = {1: 17.0, 2: 13.5, 3: 11.5}


def lay_out(blocks, layout):
    size, leading = 10.0, 13.5
    previous = ""
    for block in blocks:
        if previous == "item" and block.kind != "item":
            layout.y -= 5
        previous = block.kind
        if block.kind == "heading":
            heading_size = HEADING_SIZE.get(block.level, 10.5)
            if block.level == 1 and layout.y < PAGE_HEIGHT - MARGIN_TOP:
                layout.new_page()
            layout.ensure(heading_size * 1.4 + 4 * leading)
            layout.y -= 8 if block.level > 1 else 0
            layout.runs(MARGIN_X, [("bold", text) for _, text in inline(block.text)], heading_size,
                        heading_size * 1.3, BODY_WIDTH)
            layout.y -= 4
        elif block.kind == "paragraph":
            layout.runs(MARGIN_X, inline(block.text), size, leading, BODY_WIDTH)
            layout.y -= 6
        elif block.kind == "quote":
            layout.runs(MARGIN_X + 12, inline(block.text), size - 0.5, leading, BODY_WIDTH - 12, bar=True)
            layout.y -= 6
        elif block.kind == "item":
            indent = MARGIN_X + 14 * (block.level + 1)
            # The first line always fits after ensure(), so the label sits beside it on the same page.
            layout.ensure(leading)
            page, label_y = len(layout.pages) - 1, layout.y - leading
            layout.runs(indent, inline(block.text), size, leading, PAGE_WIDTH - MARGIN_X - indent)
            layout.text(indent - 12, label_y, "regular", size, block.label, page=page)
            layout.y -= 2
        elif block.kind == "rule":
            layout.ensure(12)
            layout.y -= 6
            layout.line(MARGIN_X, layout.y, PAGE_WIDTH - MARGIN_X, layout.y)
            layout.y -= 6
        elif block.kind == "table":
            _table(block.rows, layout)
            layout.y -= 6
        elif block.kind == "code":
            _code(block, layout)
            layout.y -= 6


def _code(block, layout):
    code_size, code_leading = 8.0, 10.0
    columns = int((BODY_WIDTH - 12) / (0.6 * code_size))
    lines = [piece for line in block.text.split("\n") for piece in wrap_code(line, columns, block.language)]
    layout.ensure(code_leading * min(len(lines), 4) + (11 if block.label else 0) + 4)
    if block.label:
        layout.y -= 9
        layout.text(MARGIN_X, layout.y, "bold", 7.5, block.label, GREY)
        layout.y -= 2
    for line in lines:
        layout.ensure(code_leading)
        layout.rect(MARGIN_X, layout.y - code_leading, BODY_WIDTH, code_leading, LIGHT)
        layout.y -= code_leading
        layout.text(MARGIN_X + 6, layout.y + 2.5, "mono", code_size, line)


def _table(rows, layout):
    if not rows:
        return
    size, leading, pad = 8.5, 11.0, 4.0
    count = max(len(row) for row in rows)
    rows = [tuple(row) + ("",) * (count - len(row)) for row in rows]
    def cell_runs(number, cell):
        return [("bold", text) for _, text in inline(cell)] if number == 0 else inline(cell)

    natural = [max(sum(width(text, style, size) for style, text in cell_runs(number, row[column]))
                   for number, row in enumerate(rows)) + 2 * pad + 1 for column in range(count)]
    share = BODY_WIDTH / count
    widths = [min(value, share) for value in natural]
    spare = BODY_WIDTH - sum(widths)
    wanting = [max(0.0, natural[column] - widths[column]) for column in range(count)]
    if sum(wanting) > 0:
        widths = [widths[column] + spare * wanting[column] / sum(wanting) for column in range(count)]
    for number, row in enumerate(rows):
        cells = [wrap(cell_runs(number, cell), size, widths[column] - 2 * pad) for column, cell in enumerate(row)]
        height = max(len(cell) for cell in cells) * leading + pad
        layout.ensure(height)
        top = layout.y
        x = MARGIN_X
        for column, cell in enumerate(cells):
            y = top - pad / 2
            for line in cell:
                y -= leading
                cursor = x + pad
                for style, text in line:
                    layout.text(cursor, y + 2, style, size, text)
                    cursor += width(text, style, size)
            x += widths[column]
        layout.y = top - height
        layout.line(MARGIN_X, layout.y, MARGIN_X + sum(widths), layout.y)


def cover(layout, lines):
    """Draw a cover page from (style, size, text) lines, then start the body on a new page."""
    layout.y = PAGE_HEIGHT - 220
    for style, size, text in lines:
        for line in wrap([(style, text)], size, BODY_WIDTH):
            layout.y -= size * 1.5
            for run_style, value in line:
                layout.text(MARGIN_X, layout.y, run_style, size, value)
        layout.y -= size * 0.6
    layout.new_page()


def document(pages, info):
    """Serialise pages of content operators into PDF bytes. info maps Info-dictionary keys to strings."""
    fonts = list(FONTS.values())
    objects = {1: "<< /Type /Catalog /Pages 2 0 R >>"}
    first_font = 3
    for number, (_, base) in enumerate(fonts):
        objects[first_font + number] = f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>"
    info_number = first_font + len(fonts)
    objects[info_number] = "<< " + " ".join(f"/{key} ({_escape(value)})" for key, value in info.items()) + " >>"
    resources = " ".join(f"/{name} {first_font + number} 0 R" for number, (name, _) in enumerate(fonts))
    kids, streams = [], {}
    next_number = info_number + 1
    for page in pages:
        page_number, content_number = next_number, next_number + 1
        next_number += 2
        kids.append(f"{page_number} 0 R")
        objects[page_number] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                                f"/Resources << /Font << {resources} >> >> /Contents {content_number} 0 R >>")
        streams[content_number] = "\n".join(page).encode("latin-1")
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number in sorted(set(objects) | set(streams)):
        offsets[number] = len(output)
        if number in streams:
            body = streams[number]
            output += f"{number} 0 obj\n<< /Length {len(body)} >>\nstream\n".encode("latin-1")
            output += body + b"\nendstream\nendobj\n"
        else:
            output += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    total = max(offsets) + 1
    xref = len(output)
    output += f"xref\n0 {total}\n0000000000 65535 f \n".encode("latin-1")
    for number in range(1, total):
        output += f"{offsets[number]:010d} 00000 n \n".encode("latin-1")
    output += (f"trailer\n<< /Size {total} /Root 1 0 R /Info {info_number} 0 R >>\nstartxref\n{xref}\n%%EOF\n"
               .encode("latin-1"))
    return bytes(output)


def unencodable(text):
    """Characters of text that Windows-1252 cannot represent (they print as '?')."""
    return sorted({char for char in text if not _encodable(char)})


def _encodable(char):
    try:
        char.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def render(markdown, cover_lines, footer, info, describe=None):
    """Return (PDF bytes, characters that could not be encoded)."""
    layout = Layout()
    cover(layout, cover_lines)
    lay_out(parse(markdown, describe), layout)
    pages = [page for page in layout.pages if page] or [[]]
    total = len(pages)
    for number, page in enumerate(pages, start=1):
        label = f"{footer} - page {number} of {total}"
        page.append(f"{GREY} g BT /F1 8 Tf {(PAGE_WIDTH - width(label, 'regular', 8)) / 2:.2f} 30 Td "
                    f"({_escape(label)}) Tj ET")
    return document(pages, info), unencodable(markdown)
