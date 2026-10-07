"""
SISTEMA MULTIAGENTE CON CREWAI
==============================

Cuatro agentes trabajan como un equipo de oficina:

    Usuario ──►  INVESTIGADOR ──► REVISIÓN ──►  USUARIO elige ──►  QA ──►  Informe ──► MAQUETADOR ──► PDF
                └──────────── CREW 1 ────────────┘     (pausa)         └─ CREW 2 ─┘     └── CREW 3 ──┘
                                                                              (al pulsar «Descargar PDF»)

¿Por qué dos crews? Porque en medio necesitamos que una PERSONA decida.
El Crew 1 termina proponiendo opciones, la web se las enseña al usuario,
y cuando el usuario elige arrancamos el Crew 2 con esa decisión.

Conceptos de CrewAI que aparecen aquí:
  - Agent : quién trabaja  (rol + objetivo + historia + modelo LLM)
  - Task  : qué hay que hacer (descripción + resultado esperado + agente)
  - Crew  : el equipo que ejecuta las tareas en orden (Process.sequential)
  - Tool  : una función que el agente DECIDE usar (el Maquetador usa «Generar PDF del informe»)
"""
from __future__ import annotations

import os
import time
import warnings

from crewai import LLM, Agent, Crew, Process, Task
from ddgs import DDGS

from .herramientas import HerramientaPDF, desenvolver_markdown
from .models import Interpretacion, Revision

# Nuestros callbacks son funciones internas (closures) para poder pasarles `emit`.
# CrewAI avisa de que así no puede guardar checkpoints, pero no los usamos: silenciamos el aviso.
warnings.filterwarnings("ignore", message="function callbacks cannot be serialized")


# ─────────────────────────────────────────────────────────────
# PASO 1 Configuración: qué modelo usan los agentes
# ─────────────────────────────────────────────────────────────
def modelo(max_tokens: int = 900) -> LLM:
    # Por defecto usa Ollama en local (gratis, sin API key). Se cambia en .env con MODEL=...
    # (p. ej. ollama/llama3, anthropic/claude-sonnet-5-5 u openai/gpt-4o-mini)
    # max_tokens limita lo largo que escribe cada agente: en local, cada token cuesta tiempo.
    model = os.getenv("MODEL", "ollama/qwen2.5:7b")
    if model.startswith("ollama"):
        return LLM(
            model=model,
            base_url=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            temperature=0.4,
            max_tokens=max_tokens,
        )
    return LLM(model=model, temperature=0.4, max_tokens=max_tokens)


def buscar_en_internet(consulta: str) -> str:
    """Busca en DuckDuckGo y devuelve los primeros resultados (título, enlace y resumen)."""
    # DuckDuckGo es gratis y no necesita API key.
    # La llamamos desde Python (no como herramienta del agente): así es UNA búsqueda de
    # pocos segundos en vez de un bucle pensar → buscar → pensar con muchas llamadas al LLM.
    try:
        resultados = DDGS().text(consulta, max_results=6)
    except Exception as e:
        return f"No se pudo buscar: {e}"
    if not resultados:
        return "Sin resultados."
    return "\n\n".join(f"{r['title']}\n{r['href']}\n{r['body']}" for r in resultados)


def avisar_a_la_web(nombre_agente: str, emit):
    """Cada vez que un agente 'piensa', mandamos ese pensamiento a la oficina virtual."""

    def callback(paso) -> None:
        texto = getattr(paso, "thought", "") or getattr(paso, "output", "") or str(paso)
        emit(nombre_agente, "thought", str(texto)[:400])

    return callback


def contar_resultado(nombre_agente: str, texto: str, emit, max_lineas: int = 5) -> None:
    """El revisor y el QA no tienen herramientas: CrewAI hace UNA llamada al LLM y no llama
    a su step_callback. Para que se vea lo que han hecho, enseñamos las primeras líneas del resultado."""
    lineas = [l.replace("**", "").strip(" #*-") for l in texto.splitlines()]
    lineas = [l for l in lineas if l]
    for linea in lineas[:max_lineas]:
        emit(nombre_agente, "thought", linea[:400])
        time.sleep(0.8)  # pausa para que la web (que pregunta cada 0,8 s) muestre cada burbuja


# ─────────────────────────────────────────────────────────────
# PASO 2 · Los tres agentes
# ─────────────────────────────────────────────────────────────
def crear_investigador(emit) -> Agent:
    return Agent(
        role="Agente Investigador",
        goal="Investigar a fondo la tarea del usuario y reunir hechos, datos y fuentes",
        backstory="Analista meticuloso. Separas hechos de opiniones y dices claramente lo que no sabes.",
        llm=modelo(),
        step_callback=avisar_a_la_web("investigador", emit),
    )


