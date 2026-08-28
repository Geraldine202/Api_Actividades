import os
import uuid
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Error: Faltan las credenciales de Supabase en el archivo .env")

# Cliente Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(
    title="API Actividades - Aquí Todos Ganan",
    description="Backend CRUD integral para administrar actividades, almacenamiento de imágenes y tablas vinculadas",
    version="1.6.0"
)

# Configurar CORS para Ionic / Angular
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Consulta SQL reusable con JOINs a tablas de catálogo y tablas secundarias
QUERY_RELACIONES = """
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

# ==========================================
# FUNCIONES AUXILIARES
# ==========================================

def eliminar_imagen_storage(url_imagen: Optional[str]):
    """Extrae el nombre del archivo desde la URL pública y lo borra del bucket 'actividad'."""
    if not url_imagen:
        return
    try:
        # Extraer el nombre final del archivo de la URL
        nombre_archivo = url_imagen.split("/")[-1]
        if nombre_archivo:
            supabase.storage.from_("actividad").remove([nombre_archivo])
    except Exception as e:
        print(f"Advertencia: No se pudo eliminar la imagen del storage ({url_imagen}): {e}")


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
    id_estado_actividad: Optional[int] = 1  # Por defecto 1 (Programada)
    id_sede: int
    rut_usuario: str
    img_actv: Optional[str] = None  # <-- Campo de imagen (URL o String)
    
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
    img_actv: Optional[str] = None  # <-- Campo de imagen (URL o String)
    
    puntos: Optional[int] = None
    cupos: Optional[int] = None
    lugar: Optional[str] = None
    requisito: Optional[str] = None


# ==========================================
# RUTAS DE LA API (CRUD ACTIVIDADES)
# ==========================================

@app.get("/", tags=["Inicio"])
def inicio():
    return {"mensaje": "API de Actividades operativa y conectada a Supabase"}


@app.post("/upload-imagen", tags=["Archivos"])
async def subir_imagen(file: UploadFile = File(...)):
    """Subes una imagen al bucket 'actividad' y retorna su URL pública."""
    try:
        if not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="El archivo enviado no es una imagen válida.")

        file_ext = file.filename.split(".")[-1]
        file_name = f"{uuid.uuid4()}.{file_ext}"

        contents = await file.read()

        supabase.storage.from_("actividad").upload(
            file_name,
            contents,
            file_options={"content-type": file.content_type}
        )

        url_publica = supabase.storage.from_("actividad").get_public_url(file_name)

        return {"url": url_publica, "filename": file_name}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al subir imagen: {str(e)}")


@app.get("/docentes", tags=["Catálogos"])
def obtener_docentes():
    try:
        res = (
            supabase.table("usuario")
            .select("rut_usuario, nombre_completo, correo, id_tipo_usuario")
            .eq("id_tipo_usuario", 3)
            .execute()
        )
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/actividades", tags=["Actividades"])
def obtener_actividades():
    try:
        res = supabase.table("actividad").select(QUERY_RELACIONES).order("id_actividad", desc=True).execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/actividades/{id_actividad}", tags=["Actividades"])
def obtener_actividad_por_id(id_actividad: int):
    try:
        res = supabase.table("actividad").select(QUERY_RELACIONES).eq("id_actividad", id_actividad).execute()
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

        if not datos.get("id_estado_actividad"):
            datos["id_estado_actividad"] = 1

        puntos = datos.pop("puntos")
        cupos = datos.pop("cupos")
        lugar = datos.pop("lugar")
        requisito = datos.pop("requisito", None)

        fecha_cal = datos['fecha_inicio'].date().isoformat()
        hora_cal = datos['fecha_inicio'].time().strftime("%H:%M:%S")

        datos['fecha_inicio'] = datos['fecha_inicio'].isoformat()
        datos['fecha_termino'] = datos['fecha_termino'].isoformat()
        fecha_vencimiento_date = actividad.fecha_termino.date().isoformat()

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

        actividad_creada = supabase.table("actividad").select(QUERY_RELACIONES).eq("id_actividad", id_actividad).execute()
        return actividad_creada.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/actividades/{id_actividad}", tags=["Actividades"])
def actualizar_actividad(id_actividad: int, actividad: ActividadUpdate):
    try:
        # 1. Obtener la imagen actual guardada en la BD
        check = supabase.table("actividad").select("id_actividad, img_actv").eq("id_actividad", id_actividad).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada")

        img_antigua = check.data[0].get("img_actv")
        datos = actividad.model_dump(exclude_unset=True)

        # 2. Si viene una nueva imagen y es distinta a la anterior, eliminar la previa del Storage
        nueva_img = datos.get("img_actv")
        if nueva_img and img_antigua and nueva_img != img_antigua:
            eliminar_imagen_storage(img_antigua)

        puntos = datos.pop('puntos', None)
        cupos = datos.pop('cupos', None)
        lugar = datos.pop('lugar', None)
        requisito = datos.pop('requisito', None)

        dt_inicio_obj = datos.get('fecha_inicio')

        if datos:
            if 'fecha_inicio' in datos and datos['fecha_inicio']:
                datos['fecha_inicio'] = datos['fecha_inicio'].isoformat()
            if 'fecha_termino' in datos and datos['fecha_termino']:
                datos['fecha_termino'] = datos['fecha_termino'].isoformat()

            supabase.table("actividad").update(datos).eq("id_actividad", id_actividad).execute()

        if puntos is not None:
            p_res = supabase.table("puntaje_act").select("id_puntaje").eq("id_actividad", id_actividad).execute()
            if p_res.data:
                supabase.table("puntaje_act").update({"cantidad": puntos}).eq("id_actividad", id_actividad).execute()
            else:
                fecha_venc = datos.get('fecha_termino') or datetime.now().isoformat()
                supabase.table("puntaje_act").insert({
                    "cantidad": puntos,
                    "fecha_vencimiento": fecha_venc[:10] if isinstance(fecha_venc, str) else fecha_venc.date().isoformat(),
                    "id_actividad": id_actividad
                }).execute()

        if cupos is not None:
            c_res = supabase.table("cupo_actividad").select("id_cupo").eq("id_actividad", id_actividad).execute()
            if c_res.data:
                supabase.table("cupo_actividad").update({"cantidad": cupos}).eq("id_actividad", id_actividad).execute()
            else:
                supabase.table("cupo_actividad").insert({
                    "cantidad": cupos,
                    "id_actividad": id_actividad
                }).execute()

        if lugar is not None:
            l_res = supabase.table("lugar_actividad").select("id_lugar_actividad").eq("id_actividad", id_actividad).execute()
            if l_res.data:
                supabase.table("lugar_actividad").update({"descripcion": lugar}).eq("id_actividad", id_actividad).execute()
            else:
                supabase.table("lugar_actividad").insert({
                    "descripcion": lugar,
                    "id_actividad": id_actividad
                }).execute()

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
            else:
                cal_payload["id_actividad"] = id_actividad
                if "lugar" not in cal_payload:
                    cal_payload["lugar"] = lugar or "Por definir"
                if "fecha" not in cal_payload:
                    act_data = supabase.table("actividad").select("fecha_inicio").eq("id_actividad", id_actividad).execute()
                    dt = datetime.fromisoformat(act_data.data[0]["fecha_inicio"].replace('Z', '+00:00'))
                    cal_payload["fecha"] = dt.date().isoformat()
                    cal_payload["hora"] = dt.time().strftime("%H:%M:%S")
                supabase.table("calendario").insert(cal_payload).execute()

        if requisito is not None:
            r_res = supabase.table("requisito_participacion").select("id_requisito").eq("id_actividad", id_actividad).execute()
            if r_res.data:
                supabase.table("requisito_participacion").update({"descripcion": requisito}).eq("id_actividad", id_actividad).execute()
            else:
                supabase.table("requisito_participacion").insert({
                    "descripcion": requisito,
                    "id_actividad": id_actividad
                }).execute()

        res = supabase.table("actividad").select(QUERY_RELACIONES).eq("id_actividad", id_actividad).execute()
        return res.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/actividades/{id_actividad}", tags=["Actividades"])
def eliminar_actividad(id_actividad: int):
    try:
        # 1. Obtener la URL de la imagen de la actividad antes de eliminarla
        check = supabase.table("actividad").select("id_actividad, img_actv").eq("id_actividad", id_actividad).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada para eliminar")

        img_antigua = check.data[0].get("img_actv")

        # 2. Eliminar registros en tablas hijas
        supabase.table("puntaje_act").delete().eq("id_actividad", id_actividad).execute()
        supabase.table("cupo_actividad").delete().eq("id_actividad", id_actividad).execute()
        supabase.table("lugar_actividad").delete().eq("id_actividad", id_actividad).execute()
        supabase.table("requisito_participacion").delete().eq("id_actividad", id_actividad).execute()
        supabase.table("calendario").delete().eq("id_actividad", id_actividad).execute()

        # 3. Eliminar la actividad de la tabla principal
        supabase.table("actividad").delete().eq("id_actividad", id_actividad).execute()

        # 4. Eliminar el archivo de imagen del Storage
        if img_antigua:
            eliminar_imagen_storage(img_antigua)

        return {"mensaje": f"Actividad {id_actividad} e imagen asociada fueron eliminadas correctamente"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# ENDPOINTS AUXILIARES
# ==========================================

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


@app.get("/sedes", tags=["Catálogos"])
def obtener_sedes():
    try:
        res = supabase.table("sede").select("*").execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))