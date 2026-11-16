"""A small Word (.docx) writer with no extra packages, for the app's reports.

It writes the WordprocessingML parts a report needs and nothing more: styles (headings that keep with
the next paragraph, table text, bullets), US Letter pages with a running header and a "Page X of Y"
footer, tables with fixed column widths (set on the grid and on every cell, as Word and Google Docs
both need) and cell shading, a repeating header row, PNG images and external links. Text is escaped,
and characters XML can't hold are dropped, so any rule name can go in.

    doc = Document("Report title", header="Left header text", footer="Footer text")
    doc.heading("Summary"); doc.paragraph("Text"); doc.table([("Name", 3000), ("State", 1500)], rows)
    data = doc.to_bytes()
"""
from datetime import datetime, timezone
from html import escape
import io
import re
import struct
import zipfile

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NAMESPACES = (f'xmlns:w="{W}" xmlns:r="{R}" '
              'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
              'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
              'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
PAGE_WIDTH, PAGE_HEIGHT, MARGIN = 12240, 15840, 1080  # US Letter, 0.75" margins (twentieths of a point).
TEXT_WIDTH = PAGE_WIDTH - 2 * MARGIN
EMU_PER_PX = 9525  # At 96 DPI.
FONT = "Calibri"
INK, MUTED, NAVY, ACCENT, LINE, BAND = "1E293B", "5B6778", "1F3864", "2563EB", "CBD5E1", "F3F6FA"
_BAD_CHARS = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\ufffe\\uffff]")


def text_xml(value):
    """``value`` as XML text: escaped, with the characters XML 1.0 can't hold removed."""
    return escape(_BAD_CHARS.sub("", str(value if value is not None else "")), quote=True)


def png_size(data):
    """(width, height) in pixels from a PNG's header."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("Not a PNG image.")
    return struct.unpack(">II", data[16:24])


def run(text, bold=False, italic=False, color=None, size=None, font=None):
    """One run of text; line breaks in ``text`` become breaks within the paragraph."""
    props = ""
    if font: props += f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:cs="{font}"/>'
    if bold: props += "<w:b/><w:bCs/>"
    if italic: props += "<w:i/><w:iCs/>"
    if color: props += f'<w:color w:val="{color}"/>'
    if size: props += f'<w:sz w:val="{int(size * 2)}"/><w:szCs w:val="{int(size * 2)}"/>'
    parts = str(text if text is not None else "").split("\n")
    body = '<w:br/>'.join(f'<w:t xml:space="preserve">{text_xml(part)}</w:t>' for part in parts)
    return f"<w:r>{'<w:rPr>' + props + '</w:rPr>' if props else ''}{body}</w:r>"


def paragraph_xml(content, style=None, align=None, keep_next=False, space_before=None, space_after=None,
                  shade=None, border_left=None, border_bottom=None, page_break_before=False):
    """A paragraph from run XML (or plain text). Properties are written in the schema's order."""
    if not isinstance(content, (list, tuple)): content = [content]
    runs = "".join(item if isinstance(item, str) and item.startswith("<w:") else run(item) for item in content)
    props = f'<w:pStyle w:val="{style}"/>' if style else ""
    if keep_next: props += "<w:keepNext/>"
    if page_break_before: props += "<w:pageBreakBefore/>"
    if border_left or border_bottom:
        props += "<w:pBdr>"
        if border_left: props += f'<w:left w:val="single" w:sz="18" w:space="8" w:color="{border_left}"/>'
        if border_bottom: props += f'<w:bottom w:val="single" w:sz="12" w:space="6" w:color="{border_bottom}"/>'
        props += "</w:pBdr>"
    if shade: props += f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>'
    if space_before is not None or space_after is not None:
        props += "<w:spacing" + (f' w:before="{space_before}"' if space_before is not None else "") + \
                 (f' w:after="{space_after}"' if space_after is not None else "") + "/>"
    if align: props += f'<w:jc w:val="{align}"/>'
    return f"<w:p>{'<w:pPr>' + props + '</w:pPr>' if props else ''}{runs}</w:p>"


