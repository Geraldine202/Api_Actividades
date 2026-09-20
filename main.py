import os
import re
import uuid
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, status, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

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

# ==========================================
# FUNCIONES AUXILIARES
# ==========================================

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


# ==========================================
# MODELOS PYDANTIC
# ==========================================

class ActividadCreate(BaseModel):
    nombre_actividad: str
    descripcion: str
    responsable_actividad: str
    fecha_inicio: datetime
    fecha_termino: datetime
    id_tipo_actividad: int
    id_estado_actividad: Optional[int] = 1
    id_sede: int
    rut_usuario: str
    img_actv: Optional[str] = None
    
    puntos: int
    cupos: int
    lugar: str
    requisito: Optional[str] = None


class ActividadUpdate(BaseModel):
    nombre_actividad: Optional[str] = None
    descripcion: Optional[str] = None
    responsable_actividad: Optional[str] = None
    fecha_inicio: Optional[datetime] = None
    fecha_termino: Optional[datetime] = None
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


# ==========================================
# RUTAS DE SUBIDA DE IMÁGENES
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


# ==========================================
# CRUD ACTIVIDADES
# ==========================================

@app.get("/actividades", tags=["Actividades"])
def obtener_actividades():
    try:
        res = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).order("id_actividad", desc=True).execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/actividades/{id_actividad}", tags=["Actividades"])
def obtener_actividad_por_id(id_actividad: int):
    try:
        res = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).eq("id_actividad", id_actividad).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="La actividad no existe")
        return res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actividades", status_code=status.HTTP_201_CREATED, tags=["Actividades"])
def crear_actividad(actividad: ActividadCreate):
    try:
        datos = actividad.model_dump()

        if datos.get("img_actv") == "":
            datos["img_actv"] = None

        if not datos.get("id_estado_actividad"):
            datos["id_estado_actividad"] = 1

        puntos = datos.pop("puntos")
        cupos = datos.pop("cupos")
        lugar = datos.pop("lugar")
        requisito = datos.pop("requisito", None)

        dt_inicio_str = str(actividad.fecha_inicio).replace('Z', '').split('+')[0]
        dt_inicio = datetime.fromisoformat(dt_inicio_str)

        dt_termino_str = str(actividad.fecha_termino).replace('Z', '').split('+')[0]
        dt_termino = datetime.fromisoformat(dt_termino_str)

        fecha_cal = dt_inicio.date().isoformat()
        hora_cal = dt_inicio.time().strftime("%H:%M:%S")

        datos['fecha_inicio'] = dt_inicio.isoformat()
        datos['fecha_termino'] = dt_termino.isoformat()
        fecha_vencimiento_date = dt_termino.date().isoformat()

        res_act = supabase.table("actividad").insert(datos).execute()
        if not res_act.data:
            raise HTTPException(status_code=400, detail="Error al registrar la actividad")

        id_actividad = res_act.data[0]["id_actividad"]

        supabase.table("puntaje_act").insert({
            "cantidad": puntos,
            "fecha_vencimiento": fecha_vencimiento_date,
            "id_actividad": id_actividad
        }).execute()

        supabase.table("cupo_actividad").insert({
            "cantidad": cupos,
            "id_actividad": id_actividad
        }).execute()

        supabase.table("lugar_actividad").insert({
            "descripcion": lugar,
            "id_actividad": id_actividad
        }).execute()

        supabase.table("calendario").insert({
            "fecha": fecha_cal,
            "hora": hora_cal,
            "lugar": lugar,
            "id_actividad": id_actividad
        }).execute()

        if requisito:
            supabase.table("requisito_participacion").insert({
                "descripcion": requisito,
                "id_actividad": id_actividad
            }).execute()

        actividad_creada = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).eq("id_actividad", id_actividad).execute()
        return actividad_creada.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/actividades/{id_actividad}", tags=["Actividades"])
