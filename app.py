# app.py (Versión Final - Fuzzy Match + CSV Fix)
# Despliegue: usar `gunicorn app:server`

import os
import io
import re
import time
import csv
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

# Librería para Fuzzy Matching
from rapidfuzz import process, fuzz

# ============================
# Configuración general
# ============================
API_BASE_URL = "https://iepnb.gob.es/api/especie"

# Ruta de cache
cache_dir = os.getenv("CACHE_DIR", "/tmp/eidos-cache")
os.makedirs(cache_dir, exist_ok=True)
cache = diskcache.Cache(cache_dir)
background_callback_manager = dash.DiskcacheManager(cache)

# Sesión HTTP robusta
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

# Throttle
_RATE = float(os.getenv("EIDOS_RATE", "4"))
_MIN_INTERVAL = 1.0 / _RATE if _RATE > 0 else 0
_last_call = 0.0
_lock = Lock()

BASE_COLS = {"Especie", "Grupo taxonómico", "Nombre común", "Error", "protegido", "Notas"}

# ============================
# Utilidades HTTP y Fuzzy
# ============================

def _get_json(endpoint: str, params: dict):
    """GET para endpoints pequeños (info de especie)."""
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

@cache.memoize(expire=86400)
def obtener_lista_patron_optimizada():
    """
    Descarga la taxonomía completa en CSV pidiendo las columnas CORRECTAS.
    Basado en la documentación: columna 'name' y 'taxonid'.
    """
    try:
        endpoint = "/v_taxonomia"
        
        # 1. Pedimos formato CSV para máxima velocidad y menor memoria
        headers = {"Accept": "text/csv"}
        
        # 2. Corregimos parámetros según doc oficial 
        # select: 'taxonid' y 'name' (antes fallaba por pedir scientificname)
        params = {
            "select": "taxonid,name", 
            "limit": 250000 # Límite alto para asegurar que baja todo
        }
        
        print("📥 Iniciando descarga de Lista Patrón (CSV)...")
        t0 = time.time()
        
        # Timeout extendido para la descarga grande
        r = _session.get(
            f"{API_BASE_URL}{endpoint}", 
            params=params, 
            headers=headers,
            timeout=(15, 60)
        )
        
        if r.status_code != 200:
            print(f"Error API Checklist: {r.status_code}")
            return {}
        
        # 3. Procesamos CSV usando librería estándar (seguro contra comas en nombres)
        contenido = io.StringIO(r.text)
        reader = csv.DictReader(contenido)
        
        referencia = {}
        count = 0
        
        for row in reader:
            # DictReader usa los nombres de cabecera reales devueltos por la API
            # La API devuelve keys como 'taxonid' y 'name'
            tid = row.get('taxonid')
            name = row.get('name')
            
            if tid and name:
                referencia[name] = int(tid)
                count += 1
                    
        print(f"✅ Lista patrón procesada en {time.time()-t0:.2f}s: {count} especies.")
        return referencia

    except Exception as e:
        print(f"Excepción descargando lista patrón: {e}")
        return {}

def intento_fuzzy_match(nombre_buscado: str, lista_referencia: dict, umbral=85):
    """
    Busca el nombre más parecido en la lista de referencia.
    CAMBIO: Usamos fuzz.ratio en lugar de WRatio para evitar falsos positivos
    por coincidencias parciales (ej. coincidir solo en el epíteto 'pyrenaica').
    """
    if not lista_referencia:
        return None
    
    # Usamos fuzz.ratio para forzar similitud en la cadena completa
    resultado = process.extractOne(
        nombre_buscado, 
        lista_referencia.keys(), 
        scorer=fuzz.ratio 
    )
    
    if resultado:
        match_name, score, _ = resultado
        if score >= umbral:
            return lista_referencia[match_name], match_name, score
    return None

# ============================
# Funciones API Principales
# ============================
def obtener_id_por_nombre(nombre_cientifico: str):
    """Busca el ID exacto."""
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
        for registro in datos:
            nt = (registro.get("nametype") or "").strip().lower()
            if "aceptado" in nt or "valido" in nt or "válido" in nt:
                return registro.get("taxonid")
        return datos[0].get("taxonid") if datos else None
    except requests.exceptions.RequestException:
        return None

