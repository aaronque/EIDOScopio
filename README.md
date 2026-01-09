# 🦉 EIDOScopio

> **La herramienta de consulta masiva para el Inventario Español del Patrimonio Natural y Biodiversidad.**

[![Render](https://img.shields.io/badge/Render-Abrir_App_Web-46E3B7?style=for-the-badge&logo=render&logoColor=white)](https://eidoscopio.onrender.com)


## ⚡ El Problema vs. La Solución

Consultar el estatus legal de una lista de especies en la web oficial de EIDOS requiere buscar **una por una**, entrar en su ficha, revisar las leyes y repetir el proceso. Si tienes una lista de 50 o 100 especies, esto lleva horas.

**EIDOScopio resuelve este problema permitiendo consultas por lotes (batch processing).** Pegas tu lista completa de nombres científicos (o IDs) y obtienes al instante una tabla unificada con toda la información legal y biológica.

## 🚀 Características Clave

### 1. Búsqueda Masiva Real
Copia una columna de Excel con 200 especies, pégala en EIDOScopio y obtén una tabla completa en segundos.

### 2. Cruce de Datos Integral
Para cada especie, la herramienta consulta simultáneamente múltiples fuentes de la API del MITECO:
* **⚖️ Protección Legal:** Listado de Especies Silvestres (LESRPE), Catálogo Nacional (CEEA), Catálogos Autonómicos y Directivas Europeas (Aves/Hábitat).
* **🌍 Conservación (Biología):** Categorías de amenaza según Libros Rojos (España, Mundial) y criterios UICN.

### 3. Motor "Fuzzy Match" Inteligente
* Detecta errores tipográficos automáticamente (ej. *Vorderea* → *Borderea*).
* Utiliza lógica híbrida para evitar falsos positivos taxonómicos.

### 4. Exportación Directa
Descarga los resultados en un archivo **Excel (.xlsx)** limpio y ordenado.

---

## 🛠️ Cómo usarlo

### Opción A: Versión Web (Recomendada)
No necesitas instalar nada. Accede a la versión desplegada en la nube:
👉 **[Abrir EIDOScopio en Render](https://eidoscopio.onrender.com)**

### Opción B: Ejecución Local (Para desarrolladores)
Si prefieres correr el código en tu propia máquina:

1.  **Clonar el repositorio:**
    ```bash
    git clone https://github.com/aaronque/EIDOScopio.git
    cd EIDOScopio
    ```
2.  **Instalar dependencias:**
    ```bash
    pip install -r requirements.txt
    ```
3.  **Lanzar la aplicación:**
    ```bash
    python app.py
    ```

## ⚙️ Tecnologías

* **Frontend:** Dash & Bootstrap.
* **Backend:** Python 3 (Concurrent Futures para paralelismo).
* **Datos:** API pública del IEPNB (sin necesidad de API Key).
* **Algoritmos:** RapidFuzz para la corrección de nombres.

## 📄 Nota Legal

Esta aplicación es una herramienta de consulta desarrollada por terceros para facilitar el acceso a los datos públicos. **No tiene vinculación oficial con el MITECO.** Para fines legales vinculantes, contraste siempre la información con los documentos oficiales (BOE/BOC).

## 📝 Licencia

Este proyecto se distribuye bajo la **Licencia MIT**, lo que permite su uso, modificación y distribución libremente, siempre que se mantenga la atribución al autor original.
