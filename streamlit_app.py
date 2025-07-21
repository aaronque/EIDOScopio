# streamlit_app.py
import streamlit as st
import pandas as pd
import requests
import time
import re
import io

# --- Configuración de la Página ---
st.set_page_config(
    page_title="Eidoscopio",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Lógica de Búsqueda (Funciones) ---
API_BASE_URL = "https://iepnb.gob.es/api/especie"

def obtener_id_por_nombre(nombre_cientifico):
    """Busca el ID de taxón para un nombre científico."""
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
    """Busca el nombre científico aceptado para un ID de taxón."""
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
    """Obtiene y procesa los datos de protección para un único taxón ID."""
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
    """Orquesta la búsqueda y genera el DataFrame con una barra de progreso."""
    resultados_exitosos = []
    resultados_fallidos = []
    listado_nombres = listado_nombres or []
    listado_ids = listado_ids or []
    
    total_items = len(listado_nombres) + len(listado_ids)
    barra_progreso = st.progress(0, text="Iniciando búsqueda...")

    for i, nombre in enumerate(listado_nombres):
        barra_progreso.progress((i + 1) / total_items, text=f"Consultando nombre: {nombre[:30]}...")
        taxon_id = obtener_id_por_nombre(nombre)
        if taxon_id:
            datos_especie = obtener_datos_proteccion(taxon_id, nombre)
            resultados_exitosos.append(datos_especie)
        else:
            resultados_fallidos.append({"Especie": nombre, "Error": "ID de taxón no encontrado"})
        time.sleep(1)

    for i, taxon_id in enumerate(listado_ids):
        barra_progreso.progress((len(listado_nombres) + i + 1) / total_items, text=f"Consultando ID: {taxon_id}...")
        nombre_cientifico = obtener_nombre_por_id(taxon_id)
        if nombre_cientifico:
            datos_especie = obtener_datos_proteccion(taxon_id, nombre_cientifico)
            resultados_exitosos.append(datos_especie)
        else:
            resultados_fallidos.append({"Especie": f"ID: {taxon_id}", "Error": "Nombre científico no encontrado"})
        time.sleep(1)
    
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

# --- Barra Lateral (Sidebar) ---
st.sidebar.title("Acerca de Eidoscopio")
st.sidebar.info(
    "Esta herramienta permite consultar el estatus de protección de especies en España "
    "a través de la API de la base de datos EIDOS del MITECO."
)
st.sidebar.success("Creado con Streamlit y Python.")
st.sidebar.markdown("---")
st.sidebar.markdown("Licencia MIT")

# --- Interfaz Principal ---
st.title("Eidoscopio: Visor de Especies Protegidas 🔎")

with st.expander("ℹ️ Ver instrucciones de uso"):
    st.markdown("""
        - **Nombres Científicos**: Introduce un nombre por cada línea.
        - **IDs de Taxón**: Escribe los números separados por espacios o saltos de línea.
        - Haz clic en 'Comenzar Búsqueda' para procesar los datos y generar el Excel.
    """)

# Botón de ejemplo
EJEMPLO_NOMBRES = "Lynx pardinus\nUrsus arctos\nUna especie inventada"
EJEMPLO_IDS = "14389\n999999" # Aquila adalberti y un ID falso

if 'nombres_key' not in st.session_state:
    st.session_state.nombres_key = ""
if 'ids_key' not in st.session_state:
    st.session_state.ids_key = ""

if st.button("Cargar datos de ejemplo"):
    st.session_state.nombres_key = EJEMPLO_NOMBRES
    st.session_state.ids_key = EJEMPLO_IDS
    st.rerun()

# Columnas para la entrada de datos
col1, col2 = st.columns(2)
with col1:
    nombres_texto = st.text_area(
        "**Nombres Científicos**", height=200,
        placeholder="Achondrostoma arcasii\nSus scrofa...", key="nombres_key"
    )
with col2:
    ids_texto = st.text_area(
        "**IDs de Taxón**", height=200,
        placeholder="13431\n9322...", key="ids_key"
    )

# Botón de búsqueda y lógica de ejecución
if st.button("🚀 Comenzar Búsqueda", use_container_width=True, type="primary"):
    if not nombres_texto and not ids_texto:
        st.warning("Por favor, introduce al menos un nombre o un ID para buscar.")
    else:
        lista_nombres = [line.strip() for line in nombres_texto.strip().split('\n') if line.strip()]
        lista_ids = [int(id_num) for id_num in re.split(r'\s+', ids_texto.strip()) if id_num.isdigit()]
        
        df_resultado = generar_tabla_completa(lista_nombres, lista_ids)

        if df_resultado.empty:
            st.info("La búsqueda no produjo resultados.")
        else:
            st.markdown("---")
            st.subheader("📊 Resumen de Resultados")
            
            # Métricas
            m_col1, m_col2, m_col3 = st.columns(3)
            total_consultados = len(lista_nombres) + len(lista_ids)
            encontrados = len(df_resultado[df_resultado['Error'] == '-'])
            
            columnas_proteccion = [col for col in df_resultado.columns if 'Catálogo' in col or 'Convenio' in col]
            df_resultado['protegido'] = df_resultado[columnas_proteccion].ne('-').any(axis=1)
            protegidos = len(df_resultado[df_resultado['protegido'] & (df_resultado['Error'] == '-')])

            m_col1.metric("Especies Consultadas", f"{total_consultados}")
            m_col2.metric("Resultados Encontrados", f"{encontrados}")
            m_col3.metric("Especies con Protección", f"{protegidos}", help="Número de especies encontradas con al menos un estatus de protección.")

            # Tabla de resultados
            st.dataframe(df_resultado.drop(columns=['protegido']))
            
            # Lógica de descarga
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                df_resultado.drop(columns=['protegido']).to_excel(writer, index=False, sheet_name='ProteccionEspecies')
            output.seek(0)
            
            st.download_button(
                label="📥 Descargar Tabla como Excel",
                data=output,
                file_name="proteccion_especies.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

# --- FOOTER ---
st.markdown("---")
st.markdown(
    "Creado por **[Aarón Quesada](https://www.linkedin.com/in/aquesada/)** | "
    "**[GitHub](https://github.com/aaronque)**"
)
