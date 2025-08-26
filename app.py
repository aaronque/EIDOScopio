# app.py (final): orden de columnas fijo, botón limpiar alineado y cancelación de búsqueda
# Despliegue: usar `gunicorn app:server`

import os
import io
import re
import time
import unicodedata
from threading import Lock
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import dash
from dash import dcc, html, dash_table, Input, Output, State, no_update
import dash_bootstrap_components as dbc
import pandas as pd
import requests
import diskcache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================
# Configuración general
# ============================
API_BASE_URL = "https://iepnb.gob.es/api/especie"

# Ruta de cache segura por defecto en PaaS
cache_dir = os.getenv("CACHE_DIR", "/tmp/eidos-cache")
os.makedirs(cache_dir, exist_ok=True)
cache = diskcache.Cache(cache_dir)
background_callback_manager = dash.DiskcacheManager(cache)

# Sesión HTTP robusta (reintentos y backoff)
_session = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_adapter = HTTPAdapter(max_retries=_retry)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)

# Throttle: máx. 4 req/s (configurable con EIDOS_RATE)
_RATE = float(os.getenv("EIDOS_RATE", "4"))
_MIN_INTERVAL = 1.0 / _RATE if _RATE > 0 else 0
_last_call = 0.0
_lock = Lock()

BASE_COLS = {"Especie", "Grupo taxonómico", "Nombre común", "Error", "protegido"}

# ============================
# Utilidades HTTP
# ============================
def _get_json(endpoint: str, params: dict):
    """GET con sesión, timeouts, retries y throttle básico."""
    global _last_call
    try:
        with _lock:
            now = time.time()
            wait = (_MIN_INTERVAL - (now - _last_call))
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
        r = _session.get(f"{API_BASE_URL}{endpoint}", params=params, timeout=(5, 15))
        r.raise_for_status()
        return r.json() or []
    except requests.exceptions.RequestException:
        return []

# ============================
# Funciones API documentadas
# ============================
def obtener_id_por_nombre(nombre_cientifico: str):
    """Busca el ID de taxón para un nombre científico (RPC)."""
    try:
        r = _session.get(
            f"{API_BASE_URL}/rpc/obtenertaxonespornombre",
            params={"_nombretaxon": nombre_cientifico},
            timeout=(5, 15),
        )
        r.raise_for_status()
        datos = r.json() or []
        if not datos:
            return None
        # Normalización flexible de 'Aceptado/válido'
        for registro in datos:
            nt = (registro.get("nametype") or "").strip().lower()
            if "aceptado" in nt or "valido" in nt or "válido" in nt:
                return registro.get("taxonid")
        # Fallback: primer registro
        return datos[0].get("taxonid") if datos else None
    except requests.exceptions.RequestException:
        return None

def obtener_nombre_por_id(taxon_id: int):
    """Nombre científico aceptado para un ID de taxón (RPC)."""
    try:
        r = _session.get(
            f"{API_BASE_URL}/rpc/obtenertaxonporid",
            params={"_idtaxon": taxon_id},
            timeout=(5, 15),
        )
        r.raise_for_status()
        datos = r.json() or []
        if datos and datos[0].get("name"):
            return datos[0]["name"]
        return None
    except requests.exceptions.RequestException:
        return None

def obtener_datos_proteccion(taxon_id: int, nombre_cientifico_base: str):
    """Estados legales vigentes agrupados por columna (usa sets, sin duplicados)."""
    protecciones = {"Especie": nombre_cientifico_base}
    estados_por_col = defaultdict(set)
    try:
        r = _session.get(
            f"{API_BASE_URL}/rpc/obtenerestadoslegalesportaxonid",
            params={"_idtaxon": taxon_id},
            timeout=(5, 15),
        )
        r.raise_for_status()
        datos_legales = r.json() or []
        for item in datos_legales:
            if item.get("idvigente") != 1:
                continue
            ambito = item.get("ambito")
            estado = item.get("estadolegal")
            if not estado:
                continue
            if ambito == "Nacional":
                columna = item.get("dataset", "Catálogo Nacional")
            elif ambito == "Autonómico" or ambito == "Regional":
                columna = f"Catálogo - {item.get('ccaa', 'Desconocida')}"
            elif ambito == "Internacional":
                columna = item.get("dataset", "Convenio Internacional")
            else:
                columna = item.get("dataset") or None
            if columna:
                estados_por_col[columna].add(estado)
        for col, estados in estados_por_col.items():
            protecciones[col] = ", ".join(sorted(estados)) if estados else "-"
        return protecciones
    except requests.exceptions.RequestException:
        protecciones["Error"] = "Fallo al obtener datos legales"
        return protecciones