class Cell:
    """A table cell: text (or run XML, or a list of paragraphs' runs), with optional fill, colour and weight."""

    def __init__(self, content="", fill=None, color=None, bold=False, align=None, style="TableText", note=None):
        self.content, self.fill, self.color, self.bold, self.align = content, fill, color, bold, align
        self.style, self.note = style, note

    def xml(self, width):
        props = f'<w:tcW w:w="{width}" w:type="dxa"/>'
        if self.fill: props += f'<w:shd w:val="clear" w:color="auto" w:fill="{self.fill}"/>'
        props += '<w:vAlign w:val="center"/>'
        content = self.content
        if isinstance(content, list):  # Ready-made paragraphs.
            return f"<w:tc><w:tcPr>{props}</w:tcPr>{''.join(content) or paragraph_xml('')}</w:tc>"
        runs = content if isinstance(content, str) and content.startswith("<w:") else \
            run(content, bold=self.bold, color=self.color)
        body = paragraph_xml(runs, style=self.style, align=self.align)
        if self.note:  # A second, smaller line: a rule ID under its name, say.
            body += paragraph_xml(run(self.note, color=MUTED, size=7.5), style=self.style, align=self.align)
        return f"<w:tc><w:tcPr>{props}</w:tcPr>{body}</w:tc>"


