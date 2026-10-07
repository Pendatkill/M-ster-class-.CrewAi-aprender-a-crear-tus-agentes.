"""
HERRAMIENTAS DE LOS AGENTES
===========================

En CrewAI una herramienta (Tool) es una función que un agente puede DECIDIR usar.
El agente ve su nombre, su descripción y sus argumentos, y la llama cuando la necesita.

Aquí vive `HerramientaPDF`, la que usa el Agente Maquetador (CREW 3, en app/crew.py).
Convierte el informe en Markdown que entrega el Agente QA en un PDF con formato profesional:

  - Portada con el título y una "ficha del encargo" (tema, enfoque elegido, fecha, modelo…)
  - Cuerpo del informe con estilos tipográficos (títulos, listas, tablas, citas, código)
  - Anexo con la interpretación elegida por el usuario (descripción, pros y contras)
  - Cabecera y pie en cada página con numeración «Página X de Y»

Usa ReportLab (Python puro, sin dependencias del sistema) y markdown-it-py para leer el Markdown.
"""
from __future__ import annotations

import io
import re
import unicodedata
from datetime import datetime
from typing import Any, Callable
from xml.sax.saxutils import escape

from crewai.tools import BaseTool
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field, PrivateAttr
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    Preformatted,
)

# ─────────────────────────────────────────────────────────────
# Estilo visual
# ─────────────────────────────────────────────────────────────
PRIMARIO = colors.HexColor("#1e2a38")  # azul pizarra: títulos y banda de portada
ACENTO = colors.HexColor("#2fa37a")  # verde QA (el mismo que en la oficina)
TEXTO = colors.HexColor("#2b2622")
SUAVE = colors.HexColor("#7a6f64")
LINEA = colors.HexColor("#e4dacb")
FONDO = colors.HexColor("#f6f3ee")

MARGEN_X, MARGEN_SUP, MARGEN_INF = 2.2 * cm, 2.6 * cm, 2.2 * cm
ANCHO_UTIL = A4[0] - 2 * MARGEN_X
CODIGO_MAX = 80  # caracteres por línea que caben en un bloque de código (Courier 8,8 pt)
MESES = "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split()


def _estilos() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=10.5, leading=15.5, textColor=TEXTO, spaceAfter=7)
    return {
        "base": base,
        "h1": ParagraphStyle("h1", base, fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=PRIMARIO, spaceBefore=14, spaceAfter=8),
        "h2": ParagraphStyle("h2", base, fontName="Helvetica-Bold", fontSize=14, leading=18, textColor=PRIMARIO, spaceBefore=16, spaceAfter=4),
        "h3": ParagraphStyle("h3", base, fontName="Helvetica-Bold", fontSize=12, leading=16, textColor=PRIMARIO, spaceBefore=10, spaceAfter=4),
        "h4": ParagraphStyle("h4", base, fontName="Helvetica-BoldOblique", fontSize=10.5, leading=15, textColor=TEXTO, spaceBefore=8, spaceAfter=3),
        "item": ParagraphStyle("item", base, spaceAfter=3),
        "cita": ParagraphStyle("cita", base, fontName="Helvetica-Oblique", textColor=SUAVE, spaceAfter=3),
        "codigo": ParagraphStyle("codigo", base, fontName="Courier", fontSize=8.8, leading=11.5, spaceAfter=0),
        "celda": ParagraphStyle("celda", base, fontSize=9.2, leading=12.5, spaceAfter=0),
        "celda_cab": ParagraphStyle("celda_cab", base, fontName="Helvetica-Bold", fontSize=9.2, leading=12.5, textColor=colors.white, spaceAfter=0),
        "etiqueta": ParagraphStyle("etiqueta", base, fontName="Helvetica-Bold", fontSize=8.5, leading=12, textColor=SUAVE, spaceAfter=0),
        "valor": ParagraphStyle("valor", base, fontSize=10, leading=14, spaceAfter=0),
        "kicker": ParagraphStyle("kicker", base, fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=ACENTO, spaceAfter=6),
        "titulo": ParagraphStyle("titulo", base, fontName="Helvetica-Bold", fontSize=24, leading=29, textColor=colors.white, spaceAfter=6, alignment=TA_LEFT),
        "subtitulo": ParagraphStyle("subtitulo", base, fontSize=11.5, leading=16, textColor=colors.HexColor("#c9d3de"), spaceAfter=0),
    }


