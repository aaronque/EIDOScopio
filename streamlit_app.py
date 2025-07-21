# streamlit_app.py
import streamlit as st
import pandas as pd
import requests
import time
import re
import io

# --- Lógica de Búsqueda (Tus funciones) ---
API_BASE_URL = "https://iepnb.gob.es/api/especie"

def obtener_id_por_nombre(nombre_cientifico):
    try:
        respuesta = requests.get(f"{API_BASE_URL}/rpc/obtenertaxonespornombre", params={"_nombretaxon": nombre_cientifico})
        respuesta.raise_for_status()
        datos = respuesta.json()
        if not datos: return None
        for registro in datos:
            if registro.get('nametype') == 'Aceptado/válido':
                return registro.get('taxonid')
        return None
    except requests.exceptions.RequestException:
        return None

def obtener_nombre_por_id(taxon_id):
    try:
        respuesta = requests.get(f"{API_BASE_URL}/rpc/obtenertaxonporid", params={"_idtaxon": taxon_id})
        respuesta.raise_for_status()
        datos = respuesta.json()
        if datos and datos[0].get('name'):
            return datos[0]['name']
        return None
    except requests.exceptions.RequestException:
        return None

def obtener_datos_proteccion(taxon_id, nombre_cientifico_base):
    protecciones = {"Especie": nombre_cientifico_base}
    try:
        respuesta_legal = requests.get(f"{API_BASE_URL}/rpc/obtenerestadoslegalesportaxonid", params={"_idtaxon": taxon_id})
        respuesta_legal.raise_for_status()
        datos_legales = respuesta_legal.json()
        for item in datos_legales:
            if item.get('idvigente') != 1: continue
            ambito = item.get('ambito')
            estado = item.get('estadolegal')
            columna = ""
            if ambito == "Nacional": columna = item.get('dataset', 'Catálogo Nacional')
            elif ambito == "Autonómico": columna = f"Catálogo - {item.get('ccaa', 'Desconocida')}"
            elif ambito == "Internacional": columna = item.get('dataset', 'Convenio Internacional')
            if columna:
                if columna in protecciones and protecciones[columna] != '-':
                    if estado not in protecciones[columna]: protecciones[columna] += f", {estado}"
                else: protecciones[columna] = estado
        return protecciones
    except requests.exceptions.RequestException:
        protecciones['Error'] = 'Fallo al obtener datos legales'
        return protecciones

def generar_tabla_completa(listado_nombres=None, listado_ids=None):
    resultados_exitosos = []
    resultados_fallidos = []
    listado_nombres = listado_nombres or []
    listado_ids = listado_ids or []
    
    total_items = len(listado_nombres) + len(listado_ids)
    barra_progreso = st.progress(0)

    for i, nombre in enumerate(listado_nombres):
        taxon_id = obtener_id_por_nombre(nombre)
        if taxon_id:
            datos_especie = obtener_datos_proteccion(taxon_id, nombre)
            resultados_exitosos.append(datos_especie)
        else:
            resultados_fallidos.append({"Especie": nombre, "Error": "ID de taxón no encontrado con ese nombre"})
        time.sleep(1)
        barra_progreso.progress((i + 1) / total_items)

    for i, taxon_id in enumerate(listado_ids):
        nombre_cientifico = obtener_nombre_por_id(taxon_id)
        if nombre_cientifico:
            datos_especie = obtener_datos_proteccion(taxon_id, nombre_cientifico)
            resultados_exitosos.append(datos_especie)
        else:
            resultados_fallidos.append({"Especie": f"ID: {taxon_id}", "Error": "Nombre científico no encontrado con ese ID"})
        time.sleep(1)
        barra_progreso.progress((len(listado_nombres) + i + 1) / total_items)
    
    barra_progreso.empty()

    datos_para_tabla = resultados_exitosos + resultados_fallidos
    if not datos_para_tabla: return pd.DataFrame()

    df = pd.DataFrame(datos_para_tabla)
    df.fillna('-', inplace=True)
    if 'Especie' in df.columns:
        cols = df.columns.tolist()
        cols.insert(0, cols.pop(cols.index('Especie')))
        if 'Error' in cols:
            cols.append(cols.pop(cols.index('Error')))
        df = df.reindex(columns=cols)
    return df

# --- Interfaz de Streamlit ---
st.set_page_config(page_title="Buscador de Especies", layout="wide")
st.title("EIDOScopio 🔎 Consulta de Estatus Legal de Especies Silvestres presentes en España")
col1, col2 = st.columns(2)
with col1:
    nombres_texto = st.text_area("**Nombres Científicos (uno por línea)**", height=250, placeholder="Achondrostoma arcasii\nSus scrofa...")
with col2:
    ids_texto = st.text_area("**IDs de Taxón (separados por espacio o línea)**", height=250, placeholder="13431\n9322...")

if st.button("🚀 Comenzar Búsqueda", use_container_width=True):
    if not nombres_texto and not ids_texto:
        st.warning("Por favor, introduce al menos un nombre o un ID.")
    else:
        lista_nombres = [line.strip() for line in nombres_texto.strip().split('\n') if line.strip()]
        lista_ids = [int(id_num) for id_num in re.split(r'\s+', ids_texto.strip()) if id_num.isdigit()]
        
        df_resultado = generar_tabla_completa(lista_nombres, lista_ids)
        st.success("¡Búsqueda completada!")
        st.dataframe(df_resultado)
        
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df_resultado.to_excel(writer, index=False, sheet_name='ProteccionEspecies')
        output.seek(0)
        
        st.download_button(
            label="📥 Descargar Tabla como Excel",
            data=output,
            file_name="proteccion_especies.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