def obtener_nombre_por_id(taxon_id: int):
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
    
    # 1. Fijas al inicio (Incluye 'Notas')
    fixed = ["Especie", "Grupo taxonómico", "Nombre común", "Notas"]
    fixed_present = [c for c in fixed if c in df.columns]

    base_exclude = set(BASE_COLS) - {"Error"}
    legales = [c for c in df.columns if c not in base_exclude and c != "Error"]

    auton = [c for c in legales if c.startswith("Catálogo - ")]
    def es_nacional(c: str) -> bool:
        cl = _normalize(c)
        return cl == "catalogo nacional" or "nacional" in cl
    nacional = [c for c in legales if c not in auton and es_nacional(c)]
    internacional = [c for c in legales if c not in auton and c not in nacional]

    patrones_intl = [("directiva aves", 1), ("aves", 2), ("directiva habitat", 3), ("habitat", 4), ("habitats", 4), ("cites", 5), ("berna", 6), ("bonn", 7), ("cms", 7), ("aewa", 8)]
    def intl_priority(name: str) -> int:
        n = _normalize(name)
        for p, pr in patrones_intl:
            if p in n: return pr
        return 100
    internacional_sorted = sorted(internacional, key=lambda x: (intl_priority(x), x))

    def is_cat_nacional(name: str) -> bool: return _normalize(name) == "catalogo nacional"
    nacional_sorted = sorted(nacional, key=lambda x: (0 if is_cat_nacional(x) else 1, x))

    ccaa_order = ["Andalucía","Aragón","Asturias","Illes Balears","Canarias","Cantabria","Castilla-La Mancha","Castilla y León","Cataluña","Ceuta","Comunitat Valenciana","Extremadura","Galicia","La Rioja","Comunidad de Madrid","Melilla","Región de Murcia","Navarra","País Vasco"]
    rank_ccaa = {f"Catálogo - {n}": i for i, n in enumerate(ccaa_order)}
    auton_sorted = sorted(auton, key=lambda x: (rank_ccaa.get(x, 999), x))

    ordered = fixed_present + internacional_sorted + nacional_sorted + auton_sorted
    leftover = [c for c in df.columns if c not in ordered and c not in {"protegido"}]
    
    if "Error" in leftover:
        leftover.remove("Error")
        ordered += leftover + ["Error"]
    else:
        ordered += leftover

    ordered = [c for c in ordered if c in df.columns]
    return df.reindex(columns=ordered)

# ============================
# Lógica de orquestación
# ============================
def _proc_nombre(nombre: str):
    """
    1. Exacto -> 2. Fuzzy (si exacto falla) -> 3. Datos
    """
    nombre_limpio = nombre.strip()
    # 1. Intento exacto
    taxon_id = obtener_id_por_nombre(nombre_limpio)
    nota_fuzzy = "-"

    # 2. Intento Fuzzy si falla el exacto
    if not taxon_id:
        lista_ref = obtener_lista_patron_optimizada()
        if lista_ref:
            match = intento_fuzzy_match(nombre_limpio, lista_ref, umbral=85)
            if match:
                taxon_id, nombre_encontrado, score = match
                nota_fuzzy = f"Corregido autom. (similitud {score:.0f}%): '{nombre_limpio}' -> '{nombre_encontrado}'"
                nombre_limpio = nombre_encontrado 

    if not taxon_id:
        return {"Especie": nombre, "Grupo taxonómico": "-", "Nombre común": "-", "Error": "Taxón no encontrado", "Notas": "-"}

    # 3. Datos finales
    datos = obtener_datos_proteccion(taxon_id, nombre_limpio)
    datos["Grupo taxonómico"] = obtener_grupo_taxonomico_por_id(taxon_id) or "-"
    datos["Nombre común"] = obtener_nombre_comun_por_id(taxon_id) or "-"
    datos["Notas"] = nota_fuzzy
    return datos

def _proc_id(taxon_id: int):
    nombre = obtener_nombre_por_id(taxon_id)
    if not nombre:
        return {"Especie": f"ID: {taxon_id}", "Grupo taxonómico": "-", "Nombre común": "-", "Error": "ID no encontrado", "Notas": "-"}
    datos = obtener_datos_proteccion(taxon_id, nombre)
    datos["Grupo taxonómico"] = obtener_grupo_taxonomico_por_id(taxon_id) or "-"
    datos["Nombre común"] = obtener_nombre_comun_por_id(taxon_id) or "-"
    datos["Notas"] = "-"
    return datos

