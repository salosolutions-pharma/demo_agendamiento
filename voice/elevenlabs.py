import os
import io
import re
import time
import hmac
import json
import struct
import hashlib
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def _demojibake(text: str) -> str:
    """
    Repara texto típico con mojibake (UTF-8 leído como latin-1) sin afectar texto ya correcto.
    Ej.: 'SalomÃ©' -> 'Salomé', 'Â¿Quieres?' -> '¿Quieres?'
    """
    if not text:
        return text
    if any(ch in text for ch in ("Ã", "Â", "â", "Î")):
        try:
            text = text.encode("latin-1").decode("utf-8")
        except Exception:
            pass
    
    # Corrección de AM/PM para mejor pronunciación
    import re
    # Captura tanto "AM/PM" como "a.m./p.m." con o sin espacios
    text = re.sub(r'\b(\d{1,2}):?(\d{0,2})\s*(?:AM|a\.?\s*m\.?)\b', 
                  lambda m: f"{m.group(1)}{':' + m.group(2) if m.group(2) else ''} de la mañana", 
                  text, flags=re.IGNORECASE)
    
    text = re.sub(r'\b(\d{1,2}):?(\d{0,2})\s*(?:PM|p\.?\s*m\.?)\b', 
                  lambda m: f"{m.group(1)}{':' + m.group(2) if m.group(2) else ''} de la tarde" 
                           if int(m.group(1)) == 12 or int(m.group(1)) < 6 else 
                           f"{m.group(1)}{':' + m.group(2) if m.group(2) else ''} de la noche", 
                  text, flags=re.IGNORECASE)
    
    return text


