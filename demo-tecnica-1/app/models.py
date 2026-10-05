"""Salidas estructuradas compartidas por el crew real y el modo demo."""
from pydantic import BaseModel, Field


class Interpretacion(BaseModel):
    titulo: str = Field(description="Nombre corto de la interpretación")
    enfoque: str = Field(description="Ángulo o perspectiva en una frase")
    descripcion: str = Field(description="Qué cubriría el informe siguiendo esta vía")
    pros: list[str] = []
    contras: list[str] = []


class Revision(BaseModel):
    resumen_investigacion: str
    opciones: list[Interpretacion]
    pregunta_al_usuario: str
