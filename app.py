# ============================================================
# EIDOScopio — Arquitectura IEPNB-first con fuzzy matching
# Basado en la API EIDOS del IEPNB y el enfoque de eidosapi
# Miranda-Cebrián (2025), Ecosistemas 34(3) :contentReference[oaicite:0]{index=0}
# ============================================================

import os
import io
import re
import time
import json
import unicodedata
from threading import Lock
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import diskcache

from rapidfuzz import process, fuzz

import dash
from dash import dcc, html, dash_table, Input, Output, State, no_update
import dash_bootstrap_components as dbc

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

API_BASE_URL = "https://iepnb.gob.es/api/especie"
LISTA_PATRON_URL = "https://iepnb.gob.es/api/especie/listapatron"

cache_dir = os.getenv("CACHE_DIR", "/tmp/eidos-cache")
os.makedirs(cache_dir, exist_ok=True)

cache = diskcache.Cache(cache_dir)
background_callback_manager = dash.DiskcacheManager(cache)

LP_CACHE = os.path.join(cache_dir, "lista_patron.parquet")

# Sesión HTTP robusta
_session = requests.Session()
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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

BASE_COLS = {"Especie", "Nombre normalizado", "Grupo taxonómico", "Nombre común", "Error", "protegido", "Score fuzzy"}

# ============================================================
# UTILIDADES GENERALES
# ============================================================