# --- Grupo taxonómico y Nombre común (vistas con PostgREST) ---
def obtener_grupo_taxonomico_por_id(taxon_id: int):
    filas = _get_json("/v_taxonomia", {"taxonid": f"eq.{taxon_id}"})
    grupos = [f.get("taxonomicgroup") for f in filas if f.get("taxonomicgroup")]
    return grupos[0] if grupos else None

def obtener_nombre_comun_por_id(taxon_id: int):
    filas = _get_json("/v_nombrescomunes", {"idtaxon": f"eq.{taxon_id}"})
    if not filas:
        return None
    es_castellano = [f for f in filas if f.get("ididioma") == 1]
    pref_cast = [f for f in es_castellano if f.get("espreferente") is True]
    if pref_cast:
        return pref_cast[0].get("nombre_comun") or None
    if es_castellano:
        return es_castellano[0].get("nombre_comun") or None
    return filas[0].get("nombre_comun") or None

# ============================
# Orden estable de columnas
# ============================
def _normalize(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower().strip()

def ordenar_columnas_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    # 1) Fijas al inicio
    fixed = ["Grupo taxonómico", "Nombre común", "Especie"]
    fixed_present = [c for c in fixed if c in df.columns]

    # 2) Columnas legales dinámicas (excluye base + Error + protegido)
    base_exclude = set(BASE_COLS) - {"Error"}  # queremos 'Error' al final
    legales = [c for c in df.columns if c not in base_exclude and c != "Error"]

    # 3) Clasificación por ámbito (patrones en el nombre de columna)
    auton = [c for c in legales if c.startswith("Catálogo - ")]
    def es_nacional(c: str) -> bool:
        cl = _normalize(c)
        return cl == "catalogo nacional" or "nacional" in cl
    nacional = [c for c in legales if c not in auton and es_nacional(c)]
    internacional = [c for c in legales if c not in auton and c not in nacional]

    # 4) Prioridad dentro de Internacional (europeas/internacionales primero con patrón)
    patrones_intl = [
        ("directiva aves", 1),
        ("aves", 2),
        ("directiva habitat", 3),
        ("habitat", 4),
        ("habitats", 4),
        ("cites", 5),
        ("berna", 6),
        ("bonn", 7),
        ("cms", 7),
        ("aewa", 8),
    ]
    def intl_priority(name: str) -> int:
        n = _normalize(name)
        for p, pr in patrones_intl:
            if p in n:
                return pr
        return 100
    internacional_sorted = sorted(internacional, key=lambda x: (intl_priority(x), x))

    # 5) Nacional: "Catálogo Nacional" primero y luego alfabético
    def is_cat_nacional(name: str) -> bool:
        return _normalize(name) == "catalogo nacional"
    nacional_sorted = sorted(nacional, key=lambda x: (0 if is_cat_nacional(x) else 1, x))

    # 6) Autonómicas: orden fijo de CCAA, luego alfabético si no coincide
    ccaa_order = [
        "Andalucía","Aragón","Asturias","Illes Balears","Canarias","Cantabria",
        "Castilla-La Mancha","Castilla y León","Cataluña","Ceuta","Comunitat Valenciana",
        "Extremadura","Galicia","La Rioja","Comunidad de Madrid","Melilla",
        "Región de Murcia","Navarra","País Vasco"
    ]
    rank_ccaa = {f"Catálogo - {n}": i for i, n in enumerate(ccaa_order)}
    auton_sorted = sorted(auton, key=lambda x: (rank_ccaa.get(x, 999), x))

    # 7) Resto y Error
    ordered = fixed_present + internacional_sorted + nacional_sorted + auton_sorted
    # Añade cualquier otra columna no contemplada (p.ej., nuevas legales con nombres inesperados)
    leftover = [c for c in df.columns if c not in ordered and c not in {"protegido"}]
    if "Error" in leftover:
        leftover.remove("Error")
        ordered += leftover + ["Error"]
    else:
        ordered += leftover

    # Filtra a las existentes y reordena
    ordered = [c for c in ordered if c in df.columns]
    return df.reindex(columns=ordered)

# ============================
# Lógica de orquestación
# ============================
def _proc_nombre(nombre: str):
    taxon_id = obtener_id_por_nombre(nombre)
    if not taxon_id:
        return {"Especie": nombre, "Grupo taxonómico": "-", "Nombre común": "-", "Error": "ID de taxón no encontrado"}
    datos = obtener_datos_proteccion(taxon_id, nombre)
    datos["Grupo taxonómico"] = obtener_grupo_taxonomico_por_id(taxon_id) or "-"
    datos["Nombre común"] = obtener_nombre_comun_por_id(taxon_id) or "-"
    return datos

def _proc_id(taxon_id: int):
    nombre = obtener_nombre_por_id(taxon_id)
    if not nombre:
        return {"Especie": f"ID: {taxon_id}", "Grupo taxonómico": "-", "Nombre común": "-", "Error": "Nombre científico no encontrado"}
    datos = obtener_datos_proteccion(taxon_id, nombre)
    datos["Grupo taxonómico"] = obtener_grupo_taxonomico_por_id(taxon_id) or "-"
    datos["Nombre común"] = obtener_nombre_comun_por_id(taxon_id) or "-"
    return datos

def generar_tabla_completa(listado_nombres=None, listado_ids=None, progress_callback=None):
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

    # Paralelización moderada (4 workers)
    tareas = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        tareas += [ex.submit(_proc_nombre, n) for n in listado_nombres]
        tareas += [ex.submit(_proc_id, i) for i in listado_ids]
        for fut in as_completed(tareas):
            fila = fut.result()
            if fila.get("Error") and fila["Error"] != "-":
                resultados_fallidos.append(fila)
            else:
                resultados_exitosos.append(fila)
            update_progress()

    datos_para_tabla = resultados_exitosos + resultados_fallidos
    if not datos_para_tabla:
        return pd.DataFrame()

    df = pd.DataFrame(datos_para_tabla)
    df.fillna('-', inplace=True)

    # No reordenamos aquí; el orden final se aplica en el callback con `ordenar_columnas_df`.
    return df

# ============================
# App Dash
# ============================
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    background_callback_manager=background_callback_manager,
)
server = app.server

