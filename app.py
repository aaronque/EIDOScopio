# app.py
import dash
from dash import dcc, html, dash_table, Input, Output, State, no_update
import dash_bootstrap_components as dbc
import pandas as pd
import requests
import re
import io
import diskcache

# --- Configuración del Gestor para Callbacks en Segundo Plano ---
cache = diskcache.Cache("./cache")
background_callback_manager = dash.DiskcacheManager(cache)

# --- Lógica de Búsqueda (Sin cambios) ---
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

def generar_tabla_completa(listado_nombres=None, listado_ids=None, progress_callback=None):
    """Orquesta la búsqueda y genera el DataFrame, actualizando el progreso."""
    resultados_exitosos = []
    resultados_fallidos = []
    listado_nombres = listado_nombres or []
    listado_ids = listado_ids or []

    total_items = len(listado_nombres) + len(listado_ids)
    if total_items == 0:
        return pd.DataFrame()

    items_procesados = 0

    def update_progress():
        nonlocal items_procesados
        items_procesados += 1
        if progress_callback:
            progress_callback((items_procesados, total_items))

    for nombre in listado_nombres:
        taxon_id = obtener_id_por_nombre(nombre)
        if taxon_id:
            datos_especie = obtener_datos_proteccion(taxon_id, nombre)
            resultados_exitosos.append(datos_especie)
        else:
            resultados_fallidos.append({"Especie": nombre, "Error": "ID de taxón no encontrado"})
        update_progress()

    for taxon_id in listado_ids:
        nombre_cientifico = obtener_nombre_por_id(taxon_id)
        if nombre_cientifico:
            datos_especie = obtener_datos_proteccion(taxon_id, nombre_cientifico)
            resultados_exitosos.append(datos_especie)
        else:
            resultados_fallidos.append({"Especie": f"ID: {taxon_id}", "Error": "Nombre científico no encontrado"})
        update_progress()

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
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    background_callback_manager=background_callback_manager
)
server = app.server

# --- DEFINICIÓN DE LA BARRA LATERAL ---
sidebar = html.Div(
    [
        html.H2("EIDOScopio", className="display-5"),
        html.H5("🔎 Buscador de Especies", className="text-muted"),
        html.Hr(),
        html.P(
            "Una herramienta para consultar el estatus de protección de especies en la API del Inventario Español de Especies (EIDOS).",
            className="lead"
        ),
        html.Hr(),
        html.P("Creado por Aarón Quesada"),
        html.Div([
            html.A("LinkedIn", href="https://www.linkedin.com/in/aaronq/", target="_blank", className="ms-3"),
            html.A("GitHub", href="https://github.com/aaronque", target="_blank", className="ms-3"),
        ], className="d-flex justify-content-start"),
    ],
    style={
        "position": "fixed",
        "top": 0,
        "left": 0,
        "bottom": 0,
        "width": "22rem",
        "padding": "2rem 1rem",
        "background-color": "#f8f9fa",
    },
)

# --- DEFINICIÓN DEL CONTENIDO PRINCIPAL ---
content = html.Div(
    [
        dbc.Accordion([
            dbc.AccordionItem(
                [
                    html.P("- Para búsquedas por nombre científico: Introduce un nombre por cada línea."),
                    html.P("- Para búsquedas por ID de EIDOS: Escribe los números separados por espacios o saltos de línea."),
                    html.P("- Haz clic en 'Comenzar Búsqueda' para procesar los datos.")
                ],
                title="ℹ️ Ver instrucciones de uso",
            )
        ]),

        dbc.Button("Cargar datos de ejemplo", id="btn-ejemplo", color="secondary", className="mt-3 mb-3"),

        dbc.Row([
            dbc.Col(dcc.Textarea(id='area-nombres', placeholder="Achondrostoma arcasii\nSus scrofa...", style={'width': '100%', 'height': 200})),
            dbc.Col(dcc.Textarea(id='area-ids', placeholder="13431\n9322...", style={'width': '100%', 'height': 200})),
        ]),

        dbc.Button("🚀 Comenzar Búsqueda", id="btn-busqueda", color="primary", size="lg", className="mt-3 w-100"),

        html.Hr(),

        html.Div(
            [
                html.P("Procesando... por favor, espera."),
                dbc.Progress(id="progress-bar", value=0, striped=True, animated=True),
            ],
            id="progress-container",
            style={"display": "none"},
        ),

        html.Div(id='output-resultados'),
    ],
    style={
        "margin-left": "24rem",
        "margin-right": "2rem",
        "padding": "2rem 1rem",
    }
)


# --- LAYOUT DE LA APP ---
app.layout = html.Div(
    [
        dcc.Store(id='store-resultados'),
        dcc.Download(id='download-excel'),
        sidebar,
        content
    ]
)


