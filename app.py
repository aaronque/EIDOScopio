# app.py
import dash
from dash import dcc, html, dash_table, Input, Output, State, no_update
import dash_bootstrap_components as dbc
import pandas as pd
import requests
import re
import io

# --- Lógica de Búsqueda ---
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
                    if estado not in protecciones[columna]:
                        protecciones[columna] += f", {estado}"
                else:
                    protecciones[columna] = estado
        return protecciones
    except requests.exceptions.RequestException:
        protecciones['Error'] = 'Fallo al obtener datos legales'
        return protecciones

# --- Inicialización de la App ---
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP])
server = app.server

app.layout = dbc.Container([
    dcc.Store(id='store-progreso'),
    dcc.Store(id='store-total'),
    dcc.Store(id='store-resultados-parciales'),
    dcc.Interval(id='intervalo-progreso', interval=500, disabled=True),

    html.H1("EIDOScopio: 🔎 Buscador del Estatus Legal de Especies", className="my-4 text-center"),

    dbc.Accordion([
        dbc.AccordionItem([
            html.P("- Para búsquedas por nombre científico: Introduce un nombre por cada línea."),
            html.P("- Para búsquedas por ID de EIDOS: Escribe los números separados por espacios o saltos de línea."),
            html.P("- Haz clic en 'Comenzar Búsqueda' para procesar los datos.")
        ], title="ℹ️ Ver instrucciones de uso"),
    ]),

    dbc.Row([
        dbc.Col(dcc.Textarea(id='area-nombres', placeholder="Achondrostoma arcasii\nSus scrofa...", style={'width': '100%', 'height': 200})),
        dbc.Col(dcc.Textarea(id='area-ids', placeholder="13431\n9322...", style={'width': '100%', 'height': 200})),
    ]),

    dbc.Button("🚀 Comenzar Búsqueda", id="btn-busqueda", color="primary", size="lg", className="mt-3 w-100"),

    html.Div(id='contenedor-barra-progreso', children=[
        dbc.Progress(id="barra-progreso", striped=True, animated=True, value=0, max=100, className="mt-4 mb-4")
    ], style={'display': 'none'}),

    html.Div(id='output-resultados'),

    html.Hr(),
    html.Div(
        dbc.Row([
            dbc.Col(html.P("Creado por Aarón Quesada"), className="text-center text-md-start"),
            dbc.Col(html.Div([
                html.A("LinkedIn", href="https://www.linkedin.com/in/aaronq/", target="_blank", className="ms-3"),
                html.A("GitHub", href="https://github.com/aaronque", target="_blank", className="ms-3"),
            ]), className="text-center text-md-end")
        ]),
        className="text-muted")
], fluid=True)

# --- Callbacks ---

@app.callback(
    Output('store-progreso', 'data'),
    Output('store-total', 'data'),
    Output('store-resultados-parciales', 'data'),
    Output('intervalo-progreso', 'disabled'),
    Output('contenedor-barra-progreso', 'style'),
    Input('btn-busqueda', 'n_clicks'),
    State('area-nombres', 'value'),
    State('area-ids', 'value'),
    prevent_initial_call=True
)
def iniciar_progreso(n_clicks, nombres_texto, ids_texto):
    lista_nombres = [line.strip() for line in nombres_texto.strip().split('\n') if line.strip()] if nombres_texto else []
    lista_ids = [int(id_) for id_ in re.split(r'\s+', ids_texto.strip()) if id_.isdigit()] if ids_texto else []

    progreso = {
        'nombres': lista_nombres,
        'ids': lista_ids,
        'hecho': 0
    }

    return progreso, len(lista_nombres) + len(lista_ids), [], False, {'display': 'block'}

@app.callback(
    Output('store-progreso', 'data'),
    Output('store-resultados-parciales', 'data'),
    Output('barra-progreso', 'value'),
    Output('intervalo-progreso', 'disabled'),
    Output('output-resultados', 'children'),
    Input('intervalo-progreso', 'n_intervals'),
    State('store-progreso', 'data'),
    State('store-total', 'data'),
    State('store-resultados-parciales', 'data'),
    prevent_initial_call=True
)
def avanzar_búsqueda(_, progreso, total, resultados):
    if progreso is None:
        return no_update, no_update, no_update, True, no_update

    if progreso['nombres']:
        nombre = progreso['nombres'].pop(0)
        taxon_id = obtener_id_por_nombre(nombre)
        if taxon_id:
            resultados.append(obtener_datos_proteccion(taxon_id, nombre))
        else:
            resultados.append({"Especie": nombre, "Error": "ID no encontrado"})
    elif progreso['ids']:
        taxon_id = progreso['ids'].pop(0)
        nombre = obtener_nombre_por_id(taxon_id)
        if nombre:
            resultados.append(obtener_datos_proteccion(taxon_id, nombre))
        else:
            resultados.append({"Especie": f"ID: {taxon_id}", "Error": "Nombre no encontrado"})

    progreso['hecho'] += 1
    porcentaje = int(100 * progreso['hecho'] / total)

    if not progreso['nombres'] and not progreso['ids']:
        df = pd.DataFrame(resultados)
        df.fillna('-', inplace=True)
        if 'Especie' in df.columns:
            cols = df.columns.tolist()
            cols.insert(0, cols.pop(cols.index('Especie')))
            if 'Error' in cols:
                cols.append(cols.pop(cols.index('Error')))
            df = df.reindex(columns=cols)

        tabla = dash_table.DataTable(
            id='tabla-resultados',
            columns=[{"name": i, "id": i} for i in df.columns],
            data=df.to_dict('records'),
            style_table={'overflowX': 'auto'},
            page_size=10,
        )

        return None, resultados, 100, True, html.Div([
            html.H3("🔍 Resultados de la Búsqueda"),
            tabla
        ])

    return progreso, resultados, porcentaje, False, no_update

# --- Run Server ---
if __name__ == '__main__':
    app.run_server(debug=True)
