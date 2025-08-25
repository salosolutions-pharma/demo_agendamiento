import os
import logging
import hmac
import hashlib
import time
from typing import Optional
import azure.cognitiveservices.speech as speechsdk
from .base import BaseVoiceProvider

logger = logging.getLogger(__name__)


class AzureVoiceProvider:
    """
    Azure Speech Service para sintetizar audio en memoria en formato
    WAV 8 kHz μ-law (Riff8Khz8BitMonoMULaw), ideal para telefonía/Twilio.
    - Sin archivos temporales
    - SSML ajustable (velocidad/tono)
    - Utilidades para tokens efímeros (URLs seguras de TTS)
    """

    def __init__(self):
        self.subscription_key = os.getenv("AZURE_SUBSCRIPTION_KEY")
        self.region = os.getenv("AZURE_REGION", "eastus")
        self.voice_name = os.getenv("AZURE_VOICE_NAME", "es-CO-SalomeNeural")

        # Seguridad para endpoints efímeros de TTS
        self.tts_secret = os.getenv("TTS_SECRET", "change-me-in-production")
        self.tts_token_ttl = int(os.getenv("TTS_TOKEN_TTL_SECONDS", "300"))  # 5 min por defecto

        if not self.subscription_key:
            raise RuntimeError("AZURE_SUBSCRIPTION_KEY requerida")

        logger.info(f"Azure TTS listo: voz={self.voice_name} región={self.region}")

    # ---------------------------------------------------------------------
    # API COMPATIBLE CON app.py
    # ---------------------------------------------------------------------
    def generate_audio(
        self,
        texto: str,
        velocidad: float = 1.2,
        tono: int = 2,
        genero: str = "femenino",  # ignorado; mantenido por compatibilidad
    ) -> Optional[bytes]:
        """
        Genera audio TTS en memoria (WAV 8kHz μ-law) listo para telefonía.
        Coincide con la firma usada en app.py (speak_with_azure).

        Args:
            texto: Contenido a sintetizar.
            velocidad: Escala relativa (p. ej., 1.2 = 20% más rápido).
            tono: Ajuste en % entero (-20 a +20). Se convertirá a '+N%'.
            genero: No se usa (voz fija Salomé). Se mantiene por compatibilidad.

        Returns:
            bytes | None: WAV (RIFF) 8kHz 8bit Mono μ-law o None en error.
        """
        return self._synthesize_wav_mulaw(texto, velocidad=velocidad, tono=tono)

    # Alias explícito si prefieres llamarlo así desde otros módulos
    def synthesize_wav_bytes(self, texto: str) -> Optional[bytes]:
        """Alias corto que usa los defaults recomendados."""
        return self._synthesize_wav_mulaw(texto, velocidad=1.2, tono=2)

    # ---------------------------------------------------------------------
    # IMPLEMENTACIÓN
    # ---------------------------------------------------------------------
    def _synthesize_wav_mulaw(self, texto: str, velocidad: float, tono: int) -> Optional[bytes]:
        if not texto or not texto.strip():
            logger.warning("Azure TTS: texto vacío")
            return None

        # Validaciones conservadoras para telefonía
        if not (0.8 <= float(velocidad) <= 1.8):
            logger.warning(f"Velocidad fuera de rango (0.8–1.8): {velocidad}. Se forzará a 1.2.")
            velocidad = 1.2
        if not (-20 <= int(tono) <= 20):
            logger.warning(f"Tono fuera de rango (-20–20): {tono}. Se forzará a +2.")
            tono = 2

        try:
            speech_config = speechsdk.SpeechConfig(
                subscription=self.subscription_key,
                region=self.region,
            )

            # WAV RIFF 8kHz μ-law (Twilio-friendly)
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Riff8Khz8BitMonoMULaw
            )

            # Synthesizer sin dispositivo de salida: recibimos bytes en result.audio_data
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=speech_config,
                audio_config=None,
            )

            ssml = self._build_ssml(
                texto=self._clean_text(texto),
                voz=self.voice_name,
                velocidad=velocidad,
                tono=tono,
            )

            result = synthesizer.speak_ssml_async(ssml).get()

            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                logger.info(
                    f"TTS OK: {len(texto)} chars → {len(result.audio_data)} bytes (WAV μ-law 8kHz)"
                )
                return result.audio_data

            if result.reason == speechsdk.ResultReason.Canceled:
                cd = result.cancellation_details
                logger.error(f"TTS cancelado: reason={cd.reason} details={cd.error_details}")
                return None

            logger.error(f"TTS falló: reason={result.reason}")
            return None

        except Exception as e:
            logger.error(f"Azure TTS error: {e}")
            return None

    # ---------------------------------------------------------------------
    # SSML / LIMPIEZA
    # ---------------------------------------------------------------------
    def _build_ssml(self, texto: str, voz: str, velocidad: float, tono: int) -> str:
        """
        Construye SSML simple y robusto. Mantiene la semántica que ya venías
        usando (rate como factor p.ej. 1.2) y tono en porcentaje.
        """
        tono_str = f"{tono:+d}%"
        # Nota: Azure acepta rate relativo como número (p.ej. "1.2") o porcentual ("+20%").
        # Dejamos el formato que vienes usando.
        return f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="es-CO">
  <voice name="{voz}">
    <prosody rate="{velocidad}" pitch="{tono_str}">
      {texto}
    </prosody>
  </voice>
</speak>
        """.strip()

    def _clean_text(self, texto: str) -> str:
        """
        Limpia/escapa solo lo necesario para telefonía en español colombiano.
        Evita caracteres problemáticos y mejora pronunciación de abreviaturas.
        """
        import html

        t = (texto or "").strip()
        t = html.escape(t)

        # Ajustes mínimos útiles en telefonía
        replacements = {
            "Dr.": "Doctor",
            "Dra.": "Doctora",
            "AM": "A M",
            "PM": "P M",
        }
        for k, v in replacements.items():
            t = t.replace(k, v)

        # Pequeñas pausas naturales tras coma
        t = t.replace(",", ", <break time='200ms'/>")

        # Mantener signos de interrogación pero con una micro-pausa antes del cierre
        if "?" in t:
            t = t.replace("?", " <break time='250ms'/>?")

        return t

    # ---------------------------------------------------------------------
    # TOKENS EFÍMEROS PARA URLS /tts
    # ---------------------------------------------------------------------
    def create_tts_token(self, call_id: str, seq: int) -> str:
        """
        Crea un token (expiración + firma HMAC) para proteger endpoints efímeros:
        /tts/{call_id}/{seq}?token={token}
        """
        expires = int(time.time()) + self.tts_token_ttl
        message = f"{call_id}:{seq}:{expires}"
        signature = hmac.new(
            self.tts_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{expires}.{signature}"

    def validate_tts_token(self, call_id: str, seq: int, token: str) -> bool:
        """Valida el token efímero generado con create_tts_token."""
        try:
            expires_str, signature = token.split(".", 1)
            expires = int(expires_str)
            if time.time() > expires:
                return False

            message = f"{call_id}:{seq}:{expires}"
            expected = hmac.new(
                self.tts_secret.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

            return hmac.compare_digest(signature, expected)
        except Exception:
            return False
