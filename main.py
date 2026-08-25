import os
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Error: Faltan las credenciales de Supabase en el archivo .env")

# Cliente Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(
    title="API Actividades",
    description="Backend CRUD completo para administrar actividades en Supabase",
    version="1.0.0"
)

# Configurar CORS para permitir peticiones desde Ionic / Angular
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Consulta SQL reusable para obtener los datos anidados de tablas foráneas
QUERY_RELACIONES = """
    *,
    tipo_actividad(descripcion),
    estado_actividad(descripcion),
    sede(descripcion)
"""

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
    id_estado_actividad: int
    id_sede: int
    rut_usuario: str

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

# ==========================================
# RUTAS DE LA API (CRUD ACTIVIDADES)
# ==========================================

@app.get("/", tags=["Inicio"])
def inicio():
    return {"mensaje": "API de Actividades operativa y conectada a Supabase"}


# 1. Obtener la lista completa de actividades
@app.get("/actividades", tags=["Actividades"])
def obtener_actividades():
    try:
        res = supabase.table("actividad").select(QUERY_RELACIONES).order("id_actividad", desc=True).execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 2. Obtener una actividad específica por id
@app.get("/actividades/{id_actividad}", tags=["Actividades"])
def obtener_actividad_por_id(id_actividad: int):
    try:
        res = supabase.table("actividad").select(QUERY_RELACIONES).eq("id_actividad", id_actividad).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="La actividad no existe")
        return res.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 3. Crear una nueva actividad
@app.post("/actividades", status_code=status.HTTP_201_CREATED, tags=["Actividades"])
def crear_actividad(actividad: ActividadCreate):
    try:
        datos = actividad.model_dump()
        datos['fecha_inicio'] = datos['fecha_inicio'].isoformat()
        datos['fecha_termino'] = datos['fecha_termino'].isoformat()

        # Insertar y retornar inmediatamente el registro con sus relaciones
        res = supabase.table("actividad").insert(datos).execute()
        if not res.data:
            raise HTTPException(status_code=400, detail="Error al registrar la actividad")
        
        id_creado = res.data[0]["id_actividad"]
        actividad_creada = supabase.table("actividad").select(QUERY_RELACIONES).eq("id_actividad", id_creado).execute()
        return actividad_creada.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 4. Actualizar una actividad existente
@app.put("/actividades/{id_actividad}", tags=["Actividades"])
def actualizar_actividad(id_actividad: int, actividad: ActividadUpdate):
    try:
        datos = actividad.model_dump(exclude_unset=True)
        if not datos:
            raise HTTPException(status_code=400, detail="Sin campos para actualizar")

        if 'fecha_inicio' in datos and datos['fecha_inicio']:
            datos['fecha_inicio'] = datos['fecha_inicio'].isoformat()
        if 'fecha_termino' in datos and datos['fecha_termino']:
            datos['fecha_termino'] = datos['fecha_termino'].isoformat()

        res = supabase.table("actividad").update(datos).eq("id_actividad", id_actividad).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada")

        actividad_actualizada = supabase.table("actividad").select(QUERY_RELACIONES).eq("id_actividad", id_actividad).execute()
        return actividad_actualizada.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 5. Eliminar una actividad
@app.delete("/actividades/{id_actividad}", tags=["Actividades"])
def eliminar_actividad(id_actividad: int):
    try:
        res = supabase.table("actividad").delete().eq("id_actividad", id_actividad).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Actividad no encontrada para eliminar")
        return {"mensaje": f"Actividad {id_actividad} eliminada correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# ENDPOINTS AUXILIARES (Para Selects en Frontend)
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