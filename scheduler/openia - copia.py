import os
import re
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List

from openai import OpenAI  # openai >= 1.x

logger = logging.getLogger(__name__)


def _norm(t: Optional[str]) -> str:
    return (t or "").strip().lower()


def _coerce_yesno(text: str) -> Optional[bool]:
    """True si es afirmación, False si es negación, None si no claro."""
    t = _norm(text)
    afirm = ["sí","si","claro","ok","vale","afirmativo","quiero","deseo","me gustaría","me gustaria","1","agendar"]
    neg = ["no","ahora no","otro momento","después","despues","negativo","2"]
    if any(w in t for w in afirm): return True
    if any(w in t for w in neg):   return False
    return None


def _match_slot_local(user_input: str, available: List[Dict[str, Any]]) -> Optional[int]:
    """
    Heurística local para mapear la elección del usuario a un índice de 'available'.
    'available' es lista de dicts con al menos: texto, doctor, iso_inicio, iso_fin (opcional).
    """
    t = _norm(user_input)
    if not t or not available:
        return None

    # 1) Coincidencia directa por texto o doctor
    for i, h in enumerate(available):
        if _norm(h.get("texto")) and _norm(h.get("texto")) in t:
            return i
    for i, h in enumerate(available):
        if _norm(h.get("doctor")) and _norm(h.get("doctor")) in t:
            return i

    # 2) Día de semana
    dias = ["lunes","martes","miércoles","miercoles","jueves","viernes","sábado","sabado","domingo"]
    for i, h in enumerate(available):
        hx = _norm(h.get("texto")) + " " + _norm(h.get("fecha_mostrar",""))
        for d in dias:
            if d in t and d in hx:
                return i

    # 3) Hora "3 pm", "15:00", "3:30", etc.
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or "0")
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and 1 <= hh <= 11: hh += 12
        if ampm == "am" and hh == 12: hh = 0
        for i, h in enumerate(available):
            try:
                if h.get("iso_inicio"):
                    dt = datetime.fromisoformat(h["iso_inicio"].replace("Z",""))
                    if dt.hour == hh and (mm == 0 or dt.minute == mm):
                        return i
                else:
                    hx = _norm(h.get("texto"))
                    if re.search(fr"\b{hh}\s*(:\s*{mm:02d})?\b", hx):
                        return i
            except Exception:
                continue

    return None