def actualizar_actividad(id_actividad: int, actividad: ActividadUpdate):
    try:
        check = supabase.table("actividad").select("id_actividad, img_actv").eq("id_actividad", id_actividad).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada")

        img_antigua = check.data[0].get("img_actv")
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

        dt_inicio_obj = None
        if 'fecha_inicio' in datos and datos['fecha_inicio']:
            dt_inicio_obj = datos['fecha_inicio'].replace(tzinfo=None)
            datos['fecha_inicio'] = dt_inicio_obj.isoformat()

        if 'fecha_termino' in datos and datos['fecha_termino']:
            datos['fecha_termino'] = datos['fecha_termino'].replace(tzinfo=None).isoformat()

        if datos:
            supabase.table("actividad").update(datos).eq("id_actividad", id_actividad).execute()

        if puntos is not None:
            p_res = supabase.table("puntaje_act").select("id_puntaje").eq("id_actividad", id_actividad).execute()
            if p_res.data:
                supabase.table("puntaje_act").update({"cantidad": puntos}).eq("id_actividad", id_actividad).execute()
            else:
                fecha_venc = datos.get('fecha_termino') or datetime.now().isoformat()
                supabase.table("puntaje_act").insert({
                    "cantidad": puntos,
                    "fecha_vencimiento": fecha_venc[:10],
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

        if lugar is not None or dt_inicio_obj is not None:
            cal_res = supabase.table("calendario").select("id_calendario").eq("id_actividad", id_actividad).execute()
            cal_payload = {}
            if lugar is not None:
                cal_payload["lugar"] = lugar
            if dt_inicio_obj is not None:
                cal_payload["fecha"] = dt_inicio_obj.date().isoformat()
                cal_payload["hora"] = dt_inicio_obj.time().strftime("%H:%M:%S")

            if cal_res.data:
                supabase.table("calendario").update(cal_payload).eq("id_actividad", id_actividad).execute()

        if requisito is not None:
            r_res = supabase.table("requisito_participacion").select("id_requisito").eq("id_actividad", id_actividad).execute()
            if r_res.data:
                supabase.table("requisito_participacion").update({"descripcion": requisito}).eq("id_actividad", id_actividad).execute()
            else:
                supabase.table("requisito_participacion").insert({"descripcion": requisito, "id_actividad": id_actividad}).execute()

        res = supabase.table("actividad").select(QUERY_RELACIONES_ACTIVIDAD).eq("id_actividad", id_actividad).execute()
        return res.data[0]

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
# CATÁLOGOS AUXILIARES
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

    # ==========================================
# CRUD SOLICITUDES DE CANJE
# ==========================================

QUERY_RELACIONES_CANJE = """
    *,
    estado_canje(descripcion),
    premio(descripcion, imagen),
    usuario(nombre_completo, correo)
"""
@app.get("/consejeros", tags=["Catálogos"])
def obtener_consejeros():
    """
    Obtiene específicamente los usuarios con rol de Consejero de Carrera (suponiendo id_tipo_usuario = 3)
    """
    try:
        res = supabase.table("usuario")\
            .select("rut_usuario, nombre_completo, correo, id_tipo_usuario")\
            .eq("id_tipo_usuario", 4)\
            .execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/solicitudes-canje", tags=["Canjes"])
def obtener_solicitudes_canje():
    try:
        res = supabase.table("solicitud_canje").select(QUERY_RELACIONES_CANJE).order("id_canje", desc=True).execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
def generar_formatos_rut(rut: str) -> list[str]:
    """
    Limpia cualquier RUT recibido y genera sus variantes
    sin romper la ejecución de Python.
    """
    if not rut:
        return []

    # Extraemos únicamente dígitos y K/k
    limpio = re.sub(r"[^0-9kK]", "", str(rut)).upper()
    if len(limpio) < 2:
        return [rut]

    cuerpo = limpio[:-1]
    dv = limpio[-1]

    # Formato 1: Con puntos y guión (ej: 11.111.111-1)
    # Formateamos solo cuando cuerpo son únicamente dígitos
    if cuerpo.isdigit():
        cuerpo_puntos = f"{int(cuerpo):,}".replace(",", ".")
    else:
        cuerpo_puntos = cuerpo
        
    rut_puntos = f"{cuerpo_puntos}-{dv}"

    # Formato 2: Con guión sin puntos (ej: 11111111-1)
    rut_guion = f"{cuerpo}-{dv}"

    # Formato 3: Solo caracteres limpios (ej: 111111111)
    rut_limpio = f"{cuerpo}{dv}"

    # Retornamos las variantes sin duplicados
    return list(set([rut, rut_puntos, rut_guion, rut_limpio]))


@app.get("/actividades/conteo/usuario/{rut_usuario}", tags=["Actividades"])
def obtener_conteo_actividades_por_usuario(rut_usuario: str):
    """
    Retorna el conteo exacto de actividades creadas por el RUT especificado.
    """
    try:
        formatos = generar_formatos_rut(rut_usuario)
        
        # Construimos la condición OR para Supabase
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
        # Enviar detalle limpio en lugar de crash 500 silencioso
        raise HTTPException(status_code=500, detail=f"Error en consulta de actividades: {str(e)}")


@app.get("/actividades/usuario/{rut_usuario}", tags=["Actividades"])
def obtener_actividades_por_usuario(rut_usuario: str):
    """
    Retorna la lista completa de actividades asociadas a un usuario.
    """
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
        return res.data or []
    except Exception as e:
        print(f"Error crítico en /actividades/usuario/{rut_usuario}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/usuario/sesion/{rut_o_token}", tags=["Usuarios"])
def obtener_usuario_por_sesion(rut_o_token: str):
    """
    Busca al usuario por su RUT o por su Token registrado en sesion_usuario,
    retornando sus datos personales y su puntaje total de la tabla puntaje_total.
    """
    try:
        # 1. Verificar si viene un token registrado en sesion_usuario
        res_sesion = (
            supabase.table("sesion_usuario")
            .select("rut_usuario")
            .eq("token_acceso", rut_o_token)
            .execute()
        )

        rut_final = res_sesion.data[0]["rut_usuario"] if res_sesion.data else rut_o_token

        # 2. Consultar la información del usuario
        formatos = generar_formatos_rut(rut_final)
        condicion_or = ",".join([f'rut_usuario.eq."{f}"' for f in formatos])

        res_usuario = (
            supabase.table("usuario")
            .select("rut_usuario, nombre_completo, correo, id_tipo_usuario, id_sede")
            .or_(condicion_or)
            .execute()
        )

        if not res_usuario.data:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

        user_data = res_usuario.data[0]

        # 3. Consultar sus puntos reales desde la tabla puntaje_total
        res_puntos = (
            supabase.table("puntaje_total")
            .select("puntaje")
            .or_(condicion_or)
            .execute()
        )

        puntos_acumulados = res_puntos.data[0]["puntaje"] if res_puntos.data else 0

        # Combinar los datos
        user_data["puntaje_total"] = puntos_acumulados

        return user_data

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error consultando sesión de usuario: {e}")
        raise HTTPException(status_code=500, detail=str(e))