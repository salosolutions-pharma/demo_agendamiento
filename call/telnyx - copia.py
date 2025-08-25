# telnyx.py  (provider para Call Control)
import os, httpx, logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

TELNYX_API = "https://api.telnyx.com/v2"

class TelnyxCallControlProvider:
    def __init__(self):
        self.api_key = os.getenv("TELNYX_API_KEY")
        self.connection_id = os.getenv("TELNYX_CONNECTION_ID")
        if not self.api_key or not self.connection_id:
            raise RuntimeError("Faltan TELNYX_API_KEY o TELNYX_CONNECTION_ID")

        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            timeout=15
        )

    def validate_phone_number(self, num: str) -> bool:
        return isinstance(num, str) and len(num) >= 8

    # 1) Crear llamada
    def create_call(self, to_number: str, from_number: Optional[str] = None) -> Optional[str]:
        body = {
            "connection_id": self.connection_id,
            "to": to_number,
        }
        if from_number:
            body["from"] = from_number

        r = self._client.post(f"{TELNYX_API}/calls", json=body)
        if r.is_success:
            call_id = r.json()["data"]["id"]
            logger.info(f"Telnyx CC call_id: {call_id}")
            return call_id
        logger.error(f"Error creando llamada Telnyx: {r.status_code} {r.text}")
        return None

    # 2) Acciones
    def action(self, call_control_id: str, action: str, payload: Dict[str, Any]) -> bool:
        r = self._client.post(f"{TELNYX_API}/calls/{call_control_id}/actions/{action}", json=payload)
        ok = r.is_success
        if not ok:
            logger.error(f"Acción {action} fallo: {r.status_code} {r.text}")
        return ok

    def playback(self, call_control_id: str, audio_url: str) -> bool:
        return self.action(call_control_id, "audio_playback_start", {"audio_url": audio_url})

    def playback_stop(self, call_control_id: str) -> bool:
        return self.action(call_control_id, "audio_playback_stop", {})

    def hangup(self, call_control_id: str) -> bool:
        return self.action(call_control_id, "hangup", {})

    # 3) Transcripción (elige engine "telnyx" o "google")
    def transcription_start(self, call_control_id: str, language="es-MX", engine="telnyx") -> bool:
        payload = {
            "language": language,
            "transcription_engine": engine,  # "telnyx" o "google"
            "interim_results": True          # recibe parciales y finales
        }
        return self.action(call_control_id, "transcription_start", payload)

    def transcription_stop(self, call_control_id: str) -> bool:
        return self.action(call_control_id, "transcription_stop", {})