# ─────────────────────────────────────────────────────────────
# Texto: las fuentes estándar de PDF solo cubren Latin-1/Windows-1252
# ─────────────────────────────────────────────────────────────
_SUSTITUTOS = {"✓": "OK", "✔": "OK", "✗": "X", "✘": "X", "→": "->", "←": "<-",
               "≥": ">=", "≤": "<=", "≠": "!=", "⚠": "(!)", "★": "*", "☆": "*", " ": " "}


def limpiar(texto: str) -> str:
    """Sustituye o quita los caracteres que Helvetica no sabe dibujar (emojis, símbolos…)."""
    salida = []
    for c in str(texto or ""):
        if c in _SUSTITUTOS:
            salida.append(_SUSTITUTOS[c])
        else:
            try:
                c.encode("cp1252")
                salida.append(c)
            except UnicodeEncodeError:
                pass
    return "".join(salida)


def _t(texto: str) -> str:
    """Texto plano → markup seguro para Paragraph."""
    return escape(limpiar(texto))


def _inline(token) -> str:
    """Convierte el contenido en línea de Markdown (negrita, cursiva, enlaces…) en markup de ReportLab."""
    partes: list[str] = []
    for c in token.children or []:
        tipo = c.type
        if tipo == "text":
            partes.append(_t(c.content))
        elif tipo == "code_inline":
            partes.append(f'<font name="Courier" backColor="#f1ece4">{_t(c.content)}</font>')
        elif tipo in ("softbreak",):
            partes.append(" ")
        elif tipo == "hardbreak":
            partes.append("<br/>")
        elif tipo == "strong_open":
            partes.append("<b>")
        elif tipo == "strong_close":
            partes.append("</b>")
        elif tipo == "em_open":
            partes.append("<i>")
        elif tipo == "em_close":
            partes.append("</i>")
        elif tipo == "s_open":
            partes.append("<strike>")
        elif tipo == "s_close":
            partes.append("</strike>")
        elif tipo == "link_open":
            href = escape(limpiar(c.attrGet("href") or ""), {'"': "&quot;"})
            partes.append(f'<link href="{href}" color="#2fa37a"><u>')
        elif tipo == "link_close":
            partes.append("</u></link>")
        elif tipo == "image":
            partes.append(f"<i>[Imagen: {_t(c.content)}]</i>")
        elif tipo in ("html_inline",):
            partes.append(_t(c.content))
    return "".join(partes)


# ─────────────────────────────────────────────────────────────
# Markdown → bloques del PDF
# ─────────────────────────────────────────────────────────────
def _bloques(tokens, i: int, fin: str | None, est: dict, estilo_parrafo: str = "base") -> tuple[list, int]:
    """Recorre los tokens de markdown-it desde `i` hasta el token `fin` y devuelve los flowables."""
    salida: list = []
    while i < len(tokens):
        tok = tokens[i]
        tipo = tok.type
        if fin and tipo == fin:
            return salida, i + 1

        if tipo == "heading_open":
            nivel = min(int(tok.tag[1]), 4)
            texto = _inline(tokens[i + 1])
            if nivel == 2:
                # Los apartados principales llevan una línea de acento debajo
                titulo = [Paragraph(texto, est["h2"]), HRFlowable(width="100%", thickness=1.2, color=ACENTO, spaceBefore=0, spaceAfter=8)]
            else:
                titulo = [Paragraph(texto, est[f"h{nivel}"])]
            titulo[0]._es_titulo = True
            salida += titulo
            i += 3

        elif tipo == "paragraph_open":
            salida.append(Paragraph(_inline(tokens[i + 1]), est[estilo_parrafo]))
            i += 3

        elif tipo in ("bullet_list_open", "ordered_list_open"):
            ordenada = tipo == "ordered_list_open"
            cierre = tipo.replace("_open", "_close")
            inicio = int(tok.attrGet("start") or 1) if ordenada else 1
            items, i = [], i + 1
            while tokens[i].type != cierre:
                contenido, i = _bloques(tokens, i + 1, "list_item_close", est, "item")
                items.append(ListItem(contenido))
            i += 1
            salida.append(ListFlowable(
                items,
                bulletType="1" if ordenada else "bullet",
                start=inicio if ordenada else "•",
                bulletFontName="Helvetica-Bold" if ordenada else "Helvetica",
                bulletFontSize=10 if ordenada else 11,
                bulletColor=ACENTO,
                bulletFormat="%s." if ordenada else None,
                leftIndent=16,
                spaceAfter=6,
            ))

        elif tipo == "blockquote_open":
            contenido, i = _bloques(tokens, i + 1, "blockquote_close", est, "cita")
            caja = Table([[contenido]], colWidths=[ANCHO_UTIL - 0.4 * cm])
            caja.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), FONDO),
                ("LINEBEFORE", (0, 0), (0, -1), 3, ACENTO),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            salida += [caja, Spacer(1, 8)]

        elif tipo in ("fence", "code_block"):
            # Preformatted parte las líneas largas para que el código no se salga del margen
            codigo = Preformatted(limpiar(tok.content.rstrip("\n")), est["codigo"], maxLineLength=CODIGO_MAX, newLineChars="  ")
            caja = Table([[codigo]], colWidths=[ANCHO_UTIL])
            caja.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), FONDO),
                ("BOX", (0, 0), (-1, -1), 0.5, LINEA),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]))
            salida += [caja, Spacer(1, 8)]
            i += 1

        elif tipo == "table_open":
            tabla, i = _tabla(tokens, i + 1, est)
            salida += [tabla, Spacer(1, 10)]

        elif tipo == "hr":
            salida.append(HRFlowable(width="100%", thickness=0.6, color=LINEA, spaceBefore=6, spaceAfter=10))
            i += 1

        elif tipo == "html_block":
            salida.append(Paragraph(_t(tok.content), est[estilo_parrafo]))
            i += 1

        else:
            i += 1
    return salida, i


