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
# ... (Aquí van las 4 funciones: obtener_id_por_nombre, etc. Sin cambios) ...
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
    except requests.exceptions.RequestException: return None
def obtener_nombre_por_id(taxon_id):
    try:
        respuesta = requests.get(f"{API_BASE_URL}/rpc/obtenertaxonporid", params={"_idtaxon": taxon_id})
        respuesta.raise_for_status()
        datos = respuesta.json()
        if datos and datos[0].get('name'):
            return datos[0]['name']
        return None
    except requests.exceptions.RequestException: return None
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

# --- Inicialización de la App Dash ---
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.DARKLY])
server = app.server

# --- LAYOUT DE LA APP ---
app.layout = dbc.Container([
    # Componentes ocultos
    dcc.Store(id='store-tareas'),
    dcc.Store(id='store-resultados'),
    dcc.Download(id='download-excel'),
    dcc.Interval(id='interval-proceso', interval=1200, n_intervals=0, disabled=True),

    # Interfaz visible
    html.H1("EIDOScopio: 🔎 Buscador del Estatus Legal de Especies", className="my-4 text-center"),
    dbc.Accordion([dbc.AccordionItem([html.P(line) for line in ["- Para Nombres Científicos: Introduce un nombre por cada línea.", "- Para IDs de Taxón: Escribe los números separados por espacios o saltos de línea.", "- Haz clic en 'Comenzar Búsqueda' para procesar los datos."]], title="ℹ️ Ver instrucciones de uso")]),
    dbc.Button("Cargar datos de ejemplo", id="btn-ejemplo", color="secondary", className="mt-3 mb-3"),
    dbc.Row([
        dbc.Col(dcc.Textarea(id='area-nombres', placeholder="Lynx pardinus\nUrsus arctos...", style={'width': '100%', 'height': 200})),
        dbc.Col(dcc.Textarea(id='area-ids', placeholder="14389\n999999...", style={'width': '100%', 'height': 200})),
    ]),
    dbc.Button("🚀 Comenzar Búsqueda", id="btn-busqueda", color="primary", size="lg", className="mt-3 w-100"),
    html.Hr(),
    
    html.Div(id='output-zona-progreso'),
    html.Div(id='output-zona-cancelar', className="mt-2"), # NUEVO: Espacio para el botón de cancelar
    html.Div(id='output-zona-resultados'),

    # Footer
    html.Hr(className="my-5"),
    # ... (tu footer aquí) ...

], fluid=True, className="mb-5")


# --- CALLBACKS PARA LA INTERACTIVIDAD ---

@app.callback(Output('area-nombres', 'value'), Output('area-ids', 'value'), Input('btn-ejemplo', 'n_clicks'), prevent_initial_call=True)
def cargar_ejemplo(n_clicks):
    return "Lynx pardinus\nUrsus arctos\nGamusinus silvestris", "14389\n999999"

# MODIFICADO: Callback de inicio ahora también crea el botón de cancelar
@app.callback(
    Output('store-tareas', 'data'),
    Output('store-resultados', 'data', allow_duplicate=True),
    Output('interval-proceso', 'disabled'),
    Output('output-zona-progreso', 'children'),
    Output('output-zona-resultados', 'children', allow_duplicate=True),
    Output('output-zona-cancelar', 'children'), # NUEVO Output
    Input('btn-busqueda', 'n_clicks'),
    State('area-nombres', 'value'),
    State('area-ids', 'value'),
    prevent_initial_call=True
)
def iniciar_busqueda(n_clicks, nombres_texto, ids_texto):
    if not nombres_texto and not ids_texto:
        return no_update, no_update, True, dbc.Alert("Introduce datos para buscar.", color="warning"), None, None

    nombres = [line.strip() for line in nombres_texto.strip().split('\n') if line.strip()] if nombres_texto else []
    ids = [int(id_num) for id_num in re.split(r'\s+', ids_texto.strip()) if id_num.isdigit()] if ids_texto else []
    
    tareas = [('nombre', n) for n in nombres] + [('id', i) for i in ids]
    
    if not tareas:
        return no_update, no_update, True, dbc.Alert("No hay datos válidos para buscar.", color="warning"), None, None

    layout_progreso = html.Div([
        html.P(id='texto-progreso', children=f"Procesando 0 de {len(tareas)}..."),
        dbc.Progress(id='barra-progreso', value=0, style={"height": "20px"})
    ])
    
    # NUEVO: Crear el botón de cancelar
    boton_cancelar = dbc.Button("❌ Cancelar Búsqueda", id="btn-cancelar", color="danger", className="w-100")

    return {'tareas': tareas, 'total': len(tareas)}, [], False, layout_progreso, None, boton_cancelar

