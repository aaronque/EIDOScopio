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

# --- Inicialización de la App Dash ---
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.DARKLY])
server = app.server

# --- DEFINICIÓN DE COMPONENTES REUTILIZABLES ---

sidebar = html.Div(
    [
        html.H2("EIDOScopio", className="display-4"),
        html.Hr(),
        html.P("Una herramienta para consultar el estatus de protección de especies en