def _tabla(tokens, i: int, est: dict) -> tuple[Table, int]:
    filas: list[list] = []
    filas_cabecera = 0
    while tokens[i].type != "table_close":
        tok = tokens[i]
        if tok.type == "tr_open":
            filas.append([])
        elif tok.type in ("th_open", "td_open"):
            cab = tok.type == "th_open"
            filas[-1].append(Paragraph(_inline(tokens[i + 1]), est["celda_cab" if cab else "celda"]))
            if cab and len(filas) > filas_cabecera:
                filas_cabecera = len(filas)
        i += 1
    columnas = max((len(f) for f in filas), default=1)
    for f in filas:
        f += [""] * (columnas - len(f))
    tabla = Table(filas, colWidths=[ANCHO_UTIL / columnas] * columnas, repeatRows=filas_cabecera, hAlign="LEFT")
    estilo = [
        ("GRID", (0, 0), (-1, -1), 0.5, LINEA),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, filas_cabecera), (-1, -1), [colors.white, FONDO]),
    ]
    if filas_cabecera:
        estilo.append(("BACKGROUND", (0, 0), (-1, filas_cabecera - 1), PRIMARIO))
    tabla.setStyle(TableStyle(estilo))
    return tabla, i + 1


# ─────────────────────────────────────────────────────────────
# Portada, ficha del encargo y anexo
# ─────────────────────────────────────────────────────────────
def _fecha_es(d: datetime) -> str:
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}, {d:%H:%M}"


def _portada(titulo: str, subtitulo: str, est: dict) -> list:
    filas = [[Paragraph("INFORME · OFICINA MULTIAGENTE", est["kicker"])], [Paragraph(_t(titulo), est["titulo"])]]
    if subtitulo and subtitulo.lower() not in titulo.lower():  # no repetir lo que ya dice el título
        filas.append([Paragraph(_t(subtitulo), est["subtitulo"])])
    banda = Table(filas, colWidths=[ANCHO_UTIL])
    banda.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PRIMARIO),
        ("LINEBELOW", (0, -1), (-1, -1), 4, ACENTO),
        ("LEFTPADDING", (0, 0), (-1, -1), 22),
        ("RIGHTPADDING", (0, 0), (-1, -1), 22),
        ("TOPPADDING", (0, 0), (-1, 0), 22),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 22),
    ]))
    return [banda, Spacer(1, 16)]


def _ficha(meta: dict, est: dict) -> list:
    eleccion = meta.get("eleccion") or {}
    enfoque = eleccion.get("titulo", "")
    if eleccion.get("enfoque"):
        enfoque = f"{enfoque} — {eleccion['enfoque']}" if enfoque else eleccion["enfoque"]
    campos = [
        ("TEMA", meta.get("tema", "")),
        ("ENFOQUE ELEGIDO", enfoque),
        ("INDICACIONES", eleccion.get("comentario") or "Sin indicaciones adicionales"),
        ("EQUIPO", "Agente Investigador  ›  Agente de Revisión  ›  Agente QA"),
        ("MODELO", meta.get("modelo", "")),
        ("FECHA", _fecha_es(meta.get("fecha") or datetime.now())),
        ("REFERENCIA", meta.get("referencia", "")),
    ]
    filas = [[Paragraph(k, est["etiqueta"]), Paragraph(_t(v), est["valor"])] for k, v in campos if v]
    tabla = Table(filas, colWidths=[3.6 * cm, ANCHO_UTIL - 3.6 * cm])
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), FONDO),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINEA),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [tabla, Spacer(1, 20)]


