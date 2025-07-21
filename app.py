# app.py
import dash
from dash import dcc, html, dash_table, Input, Output, State, no_update
import dash_bootstrap_components as dbc
import pandas as pd
import requests
import time
import re
import io

# --- Lógica de Búsqueda (Tus funciones originales, sin cambios) ---
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
    """Orquesta la búsqueda y genera el DataFrame."""
    # ... (Esta función no cambia, cópiala de tu versión anterior)
    resultados_exitosos = []
    resultados_fallidos = []
    listado_nombres = listado_nombres or []
    listado_ids = listado_ids or []
    for nombre in listado_nombres:
        time.sleep(1)
        taxon_id = obtener_id_por_nombre(nombre)
        if taxon_id:
            datos_especie = obtener_datos_proteccion(taxon_id, nombre)
            resultados_exitosos.append(datos_especie)
        else:
            resultados_fallidos.append({"Especie": nombre, "Error": "ID de taxón no encontrado"})
    for taxon_id in listado_ids:
        time.sleep(1)
        nombre_cientifico = obtener_nombre_por_id(taxon_id)
        if nombre_cientifico:
            datos_especie = obtener_datos_proteccion(taxon_id, nombre_cientifico)
            resultados_exitosos.append(datos_especie)
        else:
            resultados_fallidos.append({"Especie": f"ID: {taxon_id}", "Error": "Nombre científico no encontrado"})
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
    
# --- Inicialización de la App Dash ---
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP]) # Tema oscuro como ejemplo
server = app.server

# --- DEFINICIÓN DE COMPONENTES REUTILIZABLES ---

# Contenido de la Barra Lateral
sidebar = html.Div(
    [
        html.H2("EIDOScopio", className="display-4"),
        html.Hr(),
        html.P(
            "Una herramienta para consultar el estatus de protección de especies silvestres en España.",
            className="lead"
        ),
        dbc.Alert(
            "Los datos se obtienen de la API de la base de datos EIDOS del MITECO.",
            color="info",
        ),
        html.Hr(),
        html.P("Creado por Aarón Quesada"),
        html.P([
            html.A("LinkedIn", href="https://www.linkedin.com/in/aaronq/", target="_blank"),
            " | ",
            html.A("GitHub", href="https://github.com/aaronque", target="_blank")
        ])
    ],
    style={"position": "fixed", "top": 0, "left": 0, "bottom": 0, "width": "20rem", "padding": "2rem 1rem", "background-color": "#222", "color": "#ccc"}
)

# Contenido Principal de la página
content = html.Div(
    [
        html.H1("🔎 Buscador del Estatus Legal de Especies", className="my-4 text-center"),
        dbc.Accordion([dbc.AccordionItem([html.P("- Para búsquedas por nombre científico: Introduce un nombre por cada línea."), html.P("- Para búsquedas por ID de EIDOS: Escribe los números separados por espacios o saltos de línea."), html.P("- Haz clic en 'Comenzar Búsqueda' para procesar los datos.")], title="ℹ️ Ver instrucciones de uso")]),
        dbc.Button("Cargar datos de ejemplo", id="btn-ejemplo", color="secondary", className="mt-3 mb-3"),
        dbc.Row([
            dbc.Col(dcc.Textarea(id='area-nombres', placeholder="Achondrostoma arcasii\nSus scrofa...", style={'width': '100%', 'height': 200})),
            dbc.Col(dcc.Textarea(id='area-ids', placeholder="13431\n9322...", style={'width': '100%', 'height': 200})),
        ]),
        dbc.Button("🚀 Comenzar Búsqueda", id="btn-busqueda", color="primary", size="lg", className="mt-3 w-100"),
        html.Hr(),
        html.Div(id='output-zona-progreso'),
        html.Div(id='output-zona-resultados'),
    ],
    style={"margin-left": "22rem", "padding": "2rem 1rem"}
)

# --- LAYOUT DE LA APP ---
app.layout = html.Div([
    # Componentes ocultos
    dcc.Store(id='store-tareas'),
    dcc.Store(id='store-resultados'),
    dcc.Download(id='download-excel'),
    dcc.Interval(id='interval-proceso', interval=1200, n_intervals=0, disabled=True),
    # Layout visible
    sidebar,
    content
])

# --- CALLBACKS PARA LA INTERACTIVIDAD ---
# ... (Todos tus callbacks van aquí, sin cambios. Cópialos de tu versión anterior) ...
@app.callback(Output('area-nombres', 'value'), Output('area-ids', 'value'), Input('btn-ejemplo', 'n_clicks'), prevent_initial_call=True)
def cargar_ejemplo(n_clicks):
    return "Lynx pardinus\nUrsus arctos\nGamusinus silvestris", "14389\n999999"
