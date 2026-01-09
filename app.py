# app.py (Versión Definitiva: Fuzzy Híbrido + Estatus Conservación + CSV Opt)
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
    """GET para endpoints pequeños."""
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
def obtener_lista_patron_v5():
    """
    Descarga la taxonomía completa en CSV pidiendo las columnas CORRECTAS.
    """
    try:
        endpoint = "/v_taxonomia"
        headers = {"Accept": "text/csv"}
        # Usamos 'taxonid' y 'name' (según doc oficial)
        params = {
            "select": "taxonid,name", 
            "limit": 250000 
        }
        
        print("📥 Iniciando descarga de Lista Patrón (CSV)...")
        t0 = time.time()
        r = _session.get(f"{API_BASE_URL}{endpoint}", params=params, headers=headers, timeout=(15, 60))
        
        if r.status_code != 200:
            print(f"Error API Checklist: {r.status_code}")
            return {}
        
        # Procesado manual del CSV
        contenido = io.StringIO(r.text)
        reader = csv.DictReader(contenido)
        referencia = {}
        count = 0
        for row in reader:
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
    Lógica Híbrida Definitiva:
    1. Partial Ratio: Permite coincidencia flexible (Borderea pyrenaica Miégev -> Vorderea pyrenaica).
    2. Validación Prefijo: Bloquea falsos positivos cortos (Fusinus != Gamusinus).
    """
    if not lista_referencia:
        return None
    
    # 1. Candidato por coincidencia parcial
    match = process.extractOne(
        nombre_buscado, 
        lista_referencia.keys(), 
        scorer=fuzz.partial_ratio
    )
    
    if not match: return None

    match_name, score, _ = match
    if score < umbral: return None

    # 2. Validación de Prefijo (El "Filtro Anti-Gamusinus")
    if len(nombre_buscado) > len(match_name):
        # Si buscamos algo más largo que el candidato, exigimos coincidencia total
        if fuzz.ratio(nombre_buscado, match_name) < umbral:
            return None
    else:
        # Si el candidato es más largo (tiene Autor), comparamos solo el inicio
        recorte = match_name[:len(nombre_buscado)]
        if fuzz.ratio(nombre_buscado, recorte) < umbral:
            return None 

    return lista_referencia[match_name], match_name, score

# ============================
# Funciones API Principales
# ============================
def obtener_id_por_nombre(nombre_cientifico: str):
    try:
        r = _session.get(
            f"{API_BASE_URL}/rpc/obtenertaxonespornombre",
            params={"_nombretaxon": nombre_cientifico},
            timeout=(5, 15),
        )
        r.raise_for_status()
        datos = r.json() or []
        if not datos: return None
        for registro in datos:
            nt = (registro.get("nametype") or "").strip().lower()
            if "aceptado" in nt or "valido" in nt:
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
        d = r.json() or []
        return d[0]["name"] if d and d[0].get("name") else None
    except:
        return None

# --- NUEVA FUNCIÓN: ESTADO DE CONSERVACIÓN (UICN / LIBRO ROJO) ---
def obtener_datos_conservacion(taxon_id: int):
    datos_cons = {}
    try:
        # Endpoint documentado con _idtaxon
        r = _session.get(
            f"{API_BASE_URL}/rpc/obtenerestadosconservacionportaxonid",
            params={"_idtaxon": taxon_id},
            timeout=(5, 15),
        )
        r.raise_for_status()
        lista = r.json() or []
        
        por_ambito = defaultdict(list)
        
        for item in lista:
            # Detectamos ambito/aplicaa y unimos categoría con año
            ambito = item.get("ambito") or item.get("aplicaa") or "Desconocido"
            categoria = item.get("categoriaconservacion")
            anio = item.get("anio")
            
            if categoria:
                texto = f"{categoria}"
                if anio: texto += f" ({anio})"
                por_ambito[ambito].append(texto)
        
        for amb, cats in por_ambito.items():
            col_name = f"Libro Rojo - {amb}"
            datos_cons[col_name] = "; ".join(sorted(set(cats)))
            
    except requests.exceptions.RequestException:
        pass 
    return datos_cons

def obtener_datos_proteccion(taxon_id: int):
    protecciones = {}
    estados_por_col = defaultdict(set)
    try:
        r = _session.get(
            f"{API_BASE_URL}/rpc/obtenerestadoslegalesportaxonid",
            params={"_idtaxon": taxon_id},
            timeout=(5, 15),
        )
        r.raise_for_status()
        datos = r.json() or []
        for item in datos:
            if item.get("idvigente") != 1: continue
            ambito = item.get("ambito")
            estado = item.get("estadolegal")
            if not estado: continue
            
            if ambito == "Nacional":
                col = item.get("dataset", "Catálogo Nacional")
            elif ambito in ["Autonómico", "Regional"]:
                col = f"Catálogo - {item.get('ccaa', 'Desconocida')}"
            elif ambito == "Internacional":
                col = item.get("dataset", "Convenio Internacional")
            else:
                col = item.get("dataset") or "Otras Normas"
            
            if col: estados_por_col[col].add(estado)
            
        for col, ests in estados_por_col.items():
            protecciones[col] = ", ".join(sorted(ests))
    except:
        protecciones["Error"] = "Fallo API Legal"
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
            if pref: info["Nombre común"] = pref[0].get("nombre_comun")
            elif es: info["Nombre común"] = es[0].get("nombre_comun")
            else: info["Nombre común"] = f_nom[0].get("nombre_comun")
    except:
        pass
    return info

# ============================
# Orden de Columnas
# ============================
def ordenar_columnas_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty: return df.copy()
    
    fixed = ["Especie", "Grupo taxonómico", "Nombre común", "Notas"]
    fixed_present = [c for c in fixed if c in df.columns]

    # Ordenar columnas de conservación
    conservacion = [c for c in df.columns if "Libro Rojo" in c]
    def orden_cons(c):
        if "Mundial" in c: return 1
        if "España" in c: return 2
        return 3
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

# ============================
# Orquestación
# ============================
def _proc_nombre(nombre: str):
    nombre_limpio = nombre.strip()
    taxon_id = obtener_id_por_nombre(nombre_limpio)
    nota_fuzzy = "-"

    if not taxon_id:
        lista_ref = obtener_lista_patron_v5()
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
    if not nombre:
        return {"Especie": f"ID: {taxon_id}", "Error": "ID desconocido", "Notas": "-", "Grupo taxonómico": "-", "Nombre común": "-"}
    
    base = {"Especie": nombre, "Notas": "-"}
    tax = obtener_info_taxonomica(taxon_id)
    leg = obtener_datos_proteccion(taxon_id)
    cons = obtener_datos_conservacion(taxon_id)
    
    return {**base, **tax, **leg, **cons}

def generar_tabla_completa(nombres=None, ids=None, progress_callback=None):
    exitosos, fallidos = [], []
    nombres, ids = nombres or [], ids or []
    
    if nombres: obtener_lista_patron_v5()

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
# App Dash
# ============================
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP], background_callback_manager=background_callback_manager)
server = app.server

sidebar = html.Div([
    html.H2("EIDOScopio", className="display-5"),
    html.H5("🔎 Buscador Integral", className="text-muted"),
    html.Hr(),
    html.P("Consulta Estatus Legal (Catálogos) y Biológico (Libro Rojo).", className="lead"),
    dbc.Badge("Fuzzy Match + UICN", color="success", className="mb-2"),
    html.A(html.Img(src="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png", style={"width": "28px"}), href="https://github.com/aaronque/EIDOScopio", target="_blank", style={"marginTop": "auto"})
], style={"position": "fixed", "top": 0, "left": 0, "bottom": 0, "width": "22rem", "padding": "2rem", "background-color": "#f8f9fa", "display": "flex", "flex-direction": "column"})

content = html.Div([
    dbc.Alert("💡 Novedad: Ahora la tabla incluye columnas 'Libro Rojo' con el estado de conservación (UICN).", color="info", dismissable=True),
    dbc.Row([
        dbc.Col(dcc.Textarea(id='area-nombres', placeholder="Achondrostoma arcasii\nVorderea pyrenaica...", style={'width': '100%', 'height': 200})),
        dbc.Col(dcc.Textarea(id='area-ids', placeholder="13431, 9322...", style={'width': '100%', 'height': 200})),
    ]),
    dbc.Button("🔎 Buscar", id="btn-busqueda", color="primary", size="lg", className="mt-3 w-100"),
    html.Div([html.P("Consultando API..."), dbc.Progress(id="progress-bar", value=0, striped=True, animated=True)], id="progress-container", style={"display": "none"}),
    html.Div(id='output-resultados'),
], style={"margin-left": "24rem", "padding": "2rem"})

app.layout = html.Div([dcc.Store(id='store-res'), dcc.Store(id='run-flag'), dcc.Download(id='dl-excel'), sidebar, content])

@app.callback(
    Output('run-flag', 'data'), Input('btn-busqueda', 'n_clicks'), State('run-flag', 'data'), prevent_initial_call=True
)
def toggle(n, flag): return not bool(flag)

@app.callback(
    Output('output-resultados', 'children'), Output('store-res', 'data'),
    Input('run-flag', 'data'), State('area-nombres', 'value'), State('area-ids', 'value'),
    running=[(Output('btn-busqueda', 'disabled'), True, False), (Output('progress-container', 'style'), {'display': 'block'}, {'display': 'none'})],
    progress=[Output('progress-bar', 'value'), Output('progress-bar', 'label')],
    background=True, prevent_initial_call=True
)
def search(set_prog, run, txt_n, txt_i):
    if not run: return no_update
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