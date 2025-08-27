# openia.py
# Asistente conversacional con Tool Calling (Contrato A)

import os
import json
import logging
from typing import Optional, Dict, Any, List

from openai import OpenAI

logger = logging.getLogger(__name__)


def _norm(t: Optional[str]) -> str:
    return (t or "").strip().lower()


def _limit_words(text: str, max_words: int = 150) -> str:
    words = (text or "").split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",.;:¡!¿?") + "..."


class OpenAIConversationAssistant:
    """
    Tool-calling puro (Contrato A):
      Tools: get_slots, answer_faq, schedule
      Respuesta:
        {
          "say_text": str,
          "actions": [{"type":"schedule", ...}] (opcional),
          "slots": [ ... ] (opcional si ofreció opciones),
          "end_call": bool
        }
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        api_key = api_key or os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY no configurada.")
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None

        self.client = OpenAI(api_key=api_key) if not base_url else OpenAI(api_key=api_key, base_url=base_url)
        _ = self.client.models.list()  # smoke test

        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        self.faq = {
            "direccion": "Calle 123 #45-67, Medellín (Barrio Laureles).",
            "sede": "Nuestra sede principal está en Laureles, Medellín.",
            "horario": "Lunes a viernes de 9:00 a 16:30 (última cita inicia 16:00).",
            "telefono": "+57 314 000 0000",
            "whatsapp": "+57 314 000 0000 (solo mensajes).",
            "parqueadero": "Sí, contamos con parqueadero propio (cupo limitado).",
        }

        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_slots",
                    "description": "Consulta hasta 3 horarios recomendados para ofrecer al usuario.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "days_ahead": {"type": "integer", "description": "Días hacia adelante a buscar", "default": 5},
                            "doctor_hint": {"type": "string", "description": "Preferencia de doctor", "nullable": True}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "answer_faq",
                    "description": "Responde dirección, sede, horario, teléfono, WhatsApp, parqueadero.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Pregunta del usuario"}
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule",
                    "description": "Pide al backend agendar por índice (0..2) o por fecha/hora ISO explícita.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "nullable": True},
                            "iso_inicio": {"type": "string", "nullable": True},
                            "iso_fin": {"type": "string", "nullable": True}
                        }
                    }
                }
            }
        ]

        self.system_prompt = (
            "Eres Salomé (español colombiano), amable, cordial y breve. Objetivo: agendar cita.\n"
            "CONTEXTO IMPORTANTE: Si hay un 'nombre_paciente' en el contexto, ESE ES EL PACIENTE que llama, NO un doctor.\n"
            "Los doctores disponibles son los que aparecen en los slots (Dr. Martínez, etc.).\n"
            "NUNCA confundas el nombre del paciente con el nombre de un doctor.\n"
            "\n"
            "Puedes usar funciones cuando lo necesites:\n"
            " - get_slots: consulta horarios reales y ofrece 2-3 opciones claras.\n"
            " - answer_faq: responde dirección/horarios/teléfono/WhatsApp/parqueadero; tras responder, vuelve a ofrecer agendamiento.\n"
            " - schedule: confirma la cita (por índice de las opciones ofrecidas o por ISO si el usuario lo especifica).\n"
            "\n"
            "Reglas:\n"
            " - Responde en UNA sola oración corta (≤150 palabras), natural, cordial y concreta.\n"
            " - Si el usuario ya eligió horario (por índice, día/hora o frase libre), intenta llamar 'schedule'.\n"
            " - Tras confirmar con 'schedule', responde con un tono cercano y tranquilizador.\n"
            " - Si el usuario habla de algo irrelevante, responde amablemente y guíalo hacia el agendamiento.\n"
            " - Si pregunta algo dentro de FAQs, usa 'answer_faq' y retoma el agendamiento.\n"
            " - Si no quiere agendar, despídete cordialmente y termina.\n"
            "\n"
            "Estilo de fecha/hora:\n"
            " - NUNCA leas fechas en formato numérico; convierte a natural: 'martes 26 de agosto a las 8:00 a. m.'\n"
            " - Usa meses en palabras (enero… diciembre) y reloj de 12 horas con 'a. m.' / 'p. m.'\n"
        )

    # ---------------- Tool handlers (backend) ----------------

    def _tool_get_slots(self, calendar, days_ahead: int = 5, doctor_hint: Optional[str] = None) -> Dict[str, Any]:
        slots = calendar.get_available_appointments(days_ahead=days_ahead)
        simple = [
            {
                "texto": s.get("texto"),
                "doctor": s.get("doctor"),
                "iso_inicio": s.get("iso_inicio"),
                "iso_fin": s.get("iso_fin"),
            }
            for s in (slots or [])
        ]
        return {"slots": simple}

    def _tool_answer_faq(self, query: str) -> Dict[str, Any]:
        q = _norm(query)
        if any(k in q for k in ["dirección", "direccion", "ubica", "ubicación", "dónde", "donde", "cómo llegar", "como llegar"]):
            a = f"{self.faq['direccion']}"
        elif any(k in q for k in ["sede"]):
            a = f"{self.faq['sede']}"
        elif any(k in q for k in ["horario", "hora", "atienden", "sabado", "sábado", "sábados", "sabados"]):
            a = f"{self.faq['horario']}"
        elif any(k in q for k in ["tel", "telefono", "teléfono", "whatsapp", "wasap", "cel", "celular"]):
            a = f"Tel: {self.faq['telefono']} · WhatsApp: {self.faq['whatsapp']}"
        elif any(k in q for k in ["parqueadero", "parqueo", "parquear"]):
            a = f"{self.faq['parqueadero']}"
        else:
            a = "Puedo ayudarte con dirección, horarios, teléfonos, WhatsApp y parqueadero."
        return {"answer": a}

    def _tool_schedule(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"action": {
            "type": "schedule",
            "index": args.get("index"),
            "iso_inicio": args.get("iso_inicio"),
            "iso_fin": args.get("iso_fin"),
        }}

    # ---------------- Orquestación principal ----------------

    def process(self, call_id: str, user_text: str, context: Dict[str, Any], calendar=None) -> Dict[str, Any]:
        nombre_paciente = (context or {}).get("nombre_paciente") or "Cliente"
        history: List[Dict[str, str]] = (context or {}).get("history", [])
        offered_slots = (context or {}).get("slots", [])

        messages = [{"role": "system", "content": self.system_prompt}]
        
        # Agregar contexto explícito sobre el paciente ANTES del historial
        if nombre_paciente and nombre_paciente != "Cliente":
            messages.append({
                "role": "system", 
                "content": f"CONTEXTO: El paciente que llama se llama {nombre_paciente}. Este NO es un doctor, es el PACIENTE que necesita agendar una cita."
            })
        
        # Agregar historial
        for h in history[-2:]:
            if u := h.get("user"):
                messages.append({"role": "user", "content": u})
            if a := h.get("assistant"):
                messages.append({"role": "assistant", "content": a})
        
        # Mensaje actual del usuario
        messages.append({"role": "user", "content": f"{nombre_paciente}: {user_text}"})

        if offered_slots:
            messages.append({
                "role": "system",
                "content": f"Slots_ofrecidos_actualmente={json.dumps(offered_slots, ensure_ascii=False)}"
            })

        say_text, actions, end_call = None, [], False
        tool_runs = 0
        cache: Dict[str, Any] = {}

        while tool_runs < 3:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.4,
                tools=self.tools,
                tool_choice="auto",
                max_tokens=200,
            )
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            if tool_calls:
                tool_runs += 1

                # 🔴🔴🔴 IMPORTANTE: añade el MENSAJE DEL ASISTENTE con sus tool_calls
                # para que los siguientes mensajes role="tool" sean válidos
                assistant_with_tools = {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in tool_calls
                    ],
                }
                messages.append(assistant_with_tools)  # <-- IMPORTANTE: registrar assistant con tool_calls

                # Ejecutar cada tool_call y responder con role="tool"
                for tc in tool_calls:
                    fname = tc.function.name
                    fargs = json.loads(tc.function.arguments or "{}")

                    if fname == "get_slots":
                        if "get_slots" not in cache:
                            cache["get_slots"] = self._tool_get_slots(calendar, **fargs)
                        result = cache["get_slots"]
                        offered_slots = result.get("slots", [])
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": fname,
                            "content": json.dumps(result, ensure_ascii=False)
                        })

                    elif fname == "answer_faq":
                        result = self._tool_answer_faq(fargs.get("query", ""))
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": fname,
                            "content": json.dumps(result, ensure_ascii=False)
                        })

                    elif fname == "schedule":
                        result = self._tool_schedule(fargs)
                        actions.append(result["action"])
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": fname,
                            "content": json.dumps({"ok": True, "queued": True}, ensure_ascii=False)
                        })

                # Iteración siguiente: el modelo verá los tool-results y redactará
                continue

            # Sin más tools: ya es el mensaje final
            candidate = (msg.content or "").strip()
            if candidate:
                say_text = _limit_words(candidate, 150)
            break

        if say_text and any(x in _norm(say_text) for x in ["hasta luego", "gracias", "feliz día", "buen día"]):
            end_call = True

        reply: Dict[str, Any] = {
            "say_text": say_text or "¿Te gustaría agendar una cita? Puedo proponerte horarios.",
            "actions": actions,
            "end_call": end_call
        }
        if offered_slots:
            reply["slots"] = offered_slots

        logger.info(f"[{call_id}] reply={ {k: (v if k!='slots' else f'{len(v)} slots') for k,v in reply.items()} }")
        return reply
