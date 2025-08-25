from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum

class CallStatus(Enum):
    """Estados unificados de llamada"""
    INITIATED = "initiated"
    RINGING = "ringing" 
    ANSWERED = "answered"
    COMPLETED = "completed"
    FAILED = "failed"
    BUSY = "busy"
    NO_ANSWER = "no_answer"

class EventType(Enum):
    """Tipos de eventos unificados"""
    CALL_INITIATED = "call_initiated"
    CALL_RINGING = "call_ringing"
    CALL_ANSWERED = "call_answered" 
    CALL_ENDED = "call_ended"
    SPEECH_STARTED = "speech_started"
    SPEECH_ENDED = "speech_ended"
    TRANSCRIPTION_RECEIVED = "transcription_received"
    TRANSCRIPTION_FINAL = "transcription_final"
    ERROR = "error"

@dataclass
class CallResponse:
    """Respuesta unificada para operaciones de llamada"""
    success: bool
    call_id: str
    provider: str
    status: CallStatus
    error_message: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

@dataclass
class TranscriptionEvent:
    """Evento de transcripción unificado"""
    text: str
    is_final: bool
    confidence: Optional[float] = None
    language: Optional[str] = None

@dataclass
class CallEvent:
    """Evento unificado de llamada"""
    event_type: EventType
    call_id: str
    provider: str
    timestamp: str
    data: Dict[str, Any]
    transcription: Optional[TranscriptionEvent] = None

class BaseCallProvider(ABC):
    """Interfaz base unificada para proveedores de llamadas con STT/TTS en tiempo real"""
    
    @abstractmethod
    def get_provider_name(self) -> str:
        """Retorna el nombre del proveedor: 'telnyx' | 'twilio'"""
        pass
    
    # === CALL MANAGEMENT ===
    @abstractmethod
    def make_call(self, to_number: str, webhook_url: str, 
                  from_number: Optional[str] = None) -> CallResponse:
        """
        Inicia una llamada con webhook para eventos en tiempo real
        
        Args:
            to_number: Número destino (+573137727034)
            webhook_url: URL para recibir eventos del proveedor
            from_number: Número origen (opcional, usa el configurado por defecto)
            
        Returns:
            CallResponse: Respuesta con call_id si exitoso
        """
        pass
    
    @abstractmethod
    def hangup_call(self, call_id: str) -> bool:
        """Termina una llamada activa"""
        pass
    
    @abstractmethod
    def get_call_status(self, call_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene el estado actual de una llamada"""
        pass
    
    # === REAL-TIME SPEECH-TO-TEXT ===
    @abstractmethod
    def start_transcription(self, call_id: str, language: str = "es-MX", 
                          interim_results: bool = True) -> bool:
        """
        Inicia transcripción en tiempo real (STT)
        
        Args:
            call_id: ID de la llamada
            language: Idioma (es-MX, es-CO, es-ES)
            interim_results: Recibir resultados parciales
        """
        pass
    
    @abstractmethod
    def stop_transcription(self, call_id: str) -> bool:
        """Detiene la transcripción"""
        pass
    
    # === AUDIO STREAMING (Azure TTS Integration) ===
    @abstractmethod
    def play_audio_stream(self, call_id: str, audio_stream: bytes) -> bool:
        """
        Reproduce stream de audio en tiempo real en la llamada
        El audio viene pre-generado por Azure Speech Service (Salomé)
        
        Args:
            call_id: ID de la llamada
            audio_stream: Stream de audio de Azure (MP3/WAV)
        """
        pass
    
    @abstractmethod
    def stop_speech(self, call_id: str) -> bool:
        """Detiene la síntesis actual"""
        pass
    
    # === WEBHOOK EVENT PROCESSING ===
    @abstractmethod
    def process_webhook_event(self, payload: Dict[str, Any]) -> CallEvent:
        """
        Procesa eventos crudos del webhook y los normaliza
        
        Args:
            payload: Payload JSON crudo del proveedor
            
        Returns:
            CallEvent: Evento normalizado con tipo unificado
        """
        pass
    
    @abstractmethod
    def extract_call_id_from_webhook(self, payload: Dict[str, Any]) -> Optional[str]:
        """Extrae el call_id del payload del webhook"""
        pass
    
    # === VALIDATION & UTILITIES ===
    def validate_phone_number(self, phone: str) -> bool:
        """Valida formato de número telefónico internacional"""
        import re
        # Formato E.164: +[1-9][0-9]{1,14}
        pattern = r'^\+[1-9]\d{1,14}$'
        return bool(re.match(pattern, phone))
    
    def get_webhook_url(self, base_url: str) -> str:
        """Genera URL del webhook específica para el proveedor"""
        provider = self.get_provider_name()
        return f"{base_url.rstrip('/')}/webhook/{provider}"
    
    def normalize_phone_number(self, phone: str) -> str:
        """Normaliza número telefónico a formato E.164"""
        # Remover espacios, guiones, paréntesis
        clean = ''.join(c for c in phone if c.isdigit() or c == '+')
        
        # Si no tiene +, agregar código de país por defecto (Colombia +57)
        if not clean.startswith('+'):
            if clean.startswith('57'):
                clean = '+' + clean
            elif clean.startswith('3'):  # Números móviles colombianos
                clean = '+57' + clean
            else:
                clean = '+57' + clean
                
        return clean
    
    # === PROVIDER-SPECIFIC HELPERS ===
    def get_default_voice_config(self) -> Dict[str, Any]:
        """Configuración de voz por defecto para el proveedor"""
        return {
            "voice": "es-CO-SalomeNeural",  # Voz colombiana
            "rate": 1.1,                    # Velocidad natural
            "pitch": "+2%"                  # Tono ligeramente más alto
        }
    
    def get_default_transcription_config(self) -> Dict[str, Any]:
        """Configuración de transcripción por defecto"""
        return {
            "language": "es-MX",           # Español mexicano (mejor soporte)
            "interim_results": True,       # Resultados parciales
            "profanity_filter": False,     # Sin filtro
            "punctuation": True            # Incluir puntuación
        }