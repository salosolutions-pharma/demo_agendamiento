# app.py
import os
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Request, Header, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

# --- Proveedores y capas (ajusta rutas de import según tu estructura)
from call.twilio import TwilioCallProvider                      # <--
from call.base import BaseCallProvider, CallEvent, EventType     # <--
from voice.azure import AzureVoiceProvider                       # <--
from scheduler.openia import OpenAIConversationAssistant         # <--
from scheduler.google_calendar import GoogleCalendarScheduler    # <--
from scheduler.bigquery_storage import BigQueryStorage           # <--

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI(title="Voice API - Twilio + Azure TTS", version="5.0.0")

# =========================
# Config / Estado en memoria
# =========================
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
if not BASE_URL:
    logger.warning("⚠️ BASE_URL no configurada. Debe ser accesible por Twilio (https).")

# Cache efímera de audio (clave: (call_id, seq) -> bytes)
audio_cache: Dict[tuple, bytes] = {}

# Estado de llamada
call_states: Dict[str, Dict[str, Any]] = {}

# =========================
# Instancias
# =========================
voice = AzureVoiceProvider()
assistant = OpenAIConversationAssistant()
calendar = GoogleCalendarScheduler()

# BigQuery es opcional
bq: Optional[BigQueryStorage] = None
try:
    bq = BigQueryStorage()
    logger.info("✅ BigQuery listo")
except Exception as e:
    logger.warning(f"BigQuery no disponible: {e}")

# Único carrier por ahora (Twilio). Luego puedes agregar Telnyx
def get_call_provider() -> BaseCallProvider:
    return TwilioCallProvider()


# =========================
# Modelos
# =========================
class MakeCallRequest(BaseModel):
    to_number: str
    nombre_paciente: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


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
            timeout=10,  # Aumentar de 5 a 10 segundos
            partial_result_callback=f"{BASE_URL}/twilio/partial-result",  # Opcional
        )
        
        # Agregar un Say como fallback si no detecta voz
        if say_if_no_input:
            gather.say(say_if_no_input, voice="Polly.Conchita")

        # Fallback si no habla después del timeout
        resp.say("No te escuché bien. ¿Podrías repetir?", voice="Polly.Conchita")
        # Reintentar gather
        resp.redirect(f"{BASE_URL}/twilio/twiml")

    return str(resp)


def speak_with_azure_and_build_twiml(call_id: str, text: str, gather_after: bool = True) -> Optional[str]:
    """
    1) Síntesis Azure (WAV μ-law 8kHz) en memoria
    2) Guardar en cache efímera (call_id, seq)
    3) Generar URL firmada /audio/{call_id}/{seq}?token=...
    4) Construir TwiML con <Play> y (opcional) <Gather> para siguiente turno
    """
    if not text or not text.strip():
        return None

    audio = voice.generate_audio(text)
    if not audio:
        logger.error("Azure TTS devolvió audio vacío")
        return None

    seq = next_seq(call_id)
    audio_cache[(call_id, seq)] = audio
    token = voice.create_tts_token(call_id, seq)
    play_url = f"{BASE_URL}/audio/{call_id}/{seq}?token={token}"

    return build_play_twiml(play_url, gather_after=gather_after)


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
        
        # Crear evento en Google Calendar usando el método correcto
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
                # Guardar en BigQuery usando el método correcto
                bq_id = bq.save_appointment(
                    nombre_paciente=nombre_paciente,
                    telefono_paciente=to_number,
                    doctor_asignado=doctor,
                    fecha_cita_iso=fecha_inicio,
                    duracion_minutos=30,
                    call_id=call_id,
                    calendar_event_id=event_id,
                    notas=f"Cita agendada automáticamente via llamada. Slot: {slot.get('texto', '')}"
                )
                logger.info(f"[{call_id}] Guardado en BigQuery con ID: {bq_id}")
            except Exception as e:
                logger.warning(f"[{call_id}] BigQuery save_appointment warning: {e}")
        
        if ok:
            logger.info(f"[{call_id}] ✅ Cita creada exitosamente - Calendar ID: {event_id}")
        else:
            logger.error(f"[{call_id}] ❌ Error creando cita en calendar")
        
        return ok
        
    except Exception as e:
        logger.error(f"[{call_id}] save_appointment_to_services error: {e}")
        return False


