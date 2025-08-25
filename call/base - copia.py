from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

class BaseCallProvider(ABC):
    """Interfaz común para todos los proveedores de llamadas"""
    
    @abstractmethod
    def make_call(self, to_number: str, audio_url: str, 
                  callback_url: Optional[str] = None) -> Optional[str]:
        """
        Hace una llamada telefónica.
        
        Args:
            to_number: Número destino
            audio_url: URL del audio a reproducir
            callback_url: URL para callbacks de estado
            
        Returns:
            str: ID de la llamada, None si hay error
        """
        pass
    
    @abstractmethod
    def get_provider_name(self) -> str:
        """Retorna el nombre del proveedor"""
        pass
    
    @abstractmethod
    def get_call_status(self, call_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene el estado de una llamada"""
        pass
    
    def validate_phone_number(self, phone: str) -> bool:
        """Valida formato de número telefónico"""
        import re
        pattern = r'^\+[1-9]\d{1,14}$'
        return bool(re.match(pattern, phone))