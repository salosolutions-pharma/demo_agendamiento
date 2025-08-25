import os
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from google.cloud import bigquery
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

class BigQueryStorage:
    """Almacenamiento de citas en BigQuery"""
    
    def __init__(self, credentials_file: str = None, project_id: str = None, 
                 dataset_id: str = None, table_id: str = None):
        
        # Configuración desde variables de entorno
        self.credentials_file = credentials_file or os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        self.project_id = project_id or os.getenv("BIGQUERY_PROJECT_ID")
        self.dataset_id = dataset_id or os.getenv("BIGQUERY_DATASET_ID", "citas_medicas")
        self.table_id = table_id or os.getenv("BIGQUERY_TABLE_ID", "agendamiento_citas")
        
        # Validar configuración
        if not self.project_id:
            raise ValueError("BIGQUERY_PROJECT_ID debe estar configurado en las variables de entorno")
        
        # Inicializar cliente BigQuery
        try:
            if os.path.exists(self.credentials_file):
                credentials = service_account.Credentials.from_service_account_file(self.credentials_file)
                self.client = bigquery.Client(credentials=credentials, project=self.project_id)
            else:
                # Usar credenciales por defecto del ambiente (útil en Google Cloud)
                self.client = bigquery.Client(project=self.project_id)
            
            logger.info(f"📊 BigQuery configurado:")
            logger.info(f"  - Project: {self.project_id}")
            logger.info(f"  - Dataset: {self.dataset_id}")
            logger.info(f"  - Table: {self.table_id}")
            
            # Crear dataset y tabla si no existen
            self._setup_table()
            
        except Exception as e:
            logger.error(f"❌ Error configurando BigQuery: {e}")
            raise
    
    def _setup_table(self):
        """Crea el dataset y tabla si no existen"""
        try:
            # Crear dataset si no existe
            dataset_ref = self.client.dataset(self.dataset_id)
            try:
                self.client.get_dataset(dataset_ref)
                logger.info(f"✅ Dataset {self.dataset_id} ya existe")
            except:
                dataset = bigquery.Dataset(dataset_ref)
                dataset.location = "us-central1"  
                dataset.description = "Dataset para almacenar citas médicas agendadas"
                dataset = self.client.create_dataset(dataset)
                logger.info(f"✅ Dataset {self.dataset_id} creado")
            
            # Crear tabla si no existe
            table_ref = dataset_ref.table(self.table_id)
            try:
                self.client.get_table(table_ref)
                logger.info(f"✅ Tabla {self.table_id} ya existe")
            except:
                # Definir esquema de la tabla
                schema = [
                    bigquery.SchemaField("id", "STRING", mode="REQUIRED", description="ID único de la cita"),
                    bigquery.SchemaField("nombre_paciente", "STRING", mode="REQUIRED", description="Nombre del paciente"),
                    bigquery.SchemaField("telefono_paciente", "STRING", mode="REQUIRED", description="Teléfono del paciente"),
                    bigquery.SchemaField("doctor_asignado", "STRING", mode="REQUIRED", description="Doctor asignado"),
                    bigquery.SchemaField("fecha_cita", "DATETIME", mode="REQUIRED", description="Fecha y hora de la cita"),
                    bigquery.SchemaField("duracion_minutos", "INTEGER", mode="REQUIRED", description="Duración en minutos"),
                    bigquery.SchemaField("estado_cita", "STRING", mode="REQUIRED", description="Estado: agendada, confirmada, cancelada, completada"),
                    bigquery.SchemaField("fecha_agendamiento", "DATETIME", mode="REQUIRED", description="Cuándo se agendó la cita"),
                    bigquery.SchemaField("canal_agendamiento", "STRING", mode="REQUIRED", description="Canal: llamada_automatica, web, manual"),
                    bigquery.SchemaField("call_id", "STRING", mode="NULLABLE", description="ID de la llamada (si aplica)"),
                    bigquery.SchemaField("calendar_event_id", "STRING", mode="NULLABLE", description="ID del evento en Google Calendar"),
                    bigquery.SchemaField("notas", "STRING", mode="NULLABLE", description="Notas adicionales"),
                    bigquery.SchemaField("fecha_actualizacion", "DATETIME", mode="REQUIRED", description="Última actualización"),
                ]
                
                table = bigquery.Table(table_ref, schema=schema)
                table.description = "Tabla para almacenar todas las citas médicas agendadas"
                table = self.client.create_table(table)
                logger.info(f"✅ Tabla {self.table_id} creada con esquema completo")
        
        except Exception as e:
            logger.error(f"❌ Error configurando tabla BigQuery: {e}")
            raise
    
    def save_appointment(self, 
                        nombre_paciente: str,
                        telefono_paciente: str,
                        doctor_asignado: str,
                        fecha_cita_iso: str,
                        duracion_minutos: int = 30,
                        call_id: str = None,
                        calendar_event_id: str = None,
                        notas: str = None) -> Optional[str]:
        """
        Guarda una nueva cita en BigQuery
        
        Args:
            nombre_paciente: Nombre del paciente
            telefono_paciente: Teléfono del paciente
            doctor_asignado: Doctor asignado
            fecha_cita_iso: Fecha de la cita en formato ISO
            duracion_minutos: Duración en minutos
            call_id: ID de la llamada (opcional)
            calendar_event_id: ID del evento en Google Calendar (opcional)
            notas: Notas adicionales (opcional)
            
        Returns:
            str: ID único de la cita creada, None si hay error
        """
        try:
            # Generar ID único
            import uuid
            cita_id = f"cita_{uuid.uuid4().hex[:12]}"
            
            # Convertir fecha ISO a datetime
            fecha_cita_dt = datetime.fromisoformat(fecha_cita_iso.replace('Z', '+00:00'))
            ahora = datetime.utcnow()
            
            # Preparar datos para insertar
            rows_to_insert = [{
                "id": cita_id,
                "nombre_paciente": nombre_paciente,
                "telefono_paciente": telefono_paciente,
                "doctor_asignado": doctor_asignado,
                "fecha_cita": fecha_cita_dt.strftime('%Y-%m-%d %H:%M:%S'),
                "duracion_minutos": duracion_minutos,
                "estado_cita": "agendada",
                "fecha_agendamiento": ahora.strftime('%Y-%m-%d %H:%M:%S'),
                "canal_agendamiento": "llamada_automatica",
                "call_id": call_id,
                "calendar_event_id": calendar_event_id,
                "notas": notas or f"Cita agendada automáticamente para {nombre_paciente}",
                "fecha_actualizacion": ahora.strftime('%Y-%m-%d %H:%M:%S')
            }]
            
            # Insertar en BigQuery
            table_ref = self.client.dataset(self.dataset_id).table(self.table_id)
            table = self.client.get_table(table_ref)
            
            errors = self.client.insert_rows_json(table, rows_to_insert)
            
            if errors:
                logger.error(f"❌ Errores al insertar en BigQuery: {errors}")
                return None
            
            logger.info(f"✅ Cita guardada en BigQuery con ID: {cita_id}")
            logger.info(f"  - Paciente: {nombre_paciente}")
            logger.info(f"  - Doctor: {doctor_asignado}")
            logger.info(f"  - Fecha: {fecha_cita_dt.strftime('%Y-%m-%d %H:%M')}")
            
            return cita_id
            
        except Exception as e:
            logger.error(f"❌ Error guardando cita en BigQuery: {e}")
            return None
    
    def get_appointment(self, cita_id: str) -> Optional[Dict[str, Any]]:
        """Obtiene una cita por su ID"""
        try:
            query = f"""
            SELECT *
            FROM `{self.project_id}.{self.dataset_id}.{self.table_id}`
            WHERE id = @cita_id
            LIMIT 1
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("cita_id", "STRING", cita_id)
                ]
            )
            
            query_job = self.client.query(query, job_config=job_config)
            results = query_job.result()
            
            for row in results:
                return dict(row)
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Error obteniendo cita de BigQuery: {e}")
            return None
    
    def update_appointment_status(self, cita_id: str, nuevo_estado: str, 
                                 notas: str = None) -> bool:
        """Actualiza el estado de una cita"""
        try:
            ahora = datetime.utcnow()
            
            query = f"""
            UPDATE `{self.project_id}.{self.dataset_id}.{self.table_id}`
            SET estado_cita = @nuevo_estado,
                notas = COALESCE(@notas, notas),
                fecha_actualizacion = @fecha_actualizacion
            WHERE id = @cita_id
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("cita_id", "STRING", cita_id),
                    bigquery.ScalarQueryParameter("nuevo_estado", "STRING", nuevo_estado),
                    bigquery.ScalarQueryParameter("notas", "STRING", notas),
                    bigquery.ScalarQueryParameter("fecha_actualizacion", "DATETIME", ahora)
                ]
            )
            
            query_job = self.client.query(query, job_config=job_config)
            query_job.result()  # Esperar a que termine
            
            logger.info(f"✅ Estado de cita {cita_id} actualizado a: {nuevo_estado}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error actualizando estado de cita: {e}")
            return False
    
    def get_appointments_by_date_range(self, fecha_inicio: str, fecha_fin: str) -> List[Dict[str, Any]]:
        """Obtiene citas en un rango de fechas"""
        try:
            query = f"""
            SELECT *
            FROM `{self.project_id}.{self.dataset_id}.{self.table_id}`
            WHERE fecha_cita BETWEEN @fecha_inicio AND @fecha_fin
            ORDER BY fecha_cita ASC
            """
            
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("fecha_inicio", "DATETIME", fecha_inicio),
                    bigquery.ScalarQueryParameter("fecha_fin", "DATETIME", fecha_fin)
                ]
            )
            
            query_job = self.client.query(query, job_config=job_config)
            results = query_job.result()
            
            citas = []
            for row in results:
                citas.append(dict(row))
            
            return citas
            
        except Exception as e:
            logger.error(f"❌ Error obteniendo citas por rango de fecha: {e}")
            return []
    
    def get_appointments_by_doctor(self, doctor: str, fecha_inicio: str = None) -> List[Dict[str, Any]]:
        """Obtiene citas de un doctor específico"""
        try:
            if fecha_inicio:
                query = f"""
                SELECT *
                FROM `{self.project_id}.{self.dataset_id}.{self.table_id}`
                WHERE doctor_asignado = @doctor
                AND fecha_cita >= @fecha_inicio
                ORDER BY fecha_cita ASC
                """
                
                job_config = bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("doctor", "STRING", doctor),
                        bigquery.ScalarQueryParameter("fecha_inicio", "DATETIME", fecha_inicio)
                    ]
                )
            else:
                query = f"""
                SELECT *
                FROM `{self.project_id}.{self.dataset_id}.{self.table_id}`
                WHERE doctor_asignado = @doctor
                ORDER BY fecha_cita ASC
                """
                
                job_config = bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("doctor", "STRING", doctor)
                    ]
                )
            
            query_job = self.client.query(query, job_config=job_config)
            results = query_job.result()
            
            citas = []
            for row in results:
                citas.append(dict(row))
            
            return citas
            
        except Exception as e:
            logger.error(f"❌ Error obteniendo citas del doctor: {e}")
            return []
    
    def get_statistics_summary(self) -> Dict[str, Any]:
        """Obtiene estadísticas generales de las citas"""
        try:
            query = f"""
            SELECT 
                COUNT(*) as total_citas,
                COUNT(DISTINCT doctor_asignado) as total_doctores,
                COUNT(DISTINCT DATE(fecha_cita)) as dias_con_citas,
                estado_cita,
                COUNT(*) as cantidad_por_estado
            FROM `{self.project_id}.{self.dataset_id}.{self.table_id}`
            GROUP BY estado_cita
            """
            
            query_job = self.client.query(query)
            results = query_job.result()
            
            estadisticas = {
                "total_citas": 0,
                "estados": {},
                "total_doctores": 0,
                "dias_con_citas": 0
            }
            
            for row in results:
                if row.estado_cita:
                    estadisticas["estados"][row.estado_cita] = row.cantidad_por_estado
                    estadisticas["total_citas"] += row.cantidad_por_estado
                
                if row.total_doctores:
                    estadisticas["total_doctores"] = row.total_doctores
                if row.dias_con_citas:
                    estadisticas["dias_con_citas"] = row.dias_con_citas
            
            return estadisticas
            
        except Exception as e:
            logger.error(f"❌ Error obteniendo estadísticas: {e}")
            return {"error": str(e)}
    
    def test_connection(self) -> bool:
        """Prueba la conexión con BigQuery"""
        try:
            # Hacer una consulta simple
            query = f"""
            SELECT COUNT(*) as total
            FROM `{self.project_id}.{self.dataset_id}.{self.table_id}`
            LIMIT 1
            """
            
            query_job = self.client.query(query)
            results = query_job.result()
            
            for row in results:
                logger.info(f"✅ Conexión BigQuery exitosa. Total registros: {row.total}")
                return True
                
        except Exception as e:
            logger.error(f"❌ Error probando conexión BigQuery: {e}")
            return False