# --- CALLBACKS PARA LA INTERACTIVIDAD ---

@app.callback(
    Output('area-nombres', 'value'),
    Output('area-ids', 'value'),
    Input('btn-ejemplo', 'n_clicks'),
    prevent_initial_call=True
)
def cargar_ejemplo(n_clicks):
    """Carga datos de ejemplo en las áreas de texto."""
    ejemplo_nombres = "Lynx pardinus\nUrsus arctos\nGamusinus alipendis"
    ejemplo_ids = "14389\n999999"
    return ejemplo_nombres, ejemplo_ids

@app.callback(
    Output('output-resultados', 'children'),
    Output('store-resultados', 'data'),
    Input('btn-busqueda', 'n_clicks'),
    State('area-nombres', 'value'),
    State('area-ids', 'value'),
    running=[
        (Output('btn-busqueda', 'disabled'), True, False),
        (Output('progress-container', 'style'), {'display': 'block'}, {'display': 'none'}),
        (Output('output-resultados', 'style'), {'display': 'none'}, {'display': 'block'}),
    ],
    progress=[
        Output('progress-bar', 'value'),
        Output('progress-bar', 'label'),
    ],
    background=True,
    prevent_initial_call=True
)
def ejecutar_busqueda(set_progress, n_clicks, nombres_texto, ids_texto):
    """Ejecuta la búsqueda en segundo plano y actualiza la barra de progreso."""
    nombres_texto = nombres_texto or ""
    ids_texto = ids_texto or ""

    if not nombres_texto and not ids_texto:
        return dbc.Alert("Por favor, introduce al menos un nombre o un ID para buscar.", color="warning"), no_update

    def progress_wrapper(progress_info):
        items_procesados, total = progress_info
        if total > 0:
            set_progress((items_procesados / total * 100, f"{items_procesados} / {total}"))

    lista_nombres = [line.strip() for line in nombres_texto.strip().split('\n') if line.strip()]
    lista_ids = [int(id_num) for id_num in re.split(r'\s+', ids_texto.strip()) if id_num.isdigit()]

    df_resultado = generar_tabla_completa(lista_nombres, lista_ids, progress_callback=progress_wrapper)

    if df_resultado.empty:
        return dbc.Alert("La búsqueda no produjo resultados.", color="info"), no_update

    total_consultados = len(lista_nombres) + len(lista_ids)
    encontrados = len(df_resultado[df_resultado['Error'] == '-'])
    columnas_proteccion = [col for col in df_resultado.columns if 'Catálogo' in col or 'Convenio' in col]
    df_resultado['protegido'] = df_resultado[columnas_proteccion].ne('-').any(axis=1)
    protegidos = len(df_resultado[df_resultado['protegido'] & (df_resultado['Error'] == '-')])

    layout_resultados = html.Div([
        html.H3("📊 Resumen de Resultados", className="mt-4"),
        dbc.Row([
            dbc.Col(dbc.Card([dbc.CardHeader("Consultados"), dbc.CardBody(html.H4(total_consultados, className="card-title"))])),
            dbc.Col(dbc.Card([dbc.CardHeader("Encontrados"), dbc.CardBody(html.H4(encontrados, className="card-title"))])),
            dbc.Col(dbc.Card([dbc.CardHeader("Con Protección"), dbc.CardBody(html.H4(protegidos, className="card-title"))])),
        ]),
        html.Hr(),
        dbc.Button("📥 Descargar Tabla como Excel", id="btn-descarga", color="success", className="mt-3 mb-3 w-100"),
        dash_table.DataTable(
            id='tabla-resultados',
            columns=[{"name": i, "id": i} for i in df_resultado.drop(columns=['protegido']).columns],
            data=df_resultado.to_dict('records'),
            style_table={'overflowX': 'auto'},
            page_size=10,
        )
    ])

    return layout_resultados, df_resultado.to_json(date_format='iso', orient='split')

@app.callback(
    Output('download-excel', 'data'),
    Input('btn-descarga', 'n_clicks'),
    State('store-resultados', 'data'),
    prevent_initial_call=True
)
def descargar_excel(n_clicks, json_data):
    """Prepara y envía el archivo Excel para su descarga."""
    if json_data is None:
        return no_update

    df = pd.read_json(io.StringIO(json_data), orient='split')
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.drop(columns=['protegido'], errors='ignore').to_excel(writer, index=False, sheet_name='ProteccionEspecies')
    output.seek(0)

    return dcc.send_bytes(output.getvalue(), "proteccion_especies.xlsx")

# --- Ejecución del Servidor ---
if __name__ == '__main__':
    app.run_server(debug=True)
