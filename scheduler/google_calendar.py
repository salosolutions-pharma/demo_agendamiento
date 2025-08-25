import os
import logging
import datetime
import random
import pytz
from typing import Optional, List, Dict, Any
from google.oauth2 import service_account
from googleapiclient.discovery import build
from .base import BaseSchedulerProvider

logger = logging.getLogger(__name__)

class GoogleCalendarScheduler(BaseSchedulerProvider):
    """Proveedor de agendamiento usando Google Calendar - Completamente inteligente"""
    
    def __init__(self, calendar_id: str = None, credentials_file: str = None):
        # Configuración desde variables de entorno
        self.calendar_id = calendar_id or os.getenv("CALENDAR_ID")
        self.credentials_file = credentials_file or os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        self.timezone = pytz.timezone('America/Bogota')  # UTC-5
        
        # Configuración de horarios de atención (completamente configurable)
        self.hora_inicio_atencion = int(os.getenv("HORA_INICIO_ATENCION", "9"))  # 9 AM
        self.hora_fin_atencion = int(os.getenv("HORA_FIN_ATENCION", "16"))  # 4 PM (última cita 4:30)
        self.minutos_fin_atencion = int(os.getenv("MINUTOS_FIN_ATENCION", "30"))  # 30 min
        self.duracion_cita_minutos = int(os.getenv("DURACION_CITA_MINUTOS", "30"))  # 30 min por cita
        self.intervalo_citas_minutos = int(os.getenv("INTERVALO_CITAS_MINUTOS", "30"))  # Cada 30 min
        
        # Días de atención (lunes=0, viernes=4)
        self.dias_atencion = [0, 1, 2, 3, 4]  # Lunes a Viernes
        
        # Doctores disponibles (configurable desde env o base de datos)
        doctores_env = os.getenv("DOCTORES_DISPONIBLES", "Dr. Martínez,Dra. Rodríguez,Dr. González")
        self.doctores = [doc.strip() for doc in doctores_env.split(",")]
        
        # Validar configuración
        if not self.calendar_id:
            raise ValueError("CALENDAR_ID debe estar configurado en las variables de entorno")
        
        logger.info(f"📅 Configuración Calendar:")
        logger.info(f"  - Calendar ID: {self.calendar_id}")
        logger.info(f"  - Horario: {self.hora_inicio_atencion}:00 - {self.hora_fin_atencion}:{self.minutos_fin_atencion:02d}")
        logger.info(f"  - Duración citas: {self.duracion_cita_minutos} min")
        logger.info(f"  - Doctores: {self.doctores}")
    
    def _generate_time_slots(self, fecha_dia: datetime.datetime) -> List[Dict[str, Any]]:
        """Genera todos los slots de tiempo posibles para un día específico"""
        slots = []
        
        # Empezar desde la hora de inicio
        hora_actual = fecha_dia.replace(
            hour=self.hora_inicio_atencion, 
            minute=0, 
            second=0, 
            microsecond=0
        )
        
        # Hora límite (última cita posible)
        hora_limite = fecha_dia.replace(
            hour=self.hora_fin_atencion,
            minute=self.minutos_fin_atencion,
            second=0,
            microsecond=0
        )
        
        # Generar slots cada intervalo_citas_minutos
        while hora_actual <= hora_limite:
            slots.append({
                'hora_inicio': hora_actual,
                'hora_fin': hora_actual + datetime.timedelta(minutes=self.duracion_cita_minutos)
            })
            hora_actual += datetime.timedelta(minutes=self.intervalo_citas_minutos)
        
        return slots
    
    def _get_calendar_service(self):
        """Obtiene un servicio autenticado para Google Calendar"""
        try:
            scopes = ['https://www.googleapis.com/auth/calendar']
            credentials = service_account.Credentials.from_service_account_file(
                self.credentials_file, scopes=scopes)
            service = build('calendar', 'v3', credentials=credentials)
            return service
        except Exception as e:
            logger.error(f"Error al configurar servicio de Google Calendar: {e}")
            return None
    
    def get_available_appointments(self, days_ahead: int = 5) -> List[Dict[str, Any]]:
        """Obtiene citas disponibles de forma completamente inteligente"""
        try:
            service = self._get_calendar_service()
            if not service:
                logger.warning("No se pudo conectar con Google Calendar. Usando horarios predeterminados.")
                return self._get_intelligent_default_appointments()
            
            # Trabajar en zona horaria de Colombia (UTC-5)
            ahora = datetime.datetime.now(self.timezone)
            tiempo_fin = ahora + datetime.timedelta(days=days_ahead)
            
            logger.info(f"🔍 Buscando disponibilidad desde {ahora.strftime('%Y-%m-%d %H:%M')} hasta {tiempo_fin.strftime('%Y-%m-%d %H:%M')}")
            
            # Obtener TODOS los eventos existentes en el rango
            eventos = service.events().list(
                calendarId=self.calendar_id,
                timeMin=ahora.isoformat(),
                timeMax=tiempo_fin.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            eventos_lista = eventos.get('items', [])
            logger.info(f"📅 Eventos encontrados en calendar: {len(eventos_lista)}")
            
            # Procesar TODOS los eventos ocupados
            eventos_ocupados = []
            for evento in eventos_lista:
                inicio = evento['start'].get('dateTime', evento['start'].get('date'))
                fin = evento['end'].get('dateTime', evento['end'].get('date'))
                titulo = evento.get('summary', 'Sin título')
                
                # Solo procesar eventos con hora específica (no todo el día)
                if 'T' in inicio:
                    try:
                        inicio_dt = datetime.datetime.fromisoformat(inicio.replace('Z', '+00:00'))
                        fin_dt = datetime.datetime.fromisoformat(fin.replace('Z', '+00:00'))
                        
                        # Convertir a zona horaria de Colombia
                        inicio_dt = inicio_dt.astimezone(self.timezone)
                        fin_dt = fin_dt.astimezone(self.timezone)
                        
                        eventos_ocupados.append({
                            'inicio': inicio_dt, 
                            'fin': fin_dt,
                            'titulo': titulo
                        })
                        
                        logger.info(f"  📋 Ocupado: {inicio_dt.strftime('%a %d/%m %H:%M')} - {fin_dt.strftime('%H:%M')} ({titulo})")
                    except Exception as e:
                        logger.error(f"❌ Error al procesar evento: {e}")
            
            # Generar TODOS los horarios disponibles de forma inteligente
            horarios_disponibles = []
            
            # Para cada día en el rango
            for dia_offset in range(days_ahead + 1):
                dia_actual = (ahora + datetime.timedelta(days=dia_offset)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                
                # Solo días de atención (lunes a viernes por defecto)
                if dia_actual.weekday() not in self.dias_atencion:
                    continue
                
                # Si es el día actual, no considerar horarios que ya pasaron
                if dia_offset == 0:
                    # Para hoy, empezar desde la próxima hora disponible
                    hora_minima = ahora + datetime.timedelta(hours=1)  # Al menos 1 hora de anticipación
                    hora_minima = hora_minima.replace(minute=0, second=0, microsecond=0)
                    if hora_minima.minute > 0:
                        hora_minima = hora_minima.replace(minute=30 if hora_minima.minute <= 30 else 0)
                        if hora_minima.minute == 0:
                            hora_minima += datetime.timedelta(hours=1)
                else:
                    hora_minima = None
                
                # Generar slots de tiempo para este día
                slots_del_dia = self._generate_time_slots(dia_actual)
                
                logger.info(f"📅 Analizando {dia_actual.strftime('%A %d/%m/%Y')} - {len(slots_del_dia)} slots posibles")
                
                for slot in slots_del_dia:
                    hora_inicio = slot['hora_inicio']
                    hora_fin = slot['hora_fin']
                    
                    # Asegurar zona horaria
                    if hora_inicio.tzinfo is None:
                        hora_inicio = self.timezone.localize(hora_inicio)
                        hora_fin = self.timezone.localize(hora_fin)
                    
                    # Saltar si es muy pronto (mismo día)
                    if hora_minima and hora_inicio < hora_minima:
                        continue
                    
                    # Verificar si está disponible (no hay conflictos)
                    disponible = True
                    conflicto_con = None
                    
                    for evento in eventos_ocupados:
                        # Verificar solapamiento: (inicio1 < fin2) AND (fin1 > inicio2)
                        if (hora_inicio < evento['fin'] and hora_fin > evento['inicio']):
                            disponible = False
                            conflicto_con = evento['titulo']
                            break
                    
                    if disponible:
                        # Formatear información del horario
                        dia_semana = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", 
                                      "Sábado", "Domingo"][hora_inicio.weekday()]
                        
                        fecha_mostrar = f"{hora_inicio.day}/{hora_inicio.month}/{hora_inicio.year}"
                        
                        # Formato 12h
                        if hora_inicio.hour < 12:
                            hora_str = f"{hora_inicio.hour}:{hora_inicio.minute:02d} AM"
                        elif hora_inicio.hour == 12:
                            hora_str = f"12:{hora_inicio.minute:02d} PM"
                        else:
                            hora_str = f"{hora_inicio.hour-12}:{hora_inicio.minute:02d} PM"
                        
                        # Asignar doctor de forma inteligente (rotativo)
                        doctor = self.doctores[len(horarios_disponibles) % len(self.doctores)]
                        
                        horario_info = {
                            'fecha_hora': hora_inicio,
                            'texto': f"{dia_semana} {fecha_mostrar} a las {hora_str}",
                            'doctor': doctor,
                            'fecha_mostrar': fecha_mostrar,
                            'iso_inicio': hora_inicio.isoformat(),
                            'iso_fin': hora_fin.isoformat(),
                            'dia_semana': dia_semana,
                            'hora_12h': hora_str
                        }
                        
                        horarios_disponibles.append(horario_info)
                        
                        logger.info(f"  ✅ Disponible: {dia_semana} {fecha_mostrar} {hora_str} con {doctor}")
                    else:
                        logger.info(f"  ❌ Ocupado: {hora_inicio.strftime('%H:%M')} (conflicto con: {conflicto_con})")
            
            logger.info(f"🎯 Total horarios disponibles encontrados: {len(horarios_disponibles)}")
            
            if not horarios_disponibles:
                logger.warning("⚠️ No se encontraron horarios disponibles en el calendario")
                return self._get_intelligent_default_appointments()
            
            # Seleccionar los mejores horarios de forma inteligente
            return self._select_best_appointments(horarios_disponibles)
            
        except Exception as e:
            logger.error(f"❌ Error al obtener horarios disponibles: {e}")
            return self._get_intelligent_default_appointments()
    
    def _select_best_appointments(self, horarios_disponibles: List[Dict]) -> List[Dict]:
        """Selecciona los mejores horarios de forma inteligente"""
        # Agrupar por día
        horarios_por_dia = {}
        for horario in horarios_disponibles:
            fecha_clave = horario['fecha_hora'].strftime("%Y-%m-%d")
            if fecha_clave not in horarios_por_dia:
                horarios_por_dia[fecha_clave] = []
            horarios_por_dia[fecha_clave].append(horario)
        
        horarios_seleccionados = []
        
        # Seleccionar hasta 3 días diferentes
        for fecha, horarios_del_dia in list(horarios_por_dia.items())[:3]:
            # Preferir horarios de la mañana (9-12) y tarde temprana (14-16)
            horarios_preferidos = []
            horarios_otros = []
            
            for horario in horarios_del_dia:
                hora = horario['fecha_hora'].hour
                if 9 <= hora <= 12 or 14 <= hora <= 16:
                    horarios_preferidos.append(horario)
                else:
                    horarios_otros.append(horario)
            
            # Elegir de los preferidos si existen, sino de los otros
            if horarios_preferidos:
                horario_elegido = random.choice(horarios_preferidos)
            elif horarios_otros:
                horario_elegido = random.choice(horarios_otros)
            else:
                continue
            
            horarios_seleccionados.append(horario_elegido)
        
        # Si tenemos menos de 3, completar con más opciones del mismo día
        if len(horarios_seleccionados) < 3:
            for horario in horarios_disponibles:
                if horario not in horarios_seleccionados:
                    horarios_seleccionados.append(horario)
                    if len(horarios_seleccionados) >= 3:
                        break
        
        # Aleatorizar el orden final
        random.shuffle(horarios_seleccionados)
        
        logger.info(f"🎯 Horarios seleccionados para ofrecer: {len(horarios_seleccionados)}")
        for h in horarios_seleccionados:
            logger.info(f"  📅 {h['texto']} con {h['doctor']}")
        
        return horarios_seleccionados[:3]  # Máximo 3 opciones
    
    def _get_intelligent_default_appointments(self) -> List[Dict[str, Any]]:
        """Retorna horarios predeterminados inteligentes en UTC-5"""
        hoy = datetime.datetime.now(self.timezone)
        
        # Encontrar los próximos días laborables
        dias_laborables = []
        dia_actual = hoy
        while len(dias_laborables) < 5:  # Buscar en más días para tener opciones
            dia_actual = dia_actual + datetime.timedelta(days=1)
            if dia_actual.weekday() in self.dias_atencion:
                dias_laborables.append(dia_actual)
        
        # Generar horarios usando la misma lógica inteligente
        horarios_todos = []
        for dia in dias_laborables:
            slots_del_dia = self._generate_time_slots(dia)
            
            for slot in slots_del_dia:
                hora_inicio = slot['hora_inicio']
                hora_fin = slot['hora_fin']
                
                if hora_inicio.tzinfo is None:
                    hora_inicio = self.timezone.localize(hora_inicio)
                    hora_fin = self.timezone.localize(hora_fin)
                
                # Formatear información
                dia_semana = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", 
                              "Sábado", "Domingo"][hora_inicio.weekday()]
                
                fecha_mostrar = f"{hora_inicio.day}/{hora_inicio.month}/{hora_inicio.year}"
                
                # Formato 12h
                if hora_inicio.hour < 12:
                    hora_str = f"{hora_inicio.hour}:{hora_inicio.minute:02d} AM"
                elif hora_inicio.hour == 12:
                    hora_str = f"12:{hora_inicio.minute:02d} PM"
                else:
                    hora_str = f"{hora_inicio.hour-12}:{hora_inicio.minute:02d} PM"
                
                doctor = self.doctores[len(horarios_todos) % len(self.doctores)]
                
                horarios_todos.append({
                    'fecha_hora': hora_inicio,
                    'texto': f"{dia_semana} {fecha_mostrar} a las {hora_str}",
                    'doctor': doctor,
                    'fecha_mostrar': fecha_mostrar,
                    'iso_inicio': hora_inicio.isoformat(),
                    'iso_fin': hora_fin.isoformat(),
                    'dia_semana': dia_semana,
                    'hora_12h': hora_str
                })
        
        # Usar la misma lógica de selección inteligente
        return self._select_best_appointments(horarios_todos)
    
    def create_appointment(self, nombre: str, fecha_inicio: str, fecha_fin: str, 
                          doctor: str, telefono: str) -> Optional[str]:
        """Crea evento en Google Calendar"""
        try:
            service = self._get_calendar_service()
            if not service:
                logger.error("No se pudo crear el evento: error de conexión con Google Calendar")
                return None
            
            inicio_dt = datetime.datetime.fromisoformat(fecha_inicio.replace('Z', '+00:00'))
            fin_dt = datetime.datetime.fromisoformat(fecha_fin.replace('Z', '+00:00'))
            
            # Asegurar zona horaria Colombia
            if inicio_dt.tzinfo is None:
                inicio_dt = self.timezone.localize(inicio_dt)
            else:
                inicio_dt = inicio_dt.astimezone(self.timezone)
                
            if fin_dt.tzinfo is None:
                fin_dt = self.timezone.localize(fin_dt)
            else:
                fin_dt = fin_dt.astimezone(self.timezone)
            
            evento = {
                'summary': f'Cita - {nombre}',
                'description': f'Paciente: {nombre}\nDoctor: {doctor}\nTeléfono: {telefono}',
                'start': {
                    'dateTime': inicio_dt.isoformat(),
                    'timeZone': 'America/Bogota',
                },
                'end': {
                    'dateTime': fin_dt.isoformat(),
                    'timeZone': 'America/Bogota',
                },
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'email', 'minutes': 24 * 60},
                        {'method': 'popup', 'minutes': 30},
                    ],
                },
            }
            
            evento = service.events().insert(calendarId=self.calendar_id, body=evento).execute()
            logger.info(f"✅ Evento creado en Google Calendar: {evento.get('htmlLink')}")
            return evento.get('id')
            
        except Exception as e:
            logger.error(f"❌ Error al crear evento en Google Calendar: {e}")
            return None
    
    def get_provider_name(self) -> str:
        return "google_calendar"