"""
SERVIDOR WEB (FastAPI) — el "pegamento" entre la oficina (HTML) y los agentes (crew.py)

  POST /api/jobs                 → el usuario envía la tarea   → arranca CREW 1 en segundo plano
  GET  /api/jobs/{id}            → la web pregunta cada 0,8 s  → fase actual + mensajes nuevos
  POST /api/jobs/{id}/decision   → el usuario elige una opción → arranca CREW 2
  POST /api/jobs/{id}/pdf        → el usuario pide el PDF       → arranca CREW 3 (Maquetador)
  GET  /api/jobs/{id}/informe.pdf → descarga el PDF que ha maquetado el agente

Fases que ve la oficina:
  investigando → revisando → esperando_usuario → qa → informe → terminado
"""
from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from . import demo  # noqa: E402
from .herramientas import nombre_archivo  # noqa: E402
from .models import Interpretacion  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "static"
KEY_BY_PROVIDER = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}


def demo_mode() -> bool:
    if os.getenv("DEMO_MODE", "").lower() in ("1", "true", "yes"):
        return True
    provider = os.getenv("MODEL", "ollama/qwen2.5:7b").split("/")[0]
    key = KEY_BY_PROVIDER.get(provider)
    return bool(key) and not os.getenv(key)


# ---------- Estado de los trabajos ----------
@dataclass
class Job:
    id: str
    tema: str
    fase: str = "investigando"  # investigando | revisando | esperando_usuario | qa | informe | terminado | error
    eventos: list[dict] = field(default_factory=list)
    investigacion: str = ""
    revision: dict | None = None
    eleccion: dict | None = None
    informe: str = ""
    terminado_en: datetime | None = None
    pdf_estado: str = ""  # "" | generando | listo | error
    pdf: bytes | None = None
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def emit(self, agente: str, tipo: str, texto: str) -> None:
        with self.lock:
            # El primer paso del revisor marca el cambio de fase en la oficina
            if agente == "revisor" and self.fase == "investigando":
                self.fase = "revisando"
            if agente == "qa" and tipo == "done" and self.fase == "qa":
                self.fase = "informe"
            self.eventos.append({"t": time.time(), "agente": agente, "tipo": tipo, "texto": texto})


JOBS: dict[str, Job] = {}


def _fase1(job: Job) -> None:
    try:
        if demo_mode():
            investigacion, revision = demo.run_analisis(job.tema, job.emit)
        else:
            from .crew import run_analisis

            investigacion, revision = run_analisis(job.tema, job.emit)
        job.investigacion = investigacion
        job.revision = revision.model_dump()
        job.emit("revisor", "ask", revision.pregunta_al_usuario)
        job.fase = "esperando_usuario"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        job.error, job.fase = str(e), "error"
        job.emit("sistema", "error", str(e))


def _fase2(job: Job, idx: int, comentario: str) -> None:
    try:
        opcion = Interpretacion(**job.revision["opciones"][idx])
        if demo_mode():
            informe = demo.run_informe(job.tema, job.investigacion, opcion, comentario, job.emit)
        else:
            from .crew import run_informe

            informe = run_informe(job.tema, job.investigacion, opcion, comentario, job.emit)
        job.informe = informe
        job.terminado_en = datetime.now()
        job.fase = "terminado"
        job.emit("qa", "report", "Informe final entregado.")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        job.error, job.fase = str(e), "error"
        job.emit("sistema", "error", str(e))


def _fase3(job: Job) -> None:
    meta = {
        "tema": job.tema,
        "eleccion": job.eleccion,
        "modelo": "Modo demo (simulado)" if demo_mode() else os.getenv("MODEL", "ollama/qwen2.5:7b"),
        "fecha": job.terminado_en,
        "referencia": job.id,
    }
    try:
        if demo_mode():
            pdf = demo.run_pdf(job.informe, meta, job.emit)
        else:
            from .crew import run_pdf

            pdf = run_pdf(job.informe, meta, job.emit)
        job.pdf, job.pdf_estado = pdf, "listo"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        job.pdf_estado = "error"
        job.emit("sistema", "error", f"No se pudo generar el PDF: {e}")


# ---------- API ----------
app = FastAPI(title="Oficina multiagente CrewAI")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class NuevaTarea(BaseModel):
    tarea: str


class Decision(BaseModel):
    opcion: int
    comentario: str = ""


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config():
    return {"demo": demo_mode(), "model": os.getenv("MODEL", "ollama/qwen2.5:7b")}


@app.post("/api/jobs")
def crear(t: NuevaTarea):
    tema = t.tarea.strip()
    if not tema:
        raise HTTPException(400, "La tarea no puede estar vacía")
    job = Job(id=uuid.uuid4().hex[:10], tema=tema)
    JOBS[job.id] = job
    job.emit("usuario", "task", tema)
    threading.Thread(target=_fase1, args=(job,), daemon=True).start()
    return {"id": job.id}


@app.get("/api/jobs/{job_id}")
def estado(job_id: str, since: int = 0):
    job = JOBS.get(job_id) or _404()
    with job.lock:
        return {
            "fase": job.fase,
            "eventos": job.eventos[since:],
            "total": len(job.eventos),
            "revision": job.revision if job.fase != "investigando" else None,
            "eleccion": job.eleccion,
            "informe": job.informe,
            "pdf": job.pdf_estado,
            "error": job.error,
        }


@app.post("/api/jobs/{job_id}/decision")
def decidir(job_id: str, d: Decision):
    job = JOBS.get(job_id) or _404()
    if job.fase != "esperando_usuario":
        raise HTTPException(409, "El trabajo no está esperando una decisión")
    if not 0 <= d.opcion < len(job.revision["opciones"]):
        raise HTTPException(400, "Opción no válida")
    job.eleccion = {"indice": d.opcion, "comentario": d.comentario, **job.revision["opciones"][d.opcion]}
    job.fase = "qa"
    job.emit("usuario", "decision", f"Elijo «{job.eleccion['titulo']}». {d.comentario}".strip())
    threading.Thread(target=_fase2, args=(job, d.opcion, d.comentario), daemon=True).start()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/pdf")
def pedir_pdf(job_id: str):
    job = JOBS.get(job_id) or _404()
    if job.fase != "terminado" or not job.informe:
        raise HTTPException(409, "El informe todavía no está listo")
    with job.lock:
        if job.pdf_estado in ("generando", "listo"):
            return {"pdf": job.pdf_estado}
        job.pdf_estado = "generando"
    threading.Thread(target=_fase3, args=(job,), daemon=True).start()
    return {"pdf": "generando"}


@app.get("/api/jobs/{job_id}/informe.pdf")
def descargar_pdf(job_id: str):
    job = JOBS.get(job_id) or _404()
    if job.pdf_estado != "listo" or not job.pdf:
        raise HTTPException(409, "El PDF todavía no está listo")
    return Response(
        job.pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo(job.tema)}"'},
    )


def _404():
    raise HTTPException(404, "Trabajo no encontrado")
