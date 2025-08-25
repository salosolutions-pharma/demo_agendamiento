from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from datetime import datetime

class BaseSchedulerProvider(ABC):
    """Interfaz común para todos los proveedores de agendamiento"""
    
    @abstractmethod
    def get_available_appointments(self, days_ahead: int = 5) -> List[Dict[str, Any]]:
        """
        Obtiene citas disponibles para los próximos días.
        
        Args:
            days_ahead: Número de días hacia adelante para buscar disponibilidad
            
        Returns:
            List[Dict]: Lista de diccionarios con información de citas disponibles
                Cada dict debe contener:
                - fecha_hora: datetime object
                - texto: str (descripción legible)
                - doctor: str
                - fecha_mostrar: str
                - iso_inicio: str
                - iso_fin: str
        """
        pass
    
    @abstractmethod
    def create_appointment(self, nombre: str, fecha_inicio: str, fecha_fin: str, 
                          doctor: str, telefono: str) -> Optional[str]:
        """
        Crea una nueva cita.
        
        Args:
            nombre: Nombre del paciente
            fecha_inicio: Fecha/hora de inicio en formato ISO
            fecha_fin: Fecha/hora de fin en formato ISO
            doctor: Nombre del doctor
            telefono: Teléfono del paciente
            
        Returns:
            str: ID del evento creado, None si hay error
        """
        pass
    
    @abstractmethod
    def get_provider_name(self) -> str:
        """Retorna el nombre del proveedor de agendamiento"""
        pass
    
    def validate_appointment_data(self, nombre: str, telefono: str) -> bool:
        """Valida datos básicos de la cita"""
        if not nombre or len(nombre.strip()) < 2:
            return False
        if not telefono or len(telefono.strip()) < 10:
            return False
        return True