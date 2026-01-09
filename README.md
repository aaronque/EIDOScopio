# 🦉 EIDOScopio

> **Buscador Integral de Biodiversidad: Estatus Legal y Conservación en España.**

[![Render](https://img.shields.io/badge/Render-Ver_Aplicación_En_Vivo-46E3B7?style=for-the-badge&logo=render&logoColor=white)](https://eidoscopio.onrender.com)
**EIDOScopio** es una herramienta web que cruza datos de múltiples fuentes oficiales para ofrecer una radiografía rápida de cualquier especie. Interactúa con la API del **Inventario Español del Patrimonio Natural y Biodiversidad (IEPNB/EIDOS)**.

## 🎯 ¿Qué hace?

Permite a investigadores y consultores consultar masivamente:
1.  **Protección Legal:** Catálogos (Nacional y CCAA) y Directivas Europeas.
2.  **Estado de Conservación:** Categorías de amenaza (Libros Rojos y UICN).
3.  **Corrección Taxonómica:** Un motor inteligente corrige erratas en los nombres científicos automáticamente.

---

## 💻 Para Desarrolladores (Instalación Local)

*Si solo quieres usar la herramienta, haz clic en el botón de arriba. Si eres desarrollador y quieres ejecutar el código en tu máquina, sigue estos pasos:*

### Requisitos
* Python 3.9+
* Git

### Pasos
1.  Clonar el repositorio:
    ```bash
    git clone [https://github.com/TU_USUARIO/EIDOScopio.git](https://github.com/TU_USUARIO/EIDOScopio.git)
    cd EIDOScopio
    ```
2.  Instalar dependencias:
    ```bash
    pip install -r requirements.txt
    ```
3.  Ejecutar:
    ```bash
    python app.py
    ```

## 📄 Fuente de Datos
Datos obtenidos del servicio web público del **IEPNB** (MITECO).
* [API EIDOS](https://iepnb.gob.es/servicio/externo/ServicioWebEidos)

## 📝 Licencia
MIT License.
