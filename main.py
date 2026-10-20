import os
import io
import re
import uuid
from datetime import datetime, timedelta, date, time
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status, UploadFile, File, Form, BackgroundTasks, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from supabase import create_client, Client
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.message import EmailMessage

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer

# 1. Cargar variables de entorno
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Faltan las credenciales SUPABASE_URL o SUPABASE_KEY en el archivo .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 2. Configurar Scheduler
scheduler = AsyncIOScheduler()

# 3. Definir el ciclo de vida (lifespan) para el Scheduler
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Registrar la tarea diaria a las 08:00 AM
    scheduler.add_job(tarea_verificar_recordatorios_sistema, 'cron', hour=8, minute=0)
    scheduler.start()
    print("[SCHEDULER] Tareas automáticas iniciadas correctamente.")
    yield
    scheduler.shutdown()
    print("[SCHEDULER] Tareas automáticas deteniendo...")

# 4. Instanciar FastAPI UNA SOLA VEZ con el lifespan incorporado
app = FastAPI(
    title="API Actividades y Premios - Aquí Todos Ganan",
    description="Backend CRUD ajustado a la estructura de la base de datos PostgreSQL",
    version="2.1.0",
    lifespan=lifespan
)

# 5. Middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 6. Modelos de datos (Pydantic)
class InscripcionCreate(BaseModel):
    rut_alumno: str
    id_actividad: int

class ItemAsistencia(BaseModel):
    rut_usuario: str
    presente: bool

class EstudianteAsistencia(BaseModel):
    rut_usuario: str
    presente: bool

class CargaAsistenciaBatch(BaseModel):
    estudiantes: List[EstudianteAsistencia]

class CanjeCreate(BaseModel):
    rut_usuario: str      
    id_premio: str | int

class CanjeRequest(BaseModel):
    rut_alumno: str
    id_premio: int
    lugar_entrega: str = "Sede"

class ProcesarSolicitudPayload(BaseModel):
    id_estado: int  # 2 = Aprobado, 3 = Rechazado
    observacion: Optional[str] = None
# ==========================================
# CONSULTAS CON RELACIONES (JOINs)
# ==========================================

# Constante con relaciones de la actividad (incluye inscritos en tiempo real)
QUERY_RELACIONES_ACTIVIDAD = """
    *,
    tipo_actividad(descripcion),
    estado_actividad(descripcion),
    sede(descripcion),
    usuario(nombre_completo, correo),
    puntaje_act(id_puntaje, cantidad, fecha_vencimiento),
    cupo_actividad(id_cupo, cantidad),
    lugar_actividad(id_lugar_actividad, descripcion),
    requisito_participacion(id_requisito, descripcion),
    calendario(id_calendario, fecha, hora, lugar),
    inscripcion_act(
        id_inscripcion,
        rut_usuario,
        fecha_inscripcion,
        puntos_ganados,
        usuario(nombre_completo, correo)
    )
"""

QUERY_RELACIONES_PREMIO = """
    *,
    categoria_premio(descripcion),
    sede(descripcion),
    usuario(nombre_completo, correo),
    stock_sede(id_stock, cantidad, id_sede),
    nivel(id_nivel, nombre_nivel, min_actividades)
"""
QUERY_RELACIONES_INSCRIPCION = """
    *,
    actividad(id_actividad, nombre_actividad, descripcion, fecha_inicio, fecha_termino)
"""

# ==========================================
# FUNCIONES AUXILIARES
# ==========================================
def formatear_rut_chileno(rut: str) -> str:
    # Quitar puntos y guiones para obtener solo los caracteres limpios
    rut_clean = rut.replace(".", "").replace("-", "").strip()
    if len(rut_clean) < 2:
        return rut
    
    cuerpo = rut_clean[:-1]
    dv = rut_clean[-1]
    
    # Formatear el cuerpo con puntos de miles
    try:
        cuerpo_fmt = f"{int(cuerpo):,}".replace(",", ".")
        return f"{cuerpo_fmt}-{dv}"
    except ValueError:
        return rut

def eliminar_imagen_storage(url_imagen: Optional[str], bucket_default: str = "premios"):
    """
    Elimina un archivo del bucket de Supabase Storage extrayendo
    automáticamente el bucket real y el nombre de archivo desde la URL.
    """
    if not url_imagen or not isinstance(url_imagen, str) or not url_imagen.strip():
        return
    try:
        clean_url = url_imagen.split("?")[0]
        partes = clean_url.split("/")
        nombre_archivo = partes[-1]

        bucket = bucket_default
        if "public" in partes:
            idx = partes.index("public")
            if idx + 1 < len(partes) - 1:
                bucket = partes[idx + 1]

        if nombre_archivo:
            supabase.storage.from_(bucket).remove([nombre_archivo])
            print(f"Imagen '{nombre_archivo}' eliminada correctamente del bucket '{bucket}'")
    except Exception as e:
        print(f"Advertencia: No se pudo eliminar la imagen del bucket '{bucket}': {e}")

def actividad_ha_finalizado(actividad_data: dict) -> bool:
    """
    Verifica si una actividad ya concluyó comparando su fecha y hora de término
    con la fecha y hora actual del servidor.
    """
    if not actividad_data:
        return False

    # 1. Si viene como un timestamp o string ISO en fecha_termino
    fecha_termino_str = actividad_data.get("fecha_termino")
    if fecha_termino_str:
        try:
            # Elimina zonas horarias simples para comparar de forma uniforme
            dt_termino = datetime.fromisoformat(fecha_termino_str.replace("Z", "+00:00")).replace(tzinfo=None)
            return datetime.now() >= dt_termino
        except ValueError:
            pass

    # 2. Si viene dividido en campos 'fecha' y 'hora_termino'
    fecha_act = actividad_data.get("fecha")
    hora_term = actividad_data.get("hora_termino")

    if fecha_act and hora_term:
        if isinstance(fecha_act, str):
            fecha_act = date.fromisoformat(fecha_act)
        if isinstance(hora_term, str):
            hora_term = time.fromisoformat(hora_term)

        dt_termino = datetime.combine(fecha_act, hora_term)
        return datetime.now() >= dt_termino

    return False

def generar_formatos_rut(rut: str) -> list[str]:
    """
    Limpia cualquier RUT recibido y genera sus variantes sin romper la ejecución.
    """
    if not rut:
        return []

    limpio = re.sub(r"[^0-9kK]", "", str(rut)).upper()
    if len(limpio) < 2:
        return [rut]

    cuerpo = limpio[:-1]
    dv = limpio[-1]

    if cuerpo.isdigit():
        cuerpo_puntos = f"{int(cuerpo):,}".replace(",", ".")
    else:
        cuerpo_puntos = cuerpo
        
    rut_puntos = f"{cuerpo_puntos}-{dv}"
    rut_guion = f"{cuerpo}-{dv}"
    rut_limpio = f"{cuerpo}{dv}"

    return list(set([rut, rut_puntos, rut_guion, rut_limpio]))

class ActividadCreate(BaseModel):
    nombre_actividad: str
    descripcion: str
    responsable_actividad: str
    fecha: date
    hora_inicio: time
    hora_termino: time
    id_tipo_actividad: int
    id_estado_actividad: Optional[int] = 1
    id_sede: int
    rut_usuario: str
    puntos: int
    cupos: int
    lugar: str
    requisito: Optional[str] = "Sin requisitos"
    img_actv: Optional[str] = None

class ActividadUpdate(BaseModel):
    nombre_actividad: Optional[str] = None
    descripcion: Optional[str] = None
    responsable_actividad: Optional[str] = None
    fecha: Optional[date] = None
    hora_inicio: Optional[time] = None
    hora_termino: Optional[time] = None
    id_tipo_actividad: Optional[int] = None
    id_estado_actividad: Optional[int] = None
    id_sede: Optional[int] = None
    rut_usuario: Optional[str] = None
    img_actv: Optional[str] = None
    
    puntos: Optional[int] = None
    cupos: Optional[int] = None
    lugar: Optional[str] = None
    requisito: Optional[str] = None
class ProcesarSolicitudPayload(BaseModel):
    id_estado: int  # 2 = Aprobado, 3 = Rechazado
    observacion: Optional[str] = None

class PremioCreate(BaseModel):
    descripcion: str
    puntos_requeridos: int
    id_categoria: int
    rut_usuario: str
    id_sede: int
    id_nivel: Optional[int] = 1  # 1: Bronce (0-3), 2: Oro (4-6), 3: Platino (7+)
    estado_visibilidad: bool = True
    imagen: Optional[str] = None
    stock: Optional[int] = 0

class PremioUpdate(BaseModel):
    descripcion: Optional[str] = None
    puntos_requeridos: Optional[int] = None
    id_categoria: Optional[int] = None
    rut_usuario: Optional[str] = None
    id_sede: Optional[int] = None
    id_nivel: Optional[int] = None  # Permite modificar el nivel asignado
    estado_visibilidad: Optional[bool] = None
    imagen: Optional[str] = None
    stock: Optional[int] = None


class InscripcionCreate(BaseModel):
    rut_alumno: str
    id_actividad: int

# ==========================================
# RUTAS DE SUBIDA DE IMÁGENES Y GENERALES
# ==========================================

@app.get("/", tags=["Inicio"])
def inicio():
    return {"mensaje": "API ajustada a BD PostgreSQL operativa"}


@app.post("/upload-imagen", tags=["Archivos"])
async def subir_imagen(file: UploadFile = File(...), bucket: Optional[str] = Form("actividad")):
    try:
        if not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="El archivo enviado no es una imagen válida.")

        file_ext = file.filename.split(".")[-1]
        file_name = f"{uuid.uuid4()}.{file_ext}"
        contents = await file.read()

        buckets_permitidos = ["actividad", "premio", "premios"]
        target_bucket = bucket if bucket in buckets_permitidos else "actividad"

        supabase.storage.from_(target_bucket).upload(
            file_name,
            contents,
            file_options={"content-type": file.content_type}
        )

        url_publica = supabase.storage.from_(target_bucket).get_public_url(file_name)
        return {"url": url_publica, "filename": file_name}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al subir imagen: {str(e)}")

ESTADO_PROGRAMADA = 1
ESTADO_EN_CURSO = 2
ESTADO_FINALIZADA = 3
ESTADO_CANCELADA = 4



def calcular_estado_dinamico(actividad: dict) -> dict:
    """
    1. Evalúa el estado contra el tiempo respetando inicios y términos manuales.
    2. Cuenta alumnos inscritos real-time.
    3. Define si es REPROGRAMABLE (solo si ya pasó su hora de término Y tiene 0 inscritos).
    """
    if not actividad:
        return actividad

    # CORRECCIÓN CLAVE: Si ya está CANCELADA o FINALIZADA (manual o automáticamente), 
    # NO tocar su estado ni sobrescribirlo con el reloj.
    estado_actual = actividad.get("id_estado_actividad")
    if estado_actual == ESTADO_CANCELADA or estado_actual == ESTADO_FINALIZADA:
        # Asegurar que el objeto interno de descripción se mantenga coherente
        if isinstance(actividad.get("estado_actividad"), dict):
            if estado_actual == ESTADO_FINALIZADA:
                actividad["estado_actividad"]["descripcion"] = "Finalizada"
            elif estado_actual == ESTADO_CANCELADA:
                actividad["estado_actividad"]["descripcion"] = "Cancelada"

        # Calcular cantidad de inscritos
        inscritos = actividad.get("inscripcion_act") or []
        cant_inscritos = len(inscritos) if isinstance(inscritos, list) else 0
        actividad["total_inscritos"] = cant_inscritos
        
        # Una actividad finalizada/cancelada solo se reprograma si tiene 0 inscritos
        actividad["puede_reprogramar"] = (cant_inscritos == 0)
        return actividad

    now = datetime.now()

    # A) Calcular cantidad de inscritos
    inscritos = actividad.get("inscripcion_act") or []
    cant_inscritos = len(inscritos) if isinstance(inscritos, list) else 0
    actividad["total_inscritos"] = cant_inscritos

    try:
        f_act = actividad.get("fecha")
        h_ini = actividad.get("hora_inicio")
        h_ter = actividad.get("hora_termino")

        if not f_act or not h_ini or not h_ter:
            actividad["puede_reprogramar"] = False
            return actividad

        # Normalizar fecha
        if isinstance(f_act, str):
            f_act = date.fromisoformat(f_act.split("T")[0])

        # Normalizar hora inicio
        if isinstance(h_ini, str):
            h_ini_clean = h_ini.replace('Z', '').split('+')[0]
            h_ini = time.fromisoformat(h_ini_clean)

        # Normalizar hora término
        if isinstance(h_ter, str):
            h_ter_clean = h_ter.replace('Z', '').split('+')[0]
            h_ter = time.fromisoformat(h_ter_clean)

        dt_inicio = datetime.combine(f_act, h_ini)
        dt_termino = datetime.combine(f_act, h_ter)

        # B) Evaluación de estados dinámicos
        # 1. Ya pasó la hora de término
        if now >= dt_termino:
            actividad["id_estado_actividad"] = ESTADO_FINALIZADA
            if isinstance(actividad.get("estado_actividad"), dict):
                actividad["estado_actividad"]["descripcion"] = "Finalizada"

        # 2. Estamos en el rango de horario
        elif dt_inicio <= now < dt_termino:
            actividad["id_estado_actividad"] = ESTADO_EN_CURSO
            if isinstance(actividad.get("estado_actividad"), dict):
                actividad["estado_actividad"]["descripcion"] = "En Curso"

        # 3. Aún no llega la hora de inicio (now < dt_inicio)
        elif now < dt_inicio:
            # Respetar si el encargado la inició manualmente antes de tiempo
            if actividad.get("id_estado_actividad") == ESTADO_EN_CURSO:
                if isinstance(actividad.get("estado_actividad"), dict):
                    actividad["estado_actividad"]["descripcion"] = "En Curso"
            else:
                actividad["id_estado_actividad"] = ESTADO_PROGRAMADA
                if isinstance(actividad.get("estado_actividad"), dict):
                    actividad["estado_actividad"]["descripcion"] = "Programada"

        # C) REGLA ESTRICTA DE REPROGRAMACIÓN
        es_vencida = now >= dt_termino or actividad.get("id_estado_actividad") == ESTADO_FINALIZADA
        actividad["puede_reprogramar"] = es_vencida and (cant_inscritos == 0)

    except Exception as e:
        print(f"Error calculando estado dinámico: {e}")
        actividad["puede_reprogramar"] = False

    return actividad

# ==========================================
# CRUD ACTIVIDADES
# ==========================================


@app.get("/actividades", tags=["Actividades"])
def obtener_actividades():
    try:
        res = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).order("id_actividad", desc=True).execute()
        actividades_actualizadas = [calcular_estado_dinamico(act) for act in (res.data or [])]
        return actividades_actualizadas
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/actividades/{id_actividad}", tags=["Actividades"])
def obtener_actividad_por_id(id_actividad: int):
    try:
        res = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).eq("id_actividad", id_actividad).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="La actividad no existe")
        return calcular_estado_dinamico(res.data[0])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actividades", status_code=status.HTTP_201_CREATED, tags=["Actividades"])
def crear_actividad(actividad: ActividadCreate, background_tasks: BackgroundTasks):
    try:
        now_str = datetime.now().isoformat()
        datos = actividad.model_dump()

        # Limpieza de imagen opcional
        if datos.get("img_actv") == "":
            datos["img_actv"] = None

        # Estado por defecto si no viene especificado
        if not datos.get("id_estado_actividad"):
            datos["id_estado_actividad"] = 1

        # Extraer campos que corresponden a tablas relacionadas
        puntos = datos.pop("puntos")
        cupos = datos.pop("cupos")
        lugar = datos.pop("lugar")
        requisito = datos.pop("requisito", None)

        # Convertir a string/isoformat para enviar a Supabase
        fecha_str = str(actividad.fecha)
        hora_inicio_str = str(actividad.hora_inicio)
        hora_termino_str = str(actividad.hora_termino)

        datos["fecha"] = fecha_str
        datos["hora_inicio"] = hora_inicio_str
        datos["hora_termino"] = hora_termino_str

        rut_creador = datos.get("rut_usuario", "").strip()

        # 1. Insertar en tabla principal 'actividad'
        res_act = supabase.table("actividad").insert(datos).execute()
        if not res_act.data:
            raise HTTPException(status_code=400, detail="Error al registrar la actividad en la base de datos")

        id_actividad = res_act.data[0]["id_actividad"]
        nombre_actividad_creada = res_act.data[0].get("nombre_actividad", "Nueva Actividad")

        # 2. Insertar en 'puntaje_act'
        supabase.table("puntaje_act").insert({
            "cantidad": puntos,
            "fecha_vencimiento": fecha_str,  # Se usa la fecha de la actividad como vencimiento
            "id_actividad": id_actividad
        }).execute()

        # 3. Insertar en 'cupo_actividad'
        supabase.table("cupo_actividad").insert({
            "cantidad": cupos,
            "id_actividad": id_actividad
        }).execute()

        # 4. Insertar en 'lugar_actividad'
        supabase.table("lugar_actividad").insert({
            "descripcion": lugar,
            "id_actividad": id_actividad
        }).execute()

        # 5. Insertar en 'calendario'
        supabase.table("calendario").insert({
            "fecha": fecha_str,
            "hora": hora_inicio_str,
            "lugar": lugar,
            "id_actividad": id_actividad
        }).execute()

        # 6. Insertar en 'requisito_participacion' (si aplica)
        if requisito:
            supabase.table("requisito_participacion").insert({
                "descripcion": requisito,
                "id_actividad": id_actividad
            }).execute()

        # ==============================================================================
        # 7. BONIFICACIÓN DE 5 PUNTOS SI EL CREADOR ES CONSEJERO (TIPO USUARIO 4)
        # ==============================================================================
        if rut_creador:
            try:
                formatos_rut = generar_formatos_rut(rut_creador)
                res_usr = supabase.table("usuario").select("rut_usuario, id_tipo_usuario, correo").in_("rut_usuario", formatos_rut).execute()

                if res_usr.data:
                    usr_data = res_usr.data[0]
                    rut_real_usr = usr_data.get("rut_usuario")
                    id_tipo_usr = usr_data.get("id_tipo_usuario")
                    correo_usr = usr_data.get("correo")

                    # Si es Tipo de Usuario 4 (Consejero)
                    if id_tipo_usr == 4:
                        puntos_bono = 5

                        # A) Actualizar o Crear puntaje_total
                        res_pt = supabase.table("puntaje_total").select("id_puntaje, puntaje").in_("rut_usuario", formatos_rut).execute()

                        if res_pt.data:
                            reg_pt = res_pt.data[0]
                            saldo_actual = int(reg_pt.get("puntaje") or 0)
                            nuevo_saldo = saldo_actual + puntos_bono

                            supabase.table("puntaje_total").update({
                                "puntaje": nuevo_saldo
                            }).eq("id_puntaje", reg_pt["id_puntaje"]).execute()
                        else:
                            nuevo_saldo = puntos_bono
                            fecha_vigencia = (datetime.now() + timedelta(days=365)).isoformat()

                            supabase.table("puntaje_total").insert({
                                "rut_usuario": rut_real_usr,
                                "puntaje": nuevo_saldo,
                                "vigencia": fecha_vigencia
                            }).execute()

                        # B) Registrar historial de puntos
                        try:
                            supabase.table("historial_puntos").insert({
                                "rut_usuario": rut_real_usr,
                                "puntos_actuales": nuevo_saldo,
                                "puntos_canjeados": 0,
                                "puntos_totales_obtenidos": nuevo_saldo
                            }).execute()
                        except Exception as e_hist:
                            print(f"[WARN] No se pudo guardar historial_puntos para el consejero: {e_hist}")

                        # C) Notificación en la BD
                        msg_bono = f"¡Ganaste +{puntos_bono} pts por crear la actividad '{nombre_actividad_creada}'! ¡Gracias por tu aporte!"
                        try:
                            supabase.table("notificacion").insert({
                                "rut_usuario": rut_real_usr,
                                "mensaje": msg_bono,
                                "fecha_envio": now_str,
                                "leido": False,
                                "id_tipo_notificacion": 1
                            }).execute()
                            print(f"[DEBUG NOTI CONSEJERO OK] +5 puntos otorgados a RUT: {rut_real_usr}")
                        except Exception as e_noti:
                            print(f"[WARN] Error guardando notificación para consejero: {e_noti}")

                        # D) Correo de confirmación SMTP
                        if correo_usr:
                            cuerpo_html = (
                                f"¡Hola!<br><br>"
                                f"Muchas gracias por publicar la actividad <b>{nombre_actividad_creada}</b>.<br><br>"
                                f"⭐ <b>Premio por Creación:</b> +{puntos_bono} pts<br>"
                                f"🏆 <b>Tu nuevo saldo:</b> {nuevo_saldo} pts<br><br>"
                                f"¡Sigue sumando actividades para incentivar la participación estudiantil!"
                            )
                            background_tasks.add_task(
                                enviar_correo_inscripcion,
                                destinatario=correo_usr,
                                nombre_actividad=nombre_actividad_creada,
                                asunto=f"¡Puntos sumados por crear actividad! - {nombre_actividad_creada}",
                                mensaje_cuerpo=cuerpo_html
                            )
            except Exception as e_bono:
                print(f"[WARN] Error procesando bonificación para consejero: {e_bono}")

        # Retornar la actividad con sus relaciones cargadas
        actividad_creada = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).eq("id_actividad", id_actividad).execute()
        return actividad_creada.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    
