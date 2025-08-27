import os
import logging
import re
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Request, Header, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

# --- Proveedores y capas (ajusta rutas de import según tu estructura)
from call.twilio import TwilioCallProvider                      # <-- Carrier Twilio
from call.base import BaseCallProvider                          # <-- Tipos del carrier
from voice.azure import AzureVoiceProvider                      # <-- TTS Azure
from voice.elevenlabs import ElevenLabsVoiceProvider            # <-- TTS ElevenLabs
from scheduler.openia import OpenAIConversationAssistant        # <-- Asistente (tool-calling puro)
from scheduler.google_calendar import GoogleCalendarScheduler   # <-- Calendar
from scheduler.bigquery_storage import BigQueryStorage          # <-- BigQuery (opcional)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI(title="Voice API - Twilio + TTS", version="5.0.1")

# =========================
# Config / Estado en memoria
# =========================
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
if not BASE_URL:
    logger.warning("BASE_URL no configurada. Debe ser accesible por Twilio (https).")

# Cache efímera de audio (clave: (call_id, seq) -> bytes)
audio_cache: Dict[tuple, bytes] = {}

# Estado de llamada
call_states: Dict[str, Dict[str, Any]] = {}

# =========================
# Instancias
# =========================
assistant = OpenAIConversationAssistant()
calendar = GoogleCalendarScheduler()

# BigQuery es opcional
bq: Optional[BigQueryStorage] = None
try:
    bq = BigQueryStorage()
    logger.info("BigQuery listo")
except Exception as e:
    logger.warning(f"BigQuery no disponible: {e}")

# Único carrier por ahora (Twilio). Luego puedes agregar Telnyx
def get_call_provider() -> BaseCallProvider:
    return TwilioCallProvider()

# =========================
# Utilidades anti-mojibake
# =========================
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

# =========================
# Modelos
# =========================
class MakeCallRequest(BaseModel):
    to_number: str
    nombre_paciente: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    voice_provider: Optional[str] = None  # "azure" | "elevenlabs"

# =========================
# Helpers
# =========================
def init_call_state(call_id: str, to_number: str, payload: Dict[str, Any]):
    call_states[call_id] = {
        "to_number": to_number,
        "seq": 0,
        "context": {
            "nombre_paciente": payload.get("nombre_paciente"),
            "metadata": payload.get("metadata", {}),
        },
        "voice_provider_name": "azure",
        "voice_instance": None,
        "slots": [],
        "history": [],
    }

def next_seq(call_id: str) -> int:
    st = call_states.setdefault(call_id, {})
    st["seq"] = int(st.get("seq", 0)) + 1
    return st["seq"]

def build_play_twiml(play_url: str, gather_after: bool = False, say_if_no_input: Optional[str] = None) -> str:
    """
    Genera un TwiML que hace <Play> de la URL y opcionalmente agrega un <Gather> nuevo.
    """
    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()
    resp.play(play_url)
    resp.pause(length=1)

    if gather_after:
        gather = resp.gather(
            input="speech",
            action=f"{BASE_URL}/twilio/speech-result",
            method="POST",
            speech_timeout="auto",
            language="es-MX",
            timeout=10,  # algo más generoso para telefonía
            partial_result_callback=f"{BASE_URL}/twilio/partial-result",  # Opcional
        )

        # Agregar un Say como fallback si no detecta voz
        if say_if_no_input:
            gather.say(_demojibake(say_if_no_input), voice="Polly.Conchita")

        # Fallback si no habla después del timeout
        resp.say("No te escuché bien. ¿Podrías repetir?", voice="Polly.Conchita")
        # Reintentar gather
        resp.redirect(f"{BASE_URL}/twilio/twiml")

    return str(resp)

def speak_with_tts_and_build_twiml(call_id: str, text: str, gather_after: bool = True) -> Optional[str]:
    """
    1) Síntesis TTS (WAV μ-law 8kHz) en memoria con el proveedor seleccionado
    2) Guardar en cache efímera (call_id, seq)
    3) Generar URL firmada /audio/{call_id}/{seq}?token=...
    4) Construir TwiML con <Play> y (opcional) <Gather> para siguiente turno
    """
    if not text or not text.strip():
        return None

    text = _demojibake(text)

    vprov = get_voice_for_call(call_id)
    audio = vprov.generate_audio(text)
    if not audio:
        logger.error("TTS devolvió audio vacío")
        return None

    seq = next_seq(call_id)
    audio_cache[(call_id, seq)] = audio
    token = vprov.create_tts_token(call_id, seq)
    play_url = f"{BASE_URL}/audio/{call_id}/{seq}?token={token}"

    return build_play_twiml(play_url, gather_after=gather_after)