def crear_revisor(emit) -> Agent:
    return Agent(
        role="Agente de Revisión",
        goal="Proponer varias formas distintas de interpretar la investigación para que el usuario elija",
        backstory="Editor senior. Sabes que la misma información admite lecturas distintas (técnica, estratégica, crítica...).",
        llm=modelo(max_tokens=2000),  # necesita espacio para el JSON con las opciones (si se corta, CrewAI reintenta)
        step_callback=avisar_a_la_web("revisor", emit),
    )


def crear_qa(emit) -> Agent:
    return Agent(
        role="Agente QA",
        goal="Validar la información según la opción elegida por el usuario y redactar el informe final",
        backstory="Responsable de calidad. Detectas huecos y afirmaciones sin respaldo y escribes informes claros en Markdown.",
        llm=modelo(max_tokens=1200),  # el informe final es lo más largo
        step_callback=avisar_a_la_web("qa", emit),
    )


# ─────────────────────────────────────────────────────────────
# PASO 2 · CREW 1 — Investigar y proponer interpretaciones
# ─────────────────────────────────────────────────────────────
def run_analisis(tema: str, emit) -> tuple[str, Revision]:
    investigador = crear_investigador(emit)
    revisor = crear_revisor(emit)

    tarea_investigar = Task(
        description=(
            "Investiga esta tarea del usuario: «{tema}».\n\n"
            "Resultados de búsqueda en internet:\n{resultados}\n\n"
            "Usa estos resultados (y lo que sepas) para reunir contexto, datos, debates y fuentes. "
            "Sé conciso."
        ),
        expected_output="Dossier breve en Markdown con: Contexto, Hallazgos clave, Datos, Controversias, Fuentes.",
        agent=investigador,
    )

    tarea_revisar = Task(
        description=(
            "Lee el dossier sobre «{tema}» y propone entre 3 y 4 interpretaciones MUY distintas "
            "de cómo enfocar el informe final (título, enfoque, descripción, pros y contras). "
            "Termina con una pregunta al usuario pidiéndole que elija una."
        ),
        expected_output="Resumen de la investigación, lista de opciones y pregunta al usuario.",
        agent=revisor,
        context=[tarea_investigar],  # ← recibe el resultado del investigador
        output_pydantic=Revision,  # ← respuesta estructurada (JSON) para pintar las opciones en la web
    )

    def al_terminar_tarea(salida) -> None:
        if salida.agent == investigador.role:
            contar_resultado("investigador", salida.raw, emit, max_lineas=3)
            emit("investigador", "done", "Dossier terminado. Se lo paso a Revisión.")
            # El revisor no tiene herramientas y CrewAI no llama a su step_callback,
            # así que avisamos aquí de que empieza (esto cambia la fase a «revisando»)
            emit("revisor", "start", "Recibo el dossier. Empiezo la revisión.")
        else:
            revision = salida.pydantic
            if revision:
                emit("revisor", "thought", revision.resumen_investigacion[:400])
                for op in revision.opciones:
                    time.sleep(0.8)
                    emit("revisor", "thought", f"Opción «{op.titulo}»: {op.enfoque}"[:400])
            else:
                contar_resultado("revisor", salida.raw, emit)
            emit("revisor", "done", "Opciones listas para el usuario.")

    equipo = Crew(
        agents=[investigador, revisor],
        tasks=[tarea_investigar, tarea_revisar],
        process=Process.sequential,  # primero una tarea, luego la otra
        task_callback=al_terminar_tarea,
    )

    emit("investigador", "start", "Empiezo a investigar la tarea.")
    emit("investigador", "thought", f"Buscando en internet: «{tema}»")
    resultados = buscar_en_internet(tema)
    emit("investigador", "thought", "Resultados encontrados. Redacto el dossier...")

    # {tema} y {resultados} se sustituyen en las descripciones de las tareas
    resultado = equipo.kickoff(inputs={"tema": tema, "resultados": resultados})

    return tarea_investigar.output.raw, resultado.pydantic