@app.put("/actividades/{id_actividad}", tags=["Actividades"])
def actualizar_actividad(id_actividad: int, actividad: ActividadUpdate):
    try:
        # 1. Obtener actividad existente
        check = supabase.table("actividad").select("id_actividad, fecha, hora_inicio, hora_termino, id_estado_actividad, img_actv").eq("id_actividad", id_actividad).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada")

        act_db = check.data[0]
        img_antigua = act_db.get("img_actv")
        datos = actividad.model_dump(exclude_unset=True)

        if "img_actv" in datos and datos["img_actv"] == "":
            datos["img_actv"] = None

        if "img_actv" in datos:
            nueva_img = datos["img_actv"]
            if img_antigua and nueva_img != img_antigua:
                eliminar_imagen_storage(img_antigua, bucket_default="actividad")

        puntos = datos.pop('puntos', None)
        cupos = datos.pop('cupos', None)
        lugar = datos.pop('lugar', None)
        requisito = datos.pop('requisito', None)

        # 2. Evaluar Fechas y Horas (merge con valores de BD si no vienen en request)
        f_eval = datos.get('fecha') or date.fromisoformat(str(act_db["fecha"]))
        h_ini_eval = datos.get('hora_inicio') or time.fromisoformat(str(act_db["hora_inicio"]))
        h_ter_eval = datos.get('hora_termino') or time.fromisoformat(str(act_db["hora_termino"]))

        dt_inicio_eval = datetime.combine(f_eval, h_ini_eval)
        dt_termino_eval = datetime.combine(f_eval, h_ter_eval)
        now = datetime.now()

        # Validación: Coherencia temporal
        if dt_termino_eval <= dt_inicio_eval:
            raise HTTPException(status_code=400, detail="La hora de término debe ser posterior a la hora de inicio.")

        # Convertir a ISO si vienen en el payload para guardar en BD
        if "fecha" in datos and datos["fecha"]:
            datos["fecha"] = datos["fecha"].isoformat()
        if "hora_inicio" in datos and datos["hora_inicio"]:
            datos["hora_inicio"] = datos["hora_inicio"].isoformat()
        if "hora_termino" in datos and datos["hora_termino"]:
            datos["hora_termino"] = datos["hora_termino"].isoformat()

        # 3. Validación y Control del Estado por el Admin
        nuevo_estado = datos.get("id_estado_actividad")

        if nuevo_estado is not None:
            if nuevo_estado == ESTADO_PROGRAMADA and dt_inicio_eval <= now:
                raise HTTPException(
                    status_code=400, 
                    detail="No se puede cambiar el estado a 'Programada' porque la fecha y hora de inicio ya transcurrieron."
                )

            if nuevo_estado == ESTADO_EN_CURSO:
                if now < dt_inicio_eval:
                    raise HTTPException(status_code=400, detail="No se puede forzar 'En Curso' porque el inicio es futuro.")
                if now >= dt_termino_eval:
                    raise HTTPException(status_code=400, detail="No se puede forzar 'En Curso' porque la actividad ya sobrepasó la hora de término.")

            if nuevo_estado == ESTADO_FINALIZADA and now < dt_termino_eval:
                raise HTTPException(status_code=400, detail="No se puede marcar como 'Finalizada' si la hora de término aún no se alcanza.")

        else:
            # Reevaluar estado si cambió la fecha/hora sin enviar el ID de estado
            estado_actual = act_db.get("id_estado_actividad")
            if estado_actual != ESTADO_CANCELADA:
                if dt_inicio_eval <= now < dt_termino_eval:
                    datos["id_estado_actividad"] = ESTADO_EN_CURSO
                elif now >= dt_termino_eval:
                    datos["id_estado_actividad"] = ESTADO_FINALIZADA
                elif now < dt_inicio_eval:
                    datos["id_estado_actividad"] = ESTADO_PROGRAMADA

        # 4. Actualizar tabla principal
        if datos:
            supabase.table("actividad").update(datos).eq("id_actividad", id_actividad).execute()

        # 5. Actualizar tablas secundarias
        if puntos is not None:
            p_res = supabase.table("puntaje_act").select("id_puntaje").eq("id_actividad", id_actividad).execute()
            if p_res.data:
                supabase.table("puntaje_act").update({"cantidad": puntos, "fecha_vencimiento": f_eval.isoformat()}).eq("id_actividad", id_actividad).execute()
            else:
                supabase.table("puntaje_act").insert({
                    "cantidad": puntos,
                    "fecha_vencimiento": f_eval.isoformat(),
                    "id_actividad": id_actividad
                }).execute()

        if cupos is not None:
            c_res = supabase.table("cupo_actividad").select("id_cupo").eq("id_actividad", id_actividad).execute()
            if c_res.data:
                supabase.table("cupo_actividad").update({"cantidad": cupos}).eq("id_actividad", id_actividad).execute()
            else:
                supabase.table("cupo_actividad").insert({"cantidad": cupos, "id_actividad": id_actividad}).execute()

        if lugar is not None:
            l_res = supabase.table("lugar_actividad").select("id_lugar_actividad").eq("id_actividad", id_actividad).execute()
            if l_res.data:
                supabase.table("lugar_actividad").update({"descripcion": lugar}).eq("id_actividad", id_actividad).execute()
            else:
                supabase.table("lugar_actividad").insert({"descripcion": lugar, "id_actividad": id_actividad}).execute()

        if lugar is not None or 'fecha' in datos or 'hora_inicio' in datos:
            cal_res = supabase.table("calendario").select("id_calendario").eq("id_actividad", id_actividad).execute()
            cal_payload = {}
            if lugar is not None:
                cal_payload["lugar"] = lugar
            cal_payload["fecha"] = f_eval.isoformat()
            cal_payload["hora"] = h_ini_eval.strftime("%H:%M:%S")

            if cal_res.data:
                supabase.table("calendario").update(cal_payload).eq("id_actividad", id_actividad).execute()

        if requisito is not None:
            r_res = supabase.table("requisito_participacion").select("id_requisito").eq("id_actividad", id_actividad).execute()
            if r_res.data:
                supabase.table("requisito_participacion").update({"descripcion": requisito}).eq("id_actividad", id_actividad).execute()
            else:
                supabase.table("requisito_participacion").insert({"descripcion": requisito, "id_actividad": id_actividad}).execute()

        res = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).eq("id_actividad", id_actividad).execute()
        return calcular_estado_dinamico(res.data[0])

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/actividades/{id_actividad}", tags=["Actividades"])
def eliminar_actividad(id_actividad: int):
    try:
        check = supabase.table("actividad").select("id_actividad, img_actv").eq("id_actividad", id_actividad).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada")

        img_antigua = check.data[0].get("img_actv")

        supabase.table("puntaje_act").delete().eq("id_actividad", id_actividad).execute()
        supabase.table("cupo_actividad").delete().eq("id_actividad", id_actividad).execute()
        supabase.table("lugar_actividad").delete().eq("id_actividad", id_actividad).execute()
        supabase.table("requisito_participacion").delete().eq("id_actividad", id_actividad).execute()
        supabase.table("calendario").delete().eq("id_actividad", id_actividad).execute()

        supabase.table("actividad").delete().eq("id_actividad", id_actividad).execute()

        if img_antigua:
            eliminar_imagen_storage(img_antigua, bucket_default="actividad")

        return {"mensaje": f"Actividad {id_actividad} eliminada correctamente"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/actividades/conteo/usuario/{rut_usuario}", tags=["Actividades"])
def obtener_conteo_actividades_por_usuario(rut_usuario: str):
    try:
        formatos = generar_formatos_rut(rut_usuario)
        condicion_or = ",".join([f"rut_usuario.eq.{f}" for f in formatos])

        res = (
            supabase.table("actividad")
            .select("id_actividad", count="exact")
            .or_(condicion_or)
            .execute()
        )

        total = res.count if res.count is not None else len(res.data or [])

        return {
            "rut_usuario": rut_usuario,
            "total_actividades": total
        }
    except Exception as e:
        print(f"Error crítico en /actividades/conteo/usuario/{rut_usuario}: {e}")
        raise HTTPException(status_code=500, detail=f"Error en consulta de actividades: {str(e)}")


@app.get("/actividades/usuario/{rut_usuario}", tags=["Actividades"])
def obtener_actividades_por_usuario(rut_usuario: str):
    try:
        formatos = generar_formatos_rut(rut_usuario)
        condicion_or = ",".join([f"rut_usuario.eq.{f}" for f in formatos])

        res = (
            supabase.table("actividad")
            .select(QUERY_RELACIONES_ACTIVIDAD)
            .or_(condicion_or)
            .order("id_actividad", desc=True)
            .execute()
        )
        actividades_raw = res.data or []
        return [calcular_estado_dinamico(act) for act in actividades_raw]
    except Exception as e:
        print(f"Error crítico en /actividades/usuario/{rut_usuario}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
# ==========================================
# CRUD INSCRIPCIONES A ACTIVIDADES
# ==========================================
from datetime import datetime, timedelta, date, time

def actividad_ha_finalizado(actividad_data: dict) -> bool:
    """
    Helper para verificar si la fecha y hora_termino de la actividad
    son menores o iguales a la fecha y hora actual del servidor.
    """
    if not actividad_data:
        return False

    fecha_act = actividad_data.get("fecha")
    hora_term = actividad_data.get("hora_termino")

    if not fecha_act or not hora_term:
        return False

    try:
        # 1. Normalizar fecha_act
        if isinstance(fecha_act, str):
            # Limpia posibles marcas ISO de tiempo (ej. "2026-09-27T00:00:00" -> "2026-09-27")
            fecha_act_clean = fecha_act.split("T")[0]
            fecha_act = date.fromisoformat(fecha_act_clean)

        # 2. Normalizar hora_term
        if isinstance(hora_term, str):
            # Limpia zonas horarias simples (ej. "18:30:00+00" o "18:30:00Z" -> "18:30:00")
            hora_term_clean = hora_term.replace('Z', '').split('+')[0]
            hora_term = time.fromisoformat(hora_term_clean)

        # 3. Combinar fecha y hora para comparar contra el reloj del servidor
        dt_termino = datetime.combine(fecha_act, hora_term)
        return datetime.now() >= dt_termino

    except Exception as e:
        print(f"Error al evaluar si la actividad ha finalizado: {e}")
        return False

def sincronizar_participacion_activa(rut_usuario: str):
    """
    Calcula el total de actividades con presencia confirmada (presente = True)
    y actualiza o inserta el registro en 'participacion_activa'.
    """
    try:
        # 1. Obtener id_periodo_academico del usuario
        res_usr = supabase.table("usuario")\
            .select("id_periodo_academico")\
            .eq("rut_usuario", rut_usuario)\
            .maybeSingle()\
            .execute()

        id_periodo = res_usr.data.get("id_periodo_academico") if res_usr and res_usr.data else 1

        # 2. Contar asistencias reales (presente = True)
        res_asist = supabase.table("asistencia_act")\
            .select("id_asistencia_actividad", count="exact")\
            .eq("rut_usuario", rut_usuario)\
            .eq("presente", True)\
            .execute()

        total_asistidas = res_asist.count if res_asist.count is not None else len(res_asist.data or [])

        # 3. Determinar el estado dinámico
        if total_asistidas >= 5:
            nuevo_estado = "Destacado"
        elif total_asistidas >= 2:
            nuevo_estado = "Activo"
        else:
            nuevo_estado = "Inactivo"

        # 4. Verificar si ya existe registro en 'participacion_activa'
        res_part = supabase.table("participacion_activa")\
            .select("id_participacion")\
            .eq("rut_usuario", rut_usuario)\
            .eq("id_periodo_academico", id_periodo)\
            .maybeSingle()\
            .execute()

        datos_part = {
            "total_actividades": total_asistidas,
            "estado": nuevo_estado,
            "rut_usuario": rut_usuario,
            "id_periodo_academico": id_periodo
        }

        if res_part and res_part.data:
            supabase.table("participacion_activa")\
                .update(datos_part)\
                .eq("id_participacion", res_part.data["id_participacion"])\
                .execute()
        else:
            supabase.table("participacion_activa").insert(datos_part).execute()

        print(f"[PARTICIPACIÓN ACTIVA] RUT: {rut_usuario} | Total: {total_asistidas} | Estado: {nuevo_estado}")

    except Exception as e:
        print(f"[WARN] Error al sincronizar participacion_activa para {rut_usuario}: {e}")


def actualizar_puntaje_total_alumno(rut_usuario_param: str, id_actividad_forzada: int = None):
    """
    Suma los puntos de las actividades a las que el alumno asistió (presente = True)
    y cuya actividad esté finalizada, Y RESTA los puntos de canjes aprobados o entregados.
    """
    try:
        # 1. Obtener RUT real
        res_sesion = (
            supabase.table("sesion_usuario")
            .select("rut_usuario")
            .eq("token_acceso", rut_usuario_param)
            .execute()
        )
        rut_real = res_sesion.data[0]["rut_usuario"] if res_sesion.data else rut_usuario_param
        formatos = generar_formatos_rut(rut_real)

        # 2. Consultar asistencias CONFIRMADAS
        res_asistencia = (
            supabase.table("asistencia_act")
            .select("""
                id_actividad,
                actividad (
                    id_actividad,
                    nombre_actividad,
                    fecha,
                    hora_termino,
                    id_estado_actividad
                )
            """)
            .in_("rut_usuario", formatos)
            .eq("presente", True)
            .execute()
        )

        # 3. Filtrar actividades finalizadas
        actividades_asistidas_y_finalizadas = []
        mapa_nombres_actividades = {}

        for item in (res_asistencia.data or []):
            raw_act = item.get("actividad")
            act_data = raw_act[0] if isinstance(raw_act, list) and raw_act else raw_act

            if act_data:
                id_act = item["id_actividad"]
                id_estado = act_data.get("id_estado_actividad")
                
                es_finalizada = (
                    (id_actividad_forzada and id_act == id_actividad_forzada) or
                    id_estado == 3 or
                    actividad_ha_finalizado(act_data)
                )

                if es_finalizada:
                    actividades_asistidas_y_finalizadas.append(id_act)
                    mapa_nombres_actividades[id_act] = (
                        act_data.get("nombre_actividad") or f"la actividad #{id_act}"
                    )

        total_ganado = 0
        puntos_por_actividad = {}

        # 4.A. Sumar los puntos ganados en actividades
        if actividades_asistidas_y_finalizadas:
            res_pts = (
                supabase.table("puntaje_act")
                .select("id_actividad, cantidad")
                .in_("id_actividad", actividades_asistidas_y_finalizadas)
                .execute()
            )
            
            for item in (res_pts.data or []):
                id_act = item.get("id_actividad")
                cant = item.get("cantidad", 0)
                puntos_por_actividad[id_act] = puntos_por_actividad.get(id_act, 0) + cant

            total_ganado = sum(puntos_por_actividad.values())

        # 4.B. RESTAR PUNTOS DE SOLICITUDES DE CANJE APROBADAS O ENTREGADAS
        total_canjeado = 0
        try:
            res_estados = supabase.table("estado_canje").select("id_estado_canje, descripcion").execute()
            ids_descuento = [
                e["id_estado_canje"] for e in (res_estados.data or [])
                if any(kw in str(e.get("descripcion", "")).lower() for kw in ["aprobad", "entregad", "retirad"])
            ] or [2, 3]

            res_canjes = (
                supabase.table("solicitud_canje")
                .select("costo_puntaje")
                .in_("rut_usuario", formatos)
                .in_("id_estado_canje", ids_descuento)
                .execute()
            )

            if res_canjes.data:
                for c in res_canjes.data:
                    total_canjeado += int(c.get("costo_puntaje") or 0)
        except Exception as e_canjes:
            print(f"[WARN] Error consultando canjes descontables para {rut_real}: {e_canjes}")

        # Saldo final disponible
        total_acumulado = max(0, total_ganado - total_canjeado)

        # 5. Actualizar o insertar en 'puntaje_total'
        fecha_vigencia = (datetime.now() + timedelta(days=365)).isoformat()

        res_puntaje = (
            supabase.table("puntaje_total")
            .select("id_puntaje")
            .in_("rut_usuario", formatos)
            .execute()
        )

        if res_puntaje.data:
            id_p = res_puntaje.data[0]["id_puntaje"]
            supabase.table("puntaje_total").update({
                "puntaje": total_acumulado,
                "vigencia": fecha_vigencia
            }).eq("id_puntaje", id_p).execute()
        else:
            supabase.table("puntaje_total").insert({
                "rut_usuario": rut_real,
                "puntaje": total_acumulado,
                "vigencia": fecha_vigencia
            }).execute()

        print(f"Puntaje total actualizado con éxito para {rut_real}: {total_acumulado} pts (Ganados: {total_ganado}, Canjeados: -{total_canjeado})")

        # 6. ENVIAR NOTIFICACIÓN Y CORREO DE PUNTOS GANADOS
        try:
            res_usr = supabase.table("usuario").select("*").in_("rut_usuario", formatos).execute()
            correo_alumno = None
            if res_usr.data:
                usr = res_usr.data[0]
                correo_alumno = usr.get("correo") or usr.get("email")

            res_notis_previas = (
                supabase.table("notificacion")
                .select("mensaje")
                .in_("rut_usuario", formatos)
                .eq("id_tipo_notificacion", 2)
                .execute()
            )
            
            mensajes_existentes = [n.get("mensaje", "") for n in (res_notis_previas.data or [])]

            for id_act, pts in puntos_por_actividad.items():
                nom_act = mapa_nombres_actividades.get(id_act, f"la actividad #{id_act}")
                
                tag_actividad = f"[ACT-{id_act}]"
                msg_notificacion = f"¡Felicitaciones! Completaste '{nom_act}' {tag_actividad} y ganaste +{pts} puntos. Tu saldo total actual es de {total_acumulado} pts."

                ya_notificado = any(tag_actividad in m for m in mensajes_existentes)

                if not ya_notificado:
                    # A) Guardar en BD
                    supabase.table("notificacion").insert({
                        "rut_usuario": rut_real,
                        "mensaje": msg_notificacion,
                        "fecha_envio": datetime.now().isoformat(),
                        "leido": False,
                        "id_tipo_notificacion": 2
                    }).execute()

                    # B) Enviar Correo
                    if correo_alumno:
                        print(f"[INFO] ENVIANDO CORREO REAL DE PUNTOS ACREDITADOS PARA '{nom_act}' A: {correo_alumno}")
                        enviar_correo_inscripcion(
                            destinatario=correo_alumno,
                            nombre_actividad=nom_act,
                            asunto=f"¡Puntos Acreditados! - {nom_act}",
                            mensaje_cuerpo=f"""
                                ¡Felicitaciones! Se ha confirmado tu asistencia a la actividad <b>{nom_act}</b>.<br><br>
                                🎉 <b>Puntos ganados:</b> +{pts} pts<br>
                                🏆 <b>Puntaje Total Acumulado:</b> {total_acumulado} pts
                            """
                        )
                    else:
                        print(f"[WARN] No se encontró correo para el usuario {rut_real}")
                else:
                    print(f"[INFO] La actividad ID #{id_act} ('{nom_act}') ya contaba con notificación enviada previamente.")

        except Exception as e_noti_mail:
            print(f"[WARN] No se pudo procesar la notificación/correo automático: {e_noti_mail}")
            
        return total_acumulado

    except Exception as e:
        print(f"Error al consolidar puntaje total para {rut_usuario_param}: {e}")
        return 0
    
@app.get("/puntaje-total/{rut_o_token}", tags=["Puntaje"])
def obtener_y_actualizar_puntaje_total(rut_o_token: str):
    """
    Recalcula el puntaje acumulado devolviendo el total del alumno.
    Soporta tanto RUT directo como Token de Sesión.
    """
    try:
        token_o_rut = rut_o_token.strip()

        # 1. Traducir Token a RUT si aplica
        res_sesion = (
            supabase.table("sesion_usuario")
            .select("rut_usuario")
            .eq("token_acceso", token_o_rut)
            .execute()
        )

        rut_evaluar = (
            res_sesion.data[0]["rut_usuario"] 
            if (res_sesion.data and len(res_sesion.data) > 0) 
            else token_o_rut
        )

        # 2. Obtener el RUT real del alumno
        formatos = generar_formatos_rut(rut_evaluar)
        res_usuario = (
            supabase.table("usuario")
            .select("rut_usuario")
            .in_("rut_usuario", formatos)
            .execute()
        )

        rut_final = res_usuario.data[0]["rut_usuario"] if res_usuario.data else rut_evaluar

        # 3. Calcular con el RUT limpio
        total = actualizar_puntaje_total_alumno(rut_final)
        return {"puntaje": total}

    except Exception as e:
        print(f"[WARN] No se pudo consolidar puntaje para {rut_o_token}: {e}")
        return {"puntaje": 0}


# ==========================================
# ENDPOINT: INSCRIPCIONES CON VALIDACIÓN DE USUARIO
# ==========================================
@app.post("/inscripciones", status_code=status.HTTP_201_CREATED, tags=["Inscripciones"])
def inscribir_alumno_actividad(datos: InscripcionCreate):
    try:
        rut_o_token = datos.rut_alumno.strip()

        # 0. Resolver si es token de sesión o RUT directo
        res_sesion = (
            supabase.table("sesion_usuario")
            .select("rut_usuario")
            .eq("token_acceso", rut_o_token)
            .execute()
        )

        rut_evaluar = (
            res_sesion.data[0]["rut_usuario"] 
            if (res_sesion.data and len(res_sesion.data) > 0) 
            else rut_o_token
        )

        # 1. Validar usuario y su ESTADO DE MATRÍCULA
        formatos = generar_formatos_rut(rut_evaluar)
        res_usuario = (
            supabase.table("usuario")
            .select("rut_usuario, correo, id_estado_matricula, estado_matricula(descripcion)")
            .in_("rut_usuario", formatos)
            .execute()
        )

        if not res_usuario.data:
            raise HTTPException(status_code=404, detail="El alumno no se encuentra registrado")

        usuario = res_usuario.data[0]
        rut_real = usuario["rut_usuario"]
        correo_alumno = usuario.get("correo")
        id_est_mat = usuario.get("id_estado_matricula")

        if id_est_mat != 1:
            raise HTTPException(
                status_code=400, 
                detail="No cuentas con matrícula activa para inscribirte en actividades."
            )

        # -------------------------------------------------------------------
        # 1.1 VALIDACIÓN EN TABLA 'validacion_usuario' (SIN maybeSingle)
        # -------------------------------------------------------------------
        res_req = supabase.table("requisito_participacion")\
            .select("id_requisito")\
            .eq("id_actividad", datos.id_actividad)\
            .execute()

        id_req_real = None
        if res_req.data and len(res_req.data) > 0:
            id_req_real = res_req.data[0]["id_requisito"]
        else:
            req_nuevo = supabase.table("requisito_participacion").insert({
                "descripcion": "Matrícula activa y situación regular",
                "id_actividad": datos.id_actividad
            }).execute()
            if req_nuevo.data:
                id_req_real = req_nuevo.data[0]["id_requisito"]

        if id_req_real:
            res_val = supabase.table("validacion_usuario")\
                .select("*")\
                .eq("rut_usuario", rut_real)\
                .eq("id_requisito", id_req_real)\
                .execute()

            val_data = None
            if not res_val.data:
                nueva_val = {
                    "rut_usuario": rut_real,
                    "id_requisito": id_req_real,
                    "matriculado": True,
                    "suspension": False,
                    "sumario": False,
                    "cumple": True,
                    "observacion": "Habilitado para inscripción"
                }
                res_val_ins = supabase.table("validacion_usuario").insert(nueva_val).execute()
                val_data = res_val_ins.data[0] if res_val_ins.data else nueva_val
            else:
                val_data = res_val.data[0]

            if val_data and (not val_data.get("cumple") or val_data.get("suspension") or val_data.get("sumario")):
                obs = val_data.get("observacion") or "El alumno posee restricciones académicas activas (suspensión o sumario)."
                raise HTTPException(status_code=400, detail=f"Inscripción denegada: {obs}")

        # 2. Validar que no exista inscripción previa a ESTA MISMA actividad
        check_insc = (
            supabase.table("inscripcion_act")
            .select("id_inscripcion")
            .eq("rut_usuario", rut_real)
            .eq("id_actividad", datos.id_actividad)
            .execute()
        )

        if check_insc.data:
            raise HTTPException(status_code=400, detail="El alumno ya está inscrito en esta actividad")

        # 3. Obtener datos de la actividad
        res_act = (
            supabase.table("actividad")
            .select("*")
            .eq("id_actividad", datos.id_actividad)
            .execute()
        )

        if not res_act.data:
            raise HTTPException(status_code=404, detail="La actividad no existe")

        act_data = res_act.data[0]
        nombre_act = act_data.get("nombre_actividad", f"Actividad {datos.id_actividad}")
        fecha_act = str(act_data["fecha"])
        hora_ini_nueva = str(act_data["hora_inicio"])
        hora_term_nueva = str(act_data["hora_termino"])

        # 3.1 VALIDACIÓN DE TRASLAPE DE HORARIOS (CON FILTRO DE ACTIVIDADES FINALIZADAS/CANCELADAS)
        res_mis_inscripciones = (
            supabase.table("inscripcion_act")
            .select("""
                id_actividad,
                actividad (
                    id_actividad,
                    nombre_actividad,
                    fecha,
                    hora_inicio,
                    hora_termino,
                    id_estado_actividad
                )
            """)
            .eq("rut_usuario", rut_real)
            .execute()
        )

        if res_mis_inscripciones.data:
            for item in res_mis_inscripciones.data:
                act_inscrita = item.get("actividad")
                if isinstance(act_inscrita, list) and act_inscrita:
                    act_inscrita = act_inscrita[0]
                
                if act_inscrita:
                    # CORRECCIÓN CLAVE: Si la actividad previa está Finalizada (3) o Cancelada (4), SE IGNORA EL TRASLAPE
                    id_estado = Number(act_inscrita.get("id_estado_actividad") or 1) if 'Number' in globals() else int(act_inscrita.get("id_estado_actividad") or 1)
                    if id_estado in [3, 4]:
                        continue

                    if str(act_inscrita.get("fecha")) == fecha_act:
                        h_ini_prev = str(act_inscrita.get("hora_inicio"))
                        h_term_prev = str(act_inscrita.get("hora_termino"))

                        if hora_ini_nueva < h_term_prev and hora_term_nueva > h_ini_prev:
                            nom_conflicto = act_inscrita.get("nombre_actividad", "otra actividad")
                            raise HTTPException(
                                status_code=400,
                                detail=f"Conflicto de horario: Ya estás inscrito en '{nom_conflicto}' el mismo día de {h_ini_prev} a {h_term_prev}."
                            )

        dt_termino = datetime.fromisoformat(f"{fecha_act}T{hora_term_nueva}")
        now = datetime.now()

        # 4. Insertar la inscripción en 'inscripcion_act'
        nueva_inscripcion = {
            "rut_usuario": rut_real,
            "id_actividad": datos.id_actividad,
            "puntos_ganados": 0,
            "fecha_inscripcion": now.isoformat(),
            "fecha_termino": dt_termino.isoformat()
        }

        res_ins = supabase.table("inscripcion_act").insert(nueva_inscripcion).execute()

        # 5. Notificación automática
        try:
            nueva_notificacion = {
                "rut_usuario": rut_real,
                "mensaje": f"¡Inscripción exitosa! Te has inscrito correctamente en: {nombre_act}.",
                "fecha_envio": now.isoformat(),
                "leido": False,
                "id_tipo_notificacion": 1
            }
            supabase.table("notificacion").insert(nueva_notificacion).execute()
        except Exception as e_noti:
            print(f"[WARN] No se pudo crear la notificación de inscripción: {e_noti}")

        # 5.1 Enviar correo
        try:
            if correo_alumno:
                enviar_correo_inscripcion(correo_alumno, nombre_act)
        except Exception as e_email:
            print(f"[WARN] No se pudo enviar el correo de inscripción: {e_email}")

        # 6. Descontar cupo en 'cupo_actividad'
        try:
            res_cupo = (
                supabase.table("cupo_actividad")
                .select("id_cupo", "cantidad")
                .eq("id_actividad", datos.id_actividad)
                .execute()
            )
            if res_cupo.data:
                cupo_actual = res_cupo.data[0].get("cantidad", 0)
                if cupo_actual > 0:
                    supabase.table("cupo_actividad").update(
                        {"cantidad": cupo_actual - 1}
                    ).eq("id_actividad", datos.id_actividad).execute()
        except Exception as e_cupo:
            print(f"[WARN] No se pudo actualizar el cupo: {e_cupo}")

        return {
            "mensaje": "Inscripción realizada con éxito.",
            "inscripcion": res_ins.data[0] if res_ins.data else nueva_inscripcion
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR INSCRIPCION]: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Error en inscripción: {str(e)}"
        )
# ==========================================
# ENDPOINT: ASISTENCIA SIN MARCAR PRESENTE POR DEFECTO
# ==========================================
@app.post("/actividades/{id_actividad}/asistencia", tags=["Asistencia"])
def registrar_asistencia_actividad(id_actividad: int, datos: CargaAsistenciaBatch):
    try:
        # 1. Validar existencia de la actividad
        res_act = supabase.table("actividad").select("id_actividad").eq("id_actividad", id_actividad).execute()
        if not res_act.data:
            raise HTTPException(status_code=404, detail="La actividad especificada no existe.")

        # Obtener los puntos de la actividad
        res_pts_act = supabase.table("puntaje_act").select("cantidad").eq("id_actividad", id_actividad).execute()
        puntos_actividad = res_pts_act.data[0]["cantidad"] if res_pts_act.data else 0

        ruts_procesados = set()

        for est in datos.estudiantes:
            try:
                formatos = generar_formatos_rut(est.rut_usuario)
                res_user = supabase.table("usuario").select("rut_usuario").in_("rut_usuario", formatos).execute()
                
                if not res_user.data:
                    print(f"Advertencia: Usuario {est.rut_usuario} no encontrado.")
                    continue

                rut_real = res_user.data[0]["rut_usuario"]
                ruts_procesados.add(rut_real)

                # CORRECCIÓN: Si presente no viene especificado, se establece en FALSE (Ausente)
                es_presente = bool(est.presente) if est.presente is not None else False

                # 2. Verificar registro previo de asistencia
                res_asis = (
                    supabase.table("asistencia_act")
                    .select("id_asistencia_actividad")
                    .eq("id_actividad", id_actividad)
                    .eq("rut_usuario", rut_real)
                    .execute()
                )

                fecha_actual = datetime.now().isoformat()

                if res_asis.data and len(res_asis.data) > 0:
                    id_asis = res_asis.data[0]["id_asistencia_actividad"]
                    supabase.table("asistencia_act").update({
                        "fecha_asistencia": fecha_actual,
                        "presente": es_presente
                    }).eq("id_asistencia_actividad", id_asis).execute()
                else:
                    supabase.table("asistencia_act").insert({
                        "id_actividad": id_actividad,
                        "rut_usuario": rut_real,
                        "presente": es_presente,
                        "fecha_asistencia": fecha_actual
                    }).execute()

                # Sincronizar puntos_ganados en inscripcion_act
                puntos_a_guardar = puntos_actividad if es_presente else 0
                supabase.table("inscripcion_act").update({
                    "puntos_ganados": puntos_a_guardar
                }).eq("id_actividad", id_actividad).eq("rut_usuario", rut_real).execute()

            except Exception as inner_err:
                print(f"Error procesando al alumno {est.rut_usuario}: {inner_err}")
                continue

        # 3. Recalcular puntaje acumulado Y sincronizar participacion_activa para cada alumno
        for rut in ruts_procesados:
            try:
                actualizar_puntaje_total_alumno(rut)
            except Exception as pts_err:
                print(f"Error al actualizar puntaje acumulado para RUT {rut}: {pts_err}")

            try:
                sincronizar_participacion_activa(rut)
            except Exception as part_err:
                print(f"Error al sincronizar participación activa para RUT {rut}: {part_err}")

        return {"mensaje": "Asistencia y participación activa actualizadas correctamente."}

    except HTTPException:
        raise
    except Exception as e:
        print(f"CRITICAL ERROR en /actividades/{id_actividad}/asistencia: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error al registrar asistencia en el servidor: {str(e)}")


@app.put("/inscripciones/completar", tags=["Inscripciones"])
def completar_actividad(rut_alumno: str, id_actividad: int):
    """
    Acredita la asistencia individual de un alumno, asigna puntos y envía correo.
    """
    try:
        formatos = generar_formatos_rut(rut_alumno)
        condicion_or_ins = ",".join([f'rut_usuario.eq."{f}"' for f in formatos])

        res_insc = (
            supabase.table("inscripcion_act")
            .select("id_inscripcion, rut_usuario")
            .or_(condicion_or_ins)
            .eq("id_actividad", id_actividad)
            .execute()
        )

        if not res_insc.data:
            raise HTTPException(status_code=404, detail="No existe una inscripción en esta actividad para el usuario")

        rut_real = res_insc.data[0]["rut_usuario"]

        # 1. Obtener datos del usuario (evaluando 'correo' o 'email')
        res_usr = supabase.table("usuario").select("*").eq("rut_usuario", rut_real).execute()
        correo_alumno = None
        if res_usr.data:
            usr = res_usr.data[0]
            correo_alumno = usr.get("correo") or usr.get("email")

        # 2. Obtener datos de la actividad (evaluando campos posibles)
        res_act = supabase.table("actividad").select("*").eq("id_actividad", id_actividad).execute()
        act_info = res_act.data[0] if res_act.data else {}
        nombre_act = act_info.get("nombre_actividad") or act_info.get("titulo") or f"la actividad #{id_actividad}"
        puntos_actividad = act_info.get("puntos") or act_info.get("puntos_otorgados") or 0

        # 3. Marcar asistencia en 'asistencia_act'
        payload_asistencia = {
            "fecha_asistencia": datetime.now().isoformat(),
            "presente": True,
            "rut_usuario": rut_real,
            "id_actividad": id_actividad
        }

        res_asis = (
            supabase.table("asistencia_act")
            .select("id_asistencia_actividad")
            .eq("id_actividad", id_actividad)
            .eq("rut_usuario", rut_real)
            .execute()
        )

        if res_asis.data:
            id_asis = res_asis.data[0]["id_asistencia_actividad"]
            supabase.table("asistencia_act").update(payload_asistencia).eq("id_asistencia_actividad", id_asis).execute()
        else:
            supabase.table("asistencia_act").insert(payload_asistencia).execute()

        # 4. Recalcular el puntaje total
        nuevo_total = actualizar_puntaje_total_alumno(rut_real)

        # 5. REGISTRAR NOTIFICACIÓN EN BD
        now = datetime.now()
        try:
            msg_exito = f"¡Felicitaciones! Has completado '{nombre_act}' y ganaste +{puntos_actividad} puntos. Tu saldo total actual es de {nuevo_total} puntos."
            noti_completado = {
                "rut_usuario": rut_real,
                "mensaje": msg_exito,
                "fecha_envio": now.isoformat(),
                "leido": False,
                "id_tipo_notificacion": 1
            }
            supabase.table("notificacion").insert(noti_completado).execute()
        except Exception as e_noti:
            print(f"[WARN] No se pudo crear la notificación de completado: {e_noti}")

        # 6. ENVIAR CORREO CON VERIFICACIÓN Y LOGS
        if correo_alumno:
            print(f"[INFO] Intentando enviar correo de puntos acreditados a: {correo_alumno}")
            try:
                enviar_correo_inscripcion(
                    destinatario=correo_alumno,
                    nombre_actividad=nombre_act,
                    asunto=f"¡Puntos Acreditados! - {nombre_act}",
                    mensaje_cuerpo=f"""
                        ¡Felicitaciones! Se ha confirmado tu asistencia a la actividad <b>{nombre_act}</b>.<br><br>
                        🎉 <b>Puntos ganados:</b> +{puntos_actividad} pts<br>
                        🏆 <b>Puntaje Total Acumulado:</b> {nuevo_total} pts
                    """
                )
            except Exception as e_mail:
                print(f"[WARN] Falló la conexión SMTP al enviar correo: {e_mail}")
        else:
            print(f"[WARN] No se envió correo porque no se encontró email para el RUT {rut_real}")

        return {
            "mensaje": "Actividad completada exitosamente. Asistencia registrada y puntaje acreditado.",
            "puntos_ganados": puntos_actividad,
            "puntaje_total_actual": nuevo_total
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR COMPLETAR ACTIVIDAD]: {e}")
        raise HTTPException(status_code=500, detail=f"Error al completar actividad: {str(e)}")
    
@app.get("/inscripciones/alumno/{rut_o_token}", tags=["Inscripciones"])
def obtener_inscripciones_por_alumno(rut_o_token: str):
    try:
        res_sesion = (
            supabase.table("sesion_usuario")
            .select("rut_usuario")
            .eq("token_acceso", rut_o_token)
            .execute()
        )
        rut_final = res_sesion.data[0]["rut_usuario"] if (res_sesion.data and len(res_sesion.data) > 0) else rut_o_token

        rut_sin_puntos = rut_final.replace(".", "")
        rut_sin_guion = rut_sin_puntos.replace("-", "")
        rut_con_puntos = formatear_rut_chileno(rut_final)

        formatos = list(set([rut_final.strip(), rut_sin_puntos, rut_sin_guion, rut_con_puntos]))
        condiciones = [f"rut_usuario.eq.{f}" for f in formatos]
        condicion_or = ",".join(condiciones)

        # Usar los campos REALES de las tablas inscripcion_act y actividad
        respuesta = (
            supabase.table("inscripcion_act")
            .select("""
                id_inscripcion,
                rut_usuario,
                id_actividad,
                puntos_ganados,
                fecha_inscripcion,
                actividad (
                    id_actividad,
                    nombre_actividad,
                    descripcion,
                    responsable_actividad,
                    img_actv,
                    fecha,
                    hora_inicio,
                    hora_termino,
                    id_estado_actividad,
                    estado_actividad ( descripcion ),
                    lugar_actividad ( descripcion ),
                    puntaje_act ( cantidad ),
                    asistencia_act ( presente, rut_usuario )
                )
            """)
            .or_(condicion_or)
            .execute()
        )

        inscripciones = respuesta.data if respuesta.data else []

        for ins in inscripciones:
            act = ins.get("actividad")
            if act:
                act["fecha_inicio"] = f"{act.get('fecha')}T{act.get('hora_inicio')}"
                act["fecha_termino"] = f"{act.get('fecha')}T{act.get('hora_termino')}"

                # FILTRAR ASISTENCIA CORRESPONDIENTE AL ALUMNO ACTUAL
                # Como una actividad tiene asistencias de muchos alumnos, tomamos la de este RUT
                asistencias = act.get("asistencia_act", [])
                asistencia_alumno = [
                    a for a in asistencias 
                    if a.get("rut_usuario") in formatos
                ]
                
                # Adjuntamos el objeto de asistencia individual directamente al objeto actividad
                act["asistencia_act"] = asistencia_alumno

        return inscripciones

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener inscripciones: {str(e)}")
    
def limpiar_rut(rut: str) -> str:
    """Elimina puntos y convierte a mayúsculas."""
    return re.sub(r'[\.]', '', rut).strip().upper()

@app.get("/actividades/disponibles/{rut_o_token}", tags=["Actividades"])
def obtener_actividades_disponibles_por_sede(rut_o_token: str):
    """
    Obtiene únicamente las actividades 'Programadas' y con 'Cupos Disponibles' 
    pertenecientes a la sede del alumno basadas en su RUT o Token.
    """
    try:
        # 1. Resolver el RUT desde la sesión si viene un token de acceso
        res_sesion = (
            supabase.table("sesion_usuario")
            .select("rut_usuario")
            .eq("token_acceso", rut_o_token)
            .execute()
        )

        rut_final = res_sesion.data[0]["rut_usuario"] if (res_sesion.data and len(res_sesion.data) > 0) else rut_o_token

        # 2. Sanitizar el RUT quitando los puntos (.) que Angular manda en la URL
        rut_sin_puntos = limpiar_rut(rut_final)
        rut_sin_guion = rut_sin_puntos.replace("-", "")
        rut_con_puntos = formatear_rut_chileno(rut_final)

        # Generar las variantes comunes para garantizar coincidencia en la BD
        formatos = list(set([rut_final.strip(), rut_sin_puntos, rut_sin_guion, rut_con_puntos]))
        
        # Filtro OR con las variantes
        condiciones = [f"rut_usuario.eq.{f}" for f in formatos]
        condicion_or = ",".join(condiciones)

        # 3. Buscar la sede del usuario
        res_usuario = (
            supabase.table("usuario")
            .select("id_sede, sede ( descripcion )")
            .or_(condicion_or)
            .execute()
        )

        # Fallback de búsqueda si los formatos exactos no hicieron match
        if not res_usuario.data:
            res_usuario = (
                supabase.table("usuario")
                .select("id_sede, sede ( descripcion )")
                .ilike("rut_usuario", f"%{rut_sin_guion}%")
                .execute()
            )

        if not res_usuario.data:
            raise HTTPException(
                status_code=404, 
                detail=f"Usuario con RUT '{rut_final}' no fue encontrado"
            )

        usuario_data = res_usuario.data[0]
        id_sede_usuario = usuario_data.get("id_sede")

        if not id_sede_usuario:
            return {
                "sede": "",
                "id_sede": None,
                "actividades": []
            }

        # Extraer el nombre de la sede
        nombre_sede = ""
        sede_obj = usuario_data.get("sede")
        if isinstance(sede_obj, dict):
            nombre_sede = sede_obj.get("descripcion", "")
        elif isinstance(sede_obj, list) and len(sede_obj) > 0:
            nombre_sede = sede_obj[0].get("descripcion", "")

        # 4. Consultar las actividades (agregando la relación con inscripcion_act para contar inscritos)
        respuesta = (
            supabase.table("actividad")
            .select("""
                id_actividad,
                nombre_actividad,
                descripcion,
                responsable_actividad,
                img_actv,
                fecha,
                hora_inicio,
                hora_termino,
                id_sede,
                id_estado_actividad,
                estado_actividad ( descripcion ),
                cupo_actividad ( cantidad ),
                lugar_actividad ( descripcion ),
                puntaje_act ( cantidad ),
                inscripcion_act ( id_inscripcion )
            """)
            .eq("id_sede", id_sede_usuario)
            .execute()
        )

        actividades_raw = respuesta.data if respuesta.data else []

        # 5. Filtrar actividades: solo 'Programadas' y con cupos disponibles
        actividades_procesadas = []
        for act in actividades_raw:
            try:
                # Normalización de la fecha de término
                fecha_str = str(act.get("fecha"))
                hora_term_str = str(act.get("hora_termino"))
                act["fecha_termino"] = f"{fecha_str}T{hora_term_str}"

                if 'calcular_estado_dinamico' in globals():
                    act = calcular_estado_dinamico(act)

                # --- VALIDACIÓN DE ESTADO PROGRAMADA ---
                estado_obj = act.get("estado_actividad")
                estado_desc = ""
                if isinstance(estado_obj, dict):
                    estado_desc = estado_obj.get("descripcion", "").lower()
                elif isinstance(estado_obj, list) and len(estado_obj) > 0:
                    estado_desc = estado_obj[0].get("descripcion", "").lower()

                # --- CÁLCULO DE CUPOS Y INSCRITOS ---
                cupo_obj = act.get("cupo_actividad")
                total_cupos = 0
                if isinstance(cupo_obj, dict):
                    total_cupos = cupo_obj.get("cantidad", 0)
                elif isinstance(cupo_obj, list) and len(cupo_obj) > 0:
                    total_cupos = cupo_obj[0].get("cantidad", 0)

                inscripciones = act.get("inscripcion_act") or []
                total_inscritos = len(inscripciones)
                cupos_restantes = total_cupos - total_inscritos

                act["cupos_restantes"] = cupos_restantes

                # FILTRO FINAL: Solo estado 'programada' y cupos disponibles > 0
                if "programada" in estado_desc and cupos_restantes > 0:
                    actividades_procesadas.append(act)

            except Exception as e_item:
                print(f"[WARN] No se pudo procesar actividad {act.get('id_actividad')}: {e_item}")

        return {
            "sede": nombre_sede,
            "id_sede": id_sede_usuario,
            "actividades": actividades_procesadas
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR OBTENER ACTIVIDADES DISPONIBLES]: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error interno al obtener actividades por sede: {str(e)}"
        )
    
