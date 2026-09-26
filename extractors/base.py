from abc import ABC, abstractmethod
from typing import List
from core.models import JobOffer


class BaseExtractor(ABC):
    """Interfaz abstracta para los extractores de portales de empleo."""

    portal_name: str = "base"

    @abstractmethod
    async def search_jobs(self, query: str, location: str = "Bogotá, D.C.", max_pages: int = 1) -> List[JobOffer]:
        """Extrae ofertas según término de búsqueda y ubicación."""
        pass