@app.callback(Output('store-tareas', 'data'), Output('store-resultados', 'data', allow_duplicate=True), Output('interval-proceso', 'disabled'), Output('output-zona-progreso', 'children'), Output('output-zona-resultados', 'children', allow_duplicate=True), Input('btn-busqueda', 'n_clicks'), State('area-nombres', 'value'), State('area-ids', 'value'), prevent_initial_call=True)
def iniciar_busqueda(n_clicks, nombres_texto, ids_texto):
    if not nombres_texto and not ids_texto: return no_update, no_update, True, dbc.Alert("Por favor, introduce al menos un nombre o un ID para buscar.", color="warning"), None
    nombres = [line.strip() for line in nombres_texto.strip().split('\n') if line.strip()]
    ids = [int(id_num) for id_num in re.split(r'\s+', ids_texto.strip()) if id_num.isdigit()]
    tareas = [('nombre', n) for n in nombres] + [('id', i) for i in ids]
    if not tareas: return no_update, no_update, True, dbc.Alert("No hay datos válidos para buscar.", color="warning"), None
    layout_progreso = html.Div([html.P(id='texto-progreso', children=f"Procesando 0 de {len(tareas)}..."), dbc.Progress(id='barra-progreso', value=0, style={"height": "20px"})])
    return {'tareas': tareas, 'total': len(tareas)}, [], False, layout_progreso, None
@app.callback(Output('store-resultados', 'data'), Output('store-tareas', 'data', allow_duplicate=True), Output('interval-proceso', 'disabled', allow_duplicate=True), Output('barra-progreso', 'value'), Output('texto-progreso', 'children'), Output('output-zona-resultados', 'children'), Input('interval-proceso', 'n_intervals'), State('store-tareas', 'data'), State('store-resultados', 'data'), prevent_initial_call=True)
def procesar_siguiente_tarea(n, data_tareas, resultados_actuales):
    if not data_tareas or not data_tareas['tareas']: return no_update, no_update, True, no_update, no_update, no_update
    tarea_actual = data_tareas['tareas'].pop(0)
    tipo, valor = tarea_actual
    if tipo == 'nombre':
        taxon_id = obtener_id_por_nombre(valor)
        if taxon_id: resultado = obtener_datos_proteccion(taxon_id, valor)
        else: resultado = {"Especie": valor, "Error": "ID de taxón no encontrado"}
    else:
        nombre_cientifico = obtener_nombre_por_id(valor)
        if nombre_cientifico: resultado = obtener_datos_proteccion(valor, nombre_cientifico)
        else: resultado = {"Especie": f"ID: {valor}", "Error": "Nombre científico no encontrado"}
    resultados_actuales.append(resultado)
    total_tareas = data_tareas['total']
    tareas_hechas = len(resultados_actuales)
    porcentaje = (tareas_hechas / total_tareas) * 100
    texto_progreso = f"Procesando {tareas_hechas} de {total_tareas}..."
    if not data_tareas['tareas']:
        df_resultado = pd.DataFrame(resultados_actuales).fillna('-')
        if 'Especie' in df_resultado.columns:
            cols = df_resultado.columns.tolist()
            cols.insert(0, cols.pop(cols.index('Especie')))
            if 'Error' in cols: cols.append(cols.pop(cols.index('Error')))
            df_resultado = df_resultado.reindex(columns=cols)
        layout_final = html.Div([html.H3("✅ Búsqueda Completada", className="mt-4"), dbc.Button("📥 Descargar Tabla como Excel", id="btn-descarga", color="success", className="mt-3 mb-3 w-100"), dash_table.DataTable(columns=[{"name": i, "id": i} for i in df_resultado.columns], data=df_resultado.to_dict('records'), page_size=15, style_table={'overflowX': 'auto'})])
        return resultados_actuales, data_tareas, True, 100, "Completado.", layout_final
    else:
        return resultados_actuales, data_tareas, False, porcentaje, texto_progreso, no_update
@app.callback(Output('download-excel', 'data'), Input('btn-descarga', 'n_clicks'), State('store-resultados', 'data'), prevent_initial_call=True)
def descargar_excel(n_clicks, resultados_finales):
    if not resultados_finales: return no_update
    df = pd.DataFrame(resultados_finales).fillna('-')
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='ProteccionEspecies')
    output.seek(0)
    return dcc.send_bytes(output.getvalue(), "proteccion_especies.xlsx")


# --- Ejecución del Servidor ---
if __name__ == '__main__':
    app.run_server(debug=True)
