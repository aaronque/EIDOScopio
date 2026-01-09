# app.py
# Despliegue: usar `gunicorn app:server --timeout 120`

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
from dash import dcc, html, dash_table, Input, Output, State, no_update, callback_context
import dash_bootstrap_components as dbc
import pandas as pd
import requests
import diskcache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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

# Sesión HTTP
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
# Utilidades
# ============================

def _get_json(endpoint: str, params: dict):
    global _last_call
    try:
        with _lock:
            now = time.time()
            wait = (_MIN_INTERVAL - (now - _last_call))
            if wait > 0: time.sleep(wait)
            _last_call = time.time()
        r = _session.get(f"{API_BASE_URL}{endpoint}", params=params, timeout=(5, 15))
        r.raise_for_status()
        return r.json() or []
    except requests.exceptions.RequestException:
        return []

@cache.memoize(expire=86400)
def obtener_lista_patron_optimizada():
    try:
        endpoint = "/v_taxonomia"
        headers = {"Accept": "text/csv"}
        params = {"select": "taxonid,name", "limit": 250000}
        
        # print("📥 Iniciando descarga de Lista Patrón (CSV)...")
        r = _session.get(f"{API_BASE_URL}{endpoint}", params=params, headers=headers, timeout=(15, 60))
        
        if r.status_code != 200: return {}
        
        contenido = io.StringIO(r.text)
        reader = csv.DictReader(contenido)
        referencia = {}
        for row in reader:
            tid = row.get('taxonid')
            name = row.get('name')
            if tid and name: referencia[name] = int(tid)
        return referencia
    except Exception as e:
        print(f"Excepción: {e}")
        return {}

def intento_fuzzy_match(nombre_buscado: str, lista_referencia: dict, umbral=85):
    if not lista_referencia: return None
    match = process.extractOne(nombre_buscado, lista_referencia.keys(), scorer=fuzz.partial_ratio)
    if not match: return None

    match_name, score, _ = match
    if score < umbral: return None

    if len(nombre_buscado) > len(match_name):
        if fuzz.ratio(nombre_buscado, match_name) < umbral: return None
    else:
        recorte = match_name[:len(nombre_buscado)]
        if fuzz.ratio(nombre_buscado, recorte) < umbral: return None 

    return lista_referencia[match_name], match_name, score

# ============================
# Funciones API
# ============================
def obtener_id_por_nombre(nombre_cientifico: str):
    try:
        r = _session.get(f"{API_BASE_URL}/rpc/obtenertaxonespornombre", params={"_nombretaxon": nombre_cientifico}, timeout=(5, 15))
        r.raise_for_status()
        datos = r.json() or []
        if not datos: return None
        for registro in datos:
            nt = (registro.get("nametype") or "").strip().lower()
            if "aceptado" in nt or "valido" in nt: return registro.get("taxonid")
        return datos[0].get("taxonid") if datos else None
    except: return None

def obtener_nombre_por_id(taxon_id: int):
    try:
        r = _session.get(f"{API_BASE_URL}/rpc/obtenertaxonporid", params={"_idtaxon": taxon_id}, timeout=(5, 15))
        r.raise_for_status()
        d = r.json() or []
        return d[0]["name"] if d and d[0].get("name") else None
    except: return None

def obtener_datos_conservacion(taxon_id: int):
    datos_cons = {}
    try:
        r = _session.get(f"{API_BASE_URL}/rpc/obtenerestadosconservacionportaxonid", params={"_idtaxon": taxon_id}, timeout=(5, 15))
        r.raise_for_status()
        lista = r.json() or []
        por_ambito = defaultdict(list)
        for item in lista:
            ambito = item.get("ambito") or item.get("aplicaa") or "Desconocido"
            categoria = item.get("categoriaconservacion")
            anio = item.get("anio")
            if categoria:
                texto = f"{categoria}"
                if anio: texto += f" ({anio})"
                por_ambito[ambito].append(texto)
        for amb, cats in por_ambito.items():
            datos_cons[f"Libro Rojo - {amb}"] = "; ".join(sorted(set(cats)))
    except: pass
    return datos_cons