class OpenAIConversationAssistant:
    """
    Asistente que lidera TODA la conversación.
    - process(call_id, user_text, context) -> JSON contrato
    El contrato devuelto tiene claves: say_text, expect_input, end_call, actions.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        # 1) Valida API key
        env_key = os.getenv("OPENAI_API_KEY", "").strip()
        api_key = api_key or env_key
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY no configurada (vacía). Define la variable en Cloud Run.")

        # 2) Sanitiza/valida base_url si existe
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        if base_url and not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise RuntimeError(f"OPENAI_BASE_URL inválida: {base_url!r}. Debe iniciar con http(s):// o no estar definida.")

        # 3) Crea el cliente SIN kwargs raros
        try:
            # Si usas endpoint estándar de OpenAI, NO pases base_url.
            # Si usas Azure OpenAI u otro gateway, sí pásalo.
            self.client = OpenAI(api_key=api_key) if not base_url else OpenAI(api_key=api_key, base_url=base_url)
        except Exception as e:
            # Muestra variables relevantes para diagnóstico
            safe_url = base_url if base_url else "<default>"
            logger.error(f"❌ Fallo inicializando OpenAI: {repr(e)} | base_url={safe_url}")
            raise

        # 4) Modelo por defecto
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        # 5) Prueba mínima de salud (falla al arrancar si algo está mal)
        try:
            # models.list es liviano; si falla aquí, la config está mala
            _ = self.client.models.list()
            logger.info("✅ OpenAI client listo. Modelo por defecto: %s", self.model)
        except Exception as e:
            logger.error(f"❌ OpenAI client no pasó self-test: {repr(e)}")
            raise RuntimeError(
                "OpenAI no operativo. Revisa OPENAI_API_KEY, OPENAI_BASE_URL (si aplica), conectividad y permisos."
            )

        self.system_prompt = (
            "Eres Salomé, asistente de 'No Me Entregaron'. Hablas en español colombiano, breve y natural. "
            "Tu objetivo es agendar una cita: "
            "1) Confirmar intención (sí/no). "
            "2) Ofrecer 2-3 opciones claras (día, hora, doctor). "
            "3) Confirmar la elección. "
            "4) Cerrar agradeciendo. "
            "Si el usuario no quiere, despídelo cordialmente. "
            "Responde SIEMPRE en una sola oración corta (<=30 palabras)."
        )

    def process(self, call_id: str, user_text: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Método principal que debe coincidir con la llamada en app.py:
        assistant.process(call_id=call_sid, user_text=speech_result, context=state.get("context", {}))
        
        Retorna: {
            "say_text": str,
            "actions": [{"type": "get_slots", "args": {...}}, {"type": "schedule", "args": {...}}],
            "end_call": bool
        }
        """
        try:
            logger.info(f"[{call_id}] OpenAI Assistant procesando: {user_text!r}")
            
            # Obtener estado del contexto
            nombre = context.get("nombre_paciente") or "Cliente"
            user_norm = _norm(user_text)

            # Si no hay texto (silencio), repreguntar
            if not user_norm:
                return {
                    "say_text": "¿Podrías repetir por favor? ¿Deseas agendar una cita?",
                    "actions": [],
                    "end_call": False
                }

            # Detectar intención sí/no
            yn = _coerce_yesno(user_norm)
            logger.info(f"[{call_id}] Intención detectada: {yn}")

            # Si dice que sí -> solicitar slots
            if yn is True:
                logger.info(f"[{call_id}] Usuario quiere agendar, solicitando slots...")
                return {
                    "say_text": f"Perfecto {nombre}. Déjame consultar los horarios disponibles.",
                    "actions": [{"type": "get_slots", "args": {"doctor": None}}],
                    "end_call": False
                }

            # Si dice que no -> despedirse
            if yn is False:
                logger.info(f"[{call_id}] Usuario no quiere agendar, despidiendo...")
                return {
                    "say_text": f"Entiendo {nombre}. Cuando desees agendar, con gusto te ayudamos. ¡Buen día!",
                    "actions": [],
                    "end_call": True
                }

            # Si parece que está eligiendo un slot (contiene números o días)
            if any(word in user_norm for word in ["primero", "segundo", "tercero", "1", "2", "3", "lunes", "martes", "miércoles", "jueves", "viernes"]):
                logger.info(f"[{call_id}] Usuario parece estar eligiendo slot...")
                # Intentar extraer índice
                slot_index = self._extract_slot_choice(user_text)
                if slot_index is not None:
                    return {
                        "say_text": f"Perfecto {nombre}. Agendando tu cita...",
                        "actions": [{"type": "schedule", "args": {"index": slot_index}}],
                        "end_call": False
                    }

            # Caso general: usar LLM para responder
            try:
                logger.info(f"[{call_id}] Consultando OpenAI para respuesta general...")
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": f"Usuario: {user_text}. Responde en <=30 palabras."}
                    ],
                    temperature=0.4, 
                    max_tokens=60
                )
                say_text = (resp.choices[0].message.content or "").strip()
                logger.info(f"[{call_id}] OpenAI respuesta: {say_text}")
            except Exception as e:
                logger.warning(f"[{call_id}] LLM texto general falló: {e}")
                say_text = "Gracias por tu mensaje. ¿Te gustaría agendar una cita?"

            return {
                "say_text": say_text,
                "actions": [],
                "end_call": False
            }

        except Exception as e:
            logger.error(f"[{call_id}] Assistant.process error: {e}")
            return {
                "say_text": "Disculpa, tuve un inconveniente. ¿Podrías repetir por favor?",
                "actions": [],
                "end_call": False
            }

    def _extract_slot_choice(self, user_text: str) -> Optional[int]:
        """Extrae el índice de slot que el usuario eligió (0, 1, 2...)"""
        t = _norm(user_text)
        
        # Buscar números directos
        if "1" in t or "primero" in t or "primera" in t:
            return 0
        if "2" in t or "segundo" in t or "segunda" in t:
            return 1
        if "3" in t or "tercero" in t or "tercera" in t:
            return 2
            
        return None

    def format_slots_for_speech(self, slots: List[Dict[str, Any]]) -> str:
        """
        Formatea los slots disponibles para ser leídos por voz.
        Método requerido por app.py en la línea: assistant.format_slots_for_speech(slots)
        """
        if not slots:
            return "No hay horarios disponibles en este momento."
        
        if len(slots) == 1:
            slot = slots[0]
            return f"Tengo disponible {slot.get('texto', 'un horario')} con {slot.get('doctor', 'el doctor')}. ¿Te parece bien?"
        
        # Múltiples slots
        opciones = []
        for i, slot in enumerate(slots[:3]):  # Máximo 3 opciones
            num = ["primera", "segunda", "tercera"][i]
            opciones.append(f"{num} opción: {slot.get('texto', '')} con {slot.get('doctor', '')}")
        
        return f"Tengo estas opciones: {', '.join(opciones)}. ¿Cuál prefieres?"