def generar_tabla_completa(listado_nombres=None, listado_ids=None, progress_callback=None):
    resultados_exitosos = []
    resultados_fallidos = []
    listado_nombres = listado_nombres or []
    listado_ids = listado_ids or []
    
    # Precarga lista patrón (versión optimizada) si hay nombres
    if listado_nombres:
        obtener_lista_patron_optimizada()

    total_items = len(listado_nombres) + len(listado_ids)
    if total_items == 0:
        return pd.DataFrame()

    items_procesados = 0

    def update_progress():
        nonlocal items_procesados
        items_procesados += 1
        if progress_callback:
            progress_callback((items_procesados, total_items))

    with ThreadPoolExecutor(max_workers=4) as ex:
        tareas = []
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

    # --- FIX: Asegurar columnas obligatorias ---
    cols_obligatorias = ["Error", "Notas", "Especie", "Grupo taxonómico", "Nombre común"]
    for col in cols_obligatorias:
        if col not in df.columns:
            df[col] = "-"
    
    df.fillna('-', inplace=True)
    return df

# ============================
# App Dash
# ============================
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP], background_callback_manager=background_callback_manager)
server = app.server

sidebar = html.Div(
    [
        html.Div([
            html.H2("EIDOScopio", className="display-5"),
            html.H5("🔎 Buscador de Especies", className="text-muted"),
            html.Hr(),
            html.P("Explora el estatus legal de la biodiversidad española.", className="lead"),
            dbc.Badge("Fuzzy Match Activo", color="info", className="mb-2"),
        ]),
        html.A(html.Img(src="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png", style={"width": "28px", "height": "28px"}), href="https://github.com/aaronque/EIDOScopio", target="_blank", style={"marginTop": "auto", "alignSelf": "center"}),
    ],
    style={"position": "fixed", "top": 0, "left": 0, "bottom": 0, "width": "22rem", "padding": "2rem 1rem", "background-color": "#f8f9fa", "display": "flex", "flex-direction": "column"},
)

content = html.Div(
    [
        dbc.Accordion([
            dbc.AccordionItem(
                [
                    html.P("- Por nombre científico: uno por línea."),
                    html.P("- Por ID de EIDOS: números separados."),
                    html.P("- **Autocorrección:** Si escribes mal un nombre (ej. 'Vorderea'), el sistema buscará el correcto."),
                ],
                title="ℹ️ Ver instrucciones de uso",
            )
        ]),
        dbc.ButtonGroup([dbc.Button("Cargar datos de ejemplo", id="btn-ejemplo", color="secondary"), dbc.Button("🧹 Limpiar datos", id="btn-limpiar", color="light")], className="mt-3 mb-3"),
        dbc.Row([
            dbc.Col(dcc.Textarea(id='area-nombres', placeholder="Achondrostoma arcasii\nVorderea pyrenaica (error)...", style={'width': '100%', 'height': 200})),
            dbc.Col(dcc.Textarea(id='area-ids', placeholder="13431, 9322...", style={'width': '100%', 'height': 200})),
        ]),
        dbc.Button("🔎 Comenzar Búsqueda", id="btn-busqueda", color="primary", size="lg", className="mt-3 w-100"),
        html.Hr(),
        html.Div([html.P("Procesando..."), dbc.Progress(id="progress-bar", value=0, striped=True, animated=True)], id="progress-container", style={"display": "none"}),
        html.Div(id='output-resultados'),
    ],
    style={"margin-left": "24rem", "margin-right": "2rem", "padding": "2rem 1rem"}
)

app.layout = html.Div([dcc.Store(id='store-resultados'), dcc.Store(id='run-flag', data=False), dcc.Download(id='download-excel'), sidebar, content])

# ============================
# Callbacks
# ============================
@app.callback(
    Output('area-nombres', 'value'),
    Output('area-ids', 'value'),
    Input('btn-ejemplo', 'n_clicks'),
    Input('btn-limpiar', 'n_clicks'),
    prevent_initial_call=True,
)
def set_textareas(n_ejemplo, n_limpiar):
    ejemplo_nombres = "Lynx pardinus\nUrsus arctos\nVorderea pyrenaica"
    ejemplo_ids = "14389"
    ctx = dash.callback_context
    triggered = ctx.triggered[0]['prop_id'].split('.')[0] if ctx.triggered else None
    if triggered == 'btn-ejemplo':
        return ejemplo_nombres, ejemplo_ids
    if triggered == 'btn-limpiar':
        return "", ""
    return no_update, no_update

@app.callback(
    Output('run-flag', 'data'),
    Input('btn-busqueda', 'n_clicks'),
    State('run-flag', 'data'),
    prevent_initial_call=True,
)
def toggle_run_flag(n_clicks, running_now):
    return not bool(running_now)