# ─────────────────────────────────────────────────────────────
# PASO 3 · CREW 2 — QA valida y genera el informe
# ─────────────────────────────────────────────────────────────
def run_informe(tema: str, investigacion: str, opcion: Interpretacion, comentario: str, emit) -> str:
    qa = crear_qa(emit)

    tarea_validar = Task(
        description=(
            "Tema: «{tema}». El usuario eligió la interpretación «{titulo}»: {descripcion}\n"
            "Comentario del usuario: {comentario}\n\n"
            "Investigación disponible:\n{investigacion}\n\n"
            "Comprueba qué es sólido, qué falta y qué afirmaciones son dudosas para ese enfoque."
        ),
        expected_output="Checklist de QA: puntos validados, huecos, riesgos y recomendaciones.",
        agent=qa,
    )

    tarea_informe = Task(
        description="Redacta el informe final sobre «{tema}» siguiendo la interpretación «{titulo}» y el checklist de QA.",
        expected_output="Informe en Markdown: título, resumen ejecutivo, desarrollo, conclusiones, recomendaciones y limitaciones.",
        agent=qa,
        context=[tarea_validar],
    )

    def al_terminar_tarea(salida) -> None:
        if salida.expected_output == tarea_validar.expected_output:
            contar_resultado("qa", salida.raw, emit)
            emit("qa", "done", "Validación completada. Paso a redactar el informe.")
        else:
            emit("qa", "done", "Informe redactado.")

    equipo = Crew(
        agents=[qa],
        tasks=[tarea_validar, tarea_informe],
        process=Process.sequential,
        task_callback=al_terminar_tarea,
    )

    emit("qa", "start", f"Recibo tu elección: «{opcion.titulo}». Empiezo la validación.")
    resultado = equipo.kickoff(
        inputs={
            "tema": tema,
            "titulo": opcion.titulo,
            "descripcion": opcion.descripcion,
            "comentario": comentario or "(sin comentarios)",
            "investigacion": investigacion,
        }
    )
    return desenvolver_markdown(resultado.raw)  # quita el ```markdown … ``` que añaden algunos modelos


# ─────────────────────────────────────────────────────────────
# PASO 4 · CREW 3 — El Maquetador convierte el informe en un PDF profesional
# ─────────────────────────────────────────────────────────────
# Este agente es distinto a los demás: tiene una HERRAMIENTA (tools=[...]).
# CrewAI le enseña al LLM el nombre, la descripción y los argumentos de la herramienta,
# y es el propio agente quien decide llamarla y con qué título y subtítulo.
def crear_maquetador(emit, herramienta: HerramientaPDF) -> Agent:
    return Agent(
        role="Agente Maquetador",
        goal="Convertir el informe final en un documento PDF con aspecto profesional",
        backstory="Diseñador editorial. Eliges títulos claros y sobrios y entregas documentos listos para enviar a un cliente.",
        llm=modelo(max_tokens=400),
        tools=[herramienta],  # ← la herramienta que puede usar
        step_callback=avisar_a_la_web("maquetador", emit),
    )


def run_pdf(informe: str, meta: dict, emit) -> bytes:
    herramienta = HerramientaPDF(informe=informe, meta=meta, emit=emit, result_as_answer=True)
    # result_as_answer=True → lo que devuelve la herramienta es la respuesta final (ahorra una llamada al LLM)
    maquetador = crear_maquetador(emit, herramienta)

    tarea_maquetar = Task(
        description=(
            "Tema del encargo: «{tema}». Enfoque elegido por el usuario: «{enfoque}».\n\n"
            "Informe final redactado por QA:\n{informe}\n\n"
            "Decide un título profesional para la portada (máx. 12 palabras) y un subtítulo de una línea "
            "que resuma el enfoque. Después USA la herramienta «Generar PDF del informe» con ese título y subtítulo."
        ),
        expected_output="Confirmación de que el PDF se ha generado.",
        agent=maquetador,
    )

    equipo = Crew(agents=[maquetador], tasks=[tarea_maquetar], process=Process.sequential)

    emit("maquetador", "start", "Recibo el informe de QA. Preparo la maquetación en PDF.")
    eleccion = meta.get("eleccion") or {}
    equipo.kickoff(inputs={"tema": meta.get("tema", ""), "enfoque": eleccion.get("titulo", ""), "informe": informe})

    if herramienta.pdf is None:
        # Los modelos pequeños a veces responden en texto en vez de usar la herramienta.
        # Para no dejar al usuario sin su PDF, la llamamos nosotros con el título del informe.
        emit("maquetador", "thought", "No he llegado a usar la herramienta; genero el PDF con el título del informe.")
        herramienta.run(titulo="", subtitulo="")
    emit("maquetador", "done", "PDF maquetado y listo para descargar.")
    return herramienta.pdf