class Document:
    def __init__(self, title, header="", footer="", author="Sentinel Workspace", created=None):
        self.title, self.header_text, self.footer_text, self.author = title, header, footer, author
        self.created = created or datetime.now(timezone.utc)
        self.body, self.media, self.links, self.drawings = [], [], [], 0

    # ------------------------------------------------------------- content
    def add(self, xml):
        self.body.append(xml)

    def heading(self, text, level=1, page_break_before=False):
        self.add(paragraph_xml(run(text), style=f"Heading{level}", page_break_before=page_break_before))

    def paragraph(self, content, style=None, **kwargs):
        self.add(paragraph_xml(content, style=style, **kwargs))

    def bullets(self, items):
        for item in items: self.add(paragraph_xml(item, style="ListBullet"))

    def callout(self, content, color=ACCENT, fill="EEF4FF"):
        """A tinted paragraph with a coloured bar at its left: a note the reader shouldn't miss."""
        self.add(paragraph_xml(content, style="Callout", border_left=color, shade=fill))

    def page_break(self):
        self.add('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    def link(self, text, url):
        """Run XML for an external link to ``url`` (http or https only)."""
        if not re.match(r"https?://", str(url or "")): return run(text)
        self.links.append(url)
        rid = f"rIdLink{len(self.links)}"
        return (f'<w:hyperlink r:id="{rid}" w:history="1"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
                f'<w:t xml:space="preserve">{text_xml(text)}</w:t></w:r></w:hyperlink>')

    def image(self, data, height_px, name="image"):
        """Run XML showing a PNG ``height_px`` high at 96 DPI, in proportion."""
        width, height = png_size(data)
        cy = int(height_px * EMU_PER_PX); cx = int(cy * width / max(1, height))
        self.media.append(data); self.drawings += 1
        rid, n = f"rIdImage{len(self.media)}", self.drawings
        return (f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
                f'<wp:docPr id="{n}" name="{text_xml(name)} {n}"/><wp:cNvGraphicFramePr>'
                '<a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
                '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{n}" name="image{len(self.media)}.png"/><pic:cNvPicPr/></pic:nvPicPr>'
                f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
                f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
                '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
                '</wp:inline></w:drawing></w:r>')

    def table(self, columns, rows, header=True, borders=True, header_fill=NAVY, band=False, border_color=LINE,
              border_size=4):
        """A table. ``columns`` is [(title, width)] (widths in twentieths of a point, summing to the
        table's width); ``rows`` holds Cells or plain values. The header row repeats on each page, and a
        row never splits across pages. ``band`` shades every other row; a row can also be
        (cells, fill) to shade it as a whole."""
        widths = [width for _, width in columns]
        line = f'w:val="single" w:sz="{border_size}" w:space="0" w:color="{border_color}"' if borders else 'w:val="nil"'
        edges = "".join(f"<w:{edge} {line}/>" for edge in ("top", "left", "bottom", "right", "insideH", "insideV"))
        xml = [f'<w:tbl><w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblBorders>{edges}</w:tblBorders>'
               '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:top w:w="50" w:type="dxa"/>'
               '<w:left w:w="100" w:type="dxa"/><w:bottom w:w="50" w:type="dxa"/><w:right w:w="100" w:type="dxa"/>'
               '</w:tblCellMar><w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" '
               'w:lastColumn="0" w:noHBand="0" w:noVBand="1"/></w:tblPr>',
               "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths) + "</w:tblGrid>"]
        if header:
            cells = "".join(Cell(title, fill=header_fill, color="FFFFFF", bold=True, style="TableHeading").xml(w)
                            for title, w in columns)
            xml.append(f'<w:tr><w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>{cells}</w:tr>')
        for index, row in enumerate(rows):
            fill = None
            if isinstance(row, tuple) and len(row) == 2 and isinstance(row[0], list): row, fill = row
            if fill is None and band and index % 2: fill = BAND
            cells = []
            for value, width in zip(row, widths):
                cell = value if isinstance(value, Cell) else Cell(value)
                if fill and not cell.fill: cell.fill = fill
                cells.append(cell.xml(width))
            xml.append(f"<w:tr><w:trPr><w:cantSplit/></w:trPr>{''.join(cells)}</w:tr>")
        xml.append("</w:tbl>")
        self.add("".join(xml))
        # Word needs a paragraph between two tables, and after a table at the end of a cell or the body.
        self.add(paragraph_xml("", style="TableGap"))

    # ------------------------------------------------------------- parts
    def document_xml(self):
        section = (f'<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/>'
                   f'<w:footerReference w:type="default" r:id="rIdFooter"/>'
                   f'<w:pgSz w:w="{PAGE_WIDTH}" w:h="{PAGE_HEIGHT}"/>'
                   f'<w:pgMar w:top="{MARGIN}" w:right="{MARGIN}" w:bottom="{MARGIN}" w:left="{MARGIN}" '
                   'w:header="560" w:footer="560" w:gutter="0"/><w:cols w:space="720"/></w:sectPr>')
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {NAMESPACES}><w:body>'
                + "".join(self.body) + section + "</w:body></w:document>")

    def header_xml(self):
        tab = f'<w:tabs><w:tab w:val="right" w:pos="{TEXT_WIDTH}"/></w:tabs>'
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:hdr {NAMESPACES}>'
                f'<w:p><w:pPr><w:pStyle w:val="Header"/>{tab}</w:pPr>{run(self.header_text)}'
                f'<w:r><w:tab/></w:r>{run(self.title)}</w:p></w:hdr>')

    def footer_xml(self):
        tab = f'<w:tabs><w:tab w:val="right" w:pos="{TEXT_WIDTH}"/></w:tabs>'
        field = lambda code: (f'<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> {code} '
                              '</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
                              '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:ftr {NAMESPACES}>'
                f'<w:p><w:pPr><w:pStyle w:val="Footer"/>{tab}</w:pPr>{run(self.footer_text)}<w:r><w:tab/></w:r>'
                f'{run("Page ")}{field("PAGE")}{run(" of ")}{field("NUMPAGES")}</w:p></w:ftr>')

    @staticmethod
    def styles_xml():
        def style(kind, sid, name, ppr="", rpr="", based="Normal", extra=""):
            return (f'<w:style w:type="{kind}" w:styleId="{sid}"{" w:default=\"1\"" if sid == "Normal" else ""}>'
                    f'<w:name w:val="{name}"/>' + (f'<w:basedOn w:val="{based}"/>' if based else "") + extra +
                    "<w:qFormat/>" + (f"<w:pPr>{ppr}</w:pPr>" if ppr else "") + (f"<w:rPr>{rpr}</w:rPr>" if rpr else "")
                    + "</w:style>")
        font = f'<w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:eastAsia="{FONT}" w:cs="{FONT}"/>'
        size = lambda pt: f'<w:sz w:val="{int(pt * 2)}"/><w:szCs w:val="{int(pt * 2)}"/>'
        color = lambda c: f'<w:color w:val="{c}"/>'
        styles = [
            style("paragraph", "Normal", "Normal", '<w:spacing w:after="120" w:line="264" w:lineRule="auto"/>',
                  font + color(INK) + size(10), based=None),
            style("paragraph", "Title", "Title", '<w:spacing w:before="0" w:after="40"/>',
                  "<w:b/><w:bCs/>" + color(NAVY) + size(24)),
            style("paragraph", "Subtitle", "Subtitle", '<w:spacing w:before="0" w:after="0"/>', color(MUTED) + size(11)),
            style("paragraph", "Heading1", "heading 1",
                  f'<w:keepNext/><w:keepLines/><w:pBdr><w:bottom w:val="single" w:sz="8" w:space="4" w:color="{ACCENT}"/>'
                  '</w:pBdr><w:spacing w:before="360" w:after="160"/><w:outlineLvl w:val="0"/>',
                  "<w:b/><w:bCs/>" + color(NAVY) + size(14)),
            style("paragraph", "Heading2", "heading 2",
                  '<w:keepNext/><w:keepLines/><w:spacing w:before="240" w:after="100"/><w:outlineLvl w:val="1"/>',
                  "<w:b/><w:bCs/>" + color(NAVY) + size(11.5)),
            style("paragraph", "TableText", "Table Text", '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>',
                  size(9)),
            style("paragraph", "TableHeading", "Table Heading", '<w:keepNext/><w:spacing w:before="0" w:after="0"/>',
                  "<w:b/><w:bCs/>" + color("FFFFFF") + size(9)),
            style("paragraph", "TableGap", "Table Gap", '<w:spacing w:before="0" w:after="120" w:line="120" w:lineRule="auto"/>',
                  size(4)),
            style("paragraph", "Small", "Small", '<w:spacing w:after="60"/>', color(MUTED) + size(8.5)),
            style("paragraph", "Callout", "Callout", '<w:spacing w:before="60" w:after="160"/><w:ind w:left="200" w:right="120"/>',
                  size(9.5)),
            style("paragraph", "KpiValue", "KPI Value", '<w:spacing w:before="40" w:after="0"/><w:jc w:val="center"/>',
                  "<w:b/><w:bCs/>" + size(20)),
            style("paragraph", "KpiLabel", "KPI Label", '<w:spacing w:before="0" w:after="40"/><w:jc w:val="center"/>',
                  size(8.5)),
            style("paragraph", "ListBullet", "List Bullet",
                  '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr><w:spacing w:after="80"/>'
                  '<w:ind w:left="360" w:hanging="270"/>'),
            style("paragraph", "Header", "header", '<w:spacing w:after="0"/>', color(MUTED) + size(8.5)),
            style("paragraph", "Footer", "footer", '<w:spacing w:after="0"/>', color(MUTED) + size(8.5)),
            style("character", "Hyperlink", "Hyperlink", rpr=color(ACCENT) + '<w:u w:val="single"/>', based=None),
        ]
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:styles xmlns:w="{W}"><w:docDefaults><w:rPrDefault><w:rPr>{font}{size(10)}'
                '<w:lang w:val="en-US"/></w:rPr></w:rPrDefault><w:pPrDefault/></w:docDefaults>'
                + "".join(styles) + "</w:styles>")

    @staticmethod
    def numbering_xml():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/>'
                '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/>'
                '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="270"/></w:pPr>'
                f'<w:rPr><w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}"/><w:color w:val="{ACCENT}"/></w:rPr></w:lvl>'
                '</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>')

    def to_bytes(self):
        """The finished .docx. The ZIP holds files only (no folder entries), as Word requires through
        co-editing services."""
        stamp = self.created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                 '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                 '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
                 '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
                 '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
                 '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
                 '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                 '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                 '</Types>')
        root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     f'<Relationship Id="rId1" Type="{REL}officeDocument" Target="word/document.xml"/>'
                     '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                     f'<Relationship Id="rId3" Type="{REL}extended-properties" Target="docProps/app.xml"/></Relationships>')
        rels = [f'<Relationship Id="rIdStyles" Type="{REL}styles" Target="styles.xml"/>',
                f'<Relationship Id="rIdSettings" Type="{REL}settings" Target="settings.xml"/>',
                f'<Relationship Id="rIdNumbering" Type="{REL}numbering" Target="numbering.xml"/>',
                f'<Relationship Id="rIdHeader" Type="{REL}header" Target="header1.xml"/>',
                f'<Relationship Id="rIdFooter" Type="{REL}footer" Target="footer1.xml"/>']
        rels += [f'<Relationship Id="rIdImage{i}" Type="{REL}image" Target="media/image{i}.png"/>'
                 for i in range(1, len(self.media) + 1)]
        rels += [f'<Relationship Id="rIdLink{i}" Type="{REL}hyperlink" Target="{text_xml(url)}" TargetMode="External"/>'
                 for i, url in enumerate(self.links, 1)]
        document_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         + "".join(rels) + "</Relationships>")
        settings = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    f'<w:settings xmlns:w="{W}"><w:defaultTabStop w:val="720"/><w:characterSpacingControl w:val="doNotCompress"/>'
                    '<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/>'
                    '</w:compat></w:settings>')
        core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                f'<dc:title>{text_xml(self.title)}</dc:title><dc:creator>{text_xml(self.author)}</dc:creator>'
                f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
                f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified></cp:coreProperties>')
        app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
               f'<Application>{text_xml(self.author)}</Application></Properties>')
        parts = {"[Content_Types].xml": types, "_rels/.rels": root_rels, "word/document.xml": self.document_xml(),
                 "word/_rels/document.xml.rels": document_rels, "word/styles.xml": self.styles_xml(),
                 "word/settings.xml": settings, "word/numbering.xml": self.numbering_xml(),
                 "word/header1.xml": self.header_xml(), "word/footer1.xml": self.footer_xml(),
                 "docProps/core.xml": core, "docProps/app.xml": app}
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, xml in parts.items(): archive.writestr(name, xml.encode("utf-8"))
            for i, data in enumerate(self.media, 1): archive.writestr(f"word/media/image{i}.png", data)
        return out.getvalue()
