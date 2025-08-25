from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
from typing import Optional, Dict, Any
from .base import BaseCallProvider
from config import config

class TwilioCallProvider(BaseCallProvider):
    """Proveedor de llamadas usando Twilio"""
    
    def __init__(self):
        self.client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
        self.from_number = config.TWILIO_NUMBER

    def make_call(self, to_number: str, audio_url: str, callback_url: Optional[str] = None) -> Optional[str]:
        if not self.validate_phone_number(to_number):
            print(f"Número inválido: {to_number}")
            return None
        try:
            # Si la URL parece ser nuestro endpoint de TwiML, usar 'url='
            if "twilio-appointment-twiml" in audio_url:
                call = self.client.calls.create(
                    url=audio_url,
                    to=to_number,
                    from_=self.from_number,
                    status_callback=callback_url,
                    status_callback_method='POST' if callback_url else None
                )
            else:
                twiml = self._create_twiml(audio_url)
                call = self.client.calls.create(
                    twiml=twiml,
                    to=to_number,
                    from_=self.from_number,
                    status_callback=callback_url,
                    status_callback_method='POST' if callback_url else None
                )
            return call.sid
        except Exception as e:
            print(f"Error Twilio: {e}")
            return None

    
    def _create_twiml(self, audio_url: str) -> str:
        """Crea TwiML para reproducir audio"""
        response = VoiceResponse()
        response.play(audio_url)
        return str(response)
    
    def get_call_status(self, call_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene estado de llamada Twilio"""
        try:
            call = self.client.calls(call_id).fetch()
            return {
                "call_id": call.sid,
                "status": call.status,
                "duration": call.duration,
                "start_time": call.start_time,
                "end_time": call.end_time
            }
        except Exception as e:
            print(f"Error obteniendo estado Twilio: {e}")
            return None
    
    def get_provider_name(self) -> str:
        return "twilio"