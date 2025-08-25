import azure.cognitiveservices.speech as speechsdk
from typing import Optional
from .base import BaseVoiceProvider
from config import config

class AzureVoiceProvider(BaseVoiceProvider):
    """Proveedor de voz usando Azure Speech Service (Salome)"""
    
    def __init__(self):
        self.subscription_key = config.AZURE_SUBSCRIPTION_KEY
        self.region = config.AZURE_REGION
    
    def generate_audio(self, texto: str, velocidad: float = 1.0, 
                      tono: int = 0, genero: str = "femenino") -> Optional[bytes]:
        """Genera audio con Salome (voz colombiana)"""
        
        if not self.validate_params(velocidad, tono):
            return None
            
        try:
            # Configurar Azure Speech
            speech_config = speechsdk.SpeechConfig(
                subscription=self.subscription_key, 
                region=self.region
            )
            
            speech_config.speech_synthesis_voice_name = "es-CO-SalomeNeural"
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Audio16Khz32KBitRateMonoMp3
            )
            
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=speech_config, 
                audio_config=None
            )
            
            # SSML con personalización
            ssml_text = f"""
            <speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="es-CO">
                <voice name="es-CO-SalomeNeural">
                    <prosody rate="{velocidad:.1f}" pitch="{tono:+d}%">
                        {texto}
                    </prosody>
                </voice>
            </speak>
            """
            
            # Generar audio
            result = synthesizer.speak_ssml_async(ssml_text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                return result.audio_data
            else:
                print(f"Error en síntesis Azure: {result.reason}")
                return None
                
        except Exception as e:
            print(f"Error Azure Voice: {e}")
            return None
    
    def get_provider_name(self) -> str:
        return "azure"