# =========================
# Endpoints
# =========================
@app.get("/")
def root():
    return {
        "service": "Voice API - Twilio + Azure",
        "version": "5.0.0",
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
        "voice": "azure",
        "carrier": "twilio",
        "calendar": True,
        "bigquery": bool(bq),
    }


@app.post("/make-appointment-call")
async def make_appointment_call(req: MakeCallRequest, x_call_provider: str = Header(default="twilio")):
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
        # Twilio: url inicial (TwiML) y status callback lo maneja Twilio internamente
        call_sid = provider.make_call(to_number, call_id="")
        init_call_state(call_sid, to_number, payload=req.dict())
        return {"ok": True, "call_id": call_sid, "provider": "twilio"}
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
    
    # Procesar evento con el provider
    provider = get_call_provider()
    event = provider.process_webhook_event(dict(form))
    
    if event:
        logger.info(f"[{call_sid}] Event: {event.event_type}")
    
    return {"ok": True}


@app.post("/twilio/twiml")
async def twilio_twiml_handler(request: Request):
    """
    TwiML inicial: reproducimos saludo con Azure (via <Play>) y abrimos Gather para STT por turnos.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    
    logger.info(f"[{call_sid}] Iniciando TwiML handler")

    greeting = "Hola, te habla Salomé de No Me Entregaron. ¿Quieres agendar una cita médica?"
    twiml = speak_with_azure_and_build_twiml(call_sid, greeting, gather_after=True)

    # Si algo falla, devolvemos una pausa mínima para que la llamada no se caiga
    if not twiml:
        from twilio.twiml.voice_response import VoiceResponse
        resp = VoiceResponse()
        resp.pause(length=1)
        # Agregar un gather como fallback
        gather = resp.gather(
            input="speech",
            action=f"{BASE_URL}/twilio/speech-result",
            method="POST",
            speech_timeout="auto",
            language="es-MX",
            timeout=10,  # Aumentar timeout
        )
        gather.say("¿Quieres agendar una cita médica?", voice="Polly.Conchita")
        return Response(content=str(resp), media_type="application/xml")

    logger.info(f"[{call_sid}] TwiML generado correctamente")
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/partial-result")
async def twilio_partial_result(request: Request):
    """
    Maneja resultados parciales de speech recognition (opcional)
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    partial_result = form.get("PartialResult", "")
    
    logger.info(f"[{call_sid}] Partial speech: {partial_result!r}")
    
    # Solo log, no responder TwiML
    return Response(content="", media_type="text/plain")