def _anexo(eleccion: dict, est: dict) -> list:
    if not eleccion or not eleccion.get("titulo"):
        return []
    md = f"## Anexo · Interpretación elegida\n\n### {eleccion['titulo']}\n\n"
    if eleccion.get("enfoque"):
        md += f"*{eleccion['enfoque']}*\n\n"
    if eleccion.get("descripcion"):
        md += f"{eleccion['descripcion']}\n\n"
    for nombre, clave in (("Ventajas", "pros"), ("Limitaciones", "contras")):
        if eleccion.get(clave):
            md += f"**{nombre}**\n\n" + "".join(f"- {x}\n" for x in eleccion[clave]) + "\n"
    return [Spacer(1, 10), *_markdown(md, est)]


def _markdown(md: str, est: dict) -> list:
    parser = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    flowables, _ = _bloques(parser.parse(md or ""), 0, None, est)
    return _pegar_titulos(flowables)


def _pegar_titulos(flowables: list) -> list:
    """Agrupa cada título con el bloque que le sigue para que nunca quede solo al final de una página."""
    salida, i = [], 0
    while i < len(flowables):
        if getattr(flowables[i], "_es_titulo", False):
            j = i + 1
            while j < len(flowables) and (getattr(flowables[j], "_es_titulo", False) or isinstance(flowables[j], HRFlowable)):
                j += 1
            salida.append(KeepTogether(flowables[i : j + 1]))
            i = j + 1
        else:
            salida.append(flowables[i])
            i += 1
    return salida


_VALLA_MD = re.compile(r"^[ \t]*```[ \t]*(markdown|md)?[ \t]*\n(.*?)\n[ \t]*```[ \t]*$", re.S | re.M | re.I)


def desenvolver_markdown(texto: str) -> str:
    """Los modelos pequeños a menudo entregan el informe envuelto en ```markdown … ```.
    Quitamos esa «valla» para que se lea como Markdown y no como un bloque de código."""
    texto = (texto or "").strip()

    def quitar(m: re.Match) -> str:
        es_markdown = bool(m.group(1))
        al_principio = m.start() == 0
        parece_informe = re.search(r"^#{1,3}\s", m.group(2), re.M)
        return m.group(2) if es_markdown or (al_principio and parece_informe) else m.group(0)

    texto = _VALLA_MD.sub(quitar, texto).strip()
    # Valla abierta que el modelo nunca cerró: quitamos solo la línea de apertura
    abierta = re.match(r"```[ \t]*(markdown|md)?[ \t]*\n", texto, re.I)
    if abierta and texto.count("```") % 2 == 1:
        texto = texto[abierta.end():].strip()
    return texto


def _separar_titulo(md: str, tema: str) -> tuple[str, str]:
    """Si el informe empieza con un «# Título», lo usamos en la portada y lo quitamos del cuerpo."""
    m = re.match(r"\s*#\s+(.+?)\s*#*\s*(?:\n|$)", md or "")
    if m:
        titulo = re.sub(r"[*_`]", "", m.group(1)).strip()
        return titulo, md[m.end():]
    return f"Informe: {tema}", md or ""