def obtener_datos_proteccion(taxon_id: int):
    protecciones = {}
    estados_por_col = defaultdict(set)
    try:
        r = _session.get(f"{API_BASE_URL}/rpc/obtenerestadoslegalesportaxonid", params={"_idtaxon": taxon_id}, timeout=(5, 15))
        r.raise_for_status()
        datos = r.json() or []
        for item in datos:
            if item.get("idvigente") != 1: continue
            ambito = item.get("ambito")
            estado = item.get("estadolegal")
            if not estado: continue
            
            if ambito == "Nacional": col = item.get("dataset", "Catálogo Nacional")
            elif ambito in ["Autonómico", "Regional"]: col = f"Catálogo - {item.get('ccaa', 'Desconocida')}"
            elif ambito == "Internacional": col = item.get("dataset", "Convenio Internacional")
            else: col = item.get("dataset") or "Otras Normas"
            
            if col: estados_por_col[col].add(estado)
        for col, ests in estados_por_col.items(): protecciones[col] = ", ".join(sorted(ests))
    except: protecciones["Error"] = "Fallo API Legal"
    return protecciones

def obtener_info_taxonomica(taxon_id: int):
    info = {"Grupo taxonómico": "-", "Nombre común": "-"}
    try:
        f_tax = _get_json("/v_taxonomia", {"taxonid": f"eq.{taxon_id}"})
        if f_tax: info["Grupo taxonómico"] = f_tax[0].get("taxonomicgroup", "-")
        f_nom = _get_json("/v_nombrescomunes", {"idtaxon": f"eq.{taxon_id}"})
        if f_nom:
            es = [f for f in f_nom if f.get("ididioma") == 1]
            pref = [f for f in es if f.get("espreferente") is True]
            nom = pref[0].get("nombre_comun") if pref else (es[0].get("nombre_comun") if es else f_nom[0].get("nombre_comun"))
            info["Nombre común"] = nom
    except: pass
    return info

def ordenar_columnas_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty: return df.copy()
    fixed = ["Especie", "Grupo taxonómico", "Nombre común", "Notas"]
    fixed_present = [c for c in fixed if c in df.columns]

    conservacion = [c for c in df.columns if "Libro Rojo" in c]
    def orden_cons(c): return 1 if "Mundial" in c else (2 if "España" in c else 3)
    conservacion_sorted = sorted(conservacion, key=orden_cons)

    base_exclude = set(BASE_COLS) | set(conservacion)
    legales = [c for c in df.columns if c not in base_exclude and c != "Error"]

    auton = [c for c in legales if c.startswith("Catálogo - ")]
    nacional = [c for c in legales if c not in auton and ("nacional" in _normalize(c))]
    internacional = [c for c in legales if c not in auton and c not in nacional]

    patrones_intl = [("directiva aves", 1), ("habitat", 2), ("cites", 3), ("berna", 4), ("bonn", 5)]
    def intl_prio(n):
        for p, i in patrones_intl: 
            if p in _normalize(n): return i
        return 99
    internacional_sorted = sorted(internacional, key=lambda x: (intl_prio(x), x))
    
    ccaa_order = ["Andalucía","Aragón","Asturias","Illes Balears","Canarias","Cantabria","Castilla-La Mancha","Castilla y León","Cataluña","Ceuta","Comunitat Valenciana","Extremadura","Galicia","La Rioja","Comunidad de Madrid","Melilla","Región de Murcia","Navarra","País Vasco"]
    rank_ccaa = {f"Catálogo - {n}": i for i, n in enumerate(ccaa_order)}
    auton_sorted = sorted(auton, key=lambda x: (rank_ccaa.get(x, 999), x))

    ordered = fixed_present + conservacion_sorted + internacional_sorted + nacional + auton_sorted
    leftover = [c for c in df.columns if c not in ordered and c not in {"protegido", "Error"}]
    final_cols = ordered + leftover
    if "Error" in df.columns: final_cols.append("Error")
    return df.reindex(columns=final_cols)