# --- Sidebar ---
sidebar = html.Div(
    [
        html.Div([
            html.H2("EIDOScopio", className="display-5"),
            html.H5("🔎 Buscador de Especies", className="text-muted"),
            html.Hr(),
            html.P(
                "Herramienta para explorar de forma masiva el estatus legal de la biodiversidad española a través de la API de EIDOS.",
                className="lead",
            ),
        ]),
        # Ícono GitHub anclado abajo
        html.A(
            html.Img(
                src="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png",
                style={"width": "28px", "height": "28px"}
            ),
            href="https://github.com/aaronque/EIDOScopio",
            target="_blank",
            title="Repositorio en GitHub",
            style={"marginTop": "auto", "alignSelf": "center"}
        ),
    ],
    style={
        "position": "fixed",
        "top": 0,
        "left": 0,
        "bottom": 0,
        "width": "22rem",
        "padding": "2rem 1rem",
        "background-color": "#f8f9fa",
        "display": "flex",
        "flex-direction": "column",
    },
)

# --- Content ---
content = html.Div(
    [
        dbc.Accordion([
            dbc.AccordionItem(
                [
                    html.P("- Por nombre científico: uno por línea o separados por comas/;"),
                    html.P("- Por ID de EIDOS: números separados por comas, punto y coma, espacios o saltos de línea. Se ignoran puntos de miles (14.389 → 14389)."),
                    html.P("- Pulsa 'Comenzar Búsqueda'."),
                ],
                title="ℹ️ Ver instrucciones de uso",
            )
        ]),

        # Botones alineados en ButtonGroup
        dbc.ButtonGroup(
            [
                dbc.Button("Cargar datos de ejemplo", id="btn-ejemplo", color="secondary"),
                dbc.Button("🧹 Limpiar datos", id="btn-limpiar", color="light"),
            ],
            className="mt-3 mb-3",
        ),

        dbc.Row([
            dbc.Col(dcc.Textarea(id='area-nombres', placeholder="Achondrostoma arcasii\nSus scrofa...", style={'width': '100%', 'height': 200})),
            dbc.Col(dcc.Textarea(id='area-ids', placeholder="13431, 9322; 14.389...", style={'width': '100%', 'height': 200})),
        ]),

        dbc.Button("🔎 Comenzar Búsqueda", id="btn-busqueda", color="primary", size="lg", className="mt-3 w-100"),

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

# --- Layout ---
app.layout = html.Div(
    [
        dcc.Store(id='store-resultados'),
        dcc.Store(id='run-flag', data=False),  # Flag de ejecución/cancelación
        dcc.Download(id='download-excel'),
        sidebar,
        content,
    ]
)

# ============================
# Callbacks
# ============================
# Unificado: cargar ejemplo o limpiar datos
@app.callback(
    Output('area-nombres', 'value'),
    Output('area-ids', 'value'),
    Input('btn-ejemplo', 'n_clicks'),
    Input('btn-limpiar', 'n_clicks'),
    prevent_initial_call=True,
)
def set_textareas(n_ejemplo, n_limpiar):
    ejemplo_nombres = "Lynx pardinus\nUrsus arctos\nGamusinus alipendis"
    ejemplo_ids = "14389\n999999"
    ctx = dash.callback_context
    triggered = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else None
    if triggered == 'btn-ejemplo':
        return ejemplo_nombres, ejemplo_ids
    if triggered == 'btn-limpiar':
        return "", ""
    return no_update, no_update

# Toggle de ejecución/cancelación con el mismo botón
@app.callback(
    Output('run-flag', 'data'),
    Input('btn-busqueda', 'n_clicks'),
    State('run-flag', 'data'),
    prevent_initial_call=True,
)
def toggle_run_flag(n_clicks, running_now):
    # False -> True (inicia); True -> False (cancela)
    return not bool(running_now)

# Long callback en segundo plano con cancelación y orden de columnas fijo
@app.callback(
    Output('output-resultados', 'children'),
    Output('store-resultados', 'data'),
    Input('run-flag', 'data'),  # dispara al pasar a True (iniciar) y a False (cancelar)
    State('area-nombres', 'value'),
    State('area-ids', 'value'),
    running=[
        # No deshabilitamos el botón para poder cancelarlo
        (Output('btn-busqueda', 'children'), "⏹️ Detener búsqueda", "🔎 Comenzar Búsqueda"),
        (Output('btn-busqueda', 'color'), "danger", "primary"),
        (Output('progress-container', 'style'), {'display': 'block'}, {'display': 'none'}),
        (Output('output-resultados', 'style'), {'display': 'none'}, {'display': 'block'}),
    ],
    progress=[
        Output('progress-bar', 'value'),
        Output('progress-bar', 'label'),
    ],
    cancel=[Input('run-flag', 'data')],  # al cambiar, cancela el trabajo en curso
    background=True,
    prevent_initial_call=True,
)
def ejecutar_busqueda(set_progress, run_flag, nombres_texto, ids_texto):
    # Si el flag está en False, esta invocación es un "disparo" de cancelación: no iniciar nada nuevo.
    if not run_flag:
        return no_update, no_update

    nombres_texto = (nombres_texto or "").strip()
    ids_texto = (ids_texto or "").strip()
    if not nombres_texto and not ids_texto:
        return dbc.Alert("Por favor, introduce al menos un nombre o un ID para buscar.", color="warning"), no_update

    def progress_wrapper(progress_info):
        items_procesados, total = progress_info
        if total > 0:
            set_progress((items_procesados / total * 100, f"{items_procesados} / {total}"))

    lista_nombres = [item.strip() for item in re.split(r'[\n,;]+', nombres_texto) if item.strip()]
    raw_tokens = [t for t in re.split(r'[\s,;]+', ids_texto) if t]
    def norm_id(tok: str):
        t = tok.replace('.', '').strip()
        return t if t.isdigit() else None
    ids_norm = [norm_id(t) for t in raw_tokens]
    lista_ids = [int(n) for n in ids_norm if n is not None]

    df_resultado = generar_tabla_completa(lista_nombres, lista_ids, progress_callback=progress_wrapper)
    if df_resultado.empty:
        return dbc.Alert("La búsqueda no produjo resultados.", color="info"), no_update

    # 'protegido' calculado a partir de todas las columnas legales
    columnas_legales = [c for c in df_resultado.columns if c not in BASE_COLS]
    df_resultado['protegido'] = df_resultado[columnas_legales].ne('-').any(axis=1) if columnas_legales else False

    # Resumen
    total_consultados = len(lista_nombres) + len(lista_ids)
    if 'Error' in df_resultado.columns:
        encontrados = int((df_resultado['Error'] == '-').sum())
        protegidos = int((df_resultado['protegido'] & (df_resultado['Error'] == '-')).sum())
    else:
        encontrados = total_consultados
        protegidos = int(df_resultado['protegido'].sum())

    # Ordenar columnas de forma estable e invariable
    df_ordenado = ordenar_columnas_df(df_resultado)

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
            columns=[{"name": i, "id": i} for i in df_ordenado.drop(columns=['protegido'], errors='ignore').columns],
            data=df_ordenado.to_dict('records'),
            style_table={'overflowX': 'auto'},
            page_action='native',
            page_size=10,
            filter_action='native',
            sort_action='native',
        ),
    ])

    return layout_resultados, df_ordenado.to_json(date_format='iso', orient='split')

@app.callback(
    Output('download-excel', 'data'),
    Input('btn-descarga', 'n_clicks'),
    State('store-resultados', 'data'),
    prevent_initial_call=True,
)
def descargar_excel(n_clicks, json_data):
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
    app.run_server(debug=os.getenv("DASH_DEBUG", "false").lower() == "true")