@app.callback(
    Output('output-resultados', 'children'),
    Output('store-resultados', 'data'),
    Input('run-flag', 'data'),
    State('area-nombres', 'value'),
    State('area-ids', 'value'),
    running=[
        (Output('btn-busqueda', 'children'), "⏹️ Detener búsqueda", "🔎 Comenzar Búsqueda"),
        (Output('btn-busqueda', 'color'), "danger", "primary"),
        (Output('progress-container', 'style'), {'display': 'block'}, {'display': 'none'}),
        (Output('output-resultados', 'style'), {'display': 'none'}, {'display': 'block'}),
    ],
    progress=[Output('progress-bar', 'value'), Output('progress-bar', 'label')],
    cancel=[Input('run-flag', 'data')],
    background=True,
    prevent_initial_call=True,
)
def ejecutar_busqueda(set_progress, run_flag, nombres_texto, ids_texto):
    if not run_flag: return no_update, no_update
    
    nombres_texto = (nombres_texto or "").strip()
    ids_texto = (ids_texto or "").strip()
    
    def progress_wrapper(progress_info):
        items_procesados, total = progress_info
        if total > 0: set_progress((items_procesados / total * 100, f"{items_procesados} / {total}"))

    lista_nombres = [item.strip() for item in re.split(r'[\n,;]+', nombres_texto) if item.strip()]
    raw_tokens = [t for t in re.split(r'[\s,;]+', ids_texto) if t]
    ids_norm = [t.replace('.', '').strip() for t in raw_tokens if t.replace('.', '').strip().isdigit()]
    lista_ids = [int(n) for n in ids_norm]

    if not lista_nombres and not lista_ids:
        return dbc.Alert("Introduce datos para buscar.", color="warning"), no_update

    df_resultado = generar_tabla_completa(lista_nombres, lista_ids, progress_callback=progress_wrapper)
    
    if df_resultado.empty:
        return dbc.Alert("Sin resultados.", color="info"), no_update

    columnas_legales = [c for c in df_resultado.columns if c not in BASE_COLS]
    df_resultado['protegido'] = df_resultado[columnas_legales].ne('-').any(axis=1) if columnas_legales else False

    total = len(lista_nombres) + len(lista_ids)
    encontrados = int((df_resultado['Error'] == '-').sum())
    corregidos = int(df_resultado['Notas'].str.contains("Corregido", na=False).sum())
    
    df_ordenado = ordenar_columnas_df(df_resultado)

    layout = html.Div([
        html.H3("📊 Resumen de Resultados", className="mt-4"),
        dbc.Row([
            dbc.Col(dbc.Card([dbc.CardHeader("Consultados"), dbc.CardBody(html.H4(total))])),
            dbc.Col(dbc.Card([dbc.CardHeader("Encontrados"), dbc.CardBody(html.H4(encontrados))])),
            dbc.Col(dbc.Card([dbc.CardHeader("Corregidos (Fuzzy)"), dbc.CardBody(html.H4(corregidos, className="text-info"))])),
        ]),
        html.Hr(),
        dbc.Button("📥 Descargar Tabla", id="btn-descarga", color="success", className="mb-3 w-100"),
        dash_table.DataTable(
            data=df_ordenado.to_dict('records'),
            columns=[{"name": i, "id": i} for i in df_ordenado.drop(columns=['protegido'], errors='ignore').columns],
            style_table={'overflowX': 'auto'},
            page_size=10,
            sort_action='native',
            filter_action='native',
            style_data_conditional=[
                {
                    'if': {'filter_query': '{Notas} contains "Corregido"'},
                    'backgroundColor': '#e3f2fd', 
                },
                {
                    'if': {'filter_query': '{Error} != "-"'},
                    'backgroundColor': '#ffebee',
                }
            ]
        ),
    ])
    return layout, df_ordenado.to_json(date_format='iso', orient='split')

@app.callback(
    Output('download-excel', 'data'),
    Input('btn-descarga', 'n_clicks'),
    State('store-resultados', 'data'),
    prevent_initial_call=True,
)
def descargar_excel(n_clicks, json_data):
    if not json_data: return no_update
    df = pd.read_json(io.StringIO(json_data), orient='split')
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.drop(columns=['protegido'], errors='ignore').to_excel(writer, index=False, sheet_name='Resultados')
    output.seek(0)
    return dcc.send_bytes(output.getvalue(), "resultados_eidos.xlsx")

if __name__ == '__main__':
    app.run_server(debug=os.getenv("DASH_DEBUG", "false").lower() == "true")