def find_slot_by_datetime(slots: List[Dict[str, Any]], iso_inicio: str) -> Optional[Dict[str, Any]]:
    """Encuentra el slot que coincide con la fecha/hora solicitada."""
    for slot in slots:
        if slot.get("iso_inicio") == iso_inicio:
            return slot
    return None

def save_appointment_to_services(call_id: str, slot: Dict[str, Any]) -> bool:
    """
    Crea evento en Calendar y (si aplica) guarda en BigQuery.
    Slot esperado: {"iso_inicio": ..., "iso_fin": ..., "doctor": ..., "texto": ...}
    """
    try:
        # Obtener datos del contexto de la llamada
        state = call_states.get(call_id, {})
        context = state.get("context", {})
        nombre_paciente = context.get("nombre_paciente") or "Paciente"
        to_number = state.get("to_number", "")

        # Extraer datos del slot
        fecha_inicio = slot.get("iso_inicio")
        fecha_fin = slot.get("iso_fin")
        doctor = slot.get("doctor", "Doctor")

        if not fecha_inicio or not fecha_fin:
            logger.error(f"[{call_id}] Slot inválido: faltan fechas {slot}")
            return False

        logger.info(f"[{call_id}] Creando cita: {nombre_paciente} con {doctor} el {fecha_inicio}")

        # Crear evento en Google Calendar
        event_id = calendar.create_appointment(
            nombre=nombre_paciente,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
            doctor=doctor,
            telefono=to_number
        )

        ok = bool(event_id)

        if ok and bq:
            try:
                bq_id = bq.save_appointment(
                    nombre_paciente=nombre_paciente,
                    telefono_paciente=to_number,
                    doctor_asignado=doctor,
                    fecha_cita_iso=fecha_inicio,
                    duracion_minutos=30,
                    call_id=call_id,
                    calendar_event_id=event_id,
                    notas=f"Cita agendada automáticamente vía llamada. Slot: {slot.get('texto', '')}"
                )
                logger.info(f"[{call_id}] Guardado en BigQuery con ID: {bq_id}")
            except Exception as e:
                logger.warning(f"[{call_id}] BigQuery save_appointment warning: {e}")

        if ok:
            logger.info(f"[{call_id}] Cita creada exitosamente - Calendar ID: {event_id}")
        else:
            logger.error(f"[{call_id}] Error creando cita en calendar")

        return ok

    except Exception as e:
        logger.error(f"[{call_id}] save_appointment_to_services error: {e}")
        return False

# --- Voice provider selection per call ---
def make_voice_provider(name: str):
    name = (name or "").strip().lower()
    logger.info(f"Creando voice provider: {name}")

    if name in ("elevenlabs", "11labs", "eleven"):
        logger.info("Usando ElevenLabs TTS")
        return ElevenLabsVoiceProvider()

    # default
    logger.info("Usando Azure TTS (default)")
    return AzureVoiceProvider()

def set_call_voice_provider(call_id: str, provider_name: str):
    st = call_states.setdefault(call_id, {})
    provider_name = (provider_name or "azure").lower()
    st["voice_provider_name"] = provider_name
    st["voice_instance"] = make_voice_provider(provider_name)
    logger.info(f"[{call_id}] Voice provider configurado: {provider_name} -> {type(st['voice_instance']).__name__}")

def get_voice_for_call(call_id: str):
    st = call_states.setdefault(call_id, {})
    v = st.get("voice_instance")
    if v is None:
        provider_name = st.get("voice_provider_name", "azure")
        logger.info(f"[{call_id}] No voice_instance, creando {provider_name}")
        v = make_voice_provider(provider_name)
        st["voice_instance"] = v   # cache
    return v