def _normalize(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii").lower()

def _proc_nombre(nombre: str):
    nombre_limpio = nombre.strip()
    taxon_id = obtener_id_por_nombre(nombre_limpio)
    nota_fuzzy = "-"

    if not taxon_id:
        lista_ref = obtener_lista_patron_optimizada()
        if lista_ref:
            match = intento_fuzzy_match(nombre_limpio, lista_ref, umbral=85)
            if match:
                taxon_id, nombre_match, score = match
                nota_fuzzy = f"Corregido (similitud {score:.0f}%): '{nombre_limpio}' -> '{nombre_match}'"
                nombre_limpio = nombre_match 

    base = {"Especie": nombre_limpio, "Notas": nota_fuzzy}
    if not taxon_id:
        base.update({"Error": "Taxón no encontrado", "Grupo taxonómico": "-", "Nombre común": "-"})
        return base

    tax = obtener_info_taxonomica(taxon_id)
    leg = obtener_datos_proteccion(taxon_id)
    cons = obtener_datos_conservacion(taxon_id)
    return {**base, **tax, **leg, **cons}

def _proc_id(taxon_id: int):
    nombre = obtener_nombre_por_id(taxon_id)
    if not nombre: return {"Especie": f"ID: {taxon_id}", "Error": "ID desconocido", "Notas": "-", "Grupo taxonómico": "-", "Nombre común": "-"}
    base = {"Especie": nombre, "Notas": "-"}
    tax = obtener_info_taxonomica(taxon_id)
    leg = obtener_datos_proteccion(taxon_id)
    cons = obtener_datos_conservacion(taxon_id)
    return {**base, **tax, **leg, **cons}

def generar_tabla_completa(nombres=None, ids=None, progress_callback=None):
    exitosos, fallidos = [], []
    nombres, ids = nombres or [], ids or []
    if nombres: obtener_lista_patron_optimizada()
    total = len(nombres) + len(ids)
    if total == 0: return pd.DataFrame()
    count = 0
    def update():
        nonlocal count
        count += 1
        if progress_callback: progress_callback((count, total))

    with ThreadPoolExecutor(max_workers=4) as ex:
        tareas = [ex.submit(_proc_nombre, n) for n in nombres] + [ex.submit(_proc_id, i) for i in ids]
        for fut in as_completed(tareas):
            res = fut.result()
            (fallidos if res.get("Error") and res.get("Error") != "-" else exitosos).append(res)
            update()

    df = pd.DataFrame(exitosos + fallidos)
    for c in ["Error", "Notas", "Especie", "Grupo taxonómico", "Nombre común"]:
        if c not in df.columns: df[c] = "-"
    df.fillna('-', inplace=True)
    return df

# ============================
# App Dash (UI RESTAURADA)
# ============================
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP], background_callback_manager=background_callback_manager)
server = app.server

sidebar = html.Div([
    html.Div([
        html.Img(
            src="/assets/buho.png",  
            style={"width": "120px", "display": "block", "margin": "0 auto 1rem auto"}
            ),
        html.H2("EIDOScopio", className="display-5"),
        html.H5("🔎 Buscador de Especies", className="text-muted"),
        html.Hr(),
        html.P("Herramienta para explorar de forma masiva el estatus legal de la biodiversidad española a través de la API de EIDOS (IEPNB).", className="lead"),
        
    ]),
    html.A(html.Img(src="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png", style={"width": "28px", "height": "28px"}), href="https://github.com/aaronque/EIDOScopio", target="_blank", title="Repositorio en GitHub", style={"marginTop": "auto", "alignSelf": "center"}),
], style={"position": "fixed", "top": 0, "left": 0, "bottom": 0, "width": "22rem", "padding": "2rem 1rem", "background-color": "#f8f9fa", "display": "flex", "flex-direction": "column"})

content = html.Div([
    dbc.Alert("💡 Novedad: Ahora la tabla incluye columnas 'Libro Rojo' con el estado de conservación (UICN).", color="info", dismissable=True),
    
    # ACORDEON RECUPERADO
    dbc.Accordion([
        dbc.AccordionItem([
            html.P("- Por nombre científico: uno por línea o separados por comas."),
            html.P("- Por ID de EIDOS: números separados por comas, punto y coma, espacios o saltos de línea."),
            html.P("- Pulsa 'Comenzar Búsqueda'."),
        ], title="ℹ️ Ver instrucciones de uso")
    ]),

    # BOTONES DE AYUDA RECUPERADOS
    dbc.ButtonGroup([
        dbc.Button("Cargar datos de ejemplo", id="btn-ejemplo", color="secondary"),
        dbc.Button("🧹 Limpiar datos", id="btn-limpiar", color="light"),
    ], className="mt-3 mb-3"),

    dbc.Row([
        dbc.Col(dcc.Textarea(id='area-nombres', placeholder="Lynx pardinus\nUrsus arctos\nVorderea pyrenaica...", style={'width': '100%', 'height': 200})),
        dbc.Col(dcc.Textarea(id='area-ids', placeholder="14389\n999999", style={'width': '100%', 'height': 200})),
    ]),

    # SISTEMA DUAL DE BOTONES (Swap Search/Stop)
    html.Div([
        dbc.Button("🔎 Comenzar Búsqueda", id="btn-busqueda", color="primary", size="lg", className="mt-3 w-100"),
        dbc.Button("🛑 Detener Búsqueda", id="btn-cancelar", color="danger", size="lg", className="mt-3 w-100", style={"display": "none"}),
    ]),

    html.Div([html.P("Procesando..."), dbc.Progress(id="progress-bar", value=0, striped=True, animated=True)], id="progress-container", style={"display": "none", "marginTop": "1rem"}),
    html.Div(id='output-resultados', style={"marginTop": "1rem"}),
], style={"margin-left": "24rem", "margin-right": "2rem", "padding": "2rem 1rem"})