def _normalize(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower().strip()

def _get_json(endpoint: str, params: dict):
    global _last_call
    try:
        with _lock:
            now = time.time()
            wait = (_MIN_INTERVAL - (now - _last_call))
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
        r = _session.get(f"{API_BASE_URL}{endpoint}", params=params, timeout=(5, 30))
        r.raise_for_status()
        return r.json() or []
    except requests.exceptions.RequestException:
        return []
# ============================================================
# DESCARGA Y CACHEO DE LA LISTA PATRÓN (IEPNB)
# Equivalente a eidos_clean_checklist() del paper
# ============================================================

_lp_df = None

def get_lista_patron():
    global _lp_df
    if _lp_df is not None:
        return _lp_df

    if os.path.exists(LP_CACHE):
        _lp_df = pd.read_parquet(LP_CACHE)
        return _lp_df

    r = _session.get(LISTA_PATRON_URL, timeout=(10, 120))
    r.raise_for_status()
    data = r.json()
    df = pd.DataFrame(data)

    # Construimos nombre completo
    df["full_name"] = (
        df["genus"].fillna("") + " " +
        df["species"].fillna("") + " " +
        df.get("subspecies", "").fillna("")
    ).str.replace(r"\s+", " ", regex=True).str.strip()

    # Normalización para matching
    df["full_name_norm"] = df["full_name"].apply(_normalize)

    df.to_parquet(LP_CACHE)
    _lp_df = df
    return _lp_df


# ============================================================
# FUZZY MATCHING CONTRA LISTA PATRÓN (como eidos_fuzzy_names)
# ============================================================

def fuzzy_taxon_match(name: str, threshold=85):
    lp = get_lista_patron()
    name_norm = _normalize(name)

    matches = process.extract(
        name_norm,
        lp["full_name_norm"],
        scorer=fuzz.token_sort_ratio,
        limit=5
    )

    if not matches:
        return None

    best_norm, score, idx = matches[0]

    if score < threshold:
        return None

    row = lp.iloc[idx]

    return {
        "supplied": name,
        "matched": row["full_name"],
        "taxonid": int(row["idtaxon"]),
        "accepted": row.get("acceptedname") or row["full_name"],
        "score": score
    }

# ============================================================
# FUNCIONES API EIDOS (RPC y vistas)
# ============================================================

def obtener_id_por_nombre_exacto(nombre_cientifico: str):
    try:
        r = _session.get(
            f"{API_BASE_URL}/rpc/obtenertaxonespornombre",
            params={"_nombretaxon": nombre_cientifico},
            timeout=(5, 30),
        )
        r.raise_for_status()
        datos = r.json() or []
        if not datos:
            return None
        for registro in datos:
            nt = (registro.get("nametype") or "").lower()
            if "aceptado" in nt or "valido" in nt or "válido" in nt:
                return registro.get("taxonid")
        return datos[0].get("taxonid")
    except requests.exceptions.RequestException:
        return None


def obtener_nombre_por_id(taxon_id: int):
    try:
        r = _session.get(
            f"{API_BASE_URL}/rpc/obtenertaxonporid",
            params={"_idtaxon": taxon_id},
            timeout=(5, 30),
        )
        r.raise_for_status()
        datos = r.json() or []
        if datos and datos[0].get("name"):
            return datos[0]["name"]
        return None
    except requests.exceptions.RequestException:
        return None


def obtener_datos_proteccion(taxon_id: int, nombre_base: str):
    protecciones = {"Especie": nombre_base}
    estados_por_col = defaultdict(set)

    try:
        r = _session.get(
            f"{API_BASE_URL}/rpc/obtenerestadoslegalesportaxonid",
            params={"_idtaxon": taxon_id},
            timeout=(5, 30),
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
            elif ambito in ("Autonómico", "Regional"):
                columna = f"Catálogo - {item.get('ccaa', 'Desconocida')}"
            elif ambito == "Internacional":
                columna = item.get("dataset", "Convenio Internacional")
            else:
                columna = item.get("dataset")

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
    pref = [f for f in es_castellano if f.get("espreferente") is True]
    if pref:
        return pref[0].get("nombre_comun")
    if es_castellano:
        return es_castellano[0].get("nombre_comun")
    return filas[0].get("nombre_comun")
# ============================================================
# RESOLUCIÓN TAXONÓMICA IEPNB-FIRST
# (fuzzy → exacto → fallo)
# ============================================================

def resolver_taxon(nombre):
    # 1) Fuzzy contra Lista Patrón
    fuzzy = fuzzy_taxon_match(nombre)
    if fuzzy:
        return {
            "taxonid": fuzzy["taxonid"],
            "matched": fuzzy["matched"],
            "score": fuzzy["score"]
        }

    # 2) Fallback exacto EIDOS
    taxonid = obtener_id_por_nombre_exacto(nombre)
    if taxonid:
        return {
            "taxonid": taxonid,
            "matched": nombre,
            "score": 100
        }

    return None


# ============================================================
# PROCESAMIENTO DE NOMBRES E IDS
# ============================================================

def _proc_nombre(nombre):
    res = resolver_taxon(nombre)

    if not res:
        return {
            "Especie": nombre,
            "Nombre normalizado": "-",
            "Grupo taxonómico": "-",
            "Nombre común": "-",
            "Score fuzzy": "-",
            "Error": "No encontrado (ni fuzzy ni exacto)"
        }

    taxonid = res["taxonid"]
    nombre_norm = res["matched"]
    score = res["score"]

    datos = obtener_datos_proteccion(taxonid, nombre_norm)
    datos["Nombre normalizado"] = nombre_norm
    datos["Score fuzzy"] = score
    datos["Grupo taxonómico"] = obtener_grupo_taxonomico_por_id(taxonid) or "-"
    datos["Nombre común"] = obtener_nombre_comun_por_id(taxonid) or "-"

    return datos


def _proc_id(taxon_id):
    nombre = obtener_nombre_por_id(taxon_id)
    if not nombre:
        return {
            "Especie": f"ID:{taxon_id}",
            "Nombre normalizado": "-",
            "Grupo taxonómico": "-",
            "Nombre común": "-",
            "Score fuzzy": "-",
            "Error": "ID sin nombre en EIDOS"
        }

    datos = obtener_datos_proteccion(taxon_id, nombre)
    datos["Nombre normalizado"] = nombre
    datos["Score fuzzy"] = 100
    datos["Grupo taxonómico"] = obtener_grupo_taxonomico_por_id(taxon_id) or "-"
    datos["Nombre común"] = obtener_nombre_comun_por_id(taxon_id) or "-"

    return datos
# ============================================================
# ORQUESTACIÓN Y TABLA
# ============================================================

def generar_tabla_completa(listado_nombres=None, listado_ids=None, progress_callback=None):
    resultados_ok = []
    resultados_err = []

    listado_nombres = listado_nombres or []
    listado_ids = listado_ids or []

    total = len(listado_nombres) + len(listado_ids)
    if total == 0:
        return pd.DataFrame()

    done = 0

    def update():
        nonlocal done
        done += 1
        if progress_callback:
            progress_callback((done, total))

    tareas = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        tareas += [ex.submit(_proc_nombre, n) for n in listado_nombres]
        tareas += [ex.submit(_proc_id, i) for i in listado_ids]

        for fut in as_completed(tareas):
            fila = fut.result()
            if fila.get("Error"):
                resultados_err.append(fila)
            else:
                resultados_ok.append(fila)
            update()

    df = pd.DataFrame(resultados_ok + resultados_err)
    df.fillna("-", inplace=True)
    return df


# ============================================================
# ORDEN ESTABLE DE COLUMNAS (igual que tu app original)
# ============================================================

def ordenar_columnas_df(df):
    if df.empty:
        return df.copy()

    fixed = ["Grupo taxonómico", "Nombre común", "Especie", "Nombre normalizado", "Score fuzzy"]
    fixed_present = [c for c in fixed if c in df.columns]

    base_exclude = set(BASE_COLS) - {"Error"}
    legales = [c for c in df.columns if c not in base_exclude and c != "Error"]

    auton = [c for c in legales if c.startswith("Catálogo - ")]

    def es_nacional(c):
        cl = _normalize(c)
        return cl == "catalogo nacional" or "nacional" in cl

    nacional = [c for c in legales if c not in auton and es_nacional(c)]
    internacional = [c for c in legales if c not in auton and c not in nacional]

    patrones_intl = [
        ("directiva aves", 1), ("aves", 2), ("directiva habitat", 3),
        ("habitat", 4), ("habitats", 4), ("cites", 5),
        ("berna", 6), ("bonn", 7), ("cms", 7), ("aewa", 8),
    ]

    def intl_prio(name):
        n = _normalize(name)
        for p, pr in patrones_intl:
            if p in n:
                return pr
        return 100

    internacional_sorted = sorted(internacional, key=lambda x: (intl_prio(x), x))
    nacional_sorted = sorted(nacional, key=lambda x: (0 if _normalize(x) == "catalogo nacional" else 1, x))

    ccaa_order = [
        "Andalucía","Aragón","Asturias","Illes Balears","Canarias","Cantabria",
        "Castilla-La Mancha","Castilla y León","Cataluña","Ceuta","Comunitat Valenciana",
        "Extremadura","Galicia","La Rioja","Comunidad de Madrid","Melilla",
        "Región de Murcia","Navarra","País Vasco"
    ]
    rank = {f"Catálogo - {n}": i for i, n in enumerate(ccaa_order)}
    auton_sorted = sorted(auton, key=lambda x: (rank.get(x, 999), x))

    ordered = fixed_present + internacional_sorted + nacional_sorted + auton_sorted
    leftover = [c for c in df.columns if c not in ordered and c not in {"protegido"}]

    if "Error" in leftover:
        leftover.remove("Error")
        ordered += leftover + ["Error"]
    else:
        ordered += leftover

    ordered = [c for c in ordered if c in df.columns]
    return df.reindex(columns=ordered)
# ============================================================
# DASH APP (UI, igual que tu versión original)
# ============================================================

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    background_callback_manager=background_callback_manager,
)
server = app.server

sidebar = html.Div(
    [
        html.Div([
            html.H2("EIDOScopio", className="display-5"),
            html.H5("🔎 Buscador de Especies", className="text-muted"),
            html.Hr(),
            html.P(
                "Herramienta para explorar de forma masiva el estatus legal de la biodiversidad española "
                "a través de la API de EIDOS (IEPNB).",
                className="lead",
            ),
        ]),
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

content = html.Div(
    [
        dbc.Accordion([
            dbc.AccordionItem(
                [
                    html.P("- Por nombre científico: uno por línea o separados por comas."),
                    html.P("- Por ID de EIDOS: números separados por comas, punto y coma, espacios o saltos de línea."),
                    html.P("- Pulsa 'Comenzar Búsqueda'."),
                ],
                title="ℹ️ Ver instrucciones de uso",
            )
        ]),

        dbc.ButtonGroup(
            [
                dbc.Button("Cargar datos de ejemplo", id="btn-ejemplo", color="secondary"),
                dbc.Button("🧹 Limpiar datos", id="btn-limpiar", color="light"),
            ],
            className="mt-3 mb-3",
        ),

        dbc.Row([
            dbc.Col(dcc.Textarea(id='area-nombres', placeholder="Lynx pardinus\nUrsus arctos", style={'width': '100%', 'height': 200})),
            dbc.Col(dcc.Textarea(id='area-ids', placeholder="14389\n999999", style={'width': '100%', 'height': 200})),
        ]),

        dbc.Button("🔎 Comenzar Búsqueda", id="btn-busqueda", color="primary", size="lg", className="mt-3 w-100"),

        html.Hr(),

        html.Div(
            [
                html.P("Procesando..."),
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

app.layout = html.Div(
    [
        dcc.Store(id='store-resultados'),
        dcc.Store(id='run-flag', data=False),
        dcc.Download(id='download-excel'),
        sidebar,
        content,
    ]
)
# ============================================================
# CALLBACKS DE CONTROL (ejemplo, limpiar, toggle)
# ============================================================

@app.callback(
    Output('area-nombres', 'value'),
    Output('area-ids', 'value'),
    Input('btn-ejemplo', 'n_clicks'),
    Input('btn-limpiar', 'n_clicks'),
    prevent_initial_call=True,
)
def set_textareas(n_ejemplo, n_limpiar):
    ejemplo_nombres = "Lynx pardinus\nUrsus arctos\nVorderea pyrenaica"
    ejemplo_ids = "14389\n999999"
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
# ============================================================
# CALLBACK PRINCIPAL (búsqueda en background)
# ============================================================

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
    progress=[
        Output('progress-bar', 'value'),
        Output('progress-bar', 'label'),
    ],
    cancel=[Input('run-flag', 'data')],
    background=True,
    prevent_initial_call=True,
)
def ejecutar_busqueda(set_progress, run_flag, nombres_texto, ids_texto):
    if not run_flag:
        return no_update, no_update

    nombres_texto = (nombres_texto or "").strip()
    ids_texto = (ids_texto or "").strip()

    if not nombres_texto and not ids_texto:
        return dbc.Alert("Introduce al menos un nombre o un ID.", color="warning"), no_update

    def progress_wrapper(info):
        done, total = info
        if total > 0:
            set_progress((done / total * 100, f"{done} / {total}"))

    lista_nombres = [i.strip() for i in re.split(r'[\n,;]+', nombres_texto) if i.strip()]
    raw_ids = [t for t in re.split(r'[\s,;]+', ids_texto) if t]
    lista_ids = [int(t.replace(".", "")) for t in raw_ids if t.replace(".", "").isdigit()]

    df = generar_tabla_completa(lista_nombres, lista_ids, progress_callback=progress_wrapper)

    if df.empty:
        return dbc.Alert("No se obtuvieron resultados.", color="info"), no_update

    columnas_legales = [c for c in df.columns if c not in BASE_COLS]
    df["protegido"] = df[columnas_legales].ne("-").any(axis=1) if columnas_legales else False

    df = ordenar_columnas_df(df)

    layout = html.Div([
        html.H3("Resultados"),
        dbc.Button("📥 Descargar Excel", id="btn-descarga", color="success", className="mb-3 w-100"),
        dash_table.DataTable(
            id='tabla-resultados',
            columns=[{"name": i, "id": i} for i in df.drop(columns=['protegido'], errors='ignore').columns],
            data=df.to_dict("records"),
            page_size=10,
            filter_action="native",
            sort_action="native",
            style_table={"overflowX": "auto"},
        ),
    ])

    return layout, df.to_json(orient="split")
# ============================================================
# DESCARGA A EXCEL
# ============================================================

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
        df.drop(columns=['protegido'], errors='ignore').to_excel(
            writer, index=False, sheet_name='ProteccionEspecies'
        )
    output.seek(0)
    return dcc.send_bytes(output.getvalue(), "proteccion_especies.xlsx")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    debug = os.getenv("DASH_DEBUG", "false").lower() == "true"
    app.run_server(debug=debug)