# =========================
# Endpoints de debug (opcional)
# =========================
@app.get("/debug/voice-providers")
def debug_voice_providers():
    try:
        azure_instance = AzureVoiceProvider()
        azure_ok = bool(getattr(azure_instance, "subscription_key", None))

        elevenlabs_instance = ElevenLabsVoiceProvider()
        elevenlabs_ok = bool(getattr(elevenlabs_instance, "api_key", None) and getattr(elevenlabs_instance, "voice_id", None))

        return {
            "azure": {
                "available": azure_ok,
                "region": getattr(azure_instance, "region", None),
                "voice": getattr(azure_instance, "voice_name", None),
            },
            "elevenlabs": {
                "available": elevenlabs_ok,
                "voice_id": getattr(elevenlabs_instance, "voice_id", None),
                "model": getattr(elevenlabs_instance, "model_id", None),
            },
            "current_calls": {
                call_id: {
                    "voice_provider_name": state.get("voice_provider_name"),
                    "voice_instance_type": type(state.get("voice_instance", None)).__name__ if state.get("voice_instance") else None
                }
                for call_id, state in call_states.items()
            }
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/test-tts/{provider}")
def debug_test_tts(provider: str):
    try:
        voice_provider = make_voice_provider(provider)
        test_text = "Hola, esta es una prueba de texto a voz."
        audio_data = voice_provider.generate_audio(test_text)
        return {
            "provider": provider,
            "provider_class": type(voice_provider).__name__,
            "audio_generated": bool(audio_data),
            "audio_size_bytes": len(audio_data) if audio_data else 0
        }
    except Exception as e:
        return {"provider": provider, "error": str(e)}

# =========================
# Endpoints principales
# =========================
@app.get("/")
def root():
    return {
        "service": "Voice API - Twilio + TTS",
        "version": "5.0.1",
        "endpoints": [
            "POST /make-appointment-call",
            "POST /twilio/twiml",
            "POST /twilio/speech-result",
            "POST /twilio/partial-result",
            "POST /webhook/twilio",
            "GET  /audio/{call_id}/{seq}?token=...",
            "GET  /health",
        ],
    }

@app.get("/health")
def health():
    return {
        "ok": True,
        "timestamp": datetime.now().isoformat(),
        "carrier": "twilio",
        "calendar": True,
        "bigquery": bool(bq),
    }

@app.post("/make-appointment-call")
async def make_appointment_call(
    req: MakeCallRequest,
    x_call_provider: str = Header(default="twilio"),
    x_voice_provider: str = Header(default=None)
):
    """
    Inicia la llamada saliente con Twilio.
    """
    if x_call_provider.lower() != "twilio":
        raise HTTPException(400, "Por ahora este endpoint usa Twilio. (Telnyx se agrega luego)")

    provider = get_call_provider()

    to_number = req.to_number
    if not to_number:
        raise HTTPException(status_code=400, detail="to_number es requerido")

    try:
        chosen_voice = (x_voice_provider or req.voice_provider or "azure").lower()
        twiml_url = f"{BASE_URL}/twilio/twiml?voice={chosen_voice}"

        # requiere que TwilioCallProvider soporte url personalizada:
        call_sid = provider.make_call(to_number, call_id="", twiml_url=twiml_url)

        init_call_state(call_sid, to_number, payload=req.dict())
        set_call_voice_provider(call_sid, chosen_voice)
        return {"ok": True, "call_id": call_sid, "provider": "twilio", "voice_provider": chosen_voice}
    except Exception as e:
        logger.error(f"make_appointment_call error: {e}")
        raise HTTPException(status_code=500, detail="No se pudo iniciar la llamada")

@app.post("/webhook/twilio")
async def twilio_webhook_handler(request: Request):
    """
    Maneja webhooks de estado de llamada de Twilio
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")

    logger.info(f"[{call_sid}] Webhook status: {call_status}")

    provider = get_call_provider()
    _ = provider.process_webhook_event(dict(form))  # logs/normaliza

    return {"ok": True}

@app.post("/twilio/twiml")
async def twilio_twiml_handler(request: Request):
    """
    TwiML inicial: reproducimos saludo con TTS (via <Play>) y abrimos Gather para STT por turnos.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")

    provider_qs = request.query_params.get("voice")
    if provider_qs:
        set_call_voice_provider(call_sid, provider_qs)

    logger.info(f"[{call_sid}] Iniciando TwiML handler")

    greeting = "Buen dia, mi nombre es claudia. Le hablo de parte del centro de atención en salud de la Universidad nacional. ¿Desea agendar su cita de médicina general?"
    greeting = _demojibake(greeting)

    twiml = speak_with_tts_and_build_twiml(call_sid, greeting, gather_after=True)

    # Registrar el saludo en el historial para contexto del LLM
    st = call_states.setdefault(call_sid, {"history": [], "context": {}, "slots": [], "seq": 0})
    st["history"].append({"assistant": greeting, "timestamp": datetime.now().isoformat()})
    st.setdefault("context", {})["history"] = st["history"]

    # Si algo falla, fallback sencillo
    if not twiml:
        from twilio.twiml.voice_response import VoiceResponse
        resp = VoiceResponse()
        resp.pause(length=1)
        gather = resp.gather(
            input="speech",
            action=f"{BASE_URL}/twilio/speech-result",
            method="POST",
            speech_timeout="auto",
            language="es-MX",
            timeout=10,
        )
        gather.say("¿Quieres agendar una cita médica?", voice="Polly.Conchita")
        return Response(content=str(resp), media_type="application/xml; charset=utf-8")

    logger.info(f"[{call_sid}] TwiML generado correctamente")
    return Response(content=twiml, media_type="application/xml; charset=utf-8")

@app.post("/twilio/partial-result")
async def twilio_partial_result(request: Request):
    """
    Maneja resultados parciales de speech recognition (opcional)
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    partial_result = _demojibake(form.get("PartialResult", ""))

    logger.info(f"[{call_sid}] Partial speech: {partial_result!r}")

    # Solo log, no responder TwiML
    return Response(content="", media_type="text/plain; charset=utf-8")

@app.post("/twilio/speech-result")
async def twilio_speech_result(request: Request):
    """
    Procesa resultados de <Gather input="speech"> de Twilio.
    Devuelve TwiML con <Play> del TTS y un nuevo <Gather> (si continúa),
    o <Hangup> si el assistant decide terminar.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    speech_result = _demojibake((form.get("SpeechResult") or "").strip())
    confidence = form.get("Confidence", "")

    logger.info(f"[{call_sid}] SpeechResult: {speech_result!r} (confidence: {confidence})")

    # Si no hay resultado de speech, reintentar
    if not speech_result:
        from twilio.twiml.voice_response import VoiceResponse
        resp = VoiceResponse()
        resp.say("No te escuché. ¿Podrías repetir si quieres agendar una cita?", voice="Polly.Conchita")
        resp.gather(
            input="speech",
            action=f"{BASE_URL}/twilio/speech-result",
            method="POST",
            speech_timeout="auto",
            language="es-MX",
            timeout=10,
        )
        return Response(content=str(resp), media_type="application/xml; charset=utf-8")

    # Estado de la llamada
    state = call_states.setdefault(call_sid, {
        "seq": 0,
        "slots": [],
        "history": [],
        "context": {},
    })

    # Agregar a historial (lo que dijo el usuario)
    state["history"].append({"user": speech_result, "timestamp": datetime.now().isoformat()})

    logger.info(f"[{call_sid}] Procesando con assistant...")

    # Procesar con assistant (Contrato A - tool-calling puro)
    try:
        # Preparar contexto más explícito para evitar confusión nombre paciente/doctor
        context = {
            **state.get("context", {}),
            "history": state.get("history", []),
            "slots": state.get("slots", []),
        }
        
        # Asegurar que el nombre del paciente esté claramente identificado
        nombre_paciente = context.get("nombre_paciente")
        if nombre_paciente:
            context["paciente_actual"] = nombre_paciente  # Campo adicional más explícito
            logger.info(f"[{call_sid}] Contexto paciente: {nombre_paciente}")
        
        # DEBUG: Log del contexto para verificar qué se está pasando
        logger.info(f"[{call_sid}] Context keys: {list(context.keys())}")
        
        reply = assistant.process(
            call_id=call_sid,
            user_text=speech_result,
            context=context,
            calendar=calendar,  # para que el tool get_slots funcione
        )
        logger.info(f"[{call_sid}] Assistant reply: { {k: (v if k!='slots' else f'{len(v)} slots') for k,v in reply.items()} }")
    except Exception as e:
        logger.error(f"[{call_sid}] Error en assistant.process: {e}")
        reply = {
            "say_text": "Disculpa, tuve un problema técnico. ¿Podrías repetir si quieres agendar una cita?",
            "actions": [],
            "end_call": False
        }

    # Sincroniza slots que haya devuelto el asistente (Contrato A)
    new_slots = reply.get("slots")
    if new_slots:
        state["slots"] = new_slots
        state.setdefault("context", {})["slots"] = new_slots

    # Acciones: en Contrato A solo esperamos 'schedule' aquí
    say_parts: List[str] = []
    for act in (reply.get("actions") or []):
        if act.get("type") == "schedule":
            idx = act.get("index")
            slots = state.get("slots", [])

            if act.get("iso_inicio") and act.get("iso_fin"):
                # Buscar el slot que coincide con la fecha/hora solicitada
                matching_slot = find_slot_by_datetime(slots, act["iso_inicio"])
                if matching_slot:
                    ok = save_appointment_to_services(call_sid, matching_slot)
                    logger.info(f"[{call_sid}] Usando slot coincidente: {matching_slot.get('doctor')} - {matching_slot.get('texto')}")
                else:
                    # Fallback: crear slot con datos de la acción (sin doctor específico)
                    logger.warning(f"[{call_sid}] No se encontró slot para {act['iso_inicio']}, usando fallback")
                    ok = save_appointment_to_services(call_sid, {
                        "iso_inicio": act["iso_inicio"],
                        "iso_fin": act["iso_fin"],
                        "doctor": "Doctor",
                        "texto": "cita por fecha/hora solicitada"
                    })
            elif isinstance(idx, int) and 0 <= idx < len(slots):
                ok = save_appointment_to_services(call_sid, slots[idx])
                logger.info(f"[{call_sid}] Usando slot por índice {idx}: {slots[idx].get('doctor')} - {slots[idx].get('texto')}")
            else:
                ok = False
                logger.error(f"[{call_sid}] Acción schedule sin datos válidos: {act}")

            if ok:
                say_parts.append("¡Listo! Tu cita quedó agendada. Te enviaremos la confirmación.")
                reply["end_call"] = True
            else:
                say_parts.append("No pude agendar con ese horario. ¿Quieres que te proponga otras opciones?")

    # Texto principal del assistant
    main_text = (reply.get("say_text") or "").strip()
    if main_text:
        say_parts.insert(0, main_text)

    # ¿Terminar llamada?
    end_call = bool(reply.get("end_call"))

    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()

    if say_parts:
        combined = _demojibake(" ".join(say_parts))
        # Guarda en historial lo que dirá el bot (para contexto del LLM)
        state["history"].append({"assistant": combined, "timestamp": datetime.now().isoformat()})
        state.setdefault("context", {})["history"] = state["history"]

        # Generar audio con TTS y devolver TwiML (<Play> + Gather si continúa)
        logger.info(f"[{call_sid}] Generando respuesta TTS: {combined[:120]}...")
        twiml = speak_with_tts_and_build_twiml(call_sid, combined, gather_after=(not end_call))
        if twiml:
            return Response(content=twiml, media_type="application/xml; charset=utf-8")
        else:
            logger.error(f"[{call_sid}] Error generando TwiML con TTS")

    # Fallback si no hubo TTS por cualquier razón
    if end_call:
        logger.info(f"[{call_sid}] Terminando llamada")
        resp.hangup()
    else:
        logger.info(f"[{call_sid}] Fallback: creando gather básico")
        resp.pause(length=1)
        resp.gather(
            input="speech",
            action=f"{BASE_URL}/twilio/speech-result",
            method="POST",
            speech_timeout="auto",
            language="es-MX",
            timeout=10,
        )

    return Response(content=str(resp), media_type="application/xml; charset=utf-8")

@app.get("/audio/{call_id}/{seq}")
async def serve_tts_audio(call_id: str, seq: int, token: str = Query(...)):
    """
    Sirve WAV μ-law 8kHz generado por el TTS seleccionado para que Twilio lo reproduzca con <Play>.
    Protegido con token HMAC efímero.
    """
    vprov = get_voice_for_call(call_id)
    if not vprov.validate_tts_token(call_id, seq, token):
        raise HTTPException(status_code=401, detail="token inválido o expirado")

    key = (call_id, seq)
    audio = audio_cache.get(key)
    if not audio:
        raise HTTPException(status_code=404, detail="audio no encontrado")

    # Si quieres que sea one-shot, puedes eliminarlo luego de servir:
    # audio_cache.pop(key, None)

    #return StreamingResponse(iter([audio]), media_type="audio/wav")
    return StreamingResponse(iter([audio]), media_type=getattr(vprov, "get_mime_type", lambda: "audio/wav")())