@app.post("/twilio/speech-result")
async def twilio_speech_result(request: Request):
    """
    Procesa resultados de <Gather input="speech"> de Twilio.
    Devuelve TwiML con <Play> de Azure y un nuevo <Gather> (si continúa),
    o <Hangup> si el assistant decide terminar.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    speech_result = (form.get("SpeechResult") or "").strip()
    confidence = form.get("Confidence", "")

    logger.info(f"[{call_sid}] SpeechResult: {speech_result!r} (confidence: {confidence})")

    # Si no hay resultado de speech, reintentar
    if not speech_result:
        logger.warning(f"[{call_sid}] Sin resultado de speech, reintentando...")
        from twilio.twiml.voice_response import VoiceResponse
        resp = VoiceResponse()
        resp.say("No te escuché. ¿Podrías repetir si quieres agendar una cita?", voice="Polly.Conchita")
        gather = resp.gather(
            input="speech",
            action=f"{BASE_URL}/twilio/speech-result",
            method="POST",
            speech_timeout="auto",
            language="es-MX",
            timeout=10,
        )
        return Response(content=str(resp), media_type="application/xml")

    # Estado de la llamada
    state = call_states.setdefault(call_sid, {
        "seq": 0,
        "slots": [],
        "history": [],
        "context": {},
    })

    # Agregar a historial
    state["history"].append({"user": speech_result, "timestamp": datetime.now().isoformat()})

    logger.info(f"[{call_sid}] Procesando con assistant...")

    # Procesar con assistant
    try:
        reply = assistant.process(
            call_id=call_sid,
            user_text=speech_result,
            context=state.get("context", {}),
        )
        logger.info(f"[{call_sid}] Assistant reply: {reply}")
    except Exception as e:
        logger.error(f"[{call_sid}] Error en assistant.process: {e}")
        # Fallback response
        reply = {
            "say_text": "Disculpa, tuve un problema técnico. ¿Podrías repetir si quieres agendar una cita?",
            "actions": [],
            "end_call": False
        }

    # Acciones: consultar slots o agendar
    say_parts: List[str] = []
    for act in (reply.get("actions") or []):
        if act.get("type") == "get_slots":
            logger.info(f"[{call_sid}] Consultando slots disponibles...")
            
            # Llamar sin parámetros como está definido en google_calendar.py
            slots = calendar.get_available_appointments()
            
            state["slots"] = slots or []
            if slots:
                logger.info(f"[{call_sid}] Encontrados {len(slots)} slots disponibles")
                # Log detallado para debugging
                for i, slot in enumerate(slots):
                    logger.info(f"[{call_sid}]   Slot {i}: {slot.get('texto')} - {slot.get('doctor')}")
                
                say_parts.append(assistant.format_slots_for_speech(slots))
            else:
                logger.warning(f"[{call_sid}] No se encontraron slots disponibles")
                say_parts.append("Lo siento, no tengo horarios disponibles en este momento. ¿Te gustaría que te contacte más tarde?")

        elif act.get("type") == "schedule":
            logger.info(f"[{call_sid}] Intentando agendar cita...")
            idx = act.get("args", {}).get("index")
            slots = state.get("slots", [])
            if isinstance(idx, int) and 0 <= idx < len(slots):
                ok = save_appointment_to_services(call_sid, slots[idx])
                if ok:
                    logger.info(f"[{call_sid}] Cita agendada exitosamente")
                    say_parts.append("¡Listo! Tu cita quedó agendada. Te enviaremos la confirmación.")
                else:
                    logger.error(f"[{call_sid}] Error al agendar cita")
                    say_parts.append("Hubo un problema al agendar la cita. Intentemos con otro horario.")
            else:
                logger.error(f"[{call_sid}] Índice de slot inválido: {idx}")

    # Texto principal del assistant
    main_text = (reply.get("say_text") or "").strip()
    if main_text:
        say_parts.insert(0, main_text)

    # ¿Terminar llamada?
    end_call = bool(reply.get("end_call"))

    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()

    if say_parts:
        # Generar audio Azure y <Play> + (Gather si continúa)
        combined = " ".join(say_parts)
        logger.info(f"[{call_sid}] Generando respuesta TTS: {combined[:100]}...")
        twiml = speak_with_azure_and_build_twiml(call_sid, combined, gather_after=(not end_call))
        if twiml:
            # Devolvemos ese TwiML ya armado
            logger.info(f"[{call_sid}] TwiML con Azure TTS generado correctamente")
            return Response(content=twiml, media_type="application/xml")
        else:
            logger.error(f"[{call_sid}] Error generando TwiML con Azure TTS")

    # Fallback si no hubo TTS por cualquier razón
    if end_call:
        logger.info(f"[{call_sid}] Terminando llamada")
        resp.hangup()
    else:
        # Reintento mínimo: abrir un nuevo gather silencioso
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

    return Response(content=str(resp), media_type="application/xml")


@app.get("/audio/{call_id}/{seq}")
async def serve_tts_audio(call_id: str, seq: int, token: str = Query(...)):
    """
    Sirve WAV μ-law 8kHz generado por Azure para que Twilio lo reproduzca con <Play>.
    Protegido con token HMAC efímero.
    """
    if not voice.validate_tts_token(call_id, seq, token):
        raise HTTPException(status_code=401, detail="token inválido o expirado")

    key = (call_id, seq)
    audio = audio_cache.get(key)
    if not audio:
        raise HTTPException(status_code=404, detail="audio no encontrado")

    # Si quieres que sea one-shot, puedes eliminarlo luego de servir:
    # audio_cache.pop(key, None)

    return StreamingResponse(iter([audio]), media_type="audio/wav")