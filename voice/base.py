from abc import ABC, abstractmethod
from typing import Optional

class BaseVoiceProvider(ABC):
    """Interfaz común para proveedores de voz"""
    
    @abstractmethod
    def generate_audio(self, texto: str, velocidad: float = 1.0, 
                      tono: int = 0, genero: str = "femenino") -> Optional[bytes]:
        """Genera audio desde texto"""
        pass
    
    @abstractmethod
    def get_provider_name(self) -> str:
        """Retorna el nombre del proveedor"""
        pass
    
    def validate_params(self, velocidad: float, tono: int) -> bool:
        """Valida parámetros comunes"""
        return 0.5 <= velocidad <= 2.0 and -50 <= tono <= 50