# ─────────────────────────────────────────────────────────────
# Cabecera, pie y «Página X de Y»
# ─────────────────────────────────────────────────────────────
def _canvas_numerado(titulo_corto: str):
    class CanvasNumerado(rl_canvas.Canvas):
        """Guarda cada página y al final las dibuja sabiendo el total de páginas."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._paginas = []

        def showPage(self):
            self._paginas.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._paginas)
            for estado in self._paginas:
                self.__dict__.update(estado)
                self._decorar(total)
                super().showPage()
            super().save()

        def _decorar(self, total: int) -> None:
            ancho, alto = A4
            self.saveState()
            if self._pageNumber > 1:  # la portada no lleva cabecera
                self.setFont("Helvetica", 8)
                self.setFillColor(SUAVE)
                self.drawString(MARGEN_X, alto - 1.4 * cm, titulo_corto)
                self.drawRightString(ancho - MARGEN_X, alto - 1.4 * cm, "Oficina Multiagente · CrewAI")
                self.setStrokeColor(LINEA)
                self.setLineWidth(0.5)
                self.line(MARGEN_X, alto - 1.6 * cm, ancho - MARGEN_X, alto - 1.6 * cm)
            self.setStrokeColor(LINEA)
            self.line(MARGEN_X, 1.5 * cm, ancho - MARGEN_X, 1.5 * cm)
            self.setFont("Helvetica", 8)
            self.setFillColor(SUAVE)
            self.drawString(MARGEN_X, 1.05 * cm, "Generado por agentes de IA y validado por el Agente QA")
            self.drawRightString(ancho - MARGEN_X, 1.05 * cm, f"Página {self._pageNumber} de {total}")
            self.restoreState()

    return CanvasNumerado


# ─────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────
def generar_pdf(informe_md: str, meta: dict, titulo: str = "", subtitulo: str = "") -> tuple[bytes, int]:
    """
    informe_md: el informe en Markdown que entrega el Agente QA.
    meta: {"tema", "eleccion" (dict de Interpretacion + comentario), "modelo", "fecha", "referencia"}
    titulo / subtitulo: los que decide el Agente Maquetador (si faltan, se usa el «# título» del informe).
    Devuelve (bytes del PDF, número de páginas).
    """
    est = _estilos()
    tema = meta.get("tema", "")
    titulo_md, cuerpo = _separar_titulo(desenvolver_markdown(informe_md), tema)
    titulo = limpiar(titulo).strip() or titulo_md
    subtitulo = limpiar(subtitulo).strip() or tema

    historia = [
        *_portada(titulo, subtitulo, est),
        *_ficha(meta, est),
        *_markdown(cuerpo, est),
        *_anexo(meta.get("eleccion") or {}, est),
    ]

    buffer = io.BytesIO()
    titulo_corto = limpiar(titulo if len(titulo) <= 80 else titulo[:77] + "...")
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGEN_X,
        rightMargin=MARGEN_X,
        topMargin=MARGEN_SUP,
        bottomMargin=MARGEN_INF,
        title=limpiar(titulo),
        author="Oficina Multiagente · CrewAI",
        subject=limpiar(tema),
        creator="Oficina Multiagente · CrewAI",
    )
    doc.build(historia, canvasmaker=_canvas_numerado(titulo_corto))
    return buffer.getvalue(), doc.page


def nombre_archivo(tema: str) -> str:
    """«¿Qué es la IA generativa?» → informe-que-es-la-ia-generativa.pdf"""
    ascii_ = unicodedata.normalize("NFKD", tema).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_).strip("-").lower()[:60].strip("-")
    return f"informe-{slug or 'final'}.pdf"


# ─────────────────────────────────────────────────────────────
# La herramienta que usa el Agente Maquetador
# ─────────────────────────────────────────────────────────────
class ArgumentosPDF(BaseModel):
    """Lo que el agente tiene que decidir al llamar a la herramienta."""

    titulo: str = Field(description="Título profesional del informe para la portada (máx. 12 palabras)")
    subtitulo: str = Field(default="", description="Subtítulo de una línea que resume el enfoque del informe")


class HerramientaPDF(BaseTool):
    name: str = "Generar PDF del informe"
    description: str = (
        "Maqueta el informe final en un PDF profesional (portada, ficha del encargo, cuerpo y anexo). "
        "El contenido del informe ya lo tiene la herramienta: solo hay que pasarle el título y el subtítulo."
    )
    args_schema: type[BaseModel] = ArgumentosPDF

    # El informe y sus datos se cargan al crear la herramienta: así el LLM no tiene que
    # copiar el informe entero como argumento (sería lento y podría alterarlo).
    _informe: str = PrivateAttr(default="")
    _meta: dict = PrivateAttr(default_factory=dict)
    _emit: Callable[..., Any] | None = PrivateAttr(default=None)
    pdf: bytes | None = None

    def __init__(self, informe: str, meta: dict, emit=None, **kwargs):
        super().__init__(**kwargs)
        self._informe, self._meta, self._emit = informe, meta, emit

    def _run(self, titulo: str, subtitulo: str = "") -> str:
        if self._emit:
            self._emit("maquetador", "tool", f"Usando herramienta «{self.name}»: «{titulo or 'título del informe'}»")
        self.pdf, paginas = generar_pdf(self._informe, self._meta, titulo, subtitulo)
        return f"PDF generado correctamente: {paginas} páginas, {len(self.pdf) // 1024} KB."