@app.delete("/inscripciones", tags=["Inscripciones"])
def desinscribir_alumno_actividad(rut_alumno: str, id_actividad: int):
    """
    Elimina la inscripción del alumno únicamente si NO ha asistido a la actividad.
    Devuelve +1 al cupo de la actividad y notifica la anulación.
    """
    try:
        formatos = generar_formatos_rut(rut_alumno)
        condicion_or = ",".join([f'rut_usuario.eq."{f}"' for f in formatos])

        # 1. Buscar la inscripción y obtener datos del usuario
        res_ins = (
            supabase.table("inscripcion_act")
            .select("id_inscripcion, rut_usuario")
            .or_(condicion_or)
            .eq("id_actividad", id_actividad)
            .execute()
        )

        if not res_ins.data:
            raise HTTPException(status_code=404, detail="Inscripción no encontrada")

        id_inscripcion = res_ins.data[0]["id_inscripcion"]
        rut_real = res_ins.data[0]["rut_usuario"]

        # Obtener correo del alumno y nombre de la actividad para las notificaciones
        res_usr = supabase.table("usuario").select("correo").eq("rut_usuario", rut_real).execute()
        correo_alumno = res_usr.data[0].get("correo") if res_usr.data else None

        res_act = supabase.table("actividad").select("nombre_actividad").eq("id_actividad", id_actividad).execute()
        nombre_act = res_act.data[0].get("nombre_actividad") if res_act.data else f"la actividad #{id_actividad}"

        # 2. VALIDACIÓN DE ASISTENCIA: Verificar si el alumno ya asistió
        res_asis = (
            supabase.table("asistencia_act")
            .select("presente")
            .eq("id_actividad", id_actividad)
            .in_("rut_usuario", formatos)
            .execute()
        )

        if res_asis.data and len(res_asis.data) > 0:
            asistio = any(a.get("presente") is True for a in res_asis.data)
            if asistio:
                raise HTTPException(
                    status_code=400,
                    detail="No es posible desinscribirse de una actividad a la que ya asististe."
                )

        # 3. Eliminar inscripción
        supabase.table("inscripcion_act").delete().eq("id_inscripcion", id_inscripcion).execute()

        # 4. Eliminar registro de asistencia no efectiva
        supabase.table("asistencia_act").delete().eq("id_actividad", id_actividad).in_("rut_usuario", formatos).execute()

        # 5. DEVOLVER EL CUPO A LA ACTIVIDAD (+1)
        try:
            res_cupo = (
                supabase.table("cupo_actividad")
                .select("id_cupo, cantidad")
                .eq("id_actividad", id_actividad)
                .execute()
            )
            if res_cupo.data and len(res_cupo.data) > 0:
                id_cupo = res_cupo.data[0]["id_cupo"]
                cantidad_actual = res_cupo.data[0].get("cantidad", 0)
                
                # Incrementar en 1 el cupo disponible
                supabase.table("cupo_actividad").update({
                    "cantidad": cantidad_actual + 1
                }).eq("id_cupo", id_cupo).execute()
                print(f"[INFO] Cupo liberado para actividad #{id_actividad}. Nuevo cupo: {cantidad_actual + 1}")
        except Exception as e_cupo:
            print(f"[WARN] No se pudo actualizar la tabla 'cupo_actividad': {e_cupo}")

        # 6. Recalcular el puntaje acumulado
        actualizar_puntaje_total_alumno(rut_real)

        # 7. REGISTRAR NOTIFICACIÓN EN LA APP
        now = datetime.now()
        try:
            noti_retiro = {
                "rut_usuario": rut_real,
                "mensaje": f"Has anulado tu inscripción en la actividad: {nombre_act}.",
                "fecha_envio": now.isoformat(),
                "leido": False,
                "id_tipo_notificacion": 1
            }
            supabase.table("notificacion").insert(noti_retiro).execute()
        except Exception as e_noti:
            print(f"[WARN] No se pudo crear notificación de desinscripción: {e_noti}")

        # 8. ENVIAR CORREO ELECTRÓNICO DE RETIRO
        try:
            if correo_alumno:
                enviar_correo_inscripcion(
                    destinatario=correo_alumno,
                    nombre_actividad=nombre_act,
                    asunto=f"Confirmación de retiro - {nombre_act}",
                    mensaje_cuerpo=f"Te informamos que se ha procesado con éxito tu desinscripción de la actividad: <b>{nombre_act}</b>."
                )
        except Exception as e_mail:
            print(f"[WARN] No se pudo enviar el correo de desinscripción: {e_mail}")

        return {"mensaje": "Desinscripción realizada con éxito y cupo devuelto"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al desinscribir: {str(e)}") 
@app.get("/actividades/{id_actividad}/lista-inscritos", tags=["Actividades"])
def obtener_lista_estudiantes_inscritos(id_actividad: int):
    """
    Obtiene el listado de estudiantes inscritos en una actividad
    e indica si su asistencia ha sido confirmada.
    """
    try:
        # 1. Obtener inscritos en la actividad
        res_inscritos = (
            supabase.table("inscripcion_act")
            .select("rut_usuario, fecha_inscripcion, usuario(nombre_completo)")
            .eq("id_actividad", id_actividad)
            .execute()
        )

        inscritos = res_inscritos.data or []

        # 2. Obtener asistencias registradas
        res_asistencia = (
            supabase.table("asistencia_act")
            .select("rut_usuario, presente")
            .eq("id_actividad", id_actividad)
            .execute()
        )

        mapa_asistencia = {item["rut_usuario"]: item["presente"] for item in (res_asistencia.data or [])}

        estudiantes_resumen = []
        total_asistentes = 0

        for item in inscritos:
            rut = item["rut_usuario"]
            nombre = item.get("usuario", {}).get("nombre_completo") if isinstance(item.get("usuario"), dict) else "Estudiante"
            asistio = mapa_asistencia.get(rut, False)

            if asistio:
                total_asistentes += 1

            estudiantes_resumen.append({
                "rut": rut,
                "rut_usuario": rut,
                "nombre": nombre,
                "nombre_completo": nombre,
                "asistio": asistio
            })

        return {
            "id_actividad": id_actividad,
            "total_inscritos": len(estudiantes_resumen),
            "total_asistentes": total_asistentes,
            "estudiantes": estudiantes_resumen
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener estudiantes inscritos: {str(e)}")
# ==========================================
# CRUD PREMIOS
# ==========================================

# 1. Rutas generales sin parámetros en la URL
@app.get("/premios", tags=["Premios"])
def obtener_premios(rut_usuario: Optional[str] = None):
    try:
        res = supabase.table("premio").select(QUERY_RELACIONES_PREMIO).order("id_premio", desc=True).execute()
        premios = res.data or []

        # Si se envía el RUT, evaluamos el nivel actual del alumno
        if rut_usuario:
            info_nivel = obtener_nivel_alumno(rut_usuario)
            nivel_actual = info_nivel["id_nivel"]

            for premio in premios:
                id_nivel_req = premio.get("id_nivel") or 1
                premio["desbloqueado"] = nivel_actual >= id_nivel_req
                premio["info_nivel_usuario"] = info_nivel

        return premios
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# FUNCIÓN AUXILIAR PARA CONSULTAR NIVELES EN BATCH
# ==========================================
def obtener_niveles_masivo(ruts_unicos: list) -> dict:
    """
    Obtiene el nivel de todos los RUTs en una sola consulta
    en lugar de hacer N consultas individuales.
    """
    if not ruts_unicos:
        return {}

    try:
        # Consultar el conteo de actividades participadas para todos los RUTs de una vez
        res = (
            supabase.table("participacion_activa")
            .select("rut_usuario")
            .in_("rut_usuario", ruts_unicos)
            .execute()
        )
        
        # Constar cuantas actividades tiene cada RUT
        conteo_ruts = {}
        for reg in (res.data or []):
            rut = reg.get("rut_usuario")
            conteo_ruts[rut] = conteo_ruts.get(rut, 0) + 1

        # Mapear nivel según las actividades contadas
        niveles_map = {}
        for rut in ruts_unicos:
            total_act = conteo_ruts.get(rut, 0)

            if total_act >= 7:
                id_nivel = 3
                nombre_nivel = "Platino"
                faltantes = 0
            elif total_act >= 4:
                id_nivel = 2
                nombre_nivel = "Oro"
                faltantes = 7 - total_act
            else:
                id_nivel = 1
                nombre_nivel = "Bronce"
                faltantes = 4 - total_act

            niveles_map[rut] = {
                "id_nivel": id_nivel,
                "nombre_nivel": nombre_nivel,
                "total_actividades": total_act,
                "faltantes_siguiente": faltantes
            }

        return niveles_map

    except Exception as e:
        print(f"[ERROR OBTENER NIVELES BATCH]: {e}")
        # Retorno de respaldo en caso de error
        return {
            rut: {
                "id_nivel": 1,
                "nombre_nivel": "Bronce",
                "total_actividades": 0,
                "faltantes_siguiente": 4
            } for rut in ruts_unicos
        }


# ==========================================
# ENDPOINT OPTIMIZADO DE SOLICITUDES
# ==========================================
@app.get("/premios/solicitudes", tags=["Administración Canjes"])
def obtener_solicitudes_canje():
    try:
        res = (
            supabase.table("solicitud_canje")
            .select("""
                id_canje,
                fecha_solicitud,
                costo_puntaje,
                id_estado_canje,
                rut_usuario,
                id_premio,
                id_historial_canje,
                id_detalle,
                usuario:rut_usuario (
                    rut_usuario,
                    nombre_completo,
                    id_sede,
                    sede:id_sede (
                        id_sede,
                        descripcion
                    )
                ),
                premio:id_premio (
                    id_premio,
                    descripcion,
                    imagen,
                    puntos_requeridos,
                    id_nivel,
                    nivel:id_nivel (
                        id_nivel,
                        nombre_nivel,
                        min_actividades
                    ),
                    categoria_premio:id_categoria (
                        id_categoria,
                        descripcion
                    ),
                    stock_sede (
                        id_stock,
                        cantidad,
                        id_sede
                    )
                ),
                estado_canje:id_estado_canje (
                    id_estado_canje,
                    descripcion
                ),
                retiro_premio (
                    id_retiro,
                    fecha_limite,
                    retirado
                )
            """)
            .order("fecha_solicitud", desc=True)
            .limit(100) # Límite de rendimiento razonable
            .execute()
        )

        canjes = res.data or []

        # 1. Extraer los RUTs únicos de toda la lista
        ruts_unicos = list({item.get("rut_usuario") for item in canjes if item.get("rut_usuario")})

        # 2. Obtener los niveles de TODOS los RUTs en UNA sola llamada
        niveles_map = obtener_niveles_masivo(ruts_unicos)

        # 3. Asignar instantáneamente desde el diccionario en memoria O(1)
        nivel_default = {
            "id_nivel": 1,
            "nombre_nivel": "Bronce",
            "total_actividades": 0,
            "faltantes_siguiente": 4
        }

        for item in canjes:
            rut = item.get("rut_usuario")
            item["info_nivel_usuario"] = niveles_map.get(rut, nivel_default)

        return canjes

    except Exception as e:
        print(f"[ERROR OBTENER SOLICITUDES]: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Error al obtener las solicitudes de canje: {str(e)}"
        )


@app.get("/premios/solicitudes/responsable/{rut_responsable}", tags=["Administración Canjes"])
def obtener_solicitudes_canje_por_responsable(rut_responsable: str):
    try:
        # 1. Consultar en Supabase filtrando por el rut_usuario responsable en la tabla premio
        res = (
            supabase.table("solicitud_canje")
            .select("""
                id_canje,
                fecha_solicitud,
                costo_puntaje,
                id_estado_canje,
                rut_usuario,
                id_premio,
                id_historial_canje,
                id_detalle,
                usuario:rut_usuario (
                    rut_usuario,
                    nombre_completo,
                    id_sede,
                    sede:id_sede (
                        id_sede,
                        descripcion
                    )
                ),
                premio:id_premio!inner (
                    id_premio,
                    descripcion,
                    imagen,
                    puntos_requeridos,
                    id_nivel,
                    rut_usuario,
                    id_sede,
                    nivel:id_nivel (
                        id_nivel,
                        nombre_nivel,
                        min_actividades
                    ),
                    categoria_premio:id_categoria (
                        id_categoria,
                        descripcion
                    ),
                    stock_sede (
                        id_stock,
                        cantidad,
                        id_sede
                    )
                ),
                estado_canje:id_estado_canje (
                    id_estado_canje,
                    descripcion
                ),
                retiro_premio (
                    id_retiro,
                    fecha_limite,
                    retirado
                )
            """)
            .eq("premio.rut_usuario", rut_responsable) # Filtro a nivel de Supabase/PostgreSQL
            .order("fecha_solicitud", desc=True)
            .limit(100)
            .execute()
        )

        canjes = res.data or []

        if not canjes:
            return []

        # 2. Extraer los RUTs únicos de los estudiantes que solicitaron
        ruts_unicos = list({item.get("rut_usuario") for item in canjes if item.get("rut_usuario")})

        # 3. Obtener niveles de forma masiva
        niveles_map = obtener_niveles_masivo(ruts_unicos)

        nivel_default = {
            "id_nivel": 1,
            "nombre_nivel": "Bronce",
            "total_actividades": 0,
            "faltantes_siguiente": 4
        }

        for item in canjes:
            rut = item.get("rut_usuario")
            item["info_nivel_usuario"] = niveles_map.get(rut, nivel_default)

        return canjes

    except Exception as e:
        print(f"[ERROR OBTENER SOLICITUDES RESPONSABLE]: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Error al obtener las solicitudes a cargo del responsable: {str(e)}"
        )

# 3. Rutas dinámicas con parámetros al final
@app.get("/premios/{id_premio}", tags=["Premios"])
def obtener_premio_por_id(id_premio: int):
    try:
        res = supabase.table("premio").select(QUERY_RELACIONES_PREMIO).eq("id_premio", id_premio).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="El premio no existe")
        return res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/premios", status_code=status.HTTP_201_CREATED, tags=["Premios"])
def crear_premio(premio: PremioCreate):
    try:
        datos = premio.model_dump()
        stock_cantidad = datos.pop("stock", 0) or 0

        # Si id_nivel viene None, asignamos Nivel 1 (Bronce) por defecto
        if not datos.get("id_nivel"):
            datos["id_nivel"] = 1

        if datos.get("imagen") == "":
            datos["imagen"] = None

        res_premio = supabase.table("premio").insert(datos).execute()
        if not res_premio.data:
            raise HTTPException(status_code=400, detail="Error al registrar el premio")

        id_premio = res_premio.data[0]["id_premio"]

        supabase.table("stock_sede").insert({
            "cantidad": stock_cantidad,
            "id_premio": id_premio,
            "id_sede": premio.id_sede
        }).execute()

        premio_creado = supabase.table("premio").select(QUERY_RELACIONES_PREMIO).eq("id_premio", id_premio).execute()
        return premio_creado.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/premios/{id_premio}", tags=["Premios"])
def actualizar_premio(id_premio: int, premio: PremioUpdate):
    try:
        # 1. Obtener premio actual
        check = supabase.table("premio").select("id_premio, imagen, id_sede, id_nivel").eq("id_premio", id_premio).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="Premio no encontrado")

        premio_actual = check.data[0]
        img_antigua = premio_actual.get("imagen")
        id_sede_actual = premio_actual.get("id_sede")

        datos = premio.model_dump(exclude_unset=True)

        # 2. Manejo de imagen
        if "imagen" in datos and datos["imagen"] == "":
            datos["imagen"] = None

        if "imagen" in datos:
            nueva_img = datos["imagen"]
            if img_antigua and nueva_img != img_antigua:
                eliminar_imagen_storage(img_antigua, bucket_default="premios")

        # Extraer stock antes de actualizar 'premio'
        stock_cantidad = datos.pop("stock", None)

        # Determinar la sede final
        id_sede_objetivo = datos.get("id_sede", id_sede_actual)

        # 3. Actualizar la tabla principal 'premio'
        if datos:
            supabase.table("premio").update(datos).eq("id_premio", id_premio).execute()

        # 4. REASIGNACIÓN Y LIMPIEZA DE 'stock_sede'
        if stock_cantidad is not None or "id_sede" in datos:
            # Consultar todos los registros de stock de este premio
            s_res = (
                supabase.table("stock_sede")
                .select("id_stock, id_sede")
                .eq("id_premio", id_premio)
                .execute()
            )

            if s_res.data:
                # Tomamos la primera fila de stock asociada a este premio
                id_stock_existente = s_res.data[0]["id_stock"]
                
                payload_stock = {
                    "id_sede": id_sede_objetivo
                }
                if stock_cantidad is not None:
                    payload_stock["cantidad"] = stock_cantidad

                # REASIGNAMOS la sede y la cantidad en la misma fila de la BD
                supabase.table("stock_sede").update(payload_stock).eq("id_stock", id_stock_existente).execute()

                # Si por alguna razón existían duplicados de registros antiguos para este id_premio, los eliminamos
                if len(s_res.data) > 1:
                    ids_sobrantes = [item["id_stock"] for item in s_res.data[1:]]
                    supabase.table("stock_sede").delete().in_("id_stock", ids_sobrantes).execute()
            else:
                # Si el premio no tenía ninguna fila en stock_sede, la creamos
                cant_insertar = stock_cantidad if stock_cantidad is not None else 0
                supabase.table("stock_sede").insert({
                    "cantidad": cant_insertar,
                    "id_premio": id_premio,
                    "id_sede": id_sede_objetivo
                }).execute()

        # 5. Retornar el premio actualizado con sus relaciones completas
        res = supabase.table("premio").select(QUERY_RELACIONES_PREMIO).eq("id_premio", id_premio).execute()
        return res.data[0]

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR ACTUALIZAR PREMIO]: {e}")
        raise HTTPException(status_code=500, detail=str(e))
     
