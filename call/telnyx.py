import os
import httpx
import logging
from datetime import datetime
from typing import Optional, Dict, Any

from .base import (
    BaseCallProvider, CallResponse, CallEvent, TranscriptionEvent,
    CallStatus, EventType
)

logger = logging.getLogger(__name__)

TELNYX_API = "https://api.telnyx.com/v2"


class TelnyxCallProvider(BaseCallProvider):
    """Telnyx Call Control provider con STT/TTS en tiempo real"""
    
    def __init__(self):
        self.api_key = os.getenv("TELNYX_API_KEY")
        self.connection_id = os.getenv("TELNYX_CONNECTION_ID")
        self.from_number = os.getenv("TELNYX_NUMBER")
        
        if not all([self.api_key, self.connection_id]):
            raise RuntimeError("Faltan TELNYX_API_KEY o TELNYX_CONNECTION_ID")
        
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            },
            timeout=15
        )
    
    def get_provider_name(self) -> str:
        return "telnyx"
    
    def make_call(self, to_number: str, webhook_url: str, 
                  from_number: Optional[str] = None) -> CallResponse:
        """Inicia llamada Telnyx con webhook"""
        if not self.validate_phone_number(to_number):
            return CallResponse(
                success=False,
                call_id="",
                provider="telnyx",
                status=CallStatus.FAILED,
                error_message=f"Número inválido: {to_number}"
            )
        
        try:
            body = {
                "connection_id": self.connection_id,
                "to": to_number,
                "from": from_number or self.from_number,
                "webhook_url": webhook_url,
                "webhook_url_method": "POST"
            }
            
            response = self._client.post(f"{TELNYX_API}/calls", json=body)
            
            if response.is_success:
                data = response.json()["data"]
                return CallResponse(
                    success=True,
                    call_id=data["id"],
                    provider="telnyx",
                    status=CallStatus.INITIATED,
                    metadata={"call_control_id": data["id"]}
                )
            else:
                return CallResponse(
                    success=False,
                    call_id="",
                    provider="telnyx",
                    status=CallStatus.FAILED,
                    error_message=f"HTTP {response.status_code}: {response.text}"
                )
                
        except Exception as e:
            return CallResponse(
                success=False,
                call_id="",
                provider="telnyx",
                status=CallStatus.FAILED,
                error_message=str(e)
            )
    
    def hangup_call(self, call_id: str) -> bool:
        """Termina llamada"""
        return self._call_action(call_id, "hangup", {})
    
    def get_call_status(self, call_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene estado de llamada"""
        try:
            response = self._client.get(f"{TELNYX_API}/calls/{call_id}")
            return response.json()["data"] if response.is_success else None
        except Exception:
            return None
    
    def start_transcription(self, call_id: str, language: str = "es-MX", 
                          interim_results: bool = True) -> bool:
        """Inicia transcripción Telnyx nativa"""
        payload = {
            "language": language,
            "transcription_engine": "telnyx",
            "interim_results": interim_results
        }
        return self._call_action(call_id, "transcription_start", payload)
    
    def stop_transcription(self, call_id: str) -> bool:
        """Detiene transcripción"""
        return self._call_action(call_id, "transcription_stop", {})
    
    def play_audio_stream(self, call_id: str, audio_stream: bytes) -> bool:
        """Reproduce stream de Azure en llamada Telnyx usando audio_url temporal"""
        # Implementación temporal - en producción usar streaming directo
        logger.info(f"Telnyx: Playing Azure audio stream for call {call_id}")
        return True
    
    def stop_speech(self, call_id: str) -> bool:
        """Detiene síntesis actual"""
        return self._call_action(call_id, "speak_stop", {})
    
    def process_webhook_event(self, payload: Dict[str, Any]) -> CallEvent:
        """Procesa eventos de Telnyx webhook"""
        event_type = payload.get("event_type", "")
        data = payload.get("data", {})
        call_payload = data.get("payload", {})
        
        call_id = data.get("call_control_id") or call_payload.get("call_control_id")
        
        # Mapear eventos Telnyx a eventos unificados
        event_mapping = {
            "call.initiated": EventType.CALL_INITIATED,
            "call.ringing": EventType.CALL_RINGING,
            "call.answered": EventType.CALL_ANSWERED,
            "call.hangup": EventType.CALL_ENDED,
            "call.speak.started": EventType.SPEECH_STARTED,
            "call.speak.ended": EventType.SPEECH_ENDED,
            "transcription.updated": EventType.TRANSCRIPTION_RECEIVED
        }
        
        unified_event = event_mapping.get(event_type, EventType.ERROR)
        
        # Procesar transcripción si aplica
        transcription = None
        if event_type == "transcription.updated":
            transcript_text = call_payload.get("transcript", "")
            is_final = call_payload.get("is_final", False)
            
            if transcript_text:
                transcription = TranscriptionEvent(
                    text=transcript_text,
                    is_final=is_final,
                    confidence=call_payload.get("confidence")
                )
                
                unified_event = EventType.TRANSCRIPTION_FINAL if is_final else EventType.TRANSCRIPTION_RECEIVED
        
        return CallEvent(
            event_type=unified_event,
            call_id=call_id or "",
            provider="telnyx",
            timestamp=datetime.now().isoformat(),
            data=payload,
            transcription=transcription
        )
    
    def extract_call_id_from_webhook(self, payload: Dict[str, Any]) -> Optional[str]:
        """Extrae call_control_id de webhook Telnyx"""
        data = payload.get("data", {})
        return (data.get("call_control_id") or 
                data.get("payload", {}).get("call_control_id"))
    
    def _call_action(self, call_id: str, action: str, payload: Dict[str, Any]) -> bool:
        """Ejecuta acción en llamada activa"""
        try:
            response = self._client.post(
                f"{TELNYX_API}/calls/{call_id}/actions/{action}",
                json=payload
            )
            success = response.is_success
            if not success:
                logger.error(f"Telnyx action {action} failed: {response.status_code} {response.text}")
            return success
        except Exception as e:
            logger.error(f"Telnyx action {action} error: {e}")
            return False