# MODIFICADO: Callback de proceso ahora limpia la zona de cancelar al terminar
@app.callback(
    Output('store-resultados', 'data'),
    Output('store-tareas', 'data', allow_duplicate=True),
    Output('interval-proceso', 'disabled', allow_duplicate=True),
    Output('barra-progreso', 'value'),
    Output('texto-progreso', 'children'),
    Output('output-zona-resultados', 'children'),
    Output('output-zona-cancelar', 'children', allow_duplicate=True), # NUEVO Output
    Input('interval-proceso', 'n_intervals'),
    State('store-tareas', 'data'),
    State('store-resultados', 'data'),
    prevent_initial_call=True
)
def procesar_siguiente_tarea(n, data_tareas, resultados_actuales):
    if not data_tareas or not data_tareas['tareas']:
        return no_update, no_update, True, no_update, no_update, no_update, no_update

    time.sleep(0.1)
    tarea_actual = data_tareas['tareas'].pop(0)
    tipo, valor = tarea_actual

    if tipo == 'nombre':
        # ... (lógica de búsqueda por nombre)
        taxon_id = obtener_id_por_nombre(valor)
        if taxon_id: resultado = obtener_datos_proteccion(taxon_id, valor)
        else: resultado = {"Especie": valor, "Error": "ID de taxón no encontrado"}
    else: # tipo == 'id'
        # ... (lógica de búsqueda por id)
        nombre_cientifico = obtener_nombre_por_id(valor)
        if nombre_cientifico: resultado = obtener_datos_proteccion(valor, nombre_cientifico)
        else: resultado = {"Especie": f"ID: {valor}", "Error": "Nombre científico no encontrado"}

    resultados_actuales.append(resultado)
    
    total_tareas = data_tareas['total']
    tareas_hechas = len(resultados_actuales)
    porcentaje = (tareas_hechas / total_tareas) * 100
    texto_progreso = f"Procesando {tareas_hechas} de {total_tareas}..."

    if not data_tareas['tareas']:
        # Última tarea, proceso terminado
        df_resultado = pd.DataFrame(resultados_actuales).fillna('-')
        if 'Especie' in df_resultado.columns:
            # ... (reordenar columnas)
            cols = df_resultado.columns.tolist()
            cols.insert(0, cols.pop(cols.index('Especie')))
            if 'Error' in cols: cols.append(cols.pop(cols.index('Error')))
            df_resultado = df_resultado.reindex(columns=cols)

        layout_final = html.Div([
            html.H3("✅ Búsqueda Completada", className="mt-4"),
            dbc.Button("📥 Descargar Tabla como Excel", id="btn-descarga", color="success", className="mt-3 mb-3 w-100"),
            dash_table.DataTable(
                columns=[{"name": i, "id": i} for i in df_resultado.columns],
                data=df_resultado.to_dict('records'),
                page_size=15, style_table={'overflowX': 'auto'}
            )
        ])
        # MODIFICADO: Al terminar, devuelve None para limpiar el botón de cancelar
        return resultados_actuales, data_tareas, True, 100, "Completado.", layout_final, None
    else:
        # Aún quedan tareas
        return resultados_actuales, data_tareas, False, porcentaje, texto_progreso, no_update, no_update

# NUEVO: Callback para el botón de cancelar
@app.callback(
    Output('interval-proceso', 'disabled', allow_duplicate=True),
    Output('output-zona-progreso', 'children', allow_duplicate=True),
    Output('output-zona-cancelar', 'children', allow_duplicate=True),
    Output('store-tareas', 'data', allow_duplicate=True),
    Output('store-resultados', 'data', allow_duplicate=True),
    Input('btn-cancelar', 'n_clicks'),
    prevent_initial_call=True
)
def cancelar_busqueda(n_clicks):
    # Detiene el temporizador, limpia las zonas de progreso y cancelar, y resetea los datos.
    return True, None, None, {'tareas':[], 'total':0}, []


# Callback de descarga (sin cambios)
@app.callback(
    Output('download-excel', 'data'),
    Input('btn-descarga', 'n_clicks'),
    State('store-resultados', 'data'),
    prevent_initial_call=True
)
def descargar_excel(n_clicks, resultados_finales):
    if not resultados_finales:
        return no_update

    df = pd.DataFrame(resultados_finales).fillna('-')
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='ProteccionEspecies')
    output.seek(0)
    
    return dcc.send_bytes(output.getvalue(), "proteccion_especies.xlsx")

# --- Ejecución del Servidor ---
if __name__ == '__main__':
    app.run_server(debug=True)