@app.delete("/premios/{id_premio}", tags=["Premios"])
def eliminar_premio(id_premio: int):
    try:
        check = supabase.table("premio").select("id_premio, imagen").eq("id_premio", id_premio).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="Premio no encontrado")

        canjes = supabase.table("solicitud_canje").select("id_canje").eq("id_premio", id_premio).limit(1).execute()
        if canjes.data:
            raise HTTPException(
                status_code=400, 
                detail="No se puede eliminar el premio porque tiene solicitudes de canje asociadas. Cambia su visibilidad a oculta."
            )

        img_antigua = check.data[0].get("imagen")

        supabase.table("stock_sede").delete().eq("id_premio", id_premio).execute()
        supabase.table("premio").delete().eq("id_premio", id_premio).execute()

        if img_antigua:
            eliminar_imagen_storage(img_antigua, bucket_default="premios")

        return {"mensaje": f"Premio {id_premio} e imagen asociada eliminados correctamente"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# CATÁLOGOS AUXILIARES Y USUARIOS
# ==========================================

@app.get("/docentes", tags=["Catálogos"])
def obtener_docentes():
    try:
        res = supabase.table("usuario")\
            .select("rut_usuario, nombre_completo, correo")\
            .in_("id_tipo_usuario", [2, 3])\
            .execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tipos-actividad", tags=["Catálogos"])
def obtener_tipos_actividad():
    try:
        res = supabase.table("tipo_actividad").select("*").execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/estados-actividad", tags=["Catálogos"])
def obtener_estados_actividad():
    try:
        res = supabase.table("estado_actividad").select("*").execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/categorias-premio", tags=["Catálogos"])
def obtener_categorias_premio():
    try:
        res = supabase.table("categoria_premio").select("*").execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sedes", tags=["Catálogos"])
def obtener_sedes():
    try:
        res = supabase.table("sede").select("*").execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/consejeros", tags=["Catálogos"])
def obtener_consejeros():
    try:
        res = supabase.table("usuario")\
            .select("rut_usuario, nombre_completo, correo, id_tipo_usuario")\
            .eq("id_tipo_usuario", 4)\
            .execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


QUERY_RELACIONES_CANJE = """
    *,
    estado_canje(descripcion),
    premio(descripcion, imagen),
    usuario(nombre_completo, correo)
"""

@app.get("/solicitudes-canje", tags=["Canjes"])
def obtener_solicitudes_canje():
    try:
        res = (
            supabase.table("solicitud_canje")
            .select(QUERY_RELACIONES_CANJE)
            .order("id_canje", desc=True)
            .execute()
        )
        return res.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/usuario/sesion/{rut_o_token}", tags=["Usuarios"])
def obtener_usuario_por_sesion(rut_o_token: str):
    try:
        # 1. Resolver si rut_o_token es un token de sesión o el RUT directo
        res_sesion = (
            supabase.table("sesion_usuario")
            .select("rut_usuario")
            .eq("token_acceso", rut_o_token)
            .execute()
        )

        rut_final = res_sesion.data[0]["rut_usuario"] if res_sesion.data else rut_o_token

        # 2. Generar formatos de RUT compatibles para la consulta
        formatos = generar_formatos_rut(rut_final)
        condicion_or = ",".join([f"rut_usuario.eq.{f}" for f in formatos])

        # 3. Obtener los datos del usuario
        res_usuario = (
            supabase.table("usuario")
            .select("rut_usuario, nombre_completo, correo, id_tipo_usuario, id_sede")
            .or_(condicion_or)
            .execute()
        )

        if not res_usuario.data:
            raise HTTPException(status_code=404, detail="Usuario no encontrado.")

        user_data = res_usuario.data[0]

        # 4. Obtener puntaje acumulado desde la tabla 'puntaje_total'
        res_puntos = (
            supabase.table("puntaje_total")
            .select("puntaje")
            .or_(condicion_or)
            .execute()
        )

        puntos_acumulados = res_puntos.data[0]["puntaje"] if res_puntos.data else 0
        user_data["puntaje_total"] = puntos_acumulados

        return user_data

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error consultando sesión de usuario ({rut_o_token}): {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------------------
# FUNCIÓN AUXILIAR: NOTIFICACIONES SEGÚN TU TABLA
# ---------------------------------------------------------
def enviar_correo_inscripcion(destinatario: str, nombre_actividad: str, asunto: str = None, mensaje_cuerpo: str = None):
    """Envía correos institucionales garantizando la entregabilidad en Bandeja de Entrada."""
    try:
        email_emisor = os.getenv("EMAIL_USER")
        password_emisor = os.getenv("EMAIL_PASS")

        if not email_emisor or not password_emisor or not destinatario:
            print("[WARN] Faltan credenciales o destinatario para el envío del correo.")
            return

        asunto_mail = asunto or f"Inscripción Confirmada - {nombre_actividad}"
        cuerpo_mail = mensaje_cuerpo or f"Te has inscrito correctamente a la actividad: <b>{nombre_actividad}</b>."

        # Construcción directa con EmailMessage
        msg = EmailMessage()
        msg['From'] = f"Aquí Todos Ganan <{email_emisor}>"
        msg['To'] = destinatario
        msg['Subject'] = asunto_mail
        msg['Reply-To'] = email_emisor

        # 1. Versión en texto plano (imprescindible para que Gmail no lo considere Spam)
        # Extrae el texto sin etiquetas HTML para clientes que solo leen texto
        texto_plano = cuerpo_mail.replace("<br>", "\n").replace("<b>", "").replace("</b>", "")
        msg.set_content(texto_plano, charset='utf-8')

        # 2. Versión HTML compatible
        contenido_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
</head>
<body style="font-family: Arial, sans-serif; background-color: #031326; color: #ffffff; padding: 20px; margin: 0;">
    <div style="max-width: 500px; margin: 0 auto; background-color: #071a30; border: 1px solid #f5b800; border-radius: 12px; padding: 25px;">
        <h2 style="color: #f5b800; margin-top: 0; text-align: center;">Aquí Todos Ganan</h2>
        <hr style="border: 0; height: 1px; background: #f5b800; margin: 20px 0;">
        <div style="color: #cbd5e1; font-size: 15px; line-height: 1.6;">
            {cuerpo_mail}
        </div>
    </div>
</body>
</html>"""

        msg.add_alternative(contenido_html, subtype='html', charset='utf-8')

        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10) as smtp:
            smtp.login(email_emisor, password_emisor)
            smtp.send_message(msg)

        print(f"[SUCCESS] Correo enviado exitosamente a {destinatario}")

    except Exception as e:
        print(f"[WARN] Error al enviar el correo: {e}")
 
def registrar_notificacion(rut_usuario: str, id_canje: int, mensaje: str, id_tipo_notificacion: int = 1):
    """
    Inserta sincrónicamente una notificación en Supabase.
    """
    try:
        payload = {
            "rut_usuario": rut_usuario,
            "id_canje": id_canje,
            "mensaje": mensaje,
            "fecha_envio": datetime.now().isoformat(),
            "leido": False,
            "id_tipo_notificacion": id_tipo_notificacion
        }
        res = supabase.table("notificacion").insert(payload).execute()
        print(f"[DEBUG NOTI OK] Notificación insertada para RUT {rut_usuario}: {res.data}")
    except Exception as e:
        print(f"[ERROR NOTIFICACION BD]: {e}")
def generar_formatos_rut(rut: str) -> list[str]:
    """Genera variantes con y sin puntos/guion para búsquedas seguras en Supabase"""
    clean = rut.replace(".", "").replace("-", "").strip().upper()
    if len(clean) < 2:
        return [rut]
    cuerpo, dv = clean[:-1], clean[-1]
    
    # Formato con puntos y guión (ej: 12.345.678-9)
    cuerpo_fmt = f"{int(cuerpo):,}".replace(",", ".")
    rut_con_puntos = f"{cuerpo_fmt}-{dv}"
    
    # Formato solo con guión (ej: 12345678-9)
    rut_con_guion = f"{cuerpo}-{dv}"
    
    # Formato limpio (ej: 123456789)
    return list(set([clean, rut_con_guion, rut_con_puntos, rut]))

def enviar_correo_canje(destinatario: str, asunto: str, cuerpo_html: str, nombre_premio: str):
    """
    Invocación directa a la infraestructura SMTP.
    """
    try:
        print(f"[INFO SMTP] Enviando correo a '{destinatario}'...")
        # Usa la función SMTP real configurada en tu backend
        enviar_correo_inscripcion(
            destinatario=destinatario,
            nombre_actividad=nombre_premio,
            asunto=asunto,
            mensaje_cuerpo=cuerpo_html
        )
        print(f"[INFO SMTP OK] Correo enviado exitosamente a {destinatario}")
    except Exception as e:
        print(f"[ERROR SMTP]: Fallo al enviar el correo a {destinatario}: {e}")

@app.post("/premios/canjear", status_code=status.HTTP_201_CREATED, tags=["Canjes"])
def solicitar_canje_premio(datos: CanjeRequest, background_tasks: BackgroundTasks):
    """
    Registra la solicitud de canje en la BD (Pendiente de aprobación):
    1. Resuelve sesión y datos del alumno (incluyendo id_sede y correo).
    2. Valida nivel mínimo del estudiante vs nivel del premio.
    3. Valida que NO exista una solicitud activa en estado Pendiente/Aprobado.
    4. Valida puntos del alumno en 'puntaje_total'.
    5. Valida stock en 'stock_sede'.
    6. Inserta registros en detalle_canje, historial_canje y solicitud_canje.
    7. Guarda la notificación en la BD y programa el correo de confirmación.
    """
    try:
        # Marca de tiempo global para la transacción
        now_str = datetime.now().isoformat()
        rut_o_token = datos.rut_alumno.strip()

        # 1. Obtener el RUT desde sesion_usuario si envían token
        res_sesion = (
            supabase.table("sesion_usuario")
            .select("rut_usuario")
            .eq("token_acceso", rut_o_token)
            .execute()
        )
        rut_evaluar = (
            res_sesion.data[0]["rut_usuario"] 
            if (res_sesion.data and len(res_sesion.data) > 0) 
            else rut_o_token
        )
        formatos_rut = generar_formatos_rut(rut_evaluar)

        # 2. Obtener datos completos del Usuario (RUT verificado, correo e id_sede)
        res_user = (
            supabase.table("usuario")
            .select("rut_usuario", "id_sede", "nombre_completo", "correo")
            .in_("rut_usuario", formatos_rut)
            .execute()
        )
        if not res_user.data:
            raise HTTPException(status_code=404, detail="Usuario no encontrado en la base de datos.")

        usuario = res_user.data[0]
        rut_real = usuario["rut_usuario"]
        correo_usuario = usuario.get("correo")
        id_sede_usuario = usuario.get("id_sede")

        if not id_sede_usuario:
            raise HTTPException(status_code=400, detail="El usuario no tiene una sede asignada en el sistema.")

        # 3. Obtener datos del Premio e ID Nivel Requerido
        res_premio = (
            supabase.table("premio")
            .select("*, nivel(nombre_nivel)")
            .eq("id_premio", datos.id_premio)
            .execute()
        )
        if not res_premio.data:
            raise HTTPException(status_code=404, detail="El premio seleccionado no existe.")

        premio = res_premio.data[0]
        puntos_requeridos = premio.get("puntos_requeridos", 0)
        descripcion_premio = premio.get("descripcion", "Premio de Catálogo")
        id_nivel_requerido = int(premio.get("id_nivel") or 1)
        nombre_nivel_req = premio.get("nivel", {}).get("nombre_nivel") if isinstance(premio.get("nivel"), dict) else "Bronce"

        # --- VALIDACIÓN DE NIVEL DEL ESTUDIANTE ---
        nivel_estudiante = obtener_nivel_alumno(rut_real)
        if nivel_estudiante.get("id_nivel", 1) < id_nivel_requerido:
            raise HTTPException(
                status_code=400,
                detail=f"No cumples con el nivel necesario para solicitar este premio. Nivel actual: {nivel_estudiante.get('nombre_nivel')}, Nivel requerido: {nombre_nivel_req}."
            )

        # 4. Obtener id_estado_canje para 'Solicitado' y 'Aprobado'
        res_estados = supabase.table("estado_canje").select("id_estado_canje, descripcion").execute()
        id_estado_solicitado = 1
        ids_estados_activos = [1, 2] # 1: Solicitado, 2: Aprobado

        if res_estados.data:
            for est in res_estados.data:
                desc = est.get("descripcion", "").lower()
                if "solicitad" in desc or "pendient" in desc:
                    id_estado_solicitado = est["id_estado_canje"]

        # Validar solicitud previa activa
        sol_activa = (
            supabase.table("solicitud_canje")
            .select("id_canje, id_estado_canje")
            .in_("rut_usuario", formatos_rut)
            .eq("id_premio", datos.id_premio)
            .in_("id_estado_canje", ids_estados_activos)
            .execute()
        )
        if sol_activa.data:
            raise HTTPException(
                status_code=400, 
                detail="Ya tienes una solicitud pendiente o aprobada para este premio. Espera a que sea procesada o cancelada."
            )

        # 5. Validar Puntos Disponibles en 'puntaje_total'
        res_pts = (
            supabase.table("puntaje_total")
            .select("id_puntaje", "puntaje")
            .in_("rut_usuario", formatos_rut)
            .execute()
        )
        if not res_pts.data:
            raise HTTPException(status_code=400, detail="El alumno no tiene un registro de puntaje asignado.")

        reg_puntaje = res_pts.data[0]
        puntos_actuales = reg_puntaje.get("puntaje", 0)

        if puntos_actuales < puntos_requeridos:
            raise HTTPException(
                status_code=400,
                detail=f"Puntos insuficientes. Tienes {puntos_actuales} pts y el premio requiere {puntos_requeridos} pts."
            )

        # 6. Validar Stock en 'stock_sede'
        try:
            id_sede_int = int(id_sede_usuario)
            id_premio_int = int(datos.id_premio)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail=f"Identificadores de sede o premio inválidos."
            )

        res_stock = (
            supabase.table("stock_sede")
            .select("id_stock, cantidad, id_sede")
            .eq("id_premio", id_premio_int)
            .eq("id_sede", id_sede_int)
            .execute()
        )

        if not res_stock.data:
            id_sede_creadora = premio.get("id_sede")
            if id_sede_creadora == id_sede_int:
                res_nuevo = supabase.table("stock_sede").insert({
                    "id_premio": id_premio_int,
                    "id_sede": id_sede_int,
                    "cantidad": 1
                }).execute()
                stock_actual = res_nuevo.data[0].get("cantidad", 0) if res_nuevo.data else 0
            else:
                raise HTTPException(status_code=400, detail="Este premio no está disponible para tu sede.")
        else:
            stock_actual = res_stock.data[0].get("cantidad", 0)

        if stock_actual <= 0:
            raise HTTPException(status_code=400, detail="Este premio se encuentra agotado en tu sede.")

        # 7. REGISTRO DE SOLICITUD EN BASE DE DATOS
        res_detalle = supabase.table("detalle_canje").insert({
            "fecha": now_str,
            "descripcion": f"Solicitud Canje: {descripcion_premio}",
            "lugar_entrega": datos.lugar_entrega,
            "puntos_usados": puntos_requeridos
        }).execute()

        if not res_detalle.data:
            raise HTTPException(status_code=500, detail="Error al generar el detalle de la solicitud.")
        id_detalle = res_detalle.data[0]["id_detalle"]

        res_historial = supabase.table("historial_canje").insert({
            "fecha_canje": now_str,
            "rut_usuario": rut_real
        }).execute()

        if not res_historial.data:
            raise HTTPException(status_code=500, detail="Error al registrar el historial de la solicitud.")
        id_historial_canje = res_historial.data[0]["id_historial_canje"]

        res_solicitud = supabase.table("solicitud_canje").insert({
            "fecha_solicitud": now_str,
            "costo_puntaje": puntos_requeridos,
            "id_estado_canje": id_estado_solicitado,
            "id_historial_canje": id_historial_canje,
            "rut_usuario": rut_real,
            "id_premio": datos.id_premio,
            "id_detalle": id_detalle
        }).execute()

        if not res_solicitud.data:
            raise HTTPException(status_code=500, detail="Error al guardar la solicitud de canje.")
        id_canje = res_solicitud.data[0]["id_canje"]

        # 8. REGISTRAR NOTIFICACIÓN EN LA BASE DE DATOS (Sincrónico)
        try:
            res_tipo = supabase.table("tipo_notificacion").select("id_tipo_notificacion").limit(1).execute()
            id_tipo = res_tipo.data[0]["id_tipo_notificacion"] if res_tipo.data else 1

            supabase.table("notificacion").insert({
                "mensaje": f"Solicitud de canje #{id_canje} enviada para '{descripcion_premio}'. Pendiente de aprobación.",
                "fecha_envio": now_str,
                "leido": False,
                "id_canje": id_canje,
                "id_tipo_notificacion": id_tipo,
                "rut_usuario": rut_real
            }).execute()
            print(f"[DEBUG NOTI SOLICITUD] Notificación insertada con éxito para RUT: {rut_real}")
        except Exception as e_noti:
            print(f"[WARN NOTIFICACIÓN BD]: No se pudo crear la notificación del canje: {e_noti}")

        # 9. ENVIAR CORREO ELECTRÓNICO INSTITUCIONAL
        if correo_usuario:
            print(f"[INFO SMTP] Programando correo de confirmación de solicitud enviada a: {correo_usuario}")
            cuerpo_html = f"""
                ¡Hola!<br><br>
                Hemos recibido exitosamente tu solicitud de canje para el premio <b>{descripcion_premio}</b>.<br><br>
                📦 <b>Código de Solicitud:</b> #{id_canje}<br>
                ⭐ <b>Costo en Puntos:</b> {puntos_requeridos} pts<br>
                🎖️ <b>Nivel del Premio:</b> {nombre_nivel_req}<br><br>
                Tu solicitud pasará a revisión por el equipo DAE. Te notificaremos cuando sea procesada.
            """
            background_tasks.add_task(
                enviar_correo_inscripcion,
                destinatario=correo_usuario,
                nombre_actividad=descripcion_premio,
                asunto=f"Solicitud de Canje Recibida - {descripcion_premio}",
                mensaje_cuerpo=cuerpo_html
            )
        else:
            print(f"[ALERTA CORREO] No se encontró dirección de correo para el RUT: '{rut_real}'")

        return {
            "mensaje": f"¡Solicitud de canje #{id_canje} registrada con éxito! Pendiente de aprobación.",
            "saldo_restante": puntos_actuales,
            "id_canje": id_canje
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR CANJE SOLICITUD]: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error interno procesando la solicitud de canje: {str(e)}"
        )


# ---------------------------------------------------------
# 0. OBTENER TODO EL HISTORIAL DE CANJES
# ---------------------------------------------------------
@app.get("/canjes/historial", tags=["Canjes"])
async def obtener_historial_canjes():
    try:
        # Incluye relaciones completas con usuario, premio, estado, detalle y retiro
        res = supabase.table("solicitud_canje")\
            .select("""
                id_canje,
                fecha_solicitud,
                costo_puntaje,
                id_estado_canje,
                rut_usuario,
                id_premio,
                usuario (nombre_completo, correo),
                premio (descripcion, imagen, puntos_requeridos),
                estado_canje (descripcion),
                detalle_canje (descripcion, lugar_entrega, puntos_usados),
                retiro_premio (id_retiro, fecha_limite, retirado)
            """)\
            .order("fecha_solicitud", desc=True)\
            .execute()
        return res.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# ENDPOINTS ADMINISTRACIÓN DE CANJES
# ==========================================
# ==========================================
# HELPER: CÁLCULO DE NIVEL DEL ESTUDIANTE
# ==========================================
def obtener_nivel_alumno(rut_usuario: str) -> dict:
    """
    Calcula el nivel del estudiante según sus asistencias presentes en 'asistencia_act'.
    """
    formatos_rut = generar_formatos_rut(rut_usuario)
    
    total_act = 0
    try:
        res = (
            supabase.table("asistencia_act")
            .select("id_asistencia_actividad", count="exact")
            .in_("rut_usuario", formatos_rut)
            .eq("presente", True)
            .execute()
        )
        total_act = res.count if res and res.count is not None else 0
    except Exception as e:
        print(f"[WARN] Error al consultar asistencia_act para RUT '{rut_usuario}': {e}")
        total_act = 0
    
    # Regla de niveles (Bronce: 0-3, Oro: 4-6, Platino: 7+)
    if total_act >= 7:
        nivel_id = 3
        nombre = "Platino"
        faltantes = 0
    elif total_act >= 4:
        nivel_id = 2
        nombre = "Oro"
        faltantes = 7 - total_act
    else:
        nivel_id = 1
        nombre = "Bronce"
        faltantes = 4 - total_act

    return {
        "id_nivel": nivel_id,
        "nombre_nivel": nombre,
        "total_actividades": total_act,
        "faltantes_siguiente": faltantes
    }

# ==============================================================================
# 1. APROBAR SOLICITUD DE CANJE
# ==============================================================================
@app.put("/premios/aprobar/{id_canje}", tags=["Administración Canjes"])
def aprobar_solicitud_canje(id_canje: int, background_tasks: BackgroundTasks):
    try:
        now = datetime.now()
        now_str = now.isoformat()
        fecha_limite_dt = now + timedelta(days=15)
        fecha_limite_str = fecha_limite_dt.isoformat()

        # 1. Obtener la solicitud
        res_sol = supabase.table("solicitud_canje").select("*").eq("id_canje", id_canje).execute()
        if not res_sol.data:
            raise HTTPException(status_code=404, detail="Solicitud de canje no encontrada.")
        
        solicitud = res_sol.data[0]
        rut_solicitud = str(solicitud.get("rut_usuario", "")).strip()
        id_premio = solicitud.get("id_premio")
        
        try:
            costo_puntos = int(solicitud.get("costo_puntaje") or 0)
        except (ValueError, TypeError):
            costo_puntos = 0

        # Obtener id del estado 'Aprobado'
        res_est_aprobado = supabase.table("estado_canje").select("id_estado_canje").ilike("descripcion", "%Aprobado%").execute()
        if not res_est_aprobado.data:
            raise HTTPException(status_code=500, detail="Estado 'Aprobado' no configurado en 'estado_canje'.")
        
        id_estado_aprobado = res_est_aprobado.data[0]["id_estado_canje"]

        if solicitud.get("id_estado_canje") == id_estado_aprobado:
            raise HTTPException(status_code=400, detail="Esta solicitud ya ha sido aprobada previamente.")

        # Obtener descripción del premio
        nombre_premio = f"Premio #{id_premio}"
        try:
            res_premio_info = supabase.table("premio").select("descripcion").eq("id_premio", id_premio).execute()
            if res_premio_info.data:
                nombre_premio = res_premio_info.data[0].get("descripcion") or nombre_premio
        except Exception as e_p:
            print(f"[WARN] No se pudo obtener la descripción del premio: {e_p}")

        # 2. Búsqueda del usuario para obtener correo, id_sede y rut oficial
        formatos_rut = generar_formatos_rut(rut_solicitud)
        correo_alumno = None
        id_sede_user = None
        rut_usuario_oficial = rut_solicitud

        try:
            res_usr = supabase.table("usuario").select("rut_usuario, correo, id_sede").in_("rut_usuario", formatos_rut).execute()
            if not res_usr.data:
                rut_limpio = rut_solicitud.replace(".", "").replace("-", "")
                res_usr = supabase.table("usuario").select("rut_usuario, correo, id_sede").ilike("rut_usuario", f"%{rut_limpio}%").execute()

            if res_usr.data:
                usr = res_usr.data[0]
                rut_usuario_oficial = usr.get("rut_usuario") or rut_solicitud
                correo_alumno = usr.get("correo")
                id_sede_user = usr.get("id_sede")
                print(f"[DEBUG CORREO] Correo detectado para aprobación: {correo_alumno}")
            else:
                print(f"[WARN CORREO] No se encontró usuario en BD para RUT: {rut_solicitud}")
        except Exception as e_u:
            print(f"[WARN] Error consultando usuario: {e_u}")

        # Puntos del alumno
        res_pts = supabase.table("puntaje_total").select("id_puntaje, puntaje, rut_usuario").in_("rut_usuario", formatos_rut).execute()
        if not res_pts.data:
            raise HTTPException(status_code=400, detail=f"El alumno con RUT '{rut_solicitud}' no tiene un registro en 'puntaje_total'.")

        reg_pts = res_pts.data[0]
        id_puntaje_exacto = reg_pts["id_puntaje"]
        rut_exacto_pts = reg_pts["rut_usuario"]
        
        try:
            puntos_actuales = int(reg_pts.get("puntaje") or 0)
        except (ValueError, TypeError):
            puntos_actuales = 0

        if puntos_actuales < costo_puntos:
            raise HTTPException(
                status_code=400, 
                detail=f"El alumno no tiene puntos suficientes ({puntos_actuales} pts disponibles, requiere {costo_puntos} pts)."
            )

        nuevo_saldo = max(0, puntos_actuales - costo_puntos)

        # 3. DESCONTAR PUNTAJE EN 'puntaje_total'
        try:
            supabase.table("puntaje_total").update({"puntaje": nuevo_saldo}).eq("id_puntaje", id_puntaje_exacto).execute()
        except Exception as e_upd:
            print(f"[WARN] Error al actualizar por id_puntaje, reintentando por rut: {e_upd}")
            supabase.table("puntaje_total").update({"puntaje": nuevo_saldo}).eq("rut_usuario", rut_exacto_pts).execute()

        # 4. DESCONTAR STOCK EN 'stock_sede'
        nuevo_stock = 0
        try:
            res_stock = None
            if id_sede_user:
                res_stock = supabase.table("stock_sede").select("id_stock, cantidad").eq("id_premio", id_premio).eq("id_sede", id_sede_user).execute()

            if not res_stock or not res_stock.data:
                res_stock = supabase.table("stock_sede").select("id_stock, cantidad").eq("id_premio", id_premio).execute()

            if res_stock and res_stock.data:
                reg_stock = res_stock.data[0]
                stock_actual = int(reg_stock.get("cantidad") or 0)
                nuevo_stock = max(0, stock_actual - 1)
                supabase.table("stock_sede").update({"cantidad": nuevo_stock}).eq("id_stock", reg_stock["id_stock"]).execute()

                if nuevo_stock <= 3:
                    hoy_str = now.strftime("%Y-%m-%d")
                    print(f"[ALERTA STOCK BAJO] [STOCK-{id_premio}-{hoy_str}] Al premio '{nombre_premio}' le quedan {nuevo_stock} unidades.")
        except Exception as e_stk:
            print(f"[WARN] Error al actualizar stock_sede: {e_stk}")

        # 5. ACTUALIZAR ESTADO DE SOLICITUD
        supabase.table("solicitud_canje").update({"id_estado_canje": id_estado_aprobado}).eq("id_canje", id_canje).execute()

        # 6. REGISTRAR CONTROL EN 'retiro_premio'
        try:
            res_retiro = supabase.table("retiro_premio").select("*").eq("id_canje", id_canje).execute()
            if not res_retiro.data:
                supabase.table("retiro_premio").insert({
                    "id_canje": id_canje,
                    "fecha_limite": fecha_limite_str,
                    "retirado": False
                }).execute()
        except Exception as e_retiro:
            print(f"[WARN] Error insertando en 'retiro_premio': {e_retiro}")

        # 7. REGISTRAR HISTORIAL DE PUNTOS
        try:
            supabase.table("historial_puntos").insert({
                "rut_usuario": rut_usuario_oficial or rut_exacto_pts,
                "puntos_actuales": nuevo_saldo,
                "puntos_canjeados": costo_puntos,
                "puntos_totales_obtenidos": puntos_actuales
            }).execute()
        except Exception as e_hist:
            print(f"[WARN] No se pudo registrar historial_puntos: {e_hist}")

        # 8.A. INSERCIÓN DE NOTIFICACIÓN EN BD
        fecha_formateada = fecha_limite_dt.strftime("%d/%m/%Y")
        msg_notificacion = f"¡Tu solicitud #{id_canje} para '{nombre_premio}' ha sido APROBADA! Tienes hasta el {fecha_formateada} para retirarlo en DAE."
        rut_para_notificacion = rut_usuario_oficial or rut_solicitud

        try:
            payload_noti = {
                "rut_usuario": rut_para_notificacion,
                "mensaje": msg_notificacion,
                "fecha_envio": now_str,
                "leido": False,
                "id_canje": id_canje,
                "id_tipo_notificacion": 1
            }
            res_noti = supabase.table("notificacion").insert(payload_noti).execute()
            print(f"[DEBUG NOTI OK] Notificación de aprobación insertada para RUT '{rut_para_notificacion}': {res_noti.data}")
        except Exception as e_noti_db:
            print(f"[ERROR CRÍTICO NOTI BD APROBAR]: {e_noti_db}")

        # 8.B. PROGRAMACIÓN DE CORREO SMTP EN SEGUNDO PLANO (Estructura idéntica a solicitar_canje_premio)
        if correo_alumno:
            print(f"[INFO SMTP] Programando envío de correo de aprobación a: {correo_alumno}")
            cuerpo_html = f"¡Felicitaciones!<br><br>Tu solicitud de canje para el premio <b>{nombre_premio}</b> ha sido aprobada.<br><br>📦 <b>Código de Solicitud:</b> #{id_canje}<br>⭐ <b>Puntos canjeados:</b> -{costo_puntos} pts<br>🏆 <b>Tu nuevo saldo:</b> {nuevo_saldo} pts<br>📅 <b>Plazo máximo de retiro:</b> Hasta el {fecha_formateada}<br><br>📍 Acércate a la oficina DAE con tu credencial institucional para realizar el retiro."
            
            background_tasks.add_task(
                enviar_correo_inscripcion,
                destinatario=correo_alumno,
                nombre_actividad=nombre_premio,
                asunto=f"Solicitud de Canje Aprobada - {nombre_premio}",
                mensaje_cuerpo=cuerpo_html
            )
        else:
            print(f"[ALERTA CORREO] No se pudo enviar correo porque 'correo_alumno' es None. RUT: '{rut_solicitud}'")

        return {
            "message": f"Solicitud #{id_canje} aprobada con éxito.",
            "saldo_anterior": puntos_actuales,
            "nuevo_saldo": nuevo_saldo,
            "stock_restante": nuevo_stock,
            "fecha_limite_retiro": fecha_limite_str
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR CRÍTICO APROBAR CANJE]: {e}")
        raise HTTPException(status_code=500, detail=f"Error al aprobar canje: {str(e)}")


# ==============================================================================
# 2. MARCAR COMO RETIRADO / ENTREGADO PRESENCIALMENTE
# ==============================================================================
@app.put("/premios/marcar-retirado/{id_canje}", tags=["Administración Canjes"])
def marcar_como_retirado(id_canje: int, background_tasks: BackgroundTasks):
    try:
        now = datetime.now()
        now_str = now.isoformat()

        # 1. Obtener datos de la solicitud y del premio
        res_sol = supabase.table("solicitud_canje").select("*").eq("id_canje", id_canje).execute()
        if not res_sol.data:
            raise HTTPException(status_code=404, detail="Solicitud de canje no encontrada.")

        solicitud = res_sol.data[0]
        rut_solicitud = str(solicitud.get("rut_usuario", "")).strip()
        id_premio = solicitud.get("id_premio")

        # 2. Descripción del premio
        nombre_premio = f"Premio #{id_premio}"
        try:
            res_premio_info = supabase.table("premio").select("descripcion").eq("id_premio", id_premio).execute()
            if res_premio_info.data:
                nombre_premio = res_premio_info.data[0].get("descripcion") or nombre_premio
        except Exception as e_p:
            print(f"[WARN] No se pudo obtener la descripción del premio: {e_p}")

        # 3. Búsqueda del usuario y correo
        formatos_rut = generar_formatos_rut(rut_solicitud)
        correo_alumno = None
        rut_usuario_oficial = rut_solicitud

        try:
            res_usr = supabase.table("usuario").select("rut_usuario, correo").in_("rut_usuario", formatos_rut).execute()
            if not res_usr.data:
                rut_limpio = rut_solicitud.replace(".", "").replace("-", "")
                res_usr = supabase.table("usuario").select("rut_usuario, correo").ilike("rut_usuario", f"%{rut_limpio}%").execute()

            if res_usr.data:
                usr = res_usr.data[0]
                rut_usuario_oficial = usr.get("rut_usuario") or rut_solicitud
                correo_alumno = usr.get("correo")
                print(f"[DEBUG CORREO] Correo detectado para retiro: {correo_alumno}")
            else:
                print(f"[WARN CORREO] No se encontró usuario en BD para RUT: {rut_solicitud}")
        except Exception as e_u:
            print(f"[WARN] Error consultando usuario para notificación de retiro: {e_u}")

        # 4. Estado 'Entregado' / 'Retirado'
        res_est_retirado = supabase.table("estado_canje").select("id_estado_canje").ilike("descripcion", "%Entregado%").execute()
        if not res_est_retirado.data:
            res_est_retirado = supabase.table("estado_canje").select("id_estado_canje").ilike("descripcion", "%Retirado%").execute()

        id_estado_retirado = res_est_retirado.data[0]["id_estado_canje"] if res_est_retirado.data else 3

        # 5. Actualizar en 'solicitud_canje'
        supabase.table("solicitud_canje").update({"id_estado_canje": id_estado_retirado}).eq("id_canje", id_canje).execute()

        # 6. Actualizar o insertar en 'retiro_premio'
        res_retiro = supabase.table("retiro_premio").select("id_retiro").eq("id_canje", id_canje).execute()
        if res_retiro.data:
            supabase.table("retiro_premio").update({"retirado": True}).eq("id_canje", id_canje).execute()
        else:
            supabase.table("retiro_premio").insert({
                "id_canje": id_canje,
                "fecha_limite": now_str,
                "retirado": True
            }).execute()

        # 7.A. NOTIFICACIÓN EN BASE DE DATOS
        msg_notificacion = f"¡Premio entregado! Confirmamos que has retirado con éxito tu premio '{nombre_premio}' (Solicitud #{id_canje}). ¡Gracias por participar!"
        rut_para_notificacion = rut_usuario_oficial or rut_solicitud

        try:
            payload_noti = {
                "rut_usuario": rut_para_notificacion,
                "mensaje": msg_notificacion,
                "fecha_envio": now_str,
                "leido": False,
                "id_canje": id_canje,
                "id_tipo_notificacion": 1
            }
            res_noti = supabase.table("notificacion").insert(payload_noti).execute()
            print(f"[DEBUG NOTI RETIRO OK] Notificación de retiro insertada para RUT '{rut_para_notificacion}': {res_noti.data}")
        except Exception as e_noti_db:
            print(f"[ERROR CRÍTICO NOTI BD RETIRO]: {e_noti_db}")

        # 7.B. CORREO ELECTRÓNICO EN SEGUNDO PLANO
        if correo_alumno:
            fecha_entrega_fmt = now.strftime("%d/%m/%Y a las %H:%M")
            print(f"[INFO SMTP] Programando correo de entrega confirmada a: {correo_alumno}")
            cuerpo_html = f"¡Hola!<br><br>Se ha registrado exitosamente la entrega presencial de tu premio <b>{nombre_premio}</b>.<br><br>📦 <b>Solicitud:</b> #{id_canje}<br>🗓️ <b>Fecha de Entrega:</b> {fecha_entrega_fmt} hrs.<br><br>¡Esperamos que lo disfrutes y sigas participando en las actividades de <b>Aquí Todos Ganan</b>!"
            
            background_tasks.add_task(
                enviar_correo_inscripcion,
                destinatario=correo_alumno,
                nombre_actividad=nombre_premio,
                asunto=f"Entrega Confirmada - {nombre_premio}",
                mensaje_cuerpo=cuerpo_html
            )
        else:
            print(f"[ALERTA CORREO] No se encontró correo de alumno para el RUT: '{rut_solicitud}'")

        return {
            "message": f"Solicitud #{id_canje} registrada exitosamente como entregada y alumno notificado.",
            "retirado": True
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR MARCAR RETIRO]: {e}")
        raise HTTPException(status_code=500, detail=f"Error marcando retiro: {str(e)}")
# ==============================================================================
# 3. RECHAZAR / CANCELAR SOLICITUD DE CANJE
# ==============================================================================
@app.put("/premios/cancelar/{id_canje}", tags=["Administración Canjes"])
def rechazar_solicitud_canje(id_canje: int, background_tasks: BackgroundTasks):
    """
    Rechaza o Cancela una solicitud de canje:
    1. Reembolsa puntos en 'puntaje_total' y 'historial_puntos' si la solicitud estaba Aprobada (id_estado_canje == 2).
    2. Devuelve +1 unidad al 'stock_sede'.
    3. Cambia el estado en 'solicitud_canje' a 'Rechazado'/'Cancelado'.
    4. Registra la notificación en 'notificacion' y envía correo SMTP en segundo plano.
    """
    try:
        now_str = datetime.now().isoformat()

        # 1. Obtener datos básicos de la solicitud
        res_sol = supabase.table("solicitud_canje").select("*").eq("id_canje", id_canje).execute()
        if not res_sol.data:
            raise HTTPException(status_code=404, detail="Solicitud no encontrada.")

        solicitud = res_sol.data[0]
        rut_usuario = str(solicitud.get("rut_usuario", "")).strip()
        id_estado_actual = solicitud.get("id_estado_canje")
        id_premio = solicitud.get("id_premio")

        try:
            costo_puntos = int(solicitud.get("costo_puntaje") or 0)
        except (ValueError, TypeError):
            costo_puntos = 0

        formatos_rut = generar_formatos_rut(rut_usuario)

        # 2. Descripción del premio
        nombre_premio = f"Premio #{id_premio}"
        id_sede_premio = None
        try:
            res_p = supabase.table("premio").select("descripcion, id_sede").eq("id_premio", id_premio).execute()
            if res_p.data:
                p_data = res_p.data[0]
                nombre_premio = p_data.get("descripcion") or nombre_premio
                id_sede_premio = p_data.get("id_sede")
        except Exception as e_p:
            print(f"[WARN] Error consultando tabla 'premio': {e_p}")

        # 3. Datos del usuario
        correo_alumno = None
        id_sede_alumno = None
        try:
            res_u = supabase.table("usuario").select("correo, id_sede").in_("rut_usuario", formatos_rut).execute()
            if not res_u.data:
                rut_limpio = rut_usuario.replace(".", "").replace("-", "")
                res_u = supabase.table("usuario").select("correo, id_sede").ilike("rut_usuario", f"%{rut_limpio}%").execute()

            if res_u.data:
                usr = res_u.data[0]
                correo_alumno = usr.get("correo")
                id_sede_alumno = usr.get("id_sede")
                print(f"[DEBUG CORREO] Correo encontrado para RUT {rut_usuario}: {correo_alumno}")
            else:
                print(f"[WARN CORREO] No se encontró registro de usuario para RUT: {rut_usuario}")
        except Exception as e_u:
            print(f"[ERROR BUSCANDO USUARIO]: {e_u}")

        # 4. Estado Rechazado / Cancelado
        id_estado_rechazado = 4
        try:
            res_est_rechazado = supabase.table("estado_canje").select("id_estado_canje").ilike("descripcion", "%Rechazado%").execute()
            if not res_est_rechazado.data:
                res_est_rechazado = supabase.table("estado_canje").select("id_estado_canje").ilike("descripcion", "%Cancelado%").execute()
            if res_est_rechazado.data:
                id_estado_rechazado = res_est_rechazado.data[0]["id_estado_canje"]
        except Exception as e_est:
            print(f"[WARN] No se pudo obtener id_estado_canje, usando default 4: {e_est}")

        # 5. REEMBOLSO SI LA SOLICITUD ESTABA APROBADA
        puntos_reembolsados = False
        if id_estado_actual == 2:
            puntos_reembolsados = True

            # A) Reembolso en 'puntaje_total'
            try:
                res_pt = supabase.table("puntaje_total").select("id_puntaje, puntaje").in_("rut_usuario", formatos_rut).execute()
                if res_pt.data:
                    saldo_actual = int(res_pt.data[0].get("puntaje") or 0)
                    supabase.table("puntaje_total").update({
                        "puntaje": saldo_actual + costo_puntos
                    }).eq("id_puntaje", res_pt.data[0]["id_puntaje"]).execute()
            except Exception as e_pt:
                print(f"[WARN] Error en reembolso 'puntaje_total': {e_pt}")

            # B) Ajuste en 'historial_puntos'
            try:
                res_hp = supabase.table("historial_puntos").select("*").in_("rut_usuario", formatos_rut).execute()
                if res_hp.data:
                    h_rec = res_hp.data[0]
                    p_actuales = int(h_rec.get("puntos_actuales") or 0) + costo_puntos
                    p_canjeados = max(0, int(h_rec.get("puntos_canjeados") or 0) - costo_puntos)

                    supabase.table("historial_puntos").update({
                        "puntos_actuales": p_actuales,
                        "puntos_canjeados": p_canjeados
                    }).eq("id_historial_puntos", h_rec["id_historial_puntos"]).execute()
            except Exception as e_hp:
                print(f"[WARN] Error en 'historial_puntos': {e_hp}")

            # C) Reincorporar stock en 'stock_sede'
            try:
                sede_target = id_sede_alumno or id_sede_premio
                if sede_target:
                    res_stock = supabase.table("stock_sede").select("*").eq("id_premio", id_premio).eq("id_sede", sede_target).execute()
                else:
                    res_stock = supabase.table("stock_sede").select("*").eq("id_premio", id_premio).execute()

                if res_stock.data:
                    stock_rec = res_stock.data[0]
                    supabase.table("stock_sede").update({
                        "cantidad": int(stock_rec.get("cantidad") or 0) + 1
                    }).eq("id_stock", stock_rec["id_stock"]).execute()
            except Exception as e_stk:
                print(f"[WARN] Error actualizando stock: {e_stk}")

        # 6. ACTUALIZAR ESTADO DE LA SOLICITUD
        supabase.table("solicitud_canje").update({"id_estado_canje": id_estado_rechazado}).eq("id_canje", id_canje).execute()

        # 7. REGISTRAR NOTIFICACIÓN Y ENVIAR CORREO EN SEGUNDO PLANO
        try:
            if puntos_reembolsados:
                msg_notificacion = f"Tu canje #{id_canje} para '{nombre_premio}' ha sido cancelado. Se te han reembolsado {costo_puntos} pts a tu saldo."
                asunto_mail = f"Cancelación de Canje - {nombre_premio}"
                cuerpo_mail = f"""
                    Tu solicitud de canje para el premio <b>{nombre_premio}</b> (Solicitud #{id_canje}) ha sido cancelada.<br><br>
                    🔄 <b>Puntos reembolsados:</b> +{costo_puntos} pts<br>
                    💰 Los puntos han sido devueltos a tu cuenta para que puedas utilizarlos en otras recompensas.
                """
            else:
                msg_notificacion = f"Tu solicitud de canje #{id_canje} para '{nombre_premio}' ha sido rechazada."
                asunto_mail = f"Solicitud de Canje Rechazada - {nombre_premio}"
                cuerpo_mail = f"""
                    Te informamos que tu solicitud de canje para el premio <b>{nombre_premio}</b> (Solicitud #{id_canje}) no pudo ser aprobada.<br><br>
                    Si tienes dudas sobre el motivo, puedes consultar directamente en la oficina DAE de tu sede.
                """

            payload_noti = {
                "mensaje": msg_notificacion,
                "fecha_envio": now_str,
                "leido": False,
                "id_canje": id_canje,
                "id_tipo_notificacion": 1,
                "rut_usuario": rut_usuario
            }
            supabase.table("notificacion").insert(payload_noti).execute()

            if correo_alumno:
                print(f"[INFO SMTP] PROGRAMANDO CORREO DE CANCELACIÓN A: {correo_alumno}")
                background_tasks.add_task(
                    enviar_correo_inscripcion,
                    destinatario=correo_alumno,
                    nombre_actividad=nombre_premio,
                    asunto=asunto_mail,
                    mensaje_cuerpo=cuerpo_mail
                )
            else:
                print(f"[ALERTA CORREO] No se pudo enviar correo porque no se encontró dirección de email para el RUT: '{rut_usuario}'")

        except Exception as e_noti:
            print(f"[ERROR ENVIANDO NOTIFICACIÓN/CORREO]: {e_noti}")

        mensaje_respuesta = (
            f"Solicitud #{id_canje} rechazada exitosamente."
            if not puntos_reembolsados
            else f"Canje #{id_canje} cancelado. Se devolvieron {costo_puntos} pts y 1 unidad de stock al alumno."
        )

        return {"message": mensaje_respuesta}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR GENERAL EN CANCELAR/RECHAZAR]: {e}")
        raise HTTPException(status_code=500, detail=f"Error al procesar la solicitud: {str(e)}")





@app.get("/premios/stock-bajo", tags=["Administración Canjes"])
def obtener_premios_stock_bajo(limite_critico: int = 3):
    """
    Retorna la lista de premios que tienen un stock menor o igual al límite crítico.
    Ideal para mostrar alertas visuales en la interfaz del Administrador.
    """
    try:
        res = (
            supabase.table("stock_sede")
            .select("cantidad, premio(id_premio, descripcion), sede(descripcion)")
            .lte("cantidad", limite_critico)
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"[ERROR OBTENER STOCK BAJO]: {e}")
        raise HTTPException(status_code=500, detail="Error consultando stock bajo")


    
@app.get("/puntos/historial/{rut_usuario}")
def obtener_historial_puntos(rut_usuario: str):
    formatos_rut = generar_formatos_rut(rut_usuario)
    
    # Consulta asistencias e inscripciones que otorgaron puntos al alumno
    res = supabase.table("inscripcion_act")\
        .select("id_inscripcion, fecha_inscripcion, puntos_ganados, actividad(nombre_actividad, descripcion)")\
        .in_("rut_usuario", formatos_rut)\
        .order("fecha_inscripcion", desc=True)\
        .execute()
        
    return res.data or []


@app.get("/premios/mis-canjes/{rut_usuario}")
def obtener_mis_canjes(rut_usuario: str):
    formatos_rut = generar_formatos_rut(rut_usuario)
    
    # 1. NOTA: Se elimina '!inner' de retiro_premio para que actúe como LEFT JOIN.
    # 2. NOTA: Se elimina .eq("retiro_premio.retirado", True) para no filtrar solo los retirados.
    res = supabase.table("solicitud_canje")\
        .select("""
            id_canje,
            fecha_solicitud,
            costo_puntaje,
            id_estado_canje,
            estado_canje ( id_estado_canje, descripcion ),
            premio ( id_premio, descripcion, imagen, puntos_requeridos ),
            retiro_premio ( id_retiro, fecha_limite, retirado )
        """)\
        .in_("rut_usuario", formatos_rut)\
        .order("fecha_solicitud", desc=True)\
        .execute()
        
    return res.data or []
# --------------------------------------------------------------------
# ENDPOINTS DE APOYO PARA SELECTS EN EL FRONTEND
# --------------------------------------------------------------------
@app.get("/actividades")
def listar_actividades():
    res = supabase.table("actividad").select("id_actividad, nombre_actividad, fecha").execute()
    return res.data or []

@app.get("/sedes")
def listar_sedes():
    res = supabase.table("sede").select("id_sede, descripcion").execute()
    return res.data or []

# --------------------------------------------------------------------
# 1. REPORTE: DETALLE DE ACTIVIDAD Y ASISTENCIA
# --------------------------------------------------------------------
@app.get("/reportes/detalle-actividad/{id_actividad}")
def obtener_detalle_actividad(id_actividad: int):
    try:
        # A. Obtener datos de la actividad
        act_res = supabase.table("actividad")\
            .select("id_actividad, nombre_actividad, descripcion, fecha")\
            .eq("id_actividad", id_actividad)\
            .execute()

        if not act_res.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada")

        actividad = act_res.data[0]

        # B. Obtener todas las inscripciones para esta actividad
        inscripciones_res = supabase.table("inscripcion_act")\
            .select("id_inscripcion, puntos_ganados, rut_usuario")\
            .eq("id_actividad", id_actividad)\
            .execute()

        inscritos = inscripciones_res.data or []

        # C. Obtener todos los registros de asistencia para esta actividad
        asistencias_res = supabase.table("asistencia_act")\
            .select("rut_usuario, presente")\
            .eq("id_actividad", id_actividad)\
            .execute()

        # Diccionario para verificar presencia de manera rápida por RUT
        asistencia_map = {
            item["rut_usuario"]: item.get("presente", False)
            for item in (asistencias_res.data or [])
        }

        # D. Obtener datos de los usuarios inscritos
        ruts_inscritos = [i["rut_usuario"] for i in inscritos]
        usuarios_map = {}

        if ruts_inscritos:
            usuarios_res = supabase.table("usuario")\
                .select("rut_usuario, nombre_completo, correo")\
                .in_("rut_usuario", ruts_inscritos)\
                .execute()

            for usr in (usuarios_res.data or []):
                usuarios_map[usr["rut_usuario"]] = usr

        # E. Construir respuesta final y calcular métricas
        total_inscritos = len(inscritos)
        total_presentes = 0
        total_puntos_repartidos = 0
        detalles = []

        for row in inscritos:
            rut = row["rut_usuario"]
            usr_info = usuarios_map.get(rut, {})

            # Evaluación explicita de presencia (true/false)
            es_presente = bool(asistencia_map.get(rut, False))

            if es_presente:
                total_presentes += 1
                puntos = row.get("puntos_ganados", 0)
                total_puntos_repartidos += puntos
            else:
                puntos = 0

            detalles.append({
                "rut": rut,
                "estudiante": usr_info.get("nombre_completo", "Nombre no registrado"),
                "correo": usr_info.get("correo", "Sin correo"),
                "asistencia": "Presente" if es_presente else "Ausente",
                "puntos": puntos
            })

        porcentaje_asistencia = round((total_presentes / total_inscritos * 100), 1) if total_inscritos > 0 else 0.0

        return {
            "kpis": {
                "nombre_actividad": actividad.get("nombre_actividad"),
                "fecha": actividad.get("fecha"),
                "total_inscritos": total_inscritos,
                "total_presentes": total_presentes,
                "porcentaje_asistencia": porcentaje_asistencia,
                "total_puntos_repartidos": total_puntos_repartidos
            },
            "detalles": detalles
        }

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en reporte de actividad: {str(e)}")


# --------------------------------------------------------------------
# 2. REPORTE: STOCK DE PREMIOS POR SEDE
# --------------------------------------------------------------------
@app.get("/reportes/stock-sede")
def obtener_stock_sede(
    id_sede: Optional[int] = Query(None),
    estado: Optional[str] = Query("TODOS")
):
    try:
        # A. Consulta principal de la tabla stock_sede
        stock_query = supabase.table("stock_sede").select("id_stock, cantidad, id_premio, id_sede")
        
        if id_sede:
            stock_query = stock_query.eq("id_sede", id_sede)

        stock_res = stock_query.execute()
        stock_items = stock_res.data or []

        if not stock_items:
            return {
                "kpis": {
                    "total_productos": 0,
                    "total_unidades_stock": 0,
                    "alertas_criticas": 0
                },
                "detalles": []
            }

        # B. Obtener IDs únicos de premios, sedes y categorías
        ids_premios = list(set([item["id_premio"] for item in stock_items if item.get("id_premio")]))
        ids_sedes = list(set([item["id_sede"] for item in stock_items if item.get("id_sede")]))

        premios_res = supabase.table("premio")\
            .select("id_premio, descripcion, puntos_requeridos, id_categoria")\
            .in_("id_premio", ids_premios)\
            .execute()
        
        premios_map = {p["id_premio"]: p for p in (premios_res.data or [])}

        sedes_res = supabase.table("sede")\
            .select("id_sede, descripcion")\
            .in_("id_sede", ids_sedes)\
            .execute()
            
        sedes_map = {s["id_sede"]: s["descripcion"] for s in (sedes_res.data or [])}

        ids_categorias = list(set([p["id_categoria"] for p in premios_map.values() if p.get("id_categoria")]))
        categorias_map = {}
        
        if ids_categorias:
            cat_res = supabase.table("categoria_premio")\
                .select("id_categoria, descripcion")\
                .in_("id_categoria", ids_categorias)\
                .execute()
            categorias_map = {c["id_categoria"]: c["descripcion"] for c in (cat_res.data or [])}

        # C. Construir consolidado de respuesta
        detalles = []
        total_unidades = 0
        alertas_criticas = 0

        for row in stock_items:
            stock = row.get("cantidad", 0)
            premio_info = premios_map.get(row.get("id_premio"), {})
            sede_nombre = sedes_map.get(row.get("id_sede"), "Sede Desconocida")
            categoria_nombre = categorias_map.get(premio_info.get("id_categoria"), "General")

            total_unidades += stock

            if stock == 0:
                estado_stock = "Agotado"
            elif stock <= 5:
                estado_stock = "Stock Crítico"
            else:
                estado_stock = "Suficiente"

            if stock <= 5:
                alertas_criticas += 1

            # Filtros de estado opcionales
            if estado == "CRITICO" and stock > 5:
                continue
            if estado == "AGOTADO" and stock > 0:
                continue

            detalles.append({
                "premio": premio_info.get("descripcion", "Sin nombre"),
                "categoria": categoria_nombre,
                "sede": sede_nombre,
                "costo_puntos": premio_info.get("puntos_requeridos", 0),
                "stock_actual": stock,
                "estado_stock": estado_stock
            })

        return {
            "kpis": {
                "total_productos": len(detalles),
                "total_unidades_stock": total_unidades,
                "alertas_criticas": alertas_criticas
            },
            "detalles": detalles
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en reporte de stock: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


@app.post("/solicitud-canje/cancelar", tags=["Canjes"])
def cancelar_solicitud_canje(payload: dict, background_tasks: BackgroundTasks):
    """
    Permite al alumno cancelar su propia solicitud pendiente (estado 1):
    1. Cambia el estado a 'Cancelado' (4) en 'solicitud_canje'.
    2. Registra la notificación en BD.
    3. Envía correo electrónico SMTP de confirmación al alumno.
    """
    rut_alumno = str(payload.get("rut_alumno") or "").strip()
    id_premio = payload.get("id_premio")

    if not rut_alumno or not id_premio:
        raise HTTPException(
            status_code=400, 
            detail="Se requieren 'rut_alumno' e 'id_premio' para cancelar la solicitud."
        )

    now_str = datetime.now().isoformat()
    formatos_rut = generar_formatos_rut(rut_alumno)

    # 1. Obtener datos de la solicitud activa antes de actualizarla (para obtener id_canje)
    res_sol = (
        supabase.table("solicitud_canje")
        .select("id_canje, costo_puntaje")
        .in_("rut_usuario", formatos_rut)
        .eq("id_premio", id_premio)
        .eq("id_estado_canje", 1)
        .execute()
    )

    if not res_sol.data:
        raise HTTPException(
            status_code=404, 
            detail="No se encontró una solicitud pendiente para cancelar este premio."
        )

    id_canje = res_sol.data[0]["id_canje"]
    costo_puntos = res_sol.data[0].get("costo_puntaje", 0)

    # 2. Buscar datos del premio (nombre/descripción)
    nombre_premio = f"Premio #{id_premio}"
    try:
        res_p = supabase.table("premio").select("descripcion").eq("id_premio", id_premio).execute()
        if res_p.data:
            nombre_premio = res_p.data[0].get("descripcion") or nombre_premio
    except Exception as e_p:
        print(f"[WARN] Error obteniendo descripción del premio: {e_p}")

    # 3. Buscar correo del alumno y RUT oficial en la tabla 'usuario'
    correo_alumno = None
    rut_exacto_db = rut_alumno
    try:
        res_usr = supabase.table("usuario").select("rut_usuario, correo").in_("rut_usuario", formatos_rut).execute()
        if not res_usr.data:
            rut_limpio = rut_alumno.replace(".", "").replace("-", "")
            res_usr = supabase.table("usuario").select("rut_usuario, correo").ilike("rut_usuario", f"%{rut_limpio}%").execute()

        if res_usr.data:
            usr = res_usr.data[0]
            rut_exacto_db = usr.get("rut_usuario") or rut_alumno
            correo_alumno = usr.get("correo")
            print(f"[DEBUG CORREO] Correo encontrado para cancelación del alumno: {correo_alumno}")
        else:
            print(f"[WARN CORREO] No se encontró usuario en BD para RUT: {rut_alumno}")
    except Exception as e_u:
        print(f"[WARN] Error buscando usuario para correo de cancelación: {e_u}")

    # 4. Actualizar estado_canje a 4 (Cancelado) para la solicitud en estado 1 (Solicitado)
    res_upd = (
        supabase.table("solicitud_canje")
        .update({"id_estado_canje": 4})
        .eq("id_canje", id_canje)
        .execute()
    )

    if not res_upd.data:
        raise HTTPException(status_code=500, detail="No se pudo actualizar el estado de la solicitud.")

    # 5. REGISTRAR NOTIFICACIÓN EN LA BASE DE DATOS
    msg_notificacion = f"Has cancelado exitosamente tu solicitud de canje #{id_canje} para '{nombre_premio}'."
    for rut_noti in [rut_alumno, rut_exacto_db]:
        try:
            payload_noti = {
                "rut_usuario": rut_noti,
                "mensaje": msg_notificacion,
                "fecha_envio": now_str,
                "leido": False,
                "id_canje": id_canje
            }
            supabase.table("notificacion").insert(payload_noti).execute()
            print(f"[DEBUG NOTI OK] Notificación de cancelación guardada para RUT '{rut_noti}'")
            break
        except Exception as e_noti_db:
            print(f"[WARN NOTI CANCELACION BD]: Error guardando notificación para RUT '{rut_noti}': {e_noti_db}")

    # 6. ENVIAR CORREO SMTP EN SEGUNDO PLANO
    if correo_alumno:
        print(f"[INFO SMTP] Programando correo de cancelación de solicitud a: {correo_alumno}")
        cuerpo_html = f"""
            ¡Hola!<br><br>
            Confirmamos que has cancelado la solicitud de canje para el premio <b>{nombre_premio}</b> (Solicitud #{id_canje}).<br><br>
            ⭐ <b>Puntos liberados:</b> {costo_puntos} pts<br><br>
            El premio vuelve a estar disponible en el catálogo por si deseas volver a solicitarlo más adelante.
        """
        try:
            background_tasks.add_task(
                enviar_correo_inscripcion,
                destinatario=correo_alumno,
                nombre_actividad=nombre_premio,
                asunto=f"Solicitud Cancelada - {nombre_premio}",
                mensaje_cuerpo=cuerpo_html
            )
        except Exception as e_mail:
            print(f"[ERROR PROGRAMANDO CORREO CANCELACIÓN]: {e_mail}")
    else:
        print(f"[ALERTA CORREO] Imposible enviar correo porque 'correo_alumno' es None. RUT: '{rut_alumno}'")

    return {
        "mensaje": "Solicitud cancelada correctamente.",
        "id_canje": id_canje
    }
# ---------------------------------------------------------------------
# INICIAR ACTIVIDAD MANUALMENTE (Anticipado)
# ---------------------------------------------------------------------
@app.put("/actividades/{id_actividad}/iniciar", tags=["Actividades"])
def iniciar_actividad(id_actividad: int):
    try:
        # Consultar horario de la actividad
        act_res = supabase.table("actividad").select("fecha, hora_inicio, hora_termino, id_estado_actividad").eq("id_actividad", id_actividad).execute()
        if not act_res.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada.")

        act = act_res.data[0]
        now = datetime.now()

        # Parsear fecha e inicio
        f_act = date.fromisoformat(str(act["fecha"]).split("T")[0])
        h_ini = time.fromisoformat(str(act["hora_inicio"]).replace('Z','').split('+')[0])
        dt_inicio = datetime.combine(f_act, h_ini)

        # REGLA DE RESPETO A HORARIO: No permitir inicio anticipado antes de la hora estipulada
        if now < dt_inicio:
            raise HTTPException(
                status_code=400, 
                detail=f"No se puede iniciar la actividad antes de su horario programado ({act['hora_inicio']}) para no interferir con el horario de los alumnos."
            )

        # Cambiar estado a EN_CURSO (2)
        supabase.table("actividad").update({"id_estado_actividad": 2}).eq("id_actividad", id_actividad).execute()
        
        res = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).eq("id_actividad", id_actividad).execute()
        return calcular_estado_dinamico(res.data[0])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
# ---------------------------------------------------------------------
# TERMINAR ACTIVIDAD MANUALMENTE (Cierre anticipado y asignación de puntos)
# ---------------------------------------------------------------------
@app.put("/actividades/{id_actividad}/terminar", tags=["Actividades"])
def terminar_actividad(id_actividad: int):
    """
    Finaliza la actividad (id_estado_actividad = 3), actualiza puntos ganados en las inscripciones,
    recalcula el puntaje acumulado de cada alumno presente y dispara la notificación y el correo.
    """
    try:
        # 1. Verificar existencia de la actividad
        act_check = supabase.table("actividad").select("id_actividad, nombre_actividad").eq("id_actividad", id_actividad).execute()
        if not act_check.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada")

        nombre_actividad = act_check.data[0].get("nombre_actividad") or f"la actividad #{id_actividad}"

        # 2. Cambiar estado a FINALIZADA (id_estado_actividad = 3)
        supabase.table("actividad").update({"id_estado_actividad": 3}).eq("id_actividad", id_actividad).execute()

        # 3. Obtener el puntaje a otorgar
        puntos_otorgados = 5
        try:
            p_res = supabase.table("puntaje_act").select("cantidad").eq("id_actividad", id_actividad).execute()
            if p_res.data and len(p_res.data) > 0:
                puntos_otorgados = p_res.data[0].get("cantidad", 5)
        except Exception as e_pts:
            print(f"[WARN] No se pudo obtener puntaje_act para actividad {id_actividad}: {e_pts}")

        # 4. Obtener todos los alumnos presentes
        asist_res = supabase.table("asistencia_act").select("*").eq("id_actividad", id_actividad).eq("presente", True).execute()

        if asist_res.data:
            for asist in asist_res.data:
                rut_estudiante = asist.get("rut_usuario") or asist.get("rut_alumno") or asist.get("rut")
                if not rut_estudiante:
                    continue

                # Mantener el RUT sin alteraciones agresivas para evitar descalce de formato
                rut_bruto = str(rut_estudiante).strip()

                # A) Actualizar puntos_ganados en inscripcion_act usando los formatos posibles del RUT
                formatos_rut = generar_formatos_rut(rut_bruto)
                try:
                    supabase.table("inscripcion_act").update({
                        "puntos_ganados": puntos_otorgados
                    }).eq("id_actividad", id_actividad).in_("rut_usuario", formatos_rut).execute()
                except Exception as e_insc:
                    print(f"[WARN] No se pudo actualizar inscripcion_act para {rut_bruto}: {e_insc}")

                # B) Recalcular el puntaje forzando la inclusión de esta actividad
                try:
                    actualizar_puntaje_total_alumno(rut_bruto, id_actividad_forzada=id_actividad)
                except Exception as e_actualizar:
                    print(f"[ERROR] Error al ejecutar actualizar_puntaje_total_alumno para {rut_bruto}: {e_actualizar}")

        # 5. Devolver la actividad actualizada
        res = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).eq("id_actividad", id_actividad).execute()
        if res.data and len(res.data) > 0:
            return calcular_estado_dinamico(res.data[0])

        return {"mensaje": "Actividad finalizada, puntos liquidados y notificaciones enviadas correctamente."}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR TERMINAR ACTIVIDAD]: {e}")
        raise HTTPException(status_code=500, detail=f"Error al finalizar la actividad: {str(e)}")
    
# ---------------------------------------------------------------------
# REPROGRAMAR ACTIVIDAD (Bloqueo estricto si existen inscritos)
# ---------------------------------------------------------------------
class ActividadReprogramarPayload(BaseModel):
    fecha: date
    hora_inicio: time
    hora_termino: time
    cupos: Optional[int] = None
    puntos: Optional[int] = None

class EncuestaCreatePayload(BaseModel):
    id_actividad: int
    id_inscripcion: int
    calificacion: int = Field(..., ge=1, le=5, description="Calificación de 1 a 5 estrellas")
    comentario: Optional[str] = None

@app.put("/actividades/{id_actividad}/reprogramar", tags=["Actividades"])
def reprogramar_actividad(id_actividad: int, payload: ActividadReprogramarPayload):
    try:
        check = supabase.table("actividad").select("id_actividad").eq("id_actividad", id_actividad).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada")

        # Validación en BD: Verificar si existen inscritos
        insc_res = supabase.table("inscripcion_act").select("id_inscripcion").eq("id_actividad", id_actividad).execute()
        if insc_res.data and len(insc_res.data) > 0:
            raise HTTPException(
                status_code=400, 
                detail="No se puede reprogramar: esta actividad ya cuenta con alumnos inscritos."
            )

        if payload.hora_termino <= payload.hora_inicio:
            raise HTTPException(status_code=400, detail="La hora de término debe ser posterior a la hora de inicio.")

        fecha_str = payload.fecha.isoformat()
        hora_ini_str = payload.hora_inicio.isoformat()
        hora_ter_str = payload.hora_termino.isoformat()

        # 1. Actualizar fecha/hora y restablecer estado a PROGRAMADA (1)
        supabase.table("actividad").update({
            "fecha": fecha_str,
            "hora_inicio": hora_ini_str,
            "hora_termino": hora_ter_str,
            "id_estado_actividad": 1
        }).eq("id_actividad", id_actividad).execute()

        # 2. Sincronizar tabla calendario
        supabase.table("calendario").update({
            "fecha": fecha_str,
            "hora": hora_ini_str
        }).eq("id_actividad", id_actividad).execute()

        # 3. ACTUALIZAR CUPOS EN 'cupo_actividad'
        if payload.cupos is not None:
            res_cupos = supabase.table("cupo_actividad").select("id_cupo").eq("id_actividad", id_actividad).execute()
            if res_cupos.data and len(res_cupos.data) > 0:
                id_cupo = res_cupos.data[0]["id_cupo"]
                supabase.table("cupo_actividad").update({"cantidad": payload.cupos}).eq("id_cupo", id_cupo).execute()
            else:
                supabase.table("cupo_actividad").insert({"id_actividad": id_actividad, "cantidad": payload.cupos}).execute()

        # 4. ACTUALIZAR PUNTAJE EN 'puntaje_act' (Opcional si también lo modificas)
        if payload.puntos is not None:
            res_pts = supabase.table("puntaje_act").select("id_puntaje").eq("id_actividad", id_actividad).execute()
            if res_pts.data and len(res_pts.data) > 0:
                id_p = res_pts.data[0]["id_puntaje"]
                supabase.table("puntaje_act").update({"cantidad": payload.puntos}).eq("id_puntaje", id_p).execute()
            else:
                supabase.table("puntaje_act").insert({"id_actividad": id_actividad, "cantidad": payload.puntos}).execute()

        res = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).eq("id_actividad", id_actividad).execute()
        return calcular_estado_dinamico(res.data[0])

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------
# OBTENER INSCRITOS EN TIEMPO REAL
# ---------------------------------------------------------------------
@app.get("/actividades/{id_actividad}/inscritos", tags=["Actividades"])
def obtener_inscritos_actividad(id_actividad: int):
    try:
        res = supabase.table("inscripcion_act").select(
            "id_inscripcion, rut_usuario, fecha_inscripcion, puntos_ganados, usuario(nombre_completo, correo)"
        ).eq("id_actividad", id_actividad).execute()
        
        return res.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 1. Enviar respuestas de la encuesta
@app.post("/encuestas", tags=["Encuestas"])
def responder_encuesta(payload: EncuestaCreatePayload):
    try:
        # Verificar si la inscripción existe
        insc = supabase.table("inscripcion_act").select("id_inscripcion").eq("id_inscripcion", payload.id_inscripcion).execute()
        if not insc.data:
            raise HTTPException(status_code=404, detail="Inscripción no encontrada.")

        # Verificar si ya respondió la encuesta para esta inscripción
        existente = supabase.table("encuesta").select("id_encuesta").eq("id_inscripcion", payload.id_inscripcion).execute()
        if existente.data:
            raise HTTPException(status_code=400, detail="Ya has respondido la encuesta para esta actividad.")

        # Insertar respuesta
        res = supabase.table("encuesta").insert({
            "calificacion": payload.calificacion,
            "comentario": payload.comentario,
            "id_actividad": payload.id_actividad,
            "id_inscripcion": payload.id_inscripcion
        }).execute()

        return {"status": "success", "message": "¡Gracias por tu opinión!", "data": res.data[0]}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 2. Consultar o generar certificado del estudiante
@app.get("/certificados/actividad/{id_actividad}/usuario/{rut_usuario}", tags=["Certificados"])
def obtener_certificado(id_actividad: int, rut_usuario: str):
    try:
        # Verificar que el alumno haya asistido a la actividad
        asistencia = supabase.table("asistencia_act")\
            .select("presente")\
            .eq("id_actividad", id_actividad)\
            .eq("rut_usuario", rut_usuario)\
            .eq("presente", True)\
            .execute()

        if not asistencia.data:
            raise HTTPException(status_code=403, detail="No registras asistencia en esta actividad para obtener certificado.")

        # Buscar si ya existe el certificado
        cert = supabase.table("certificado_part")\
            .select("*, actividad(nombre_actividad, fecha), usuario(nombre_completo, rut_usuario)")\
            .eq("id_actividad", id_actividad)\
            .eq("rut_usuario", rut_usuario)\
            .execute()

        if cert.data:
            return cert.data[0]

        # Si no existe, crearlo dinámicamente
        act_info = supabase.table("actividad").select("nombre_actividad").eq("id_actividad", id_actividad).single().execute()
        nombre_act = act_info.data["nombre_actividad"] if act_info.data else "Actividad"

        nuevo_cert = supabase.table("certificado_part").insert({
            "descripcion": f"Certificado de Participación - {nombre_act}",
            "fecha": date.today().isoformat(),
            "id_actividad": id_actividad,
            "rut_usuario": rut_usuario
        }).execute()

        # Re-consultar con relaciones
        res_completa = supabase.table("certificado_part")\
            .select("*, actividad(nombre_actividad, fecha), usuario(nombre_completo, rut_usuario)")\
            .eq("id_certificado", nuevo_cert.data[0]["id_certificado"])\
            .single()\
            .execute()

        return res_completa.data

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 3. Estado de encuestas y certificados para el perfil del alumno
@app.get("/alumnos/{rut_usuario}/actividades-completadas", tags=["Alumnos"])
def actividades_completadas(rut_usuario: str):
    try:
        # 1. Consultar todas las inscripciones del alumno junto con los datos de la actividad
        inscripciones = supabase.table("inscripcion_act")\
            .select("id_inscripcion, puntos_ganados, id_actividad, actividad(*, puntaje_act(cantidad))")\
            .eq("rut_usuario", rut_usuario)\
            .execute()

        actividades = []
        for row in inscripciones.data or []:
            id_inscripcion = row.get("id_inscripcion")
            act_data = row.get("actividad") or {}

            # 2. Buscar si existe encuesta asociada a esta inscripción exacta
            enc_data = None
            if id_inscripcion:
                enc = supabase.table("encuesta")\
                    .select("id_encuesta, calificacion")\
                    .eq("id_inscripcion", id_inscripcion)\
                    .execute()
                if enc.data and len(enc.data) > 0:
                    enc_data = enc.data[0]

            calificacion_guardada = int(enc_data["calificacion"]) if enc_data and enc_data.get("calificacion") else 0
            
            # 3. Construir el objeto de respuesta
            actividades.append({
                "id_actividad": row.get("id_actividad"),
                "id_inscripcion": id_inscripcion,
                "puntos_ganados": row.get("puntos_ganados", 0),
                "actividad": act_data,
                "encuesta_respondida": True if enc_data else False,
                "calificacion": calificacion_guardada
            })

        return actividades

    except Exception as e:
        print(f"Error en actividades_completadas: {e}")
        raise HTTPException(status_code=500, detail=str(e))
     
class CertificadoRequest(BaseModel):
    rut_usuario: str
    id_actividad: int
    correo: str | None = None

def generar_pdf_certificado(nombre_alumno: str, nombre_actividad: str, fecha_actividad: str) -> bytes:
    buffer = io.BytesIO()
    
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        rightMargin=40,
        leftMargin=40,
        topMargin=30,
        bottomMargin=30
    )
    
    story = []
    styles = getSampleStyleSheet()
    
    # 1. Agregar Logo si existe en el disco
    ruta_logo = os.path.join(os.getcwd(),"static", "logo.png") # Cambia la ruta/nombre según donde esté tu logo
    if os.path.exists(ruta_logo):
        # Ajusta ancho y alto proporcionalmente (width=120, height=60 por ejemplo)
        img_logo = Image(ruta_logo, width=250, height=250)
        img_logo.hAlign = 'CENTER'
        story.append(img_logo)
        story.append(Spacer(1, 10))

    # Estilos
    titulo_style = ParagraphStyle(
        'TituloCertificado',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=26,
        leading=30,
        textColor=HexColor('#1E293B'),
        alignment=1
    )
    
    subtitulo_style = ParagraphStyle(
        'SubtituloCertificado',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=13,
        leading=16,
        textColor=HexColor('#64748B'),
        alignment=1
    )
    
    nombre_style = ParagraphStyle(
        'NombreAlumno',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=22,
        leading=26,
        textColor=HexColor('#0EA5E9'),
        alignment=1
    )
    
    texto_style = ParagraphStyle(
        'TextoBase',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=13,
        leading=18,
        textColor=HexColor('#334155'),
        alignment=1
    )
    
    actividad_style = ParagraphStyle(
        'NombreActividad',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=17,
        leading=21,
        textColor=HexColor('#0F172A'),
        alignment=1
    )

    story.append(Paragraph("CERTIFICADO DE PARTICIPACIÓN", titulo_style))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Se otorga el presente reconocimiento a:", subtitulo_style))
    story.append(Spacer(1, 15))
    story.append(Paragraph(nombre_alumno.upper(), nombre_style))
    story.append(Spacer(1, 15))
    story.append(Paragraph("Por su valiosa participación y cumplimiento en la actividad:", texto_style))
    story.append(Spacer(1, 10))
    story.append(Paragraph(f'"{nombre_actividad}"', actividad_style))
    story.append(Spacer(1, 15))
    story.append(Paragraph(f"Fecha de realización: <b>{fecha_actividad}</b>", texto_style))
    story.append(Spacer(1, 20))
    story.append(Paragraph("________________________________________", subtitulo_style))
    story.append(Paragraph("Dirección de Asuntos Estudiantiles", subtitulo_style))
    
    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    
    return pdf_bytes


def asegurar_registro_certificado(id_actividad: int, rut_usuario: str, nombre_actividad: str):
    """
    Verifica si ya existe un registro en certificado_part; de no existir, lo inserta en Supabase.
    """
    try:
        # 1. Buscar si ya existe
        cert_existente = supabase.table("certificado_part")\
            .select("id_certificado")\
            .eq("id_actividad", id_actividad)\
            .eq("rut_usuario", rut_usuario)\
            .execute()

        if not cert_existente.data:
            # 2. Insertar nuevo registro
            nuevo = supabase.table("certificado_part").insert({
                "descripcion": f"Certificado de Participación - {nombre_actividad}",
                "fecha": date.today().isoformat(),
                "id_actividad": id_actividad,
                "rut_usuario": rut_usuario
            }).execute()
            print(f"[BD CERTIFICADO] Registrado exitosamente id_certificado: {nuevo.data[0]['id_certificado']}")
            return nuevo.data[0]
        
        return cert_existente.data[0]
    except Exception as e:
        print(f"[WARN] No se pudo guardar el certificado en la BD: {e}")
        return None
    
def enviar_certificado_core(rut_usuario: str, id_actividad: int, correo: str | None = None):
    # 1. Obtener usuario
    user_res = supabase.table("usuario").select("*").eq("rut_usuario", rut_usuario).execute()
    if not user_res.data:
        raise Exception("Usuario no encontrado")
    
    usuario = user_res.data[0]
    destino_correo = correo or usuario.get("correo") or usuario.get("email")
    if not destino_correo:
        raise Exception("El usuario no tiene correo registrado")
        
    nombre_alumno = usuario.get("nombre_completo") or f"{usuario.get('nombres', '')} {usuario.get('apellidos', '')}".strip() or "Estudiante"

    # 2. Obtener actividad
    act_res = supabase.table("actividad").select("*").eq("id_actividad", id_actividad).execute()
    if not act_res.data:
        raise Exception("Actividad no encontrada")
        
    actividad = act_res.data[0]
    nombre_actividad = actividad.get("nombre_actividad") or actividad.get("nombre") or "Actividad"
    fecha_actividad = str(actividad.get("fecha") or "2026")

    # 3. REGISTRAR O CONFIRMAR EN BASE DE DATOS (certificado_part)
    asegurar_registro_certificado(id_actividad, rut_usuario, nombre_actividad)

    # 4. Generar PDF
    pdf_bytes = generar_pdf_certificado(nombre_alumno, nombre_actividad, fecha_actividad)

    # 5. Enviar SMTP
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("EMAIL_USER", "aquitodosganan77@gmail.com")
    smtp_pass = os.getenv("EMAIL_PASS")

    if not smtp_pass:
        raise Exception("EMAIL_PASS no configurado en el .env")

    msg = MIMEMultipart()
    msg['From'] = smtp_user
    msg['To'] = destino_correo
    msg['Subject'] = f"Certificado de Participación - {nombre_actividad}"

    cuerpo_html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333;">
        <h2>¡Felicitaciones {nombre_alumno}!</h2>
        <p>Has completado exitosamente la actividad <strong>{nombre_actividad}</strong>.</p>
        <p>Adjunto encontrarás tu Certificado de Participación oficial.</p>
      </body>
    </html>
    """
    msg.attach(MIMEText(cuerpo_html, 'html'))

    adjunto = MIMEApplication(pdf_bytes, _subtype="pdf")
    adjunto.add_header('Content-Disposition', 'attachment', filename=f"Certificado_{id_actividad}.pdf")
    msg.attach(adjunto)

    server = smtplib.SMTP(smtp_host, smtp_port)
    server.starttls()
    server.login(smtp_user, smtp_pass)
    server.sendmail(smtp_user, destino_correo, msg.as_string())
    server.quit()

@app.post("/certificados/enviar-correo", tags=["Certificados"])
def enviar_certificado_correo(payload: CertificadoRequest):
    try:
        print(f"--> Generando certificado para RUT: {payload.rut_usuario}, Actividad: {payload.id_actividad}")

        # 1. Obtener usuario
        user_res = supabase.table("usuario")\
            .select("*")\
            .eq("rut_usuario", payload.rut_usuario)\
            .execute()

        if not user_res.data:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

        usuario = user_res.data[0]
        destino_correo = payload.correo or usuario.get("correo") or usuario.get("email")

        if not destino_correo:
            raise HTTPException(status_code=400, detail="El usuario no tiene correo registrado")

        nombre_alumno = usuario.get("nombre_completo") or f"{usuario.get('nombres', '')} {usuario.get('apellidos', '')}".strip() or "Estudiante"

        # 2. Obtener actividad
        act_res = supabase.table("actividad")\
            .select("*")\
            .eq("id_actividad", payload.id_actividad)\
            .execute()

        if not act_res.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada")

        actividad = act_res.data[0]
        nombre_actividad = actividad.get("nombre_actividad") or actividad.get("nombre") or "Actividad"
        fecha_actividad = str(actividad.get("fecha") or "2026")

        # 3. REGISTRAR EN BASE DE DATOS (certificado_part)
        asegurar_registro_certificado(payload.id_actividad, payload.rut_usuario, nombre_actividad)

        # 4. Generar PDF dinámico en memoria
        pdf_bytes = generar_pdf_certificado(
            nombre_alumno=nombre_alumno,
            nombre_actividad=nombre_actividad,
            fecha_actividad=fecha_actividad
        )

        # 5. Credenciales de correo desde .env
        smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", 587))
        smtp_user = os.getenv("EMAIL_USER", "aquitodosganan77@gmail.com")
        smtp_pass = os.getenv("EMAIL_PASS")

        if not smtp_pass:
            raise HTTPException(status_code=500, detail="EMAIL_PASS no configurado en el archivo .env")

        # 6. Crear correo con adjunto
        msg = MIMEMultipart()
        msg['From'] = smtp_user
        msg['To'] = destino_correo
        msg['Subject'] = f"Certificado de Participación - {nombre_actividad}"

        cuerpo_html = f"""
        <html>
          <body style="font-family: Arial, sans-serif; color: #333;">
            <h2>¡Hola {nombre_alumno}!</h2>
            <p>Felicitaciones por haber participado en la actividad <strong>{nombre_actividad}</strong>.</p>
            <p>Adjunto a este correo encontrarás tu <strong>Certificado de Participación</strong> oficial en formato PDF.</p>
            <br>
            <p>Atentamente,<br><strong>Equipo de Actividades</strong></p>
          </body>
        </html>
        """
        msg.attach(MIMEText(cuerpo_html, 'html'))

        adjunto = MIMEApplication(pdf_bytes, _subtype="pdf")
        adjunto.add_header('Content-Disposition', 'attachment', filename=f"Certificado_{payload.id_actividad}.pdf")
        msg.attach(adjunto)

        # 7. Envío SMTP
        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, destino_correo, msg.as_string())
        server.quit()

        print(f"--> Certificado enviado exitosamente a {destino_correo}")
        return {"status": "ok", "message": f"Certificado PDF enviado a {destino_correo}"}

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    
def tarea_verificar_recordatorios_sistema(horas_anticipacion: int = 48):
    """
    Tarea automática en segundo plano:
    Recorre las inscripciones y si la actividad es en las próximas N horas,
    inserta en 'notificacion' y envía el correo.
    """
    try:
        now = datetime.now()
        limite_proximo = now + timedelta(hours=horas_anticipacion)
        now_iso = now.isoformat()

        res_inscripciones = (
            supabase.table("inscripcion_act")
            .select("""
                id_inscripcion,
                rut_usuario,
                actividad (
                    id_actividad,
                    nombre_actividad,
                    fecha,
                    hora_inicio,
                    id_estado_actividad
                )
            """)
            .execute()
        )

        inscripciones = res_inscripciones.data or []
        recordatorios_enviados = 0

        for reg in inscripciones:
            rut_alumno = reg.get("rut_usuario")
            raw_act = reg.get("actividad")
            act_data = raw_act[0] if isinstance(raw_act, list) and raw_act else raw_act

            if not act_data or not act_data.get("fecha") or not rut_alumno:
                continue

            if act_data.get("id_estado_actividad") in [3, 4]:
                continue

            id_actividad = act_data["id_actividad"]
            nombre_act = act_data.get("nombre_actividad") or f"Actividad #{id_actividad}"
            fecha_str = str(act_data["fecha"])
            hora_raw = str(act_data.get("hora_inicio") or "00:00:00")
            hora_str = hora_raw[:5] if len(hora_raw) >= 5 else hora_raw

            try:
                dt_actividad = datetime.fromisoformat(f"{fecha_str}T{hora_raw}")
            except Exception:
                try:
                    dt_actividad = datetime.strptime(f"{fecha_str} {hora_raw}", "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue

            if now <= dt_actividad <= limite_proximo:
                tag_recordatorio = f"[RECORDATORIO-{id_actividad}]"
                formatos_rut = generar_formatos_rut(rut_alumno)

                res_noti_previa = (
                    supabase.table("notificacion")
                    .select("id_notificacion")
                    .in_("rut_usuario", formatos_rut)
                    .like("mensaje", f"%{tag_recordatorio}%")
                    .execute()
                )

                if not res_noti_previa.data:
                    correo_alumno = None
                    try:
                        res_u = supabase.table("usuario").select("correo").in_("rut_usuario", formatos_rut).execute()
                        if res_u.data and res_u.data[0].get("correo"):
                            correo_alumno = res_u.data[0].get("correo").strip()
                    except Exception as e_u:
                        print(f"[WARN] No se pudo obtener correo para {rut_alumno}: {e_u}")

                    msg_noti = f"⏰ Recordatorio: Se acerca tu actividad '{nombre_act}' {tag_recordatorio}. Fecha: {fecha_str} a las {hora_str} hrs."

                    # A) Guardar en BD
                    try:
                        supabase.table("notificacion").insert({
                            "rut_usuario": rut_alumno,
                            "mensaje": msg_noti,
                            "fecha_envio": now_iso,
                            "leido": False,
                            "id_tipo_notificacion": 1
                        }).execute()
                    except Exception as e_ins:
                        print(f"[WARN] Error insertando notificación en BD: {e_ins}")

                    # B) Enviar correo
                    if correo_alumno:
                        try:
                            enviar_correo_inscripcion(
                                destinatario=correo_alumno,
                                nombre_actividad=nombre_act,
                                asunto=f"⏰ ¡Se acerca tu actividad! - {nombre_act}",
                                mensaje_cuerpo=f"""
                                    ¡Hola!<br><br>
                                    Te recordamos que estás inscrito en la actividad <b>{nombre_act}</b> que se realizará muy pronto.<br><br>
                                    📅 <b>Fecha:</b> {fecha_str}<br>
                                    ⏰ <b>Hora de Inicio:</b> {hora_str} hrs.<br><br>
                                    ¡Te esperamos! Recuerda marcar tu asistencia al ingresar.
                                """
                            )
                        except Exception as e_mail:
                            print(f"[WARN] Error enviando correo a {correo_alumno}: {e_mail}")

                    recordatorios_enviados += 1

        print(f"[CRON SUCCESS] Recordatorios procesados con éxito. Enviados: {recordatorios_enviados}")

    except Exception as e:
        print(f"[ERROR CRON RECORDATORIOS]: {e}")


# 3. Registrar el ciclo de vida de FastAPI para ejecutar la tarea automáticamente
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Se programa para ejecutarse todos los días a las 08:00 AM
    scheduler.add_job(tarea_verificar_recordatorios_sistema, 'cron', hour=8, minute=0)
    scheduler.start()
    yield
    scheduler.shutdown()

def obtener_nivel_alumno(rut_usuario: str) -> dict:
    formatos_rut = generar_formatos_rut(rut_usuario)
    
    # Contamos las actividades donde el alumno estuvo presente en 'asistencia_act'
    try:
        res = (
            supabase.table("asistencia_act")
            .select("id_asistencia_actividad", count="exact")
            .in_("rut_usuario", formatos_rut)
            .eq("presente", True)
            .execute()
        )
        total_act = res.count or 0
    except Exception as e:
        print(f"[WARN] Error al consultar asistencia_act: {e}")
        total_act = 0
    
    # Lógica de rangos (Bronce: 0-3, Oro: 4-6, Platino: 7+)
    if total_act >= 7:
        nivel_id = 3
        nombre = "Platino"
        faltantes = 0
    elif total_act >= 4:
        nivel_id = 2
        nombre = "Oro"
        faltantes = 7 - total_act
    else:
        nivel_id = 1
        nombre = "Bronce"
        faltantes = 4 - total_act

    return {
        "id_nivel": nivel_id,
        "nombre_nivel": nombre,
        "total_actividades": total_act,
        "faltantes_siguiente": faltantes
    }

@app.get("/niveles", tags=["Niveles"])
def obtener_niveles():
    try:
        res = supabase.table("nivel").select("*").order("id_nivel", desc=False).execute()
        return res.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
@app.get("/usuarios/nivel/{rut_o_token}", tags=["Usuarios"])
def obtener_nivel_usuario_endpoint(rut_o_token: str):
    """
    Retorna el nivel actual del estudiante en función de sus actividades asistidas.
    """
    try:
        # Resolver si lo que viene es un token o el RUT directo
        res_sesion = supabase.table("sesion_usuario").select("rut_usuario").eq("token_acceso", rut_o_token).execute()
        rut_evaluar = res_sesion.data[0]["rut_usuario"] if (res_sesion.data and len(res_sesion.data) > 0) else rut_o_token
        
        info_nivel = obtener_nivel_alumno(rut_evaluar)
        return info_nivel
    except Exception as e:
        print(f"[ERROR OBTENER NIVEL]: {e}")
        raise HTTPException(status_code=500, detail=f"Error al obtener el nivel del usuario: {str(e)}")