app.layout = html.Div([dcc.Store(id='store-res'), dcc.Download(id='dl-excel'), sidebar, content])

# ----------------------------------------------------
# CALLBACKS DE AYUDA (Limpiar / Ejemplo)
# ----------------------------------------------------
@app.callback(
    [Output("area-nombres", "value"), Output("area-ids", "value")],
    [Input("btn-ejemplo", "n_clicks"), Input("btn-limpiar", "n_clicks")],
    prevent_initial_call=True
)
def update_inputs(btn_ej, btn_cl):
    ctx = callback_context
    if not ctx.triggered: return no_update, no_update
    btn_id = ctx.triggered[0]['prop_id'].split('.')[0]
    
    if btn_id == "btn-ejemplo":
        return "Lynx pardinus\nUrsus arctos\nVorderea pyrenaica", "14389"
    elif btn_id == "btn-limpiar":
        return "", ""
    return no_update, no_update

# ----------------------------------------------------
# CALLBACK PRINCIPAL (Con Botón Cancelar Real)
# ----------------------------------------------------
@app.callback(
    Output('output-resultados', 'children'), Output('store-res', 'data'),
    Input('btn-busqueda', 'n_clicks'),
    State('area-nombres', 'value'), State('area-ids', 'value'),
    background=True, 
    prevent_initial_call=True,
    # ESTA ES LA CLAVE: Swap de botones y Cancelación
    running=[
        (Output("btn-busqueda", "style"), {"display": "none"}, {"display": "block"}),
        (Output("btn-cancelar", "style"), {"display": "block"}, {"display": "none"}),
        (Output("progress-container", "style"), {'display': 'block', "marginTop": "1rem"}, {'display': 'none'})
    ],
    cancel=[Input("btn-cancelar", "n_clicks")],
    progress=[Output('progress-bar', 'value'), Output('progress-bar', 'label')],
)
def search(set_prog, n_clicks, txt_n, txt_i):
    ln = [x.strip() for x in re.split(r'[\n,;]+', txt_n or "") if x.strip()]
    li = [int(x.replace('.','')) for x in re.split(r'[\s,;]+', txt_i or "") if x.replace('.','').isdigit()]
    
    if not ln and not li: return dbc.Alert("Introduce datos.", color="warning"), no_update

    df = generar_tabla_completa(ln, li, lambda p: set_prog((p[0]/p[1]*100, f"{p[0]}/{p[1]}")))
    if df.empty: return dbc.Alert("Sin resultados.", color="secondary"), no_update

    df = ordenar_columnas_df(df)
    
    cond_styles = [
        {'if': {'filter_query': '{Notas} contains "Corregido"'}, 'backgroundColor': '#e3f2fd'},
        {'if': {'filter_query': '{Error} != "-"'}, 'backgroundColor': '#ffebee'}
    ]
    for col in df.columns:
        if "Libro Rojo" in col:
            cond_styles.append({'if': {'column_id': col, 'filter_query': f'{{{col}}} != "-"'}, 'backgroundColor': '#fff3e0'})

    return html.Div([
        dbc.Button("📥 Descargar Excel", id="btn-dl", color="success", className="mb-2 w-100"),
        dash_table.DataTable(
            data=df.to_dict('records'),
            columns=[{"name": i, "id": i} for i in df.columns if i != "protegido"],
            style_table={'overflowX': 'auto'},
            style_data_conditional=cond_styles,
            sort_action='native', filter_action='native', page_size=10
        )
    ]), df.to_json(orient='split')

@app.callback(Output('dl-excel', 'data'), Input('btn-dl', 'n_clicks'), State('store-res', 'data'), prevent_initial_call=True)
def download(n, data):
    if not data: return no_update
    df = pd.read_json(io.StringIO(data), orient='split')
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='xlsxwriter') as w:
        df.drop(columns=['protegido'], errors='ignore').to_excel(w, index=False)
    out.seek(0)
    return dcc.send_bytes(out.getvalue(), "EIDOS_Completo.xlsx")

if __name__ == '__main__':
    app.run_server(debug=False)