class ElevenLabsVoiceProvider:
    """
    Cliente ElevenLabs TTS que devuelve WAV listo para Twilio <Play>.
    - Por defecto: Eleven -> PCM 16 kHz crudo -> WAV PCM 8 kHz mono (ultra compatible).
    - Alternativa (set ELEVENLABS_OUTPUT_FORMAT=ulaw_8000): Eleven -> μ-law 8 kHz crudo -> WAV μ-law (formato=7) con chunk fact.
    Interfaz compatible con app.py:
      - generate_audio(texto) -> bytes | None
      - create_tts_token(call_id, seq) -> str
      - validate_tts_token(call_id, seq, token) -> bool
    """

    def __init__(self):
        self.api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        self.voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
        self.model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")

        # Por confiabilidad, DEFAULT = PCM 16 kHz (lo convertimos a WAV 8 kHz)
        self.preferred_output_format = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "ulaw_8000").strip().lower()

        # Seguridad para endpoints efímeros /audio
        self.tts_secret = os.getenv("TTS_SECRET", "change-me-in-production")
        self.tts_token_ttl = int(os.getenv("TTS_TOKEN_TTL_SECONDS", "300"))  # 5 min

        self.configured = True
        self.config_error = None
        if not self.api_key:
            self.configured = False
            self.config_error = "ELEVENLABS_API_KEY no configurada"
        elif not self.voice_id:
            self.configured = False
            self.config_error = "ELEVENLABS_VOICE_ID no configurada"

        if self.configured:
            logger.info(f"ElevenLabs listo · voice_id={self.voice_id} · model={self.model_id} · out={self.preferred_output_format}")
        else:
            logger.error(f"ElevenLabs NO configurado: {self.config_error}")

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def generate_audio(
        self,
        texto: str,
        velocidad: float = 1.2,
        tono: int = 2,
        genero: str = "femenino",
    ) -> Optional[bytes]:
        """
        Devuelve WAV (8 kHz mono) listo para <Play> de Twilio.
        Si ElevenLabs falla o devuelve formato inesperado, retorna None.
        """
        if not self.configured:
            logger.error(f"ElevenLabs generate_audio: config inválida: {self.config_error}")
            return None

        txt = _demojibake((texto or "").strip())
        if not txt:
            logger.warning("ElevenLabs generate_audio: texto vacío")
            return None

        # Mapear velocidad/tono a settings (0..1)
        try:
            stability = max(0.0, min(1.0, 0.6 + (float(velocidad) - 1.2) * 0.5))
        except Exception:
            stability = 0.6
        try:
            style = max(0.0, min(1.0, 0.55 + (int(tono) / 20.0) * 0.25))
        except Exception:
            style = 0.6

        out_fmt = self.preferred_output_format
        payload = {
            "text": txt,
            "model_id": self.model_id,
            "voice_settings": {
                "stability": 0.35,
                "similarity_boost": 0.92 ,
                "style": 0.80,
                "use_speaker_boost": True,
                "speed": 1.18
            },
            "output_format": out_fmt,  # "pcm_16000" (default) | "ulaw_8000"
        }
        logger.info("ElevenLabs request payload: %s", json.dumps(payload, ensure_ascii=False))

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",  # bytes crudos según output_format
        }

        try:
            resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=45)
        except Exception as e:
            logger.error("ElevenLabs request error: %s", e)
            return None

        logger.info("ElevenLabs response: status=%s", resp.status_code)
        if resp.status_code != 200:
            try:
                err = resp.json()
            except Exception:
                err = resp.text[:500]
            logger.error("ElevenLabs API error %s: %s", resp.status_code, err)
            return None

        audio_bytes = resp.content or b""
        return audio_bytes
        """if not audio_bytes:
            logger.error("ElevenLabs devolvió audio vacío")
            return None

        # Si ya es WAV, úsalo tal cual
        if self._looks_like_wav(audio_bytes):
            logger.info("ElevenLabs: audio ya es WAV; tamaño=%d", len(audio_bytes))
            return audio_bytes

        # Ruta μ-law 8 kHz -> encapsular a WAV formato=7 con chunk fact
        if out_fmt.startswith("ulaw"):
            wav = self._wrap_ulaw_to_wav(audio_bytes, sample_rate=8000, channels=1)
            logger.info("Encapsulado μ-law RAW -> WAV (formato 7 + fact); bytes=%d", len(wav))
            return wav

        # Ruta PCM 16 kHz crudo -> WAV PCM 8 kHz mono
        if out_fmt.startswith("pcm"):
            try:
                src_rate = self._parse_rate_from_pcm_format(out_fmt) or 16000
                wav = self._pcm16le_to_wav_8k(audio_bytes, src_rate)
                logger.info("Convertido PCM16 raw %dk -> WAV PCM 8k; bytes=%d", src_rate // 1000, len(wav))
                return wav
            except Exception as e:
                logger.error("Error convirtiendo PCM16 a WAV 8k: %s", e)
                return None

        # Otros formatos no soportados en este flujo
        logger.error("Formato de salida no soportado para telefonía: %s", out_fmt)
        return None"""

    # ------------------------------------------------------------------
    # Tokens efímeros (mismo contrato que AzureVoiceProvider)
    # ------------------------------------------------------------------
    def create_tts_token(self, call_id: str, seq: int) -> str:
        expires = int(time.time()) + self.tts_token_ttl
        message = f"{call_id}:{seq}:{expires}"
        signature = hmac.new(
            self.tts_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{expires}.{signature}"

    def validate_tts_token(self, call_id: str, seq: int, token: str) -> bool:
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

    # ------------------------------------------------------------------
    # Helpers de formato
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_wav(data: bytes) -> bool:
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"

    @staticmethod
    def _parse_rate_from_pcm_format(fmt: str) -> Optional[int]:
        # "pcm_16000", "pcm_22050", etc.
        try:
            return int(fmt.split("_", 1)[1])
        except Exception:
            return None

    @staticmethod
    def _build_wav_header_mulaw_with_fact(num_samples: int, sample_rate: int = 8000, channels: int = 1) -> bytes:
        """
        WAV μ-law (WAVE_FORMAT_MULAW=0x0007) con chunk fact (recomendado para formatos comprimidos).
        BitsPerSample=8, BlockAlign=1*channels, ByteRate=sample_rate*channels*1
        fmt chunk size = 18 (cbSize=0), fact chunk size = 4 (dwSampleLength).
        """
        byte_rate = sample_rate * channels * 1
        block_align = 1 * channels
        fmt_chunk_size = 18
        fact_chunk_size = 4
        data_chunk_size = num_samples
        riff_size = 4 + (8 + fmt_chunk_size) + (8 + fact_chunk_size) + (8 + data_chunk_size)

        header = (
            b"RIFF" +
            struct.pack("<I", riff_size) +
            b"WAVE" +
            b"fmt " +
            struct.pack("<I", fmt_chunk_size) +
            struct.pack("<H", 0x0007) +                 # WAVE_FORMAT_MULAW
            struct.pack("<H", channels) +
            struct.pack("<I", sample_rate) +
            struct.pack("<I", byte_rate) +
            struct.pack("<H", block_align) +
            struct.pack("<H", 8) +                      # BitsPerSample
            struct.pack("<H", 0) +                      # cbSize
            b"fact" +
            struct.pack("<I", fact_chunk_size) +
            struct.pack("<I", num_samples) +            # dwSampleLength
            b"data" +
            struct.pack("<I", data_chunk_size)
        )
        return header

    def _wrap_ulaw_to_wav(self, ulaw_bytes: bytes, sample_rate: int = 8000, channels: int = 1) -> bytes:
        return self._build_wav_header_mulaw_with_fact(len(ulaw_bytes), sample_rate, channels) + ulaw_bytes

    @staticmethod
    def _pcm16le_to_wav_8k(pcm16le_src: bytes, src_rate: int) -> bytes:
        """
        Convierte PCM lineal 16-bit LE mono a WAV PCM 8 kHz mono.
        Downsample simple por decimación; suficiente para voz telefónica.
        """
        if not pcm16le_src:
            return b""
        if src_rate == 8000:
            pcm8k = pcm16le_src
        else:
            # Decimación por factor entero aproximado
            # (si no es múltiplo exacto, elegimos el más cercano)
            import math
            factor = int(round(src_rate / 8000.0))
            factor = max(1, factor)
            step = 2 * factor  # 2 bytes por muestra * factor
            pcm8k = bytearray()
            for i in range(0, len(pcm16le_src), step):
                pcm8k += pcm16le_src[i:i+2]

        out = io.BytesIO()
        import wave as _wave
        with _wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)   # 16-bit
            w.setframerate(8000)
            w.writeframes(bytes(pcm8k))
        return out.getvalue()

    # (Opcional)
    def get_provider_name(self) -> str:
        return "elevenlabs"

    def get_mime_type(self) -> str:
        return "audio/mpeg"
    
