"""Modo demo: simula el crew sin llamar a ningún LLM.

Se activa con DEMO_MODE=1, o si el proveedor elegido necesita API key y falta.

"""
from __future__ import annotations

import random
import time

from .herramientas import HerramientaPDF
from .models import Interpretacion, Revision


def _pausa(a: float = 1.2, b: float = 2.6) -> None:
    time.sleep(random.uniform(a, b))


def run_analisis(tema: str, emit) -> tuple[str, Revision]:
    emit("investigador", "start", "Empiezo a investigar la tarea.")
    for paso in [
        f"Descompongo «{tema}» en subpreguntas: contexto, estado actual, actores y datos.",
        "Usando herramienta «búsqueda web»: consultando fuentes recientes…",
        "Contrasto tres fuentes; descarto una por falta de datos verificables.",
        "Organizo los hallazgos en el dossier: contexto, datos, controversias.",
    ]:
        _pausa()
        emit("investigador", "tool" if "herramienta" in paso else "thought", paso)
    investigacion = (
        f"# Dossier: {tema}\n\n## Contexto\nSíntesis del contexto general del tema.\n\n"
        "## Hallazgos clave\n- Hallazgo 1 respaldado por datos.\n- Hallazgo 2 con matices.\n\n"
        "## Controversias\n- Visiones enfrentadas sobre impacto y ritmo de adopción.\n\n"
        "## Fuentes\n- (modo demo: sin fuentes reales)"
    )
    emit("investigador", "done", "Dossier terminado. Se lo paso a Revisión.")

    for paso in [
        "Leo el dossier completo del Investigador.",
        "Detecto al menos tres lecturas posibles: técnica, estratégica y crítica.",
        "Redacto pros y contras de cada interpretación.",
    ]:
        _pausa()
        emit("revisor", "thought", paso)
    revision = Revision(
        resumen_investigacion=f"La investigación sobre «{tema}» muestra un tema con base sólida de datos y debates abiertos.",
        opciones=[
            Interpretacion(
                titulo="Enfoque técnico",
                enfoque="Cómo funciona y qué lo hace posible",
                descripcion="Informe detallado de mecanismos, componentes y estado del arte.",
                pros=["Profundidad", "Precisión"],
                contras=["Menos accesible para público general"],
            ),
            Interpretacion(
                titulo="Enfoque estratégico",
                enfoque="Qué oportunidades y riesgos supone para una organización",
                descripcion="Informe orientado a decisiones: impacto, costes, hoja de ruta.",
                pros=["Accionable", "Orientado a negocio"],
                contras=["Menos detalle técnico"],
            ),
            Interpretacion(
                titulo="Enfoque crítico",
                enfoque="Qué se discute y qué no está demostrado",
                descripcion="Informe que contrasta afirmaciones, límites y controversias.",
                pros=["Rigor", "Detecta humo"],
                contras=["Puede resultar menos propositivo"],
            ),
        ],
        pregunta_al_usuario="He preparado tres formas de enfocar el informe. ¿Cuál quieres que sigamos?",
    )
    emit("revisor", "done", "Opciones listas para el usuario.")
    return investigacion, revision


def run_informe(tema: str, investigacion: str, opcion: Interpretacion, comentario: str, emit) -> str:
    emit("qa", "start", f"Recibo la opción elegida: «{opcion.titulo}». Empiezo la validación.")
    for paso in [
        "Reviso que cada hallazgo del dossier encaje con el enfoque elegido.",
        "Marco dos afirmaciones que necesitan matiz.",
        "Checklist de QA completado: 5 puntos validados, 2 huecos, 1 riesgo.",
    ]:
        _pausa()
        emit("qa", "thought", paso)
    emit("qa", "done", "Validación completada. Paso a redactar el informe.")
    for paso in ["Estructuro el informe: resumen, desarrollo, conclusiones.", "Redacto recomendaciones y nota de calidad."]:
        _pausa()
        emit("qa", "thought", paso)
    return (
        f"# Informe: {tema}\n\n*Interpretación elegida: **{opcion.titulo}** — {opcion.enfoque}*\n\n"
        f"## Resumen ejecutivo\n{opcion.descripcion} Este es un informe de **modo demo**; "
        "con Ollama (o una API key) configurado, los agentes de CrewAI generarán el contenido real.\n\n"
        "## Desarrollo\n### 1. Contexto\nTexto de ejemplo.\n\n### 2. Análisis\nTexto de ejemplo.\n\n"
        "## Conclusiones\n- Conclusión 1\n- Conclusión 2\n\n"
        "## Recomendaciones\n1. Recomendación accionable.\n2. Siguiente paso.\n\n"
        f"## Nota de calidad (QA)\n- Comentario del usuario tenido en cuenta: {comentario or '(ninguno)'}\n"
        "- Limitación: contenido simulado."
    )


def run_pdf(informe: str, meta: dict, emit) -> bytes:
    """Simula al Maquetador, pero la herramienta es la real: el PDF sí se genera."""
    emit("maquetador", "start", "Recibo el informe de QA. Preparo la maquetación en PDF.")
    for paso in ["Leo el informe y su estructura de apartados.", "Elijo un título sobrio para la portada."]:
        _pausa(0.8, 1.6)
        emit("maquetador", "thought", paso)
    eleccion = meta.get("eleccion") or {}
    herramienta = HerramientaPDF(informe=informe, meta=meta, emit=emit)
    herramienta.run(titulo=f"Informe: {meta.get('tema', '')}", subtitulo=eleccion.get("enfoque", ""))
    emit("maquetador", "done", "PDF maquetado y listo para descargar.")
    return herramienta.pdf
