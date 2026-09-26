import hashlib
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class JobOffer(BaseModel):
    """Modelo canónico de una oferta de empleo."""
    id: Optional[str] = None
    portal: str = Field(..., description="Nombre del portal origen (Computrabajo, Magneto, LinkedIn, etc.)")
    external_id: Optional[str] = Field(None, description="Identificador único en el portal original")
    title: str = Field(..., description="Título del puesto")
    company: str = Field(..., description="Nombre de la empresa")
    location: Optional[str] = Field(None, description="Ciudad, país o remoto")
    modality: Optional[str] = Field(None, description="Remoto, Híbrido, Presencial")
    salary: Optional[str] = Field("A convenir", description="Salario ofertado o rango")
    description: str = Field(..., description="Descripción completa de la vacante")
    requirements: List[str] = Field(default_factory=list, description="Habilidades y requisitos extraídos")
    url: str = Field(..., description="Enlace directo a la vacante")
    posted_date: Optional[str] = Field(None, description="Fecha de publicación informada")
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    
    # Matching y estado de postulación
    match_score: Optional[float] = Field(None, description="Score de compatibilidad con el CV (0.0 a 100.0)")
    match_reason: Optional[str] = Field(None, description="Explicación del score y fortalezas/brechas")
    status: str = Field("new", description="Estado: new | matched | discarded | applied")

    # Seguimiento de la postulación (solo aplica cuando status == 'applied')
    applied_at: Optional[str] = Field(None, description="Fecha/hora en que se marcó como postulada (ISO)")
    stage: Optional[str] = Field(None, description="Etapa: postulado | en_proceso | entrevista | prueba_tecnica | oferta | rechazado | cerrada")
    stage_updated_at: Optional[str] = Field(None, description="Última actualización de la etapa (ISO)")
    notes: Optional[str] = Field(None, description="Notas libres de la postulación")

    def generate_hash(self) -> str:
        """Genera un hash único basado en portal, empresa y título para evitar duplicados absolutos."""
        key = f"{self.portal.lower().strip()}:{self.company.lower().strip()}:{self.title.lower().strip()}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


    def model_post_init(self, __context) -> None:
        if not self.id:
            self.id = self.generate_hash()
