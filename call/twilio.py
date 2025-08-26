import os
import logging
from typing import Optional, Dict, Any
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

from .base import BaseCallProvider, CallEvent, CallStatus, EventType

logger = logging.getLogger(__name__)


class TwilioCallProvider(BaseCallProvider):
    """Proveedor Twilio adaptado para voz Azure (Play URL efímera) y Gather por turnos."""

    def __init__(self):
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.from_number = os.getenv("TWILIO_NUMBER")
        self.base_url = os.getenv("BASE_URL")  # dominio HTTPS público

        if not self.account_sid or not self.auth_token:
            raise RuntimeError("⚠️ Falta TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")
        if not self.from_number:
            raise RuntimeError("⚠️ Falta TWILIO_NUMBER")
        if not self.base_url:
            logger.warning("⚠️ BASE_URL no configurado (Twilio no podrá llamar tus endpoints)")

        self.client = Client(self.account_sid, self.auth_token)

    # ---------------------------
    # Requeridos por BaseCallProvider
    # ---------------------------
    def get_provider_name(self) -> str:
        return "twilio"

    def make_call(self, to_number: str, call_id: str, twiml_url: str = None) -> str:
        try:
            twiml_url = twiml_url or f"{self.base_url}/twilio/twiml"
            webhook_url = f"{self.base_url}/webhook/twilio"

            call = self.client.calls.create(
                to=to_number,
                from_=self.from_number,
                url=twiml_url,
                status_callback=webhook_url,
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )
            logger.info(f"📞 Twilio call SID: {call.sid}")
            return call.sid
        except Exception as e:
            logger.error(f"Error iniciando llamada Twilio: {e}")
            raise


    def hangup_call(self, call_id: str) -> bool:
        """Cuelga la llamada."""
        try:
            self.client.calls(call_id).update(status="completed")
            return True
        except Exception as e:
            logger.error(f"Error al colgar llamada Twilio: {e}")
            return False

    def get_call_status(self, call_id: str) -> CallStatus:
        """Consulta estado unificado."""
        try:
            call = self.client.calls(call_id).fetch()
            status = call.status or ""
            if status in {"queued", "initiated", "ringing", "in-progress"}:
                return CallStatus.IN_PROGRESS
            if status in {"completed", "canceled", "failed", "busy", "no-answer"}:
                return CallStatus.COMPLETED
            return CallStatus.UNKNOWN
        except Exception as e:
            logger.error(f"Error al obtener estado Twilio: {e}")
            return CallStatus.UNKNOWN

    def process_webhook_event(self, payload: Dict[str, Any]) -> Optional[CallEvent]:
        """Normaliza el webhook de Twilio a CallEvent."""
        try:
            call_sid = payload.get("CallSid")
            if not call_sid:
                return None

            call_status = (payload.get("CallStatus") or "").lower()
            from_number = payload.get("From")
            to_number = payload.get("To")

            if call_status == "answered":
                etype = EventType.CALL_ANSWERED
            elif call_status == "completed":
                etype = EventType.CALL_COMPLETED
            else:
                etype = EventType.OTHER

            return CallEvent(
                provider="twilio",
                call_id=call_sid,
                event_type=etype,
                from_number=from_number,
                to_number=to_number,
                raw=payload,
            )
        except Exception as e:
            logger.error(f"process_webhook_event error: {e}")
            return None

    # ---------------------------
    # Reproducción de audio (Azure TTS servido por URL efímera)
    # ---------------------------
    def play_audio_stream(self, call_id: str, audio_stream: bytes) -> bool:
        """
        Reproduce audio en la llamada.
        Aquí audio_stream trae una URL (bytes) generada por app.py:
        /audio/{call_id}/{seq}?token=...
        """
        try:
            play_url = audio_stream.decode("utf-8")
            vr = VoiceResponse()
            vr.play(play_url)
            vr.pause(length=1)  # breve pausa evita cortar el final

            self.client.calls(call_id).update(twiml=str(vr))
            logger.info(f"▶️ Twilio <Play>: {play_url}")
            return True
        except Exception as e:
            logger.error(f"Twilio play_audio_stream error: {e}")
            return False

    # ---------------------------
    # Métodos abstractos adicionales (no-ops seguros en modo Gather)
    # ---------------------------
    def extract_call_id_from_webhook(self, payload: Dict[str, Any]) -> Optional[str]:
        """Obtiene el CallSid desde el webhook de Twilio."""
        return payload.get("CallSid")

    def start_transcription(self, call_id: str) -> bool:
        """
        NO-OP en este modo: usamos <Gather input="speech"> (STT por turnos).
        Si más adelante activas Media Streams (WS), aquí enviarías TwiML con <Connect><Stream>.
        """
        logger.debug(f"start_transcription (noop) call_id={call_id}")
        return True

    def stop_transcription(self, call_id: str) -> bool:
        """NO-OP en modo Gather."""
        logger.debug(f"stop_transcription (noop) call_id={call_id}")
        return True

    def stop_speech(self, call_id: str) -> bool:
        """
        Intenta detener cualquier reproducción activa.
        No existe una API 'stop' directa; actualizamos TwiML con una pausa.
        """
        try:
            vr = VoiceResponse()
            vr.pause(length=1)
            self.client.calls(call_id).update(twiml=str(vr))
            logger.debug(f"stop_speech aplicado a {call_id}")
            return True
        except Exception as e:
            logger.error(f"stop_speech error: {e}")
            return False
