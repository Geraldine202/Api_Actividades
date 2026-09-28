import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, status, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List
from datetime import date, time
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Faltan las credenciales SUPABASE_URL o SUPABASE_KEY en el archivo .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(
    title="API Actividades y Premios - Aquí Todos Ganan",
    description="Backend CRUD ajustado a la estructura de la base de datos PostgreSQL",
    version="2.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
class InscripcionCreate(BaseModel):
    rut_alumno: str
    id_actividad: int

class ItemAsistencia(BaseModel):
    rut_usuario: str
    presente: bool

class CargaAsistenciaBatch(BaseModel):
    estudiantes: List[ItemAsistencia]

class EstudianteAsistencia(BaseModel):
    rut_usuario: str
    presente: bool

class CargaAsistenciaBatch(BaseModel):
    estudiantes: List[EstudianteAsistencia]
# ==========================================
# CONSULTAS CON RELACIONES (JOINs)
# ==========================================

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
    calendario(id_calendario, fecha, hora, lugar)
"""

QUERY_RELACIONES_PREMIO = """
    *,
    categoria_premio(descripcion),
    sede(descripcion),
    usuario(nombre_completo, correo),
    stock_sede(id_stock, cantidad, id_sede)
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


class PremioCreate(BaseModel):
    descripcion: str
    valor: int = 0
    puntos_requeridos: int
    id_categoria: int
    rut_usuario: str
    id_sede: int
    estado_visibilidad: bool = True
    imagen: Optional[str] = None
    stock: Optional[int] = 0


class PremioUpdate(BaseModel):
    descripcion: Optional[str] = None
    valor: Optional[int] = None
    puntos_requeridos: Optional[int] = None
    id_categoria: Optional[int] = None
    rut_usuario: Optional[str] = None
    id_sede: Optional[int] = None
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
    Evalúa la actividad contra el reloj combinando `fecha`, `hora_inicio` y `hora_termino`.
    Actualiza `id_estado_actividad` y la descripción del dict dinámicamente.
    """
    if not actividad or actividad.get("id_estado_actividad") == ESTADO_CANCELADA:
        return actividad

    now = datetime.now()

    try:
        f_act = actividad.get("fecha")
        h_ini = actividad.get("hora_inicio")
        h_ter = actividad.get("hora_termino")

        if not f_act or not h_ini or not h_ter:
            return actividad

        # Normalizar fecha (date)
        if isinstance(f_act, str):
            f_act = date.fromisoformat(f_act.split("T")[0])

        # Normalizar hora de inicio (time)
        if isinstance(h_ini, str):
            h_ini_clean = h_ini.replace('Z', '').split('+')[0]
            h_ini = time.fromisoformat(h_ini_clean)

        # Normalizar hora de término (time)
        if isinstance(h_ter, str):
            h_ter_clean = h_ter.replace('Z', '').split('+')[0]
            h_ter = time.fromisoformat(h_ter_clean)

        # Combinar date + time para comparar contra datetime.now()
        dt_inicio = datetime.combine(f_act, h_ini)
        dt_termino = datetime.combine(f_act, h_ter)

        # Evaluación según la hora actual
        if dt_inicio <= now < dt_termino:
            actividad["id_estado_actividad"] = ESTADO_EN_CURSO
            if isinstance(actividad.get("estado_actividad"), dict):
                actividad["estado_actividad"]["descripcion"] = "En Curso"

        elif now >= dt_termino:
            actividad["id_estado_actividad"] = ESTADO_FINALIZADA
            if isinstance(actividad.get("estado_actividad"), dict):
                actividad["estado_actividad"]["descripcion"] = "Finalizada"

        elif now < dt_inicio:
            actividad["id_estado_actividad"] = ESTADO_PROGRAMADA
            if isinstance(actividad.get("estado_actividad"), dict):
                actividad["estado_actividad"]["descripcion"] = "Programada"

    except Exception as e:
        print(f"Error calculando estado dinámico: {e}")

    return actividad
# ==========================================
# CRUD ACTIVIDADES
# ==========================================


from datetime import datetime, date, time
from fastapi import HTTPException, status

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
def crear_actividad(actividad: ActividadCreate):
    try:
        datos = actividad.model_dump()

        # Limpieza de imagen opcional
        if datos.get("img_actv") == "":
            datos["img_actv"] = None

        # Estado por defecto si no viene especificado
        if not datos.get("id_estado_actividad"):
            datos["id_estado_actividad"] = 1

        # Extraer campos que corresponden a tablas relacionadas (no van en la tabla 'actividad')
        puntos = datos.pop("puntos")
        cupos = datos.pop("cupos")
        lugar = datos.pop("lugar")
        requisito = datos.pop("requisito", None)

        # Convertir a string/isoformat para enviar a Supabase sin problemas de serialización JSON
        fecha_str = str(actividad.fecha)
        hora_inicio_str = str(actividad.hora_inicio)
        hora_termino_str = str(actividad.hora_termino)

        datos["fecha"] = fecha_str
        datos["hora_inicio"] = hora_inicio_str
        datos["hora_termino"] = hora_termino_str

        # 1. Insertar en tabla principal 'actividad'
        res_act = supabase.table("actividad").insert(datos).execute()
        if not res_act.data:
            raise HTTPException(status_code=400, detail="Error al registrar la actividad en la base de datos")

        id_actividad = res_act.data[0]["id_actividad"]

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

def actualizar_puntaje_total_alumno(rut_usuario_param: str):
    """
    Suma ÚNICAMENTE los puntos de las actividades a las que el alumno asistió (presente = True)
    Y que además YA HAYAN FINALIZADO (fecha + hora_termino <= datetime.now()), 
    luego actualiza la tabla 'puntaje_total'.
    """
    try:
        # 1. Obtener RUT real en caso de que venga un Token de sesión
        res_sesion = (
            supabase.table("sesion_usuario")
            .select("rut_usuario")
            .eq("token_acceso", rut_usuario_param)
            .execute()
        )
        rut_real = res_sesion.data[0]["rut_usuario"] if res_sesion.data else rut_usuario_param
        formatos = generar_formatos_rut(rut_real)

        # 2. Consultar asistencias CONFIRMADAS (presente = True) junto con los datos de fecha/hora de la actividad
        res_asistencia = (
            supabase.table("asistencia_act")
            .select("""
                id_actividad,
                actividad (
                    fecha,
                    hora_termino
                )
            """)
            .in_("rut_usuario", formatos)
            .eq("presente", True)
            .execute()
        )

        # 3. Filtrar solo las actividades que ya hayan finalizado
        actividades_asistidas_y_finalizadas = []
        for item in (res_asistencia.data or []):
            act_data = item.get("actividad")
            if act_data and actividad_ha_finalizado(act_data):
                actividades_asistidas_y_finalizadas.append(item["id_actividad"])

        total_acumulado = 0

        # 4. Sumar los puntos asignados en 'puntaje_act' para cada actividad finalizada
        if actividades_asistidas_y_finalizadas:
            res_pts = (
                supabase.table("puntaje_act")
                .select("id_actividad, cantidad")
                .in_("id_actividad", actividades_asistidas_y_finalizadas)
                .execute()
            )
            
            puntos_por_actividad = {}
            for item in (res_pts.data or []):
                id_act = item.get("id_actividad")
                cant = item.get("cantidad", 0)
                puntos_por_actividad[id_act] = puntos_por_actividad.get(id_act, 0) + cant

            total_acumulado = sum(puntos_por_actividad.values())

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

        print(f"Puntaje total actualizado con éxito para {rut_real}: {total_acumulado} pts")
        return total_acumulado

    except Exception as e:
        print(f"Error al consolidar puntaje total para {rut_usuario_param}: {e}")
        return 0

@app.get("/puntaje-total/{rut_o_token}", tags=["Puntaje"])
def obtener_y_actualizar_puntaje_total(rut_o_token: str):
    """
    Recalcula el puntaje acumulado con las actividades asistidas/finalizadas,
    actualiza la tabla 'puntaje_total' y devuelve el resultado.
    """
    total = actualizar_puntaje_total_alumno(rut_o_token)
    return {"puntaje": total}   


@app.post("/inscripciones", status_code=status.HTTP_201_CREATED, tags=["Inscripciones"])
def inscribir_alumno_actividad(datos: InscripcionCreate):
    """
    Inscribe al alumno en la actividad cumpliendo la estructura exacta de la BD.
    """
    try:
        rut_o_token = datos.rut_alumno.strip()

        # 0. Resolver si es un token de sesión o un RUT directo
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

        # 1. Validar existencia del usuario
        formatos = generar_formatos_rut(rut_evaluar)
        res_usuario = (
            supabase.table("usuario")
            .select("rut_usuario")
            .in_("rut_usuario", formatos)
            .execute()
        )

        if not res_usuario.data:
            raise HTTPException(status_code=404, detail="El alumno no se encuentra registrado")

        rut_real = res_usuario.data[0]["rut_usuario"]

        # 2. Validar que no exista inscripción previa
        check_insc = (
            supabase.table("inscripcion_act")
            .select("id_inscripcion")
            .eq("rut_usuario", rut_real)
            .eq("id_actividad", datos.id_actividad)
            .execute()
        )

        if check_insc.data:
            raise HTTPException(status_code=400, detail="El alumno ya está inscrito en esta actividad")

        # 3. Obtener fecha y hora de la actividad
        res_act = (
            supabase.table("actividad")
            .select("fecha", "hora_termino")
            .eq("id_actividad", datos.id_actividad)
            .execute()
        )

        if not res_act.data:
            raise HTTPException(status_code=404, detail="La actividad no existe")

        act_data = res_act.data[0]
        fecha_act = act_data["fecha"]
        hora_term = act_data["hora_termino"]

        dt_termino = datetime.fromisoformat(f"{fecha_act}T{hora_term}")
        now = datetime.now()

        # 4. Insertar la inscripción
        nueva_inscripcion = {
            "rut_usuario": rut_real,
            "id_actividad": datos.id_actividad,
            "puntos_ganados": 0,
            "fecha_inscripcion": now.isoformat(),
            "fecha_termino": dt_termino.isoformat()
        }

        res_ins = supabase.table("inscripcion_act").insert(nueva_inscripcion).execute()

        # 5. Descontar cupo
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
            "mensaje": "Inscripción realizada con éxito. Los puntos se abonarán tras confirmar asistencia.",
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
    
@app.post("/actividades/{id_actividad}/asistencia", tags=["Asistencia"])
def registrar_asistencia_actividad(id_actividad: int, datos: CargaAsistenciaBatch):
    """
    Registra/actualiza la asistencia en la tabla 'asistencia_act'
    y sincroniza el 'puntaje_total' de los estudiantes asistentes.
    """
    try:
        # 1. Validar existencia de la actividad
        res_act = supabase.table("actividad").select("id_actividad").eq("id_actividad", id_actividad).execute()
        if not res_act.data:
            raise HTTPException(status_code=404, detail="La actividad especificada no existe.")

        # Obtener los puntos que otorga esta actividad desde 'puntaje_act'
        res_pts_act = supabase.table("puntaje_act").select("cantidad").eq("id_actividad", id_actividad).execute()
        puntos_actividad = res_pts_act.data[0]["cantidad"] if res_pts_act.data else 0

        ruts_procesados = set()

        for est in datos.estudiantes:
            try:
                formatos = generar_formatos_rut(est.rut_usuario)
                res_user = supabase.table("usuario").select("rut_usuario").in_("rut_usuario", formatos).execute()
                
                if not res_user.data:
                    print(f"Advertencia: Usuario {est.rut_usuario} no encontrado en la base de datos.")
                    continue

                rut_real = res_user.data[0]["rut_usuario"]
                ruts_procesados.add(rut_real)

                # Estado de presencia corregido
                es_presente = bool(est.presente) if est.presente is not None else True

                # 2. Verificar si ya existe registro previo de asistencia
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

                # Sincronizar puntos_ganados en la tabla inscripcion_act
                puntos_a_guardar = puntos_actividad if es_presente else 0
                supabase.table("inscripcion_act").update({
                    "puntos_ganados": puntos_a_guardar
                }).eq("id_actividad", id_actividad).eq("rut_usuario", rut_real).execute()

            except Exception as inner_err:
                print(f"Error procesando al alumno {est.rut_usuario}: {inner_err}")
                continue

        # 3. Recalcular el puntaje acumulado mediante tu función
        for rut in ruts_procesados:
            try:
                actualizar_puntaje_total_alumno(rut)
            except Exception as pts_err:
                print(f"Error al actualizar puntaje acumulado para RUT {rut}: {pts_err}")

        return {"mensaje": "Asistencia registrada y puntos actualizados correctamente."}

    except HTTPException:
        raise
    except Exception as e:
        print(f"CRITICAL ERROR en /actividades/{id_actividad}/asistencia: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error al registrar asistencia en el servidor: {str(e)}")
    
@app.put("/inscripciones/completar", tags=["Inscripciones"])
def completar_actividad(rut_alumno: str, id_actividad: int):
    """
    Acredita la asistencia individual de un alumno y recalcula 'puntaje_total'.
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

        # Marcar asistencia en 'asistencia_act'
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

        # Recalcular el puntaje total
        nuevo_total = actualizar_puntaje_total_alumno(rut_real)

        return {
            "mensaje": "Actividad completada exitosamente. Asistencia registrada y puntaje acreditado.",
            "puntaje_total_actual": nuevo_total
        }

    except HTTPException:
        raise
    except Exception as e:
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
    Si ya registra asistencia confirmada, rechaza la solicitud.
    """
    try:
        formatos = generar_formatos_rut(rut_alumno)
        condicion_or = ",".join([f'rut_usuario.eq."{f}"' for f in formatos])

        # 1. Buscar la inscripción
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

        # 2. VALIDACIÓN DE ASISTENCIA: Verificar si el alumno ya asistió
        res_asis = (
            supabase.table("asistencia_act")
            .select("presente")
            .eq("id_actividad", id_actividad)
            .in_("rut_usuario", formatos)
            .execute()
        )

        if res_asis.data and len(res_asis.data) > 0:
            # Evaluar si la asistencia está marcada como verdadera
            asistio = any(a.get("presente") is True for a in res_asis.data)
            if asistio:
                raise HTTPException(
                    status_code=400,
                    detail="No es posible desinscribirse de una actividad a la que ya asististe."
                )

        # 3. Eliminar inscripción
        supabase.table("inscripcion_act").delete().eq("id_inscripcion", id_inscripcion).execute()

        # 4. Eliminar registro de asistencia no efectiva (si existía con presente = False)
        supabase.table("asistencia_act").delete().eq("id_actividad", id_actividad).in_("rut_usuario", formatos).execute()

        # 5. Recalcular el puntaje acumulado
        actualizar_puntaje_total_alumno(rut_real)

        return {"mensaje": "Desinscripción realizada con éxito"}

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

@app.get("/premios", tags=["Premios"])
def obtener_premios():
    try:
        res = supabase.table("premio").select(QUERY_RELACIONES_PREMIO).order("id_premio", desc=True).execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        check = supabase.table("premio").select("id_premio, imagen").eq("id_premio", id_premio).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="Premio no encontrado")

        img_antigua = check.data[0].get("imagen")
        datos = premio.model_dump(exclude_unset=True)

        if "imagen" in datos and datos["imagen"] == "":
            datos["imagen"] = None

        if "imagen" in datos:
            nueva_img = datos["imagen"]
            if img_antigua and nueva_img != img_antigua:
                eliminar_imagen_storage(img_antigua, bucket_default="premios")

        stock_cantidad = datos.pop("stock", None)

        if datos:
            supabase.table("premio").update(datos).eq("id_premio", id_premio).execute()

        if stock_cantidad is not None:
            s_res = supabase.table("stock_sede").select("id_stock").eq("id_premio", id_premio).execute()
            if s_res.data:
                supabase.table("stock_sede").update({"cantidad": stock_cantidad}).eq("id_premio", id_premio).execute()
            else:
                id_sede_premio = datos.get("id_sede")
                if not id_sede_premio:
                    p_info = supabase.table("premio").select("id_sede").eq("id_premio", id_premio).execute()
                    id_sede_premio = p_info.data[0]["id_sede"]

                supabase.table("stock_sede").insert({
                    "cantidad": stock_cantidad,
                    "id_premio": id_premio,
                    "id_sede": id_sede_premio
                }).execute()

        res = supabase.table("premio").select(QUERY_RELACIONES_PREMIO).eq("id_premio", id_premio).execute()
        return res.data[0]

    except HTTPException:
        raise
    except Exception as e:
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