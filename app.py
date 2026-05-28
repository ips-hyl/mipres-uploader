"""
MIPRES Uploader  -  Flask web app (v2)
========================================
Interfaz web para que el personal de IPS H&L suba un PDF MIPRES y los datos
se transfieran automáticamente al Google Form de Solicitud.

UI:
  - Pantalla 1: drag-and-drop del PDF.
  - Pantalla 2: preview con tres secciones:
      a) Datos críticos del paciente (visible, editable)
      b) Priorización del caso (RADIO OBLIGATORIO, sin default)
      c) Datos clínicos avanzados (colapsada, auto-llenada, editable)
  - Pantalla 3: confirmación de envío.

Flujo de datos: PDF -> parser -> preview -> POST a Google Form -> Sheet.
"""
from __future__ import annotations

import logging
import os
import tempfile
from functools import wraps
from pathlib import Path

from flask import (
    Flask, flash, redirect, render_template_string,
    request, session, url_for,
)

from mipres_to_form import (
    MipresPdfParser,
    MipresRecord,
    PersonaRecord,
    DiagnosticoRecord,
    TecnologiaRecord,
    build_payload,
    submit_via_requests,
    ENTRY_IDS,
    PUBLIC_FORM_ID,
    FORM_ACTION_URL,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("uploader")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "cambia-esto-en-produccion")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB


# =============================================================================
# AUTH SIMPLE
# =============================================================================
APP_PASSWORD = os.environ.get("APP_PASSWORD")


def require_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if APP_PASSWORD and not session.get("authed"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# =============================================================================
# PLANTILLAS HTML (inline para deploy en un solo archivo)
# =============================================================================
def page(content: str, *, title: str = "MIPRES Uploader") -> str:
    return f"""<!doctype html><html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50 min-h-screen">
<div class="max-w-3xl mx-auto p-6">
  <div class="mb-6">
    <h1 class="text-2xl font-semibold text-slate-800">MIPRES Uploader</h1>
    <p class="text-sm text-slate-500">IPS H&amp;L Salud SAS — Solicitud de Tecnología</p>
  </div>
  {content}
</div></body></html>"""


TPL_LOGIN = """
<form method="post" class="bg-white border border-slate-200 rounded-lg p-6 shadow-sm">
  <label class="block text-sm font-medium text-slate-700 mb-2">Contraseña</label>
  <input type="password" name="password" autofocus required
         class="w-full border border-slate-300 rounded-md px-3 py-2">
  <button class="mt-4 w-full bg-slate-800 text-white rounded-md py-2 hover:bg-slate-900">
    Entrar
  </button>
  {% if error %}<p class="mt-3 text-sm text-red-600">{{ error }}</p>{% endif %}
</form>
"""

TPL_UPLOAD = """
<form method="post" action="{{ url_for('preview') }}" enctype="multipart/form-data"
      class="bg-white border border-slate-200 rounded-lg p-6 shadow-sm">
  <label class="block text-sm font-medium text-slate-700 mb-2">
    PDF de prescripción MIPRES
  </label>
  <input type="file" name="pdf" accept="application/pdf" required
         class="block w-full text-sm text-slate-700
                file:mr-3 file:py-2 file:px-4 file:border-0
                file:rounded-md file:bg-slate-800 file:text-white
                hover:file:bg-slate-900">
  <p class="text-xs text-slate-500 mt-3">
    El archivo se procesa en memoria y se descarta. No se almacena.
  </p>
  <button class="mt-5 w-full bg-emerald-600 text-white rounded-md py-2
                 hover:bg-emerald-700">
    Procesar PDF
  </button>
</form>
{% with msgs = get_flashed_messages() %}
  {% if msgs %}<div class="mt-4 space-y-2">
    {% for m in msgs %}
      <div class="bg-amber-50 border border-amber-200 text-amber-900
                  text-sm rounded-md px-4 py-2">{{ m }}</div>
    {% endfor %}
  </div>{% endif %}
{% endwith %}
"""

TPL_PREVIEW = """
<form method="post" action="{{ url_for('submit') }}" class="space-y-4">

  <!-- ============ SECCIÓN 1: DATOS CRÍTICOS (visible) ============ -->
  <div class="bg-white border border-slate-200 rounded-lg p-6 shadow-sm space-y-3">
    <div class="flex items-center justify-between">
      <h2 class="text-base font-semibold text-slate-800">Datos del paciente</h2>
      <span class="text-xs text-slate-500">Verificar antes de enviar</span>
    </div>

    <div>
      <label class="block text-xs font-medium text-slate-600 mb-1">Sede</label>
      <input readonly value="{{ r.sede_form_value }}"
             class="w-full border border-slate-200 bg-slate-50 rounded-md px-3 py-2 text-slate-700">
      <input type="hidden" name="codigo_ips" value="{{ r.codigo_ips }}">
    </div>

    <div>
      <label class="block text-xs font-medium text-slate-600 mb-1">N° de prescripción</label>
      <input name="numero_solicitud" value="{{ r.numero_solicitud }}" required
             class="w-full border border-slate-300 rounded-md px-3 py-2 font-mono">
    </div>

    <div class="grid grid-cols-4 gap-3">
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Primer nombre</label>
        <input name="primer_nombre" value="{{ r.paciente.primer_nombre }}"
               class="w-full border border-slate-300 rounded-md px-3 py-2">
      </div>
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Segundo nombre</label>
        <input name="segundo_nombre" value="{{ r.paciente.segundo_nombre }}"
               class="w-full border border-slate-300 rounded-md px-3 py-2">
      </div>
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Primer apellido</label>
        <input name="primer_apellido" value="{{ r.paciente.primer_apellido }}"
               class="w-full border border-slate-300 rounded-md px-3 py-2">
      </div>
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Segundo apellido</label>
        <input name="segundo_apellido" value="{{ r.paciente.segundo_apellido }}"
               class="w-full border border-slate-300 rounded-md px-3 py-2">
      </div>
    </div>

    <div class="grid grid-cols-3 gap-3">
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Tipo ID</label>
        <select name="tipo_id"
                class="w-full border border-slate-300 rounded-md px-3 py-2 bg-white">
          {% for opt in ['CC','CE','PT','RC','TI','PA','MS','AS'] %}
            <option value="{{ opt }}" {% if opt == r.paciente.tipo_id_corto %}selected{% endif %}>
              {{ opt }}
            </option>
          {% endfor %}
        </select>
      </div>
      <div class="col-span-2">
        <label class="block text-xs font-medium text-slate-600 mb-1">Número de identidad</label>
        <input name="numero_identidad" value="{{ r.paciente.numero_id }}" required
               class="w-full border border-slate-300 rounded-md px-3 py-2 font-mono">
      </div>
    </div>
  </div>

  <!-- ============ SECCIÓN 2: PRIORIZACIÓN (radio obligatorio) ============ -->
  <div class="bg-amber-50 border-2 border-amber-300 rounded-lg p-6 shadow-sm">
    <h2 class="text-base font-semibold text-amber-900 mb-1">Priorización del caso</h2>
    <p class="text-xs text-amber-800 mb-3">
      Obligatorio. Tiene implicaciones de SLA con la EPS — elegí explícitamente.
    </p>
    <div class="space-y-2">
      <label class="flex items-center gap-3 p-3 bg-white border border-amber-200
                    rounded-md cursor-pointer hover:bg-amber-25">
        <input type="radio" name="ambito_priorizacion" value="no priorizado" required
               class="w-4 h-4 text-amber-600">
        <span class="text-sm text-slate-800">Ambulatorio - <b>no priorizado</b></span>
      </label>
      <label class="flex items-center gap-3 p-3 bg-white border border-amber-200
                    rounded-md cursor-pointer hover:bg-amber-25">
        <input type="radio" name="ambito_priorizacion" value="priorizado" required
               class="w-4 h-4 text-amber-600">
        <span class="text-sm text-slate-800">Ambulatorio - <b>priorizado</b></span>
      </label>
    </div>
  </div>

  <!-- ============ SECCIÓN 3: AVANZADOS (colapsado) ============ -->
  <details class="bg-white border border-slate-200 rounded-lg shadow-sm">
    <summary class="px-6 py-4 cursor-pointer hover:bg-slate-50 select-none">
      <span class="text-base font-semibold text-slate-800">
        Datos clínicos avanzados
      </span>
      <span class="text-xs text-slate-500 ml-2">
        Auto-llenados desde el PDF · Click para ver/editar
      </span>
    </summary>
    <div class="px-6 pb-6 space-y-3 border-t border-slate-100 pt-4">

      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Fecha de prescripción</label>
          <input name="fecha_prescripcion" value="{{ r.fecha_prescripcion }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2 font-mono">
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Cédula del médico</label>
          <input name="medico_cedula" value="{{ r.medico.numero_id }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2 font-mono">
        </div>
      </div>

      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Nombre del médico prescriptor</label>
        <input name="medico_nombre" value="{{ r.medico.nombre_completo }}"
               class="w-full border border-slate-300 rounded-md px-3 py-2">
      </div>

      <div class="grid grid-cols-3 gap-3">
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">CIE-10 principal</label>
          <input name="cie10_principal" value="{{ r.diag_principal.cie10 }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2 font-mono">
        </div>
        <div class="col-span-2">
          <label class="block text-xs font-medium text-slate-600 mb-1">Descripción del diagnóstico</label>
          <input name="diag_principal_desc" value="{{ r.diag_principal.descripcion }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2">
        </div>
      </div>

      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">CIE-10 relacionado 1</label>
          <input name="cie10_relacionado_1" value="{{ r.diag_relacionado_1.cie10 }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2 font-mono">
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">CIE-10 relacionado 2</label>
          <input name="cie10_relacionado_2" value="{{ r.diag_relacionado_2.cie10 }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2 font-mono">
        </div>
      </div>

      <hr class="border-slate-200 my-2">

      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Tipo de tecnología</label>
        <select name="tipo_tecnologia"
                class="w-full border border-slate-300 rounded-md px-3 py-2 bg-white">
          {% for opt in ['Medicamento','Procedimiento','Dispositivo Médico',
                         'Producto para Soporte Nutricional','Servicio Complementario'] %}
            <option value="{{ opt }}" {% if opt == r.tecnologia.tipo %}selected{% endif %}>
              {{ opt }}
            </option>
          {% endfor %}
        </select>
      </div>

      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Nombre del producto/procedimiento</label>
        <input name="nombre_producto" value="{{ r.tecnologia.nombre }}"
               class="w-full border border-slate-300 rounded-md px-3 py-2">
      </div>

      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Forma / Presentación</label>
          <input name="forma" value="{{ r.tecnologia.forma }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2">
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Vía de administración</label>
          <input name="via_administracion" value="{{ r.tecnologia.via_administracion }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2">
        </div>
      </div>

      <div class="grid grid-cols-4 gap-3">
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Dosis</label>
          <input name="dosis" value="{{ r.tecnologia.dosis }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2">
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Frecuencia</label>
          <input name="frecuencia" value="{{ r.tecnologia.frecuencia }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2">
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Duración</label>
          <input name="duracion" value="{{ r.tecnologia.duracion }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2">
        </div>
        <div>
          <label class="block text-xs font-medium text-slate-600 mb-1">Cantidad total</label>
          <input name="cantidad_total" value="{{ r.tecnologia.cantidad_total }}"
                 class="w-full border border-slate-300 rounded-md px-3 py-2">
        </div>
      </div>

      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">Justificación clínica</label>
        <textarea name="justificacion" rows="5"
                  class="w-full border border-slate-300 rounded-md px-3 py-2 text-sm">{{ r.tecnologia.justificacion }}</textarea>
      </div>
    </div>
  </details>

  <!-- ============ BOTONES ============ -->
  <div class="flex gap-3 sticky bottom-4 bg-white p-3 border border-slate-200
              rounded-lg shadow-md">
    <a href="{{ url_for('index') }}"
       class="flex-1 text-center bg-slate-200 text-slate-700 rounded-md py-2
              hover:bg-slate-300">Cancelar</a>
    <button class="flex-1 bg-emerald-600 text-white rounded-md py-2
                   hover:bg-emerald-700 font-medium">
      Confirmar y enviar
    </button>
  </div>
</form>
"""

TPL_SUCCESS = """
<div class="bg-emerald-50 border border-emerald-200 rounded-lg p-6">
  <h2 class="text-lg font-semibold text-emerald-900 mb-2">Enviado ✓</h2>
  <p class="text-sm text-emerald-900 mb-3">
    La solicitud quedó registrada en la Sheet de la Junta MIPRES.
  </p>
  <dl class="text-sm space-y-1 text-emerald-950">
    <div><span class="text-emerald-700">Prescripción:</span>
         <span class="font-mono">{{ numero_solicitud }}</span></div>
    <div><span class="text-emerald-700">Paciente:</span>
         {{ nombre }} ({{ tipo_id }} {{ num_id }})</div>
    <div><span class="text-emerald-700">Priorización:</span>
         {{ ambito_priorizacion }}</div>
  </dl>
  <a href="{{ url_for('index') }}"
     class="inline-block mt-5 bg-slate-800 text-white rounded-md px-4 py-2
            hover:bg-slate-900">Procesar otro PDF</a>
</div>
"""

TPL_ERROR = """
<div class="bg-red-50 border border-red-200 rounded-lg p-6">
  <h2 class="text-lg font-semibold text-red-900 mb-2">Hubo un problema</h2>
  <p class="text-sm text-red-900 font-mono break-words">{{ error }}</p>
  <a href="{{ url_for('index') }}"
     class="inline-block mt-5 bg-slate-800 text-white rounded-md px-4 py-2
            hover:bg-slate-900">Volver</a>
</div>
"""


# =============================================================================
# RUTAS
# =============================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        session["authed"] = True
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authed"] = True
            return redirect(url_for("index"))
        error = "Contraseña incorrecta."
    return page(render_template_string(TPL_LOGIN, error=error), title="Login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@require_login
def index():
    return page(render_template_string(TPL_UPLOAD))


@app.route("/preview", methods=["POST"])
@require_login
def preview():
    """Sube PDF -> parsea -> muestra formulario de verificación."""
    file = request.files.get("pdf")
    if not file or not file.filename.lower().endswith(".pdf"):
        flash("Subí un archivo PDF válido.")
        return redirect(url_for("index"))

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = Path(tmp.name)

    try:
        record = MipresPdfParser(tmp_path).parse()
    except Exception as e:
        log.exception("Error parseando PDF")
        return page(render_template_string(TPL_ERROR, error=str(e)),
                    title="Error"), 400
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    return page(render_template_string(TPL_PREVIEW, r=record),
                title="Verificar datos")


# Mapeo inverso de códigos cortos a texto completo (para reconstruir record).
TIPO_ID_LARGO = {
    "CC": "Cédula de ciudadanía",
    "CE": "Cédula de extranjería",
    "PT": "Permiso por Protección Temporal",
    "RC": "Registro civil",
    "TI": "Tarjeta de identidad",
    "PA": "Pasaporte",
    "MS": "Menor sin identificación",
    "AS": "Adulto sin identificación",
}


@app.route("/submit", methods=["POST"])
@require_login
def submit():
    """Reconstruye el MipresRecord desde los campos del form y envía al Google Form."""
    try:
        f = request.form
        ambito_priorizacion = f.get("ambito_priorizacion", "").strip()
        if ambito_priorizacion not in ("priorizado", "no priorizado"):
            raise ValueError("Hay que seleccionar la priorización del caso.")

        record = MipresRecord(
            numero_solicitud=f["numero_solicitud"].strip(),
            fecha_prescripcion=f.get("fecha_prescripcion", "").strip(),
            codigo_ips=f["codigo_ips"].strip(),
            paciente=PersonaRecord(
                tipo_id_raw=TIPO_ID_LARGO.get(f["tipo_id"], ""),
                numero_id=f["numero_identidad"].strip(),
                primer_nombre=f.get("primer_nombre", "").strip(),
                segundo_nombre=f.get("segundo_nombre", "").strip(),
                primer_apellido=f.get("primer_apellido", "").strip(),
                segundo_apellido=f.get("segundo_apellido", "").strip(),
            ),
            medico=PersonaRecord(
                tipo_id_raw="Cédula de ciudadanía",  # asumimos para el médico
                numero_id=f.get("medico_cedula", "").strip(),
                # No deshacemos el split del nombre; lo metemos todo como
                # primer_nombre para que nombre_completo lo devuelva tal cual.
                primer_nombre=f.get("medico_nombre", "").strip(),
            ),
            diag_principal=DiagnosticoRecord(
                cie10=f.get("cie10_principal", "").strip(),
                descripcion=f.get("diag_principal_desc", "").strip(),
            ),
            diag_relacionado_1=DiagnosticoRecord(
                cie10=f.get("cie10_relacionado_1", "").strip()),
            diag_relacionado_2=DiagnosticoRecord(
                cie10=f.get("cie10_relacionado_2", "").strip()),
            tecnologia=TecnologiaRecord(
                tipo=f.get("tipo_tecnologia", "").strip(),
                nombre=f.get("nombre_producto", "").strip(),
                forma=f.get("forma", "").strip(),
                via_administracion=f.get("via_administracion", "").strip(),
                dosis=f.get("dosis", "").strip(),
                frecuencia=f.get("frecuencia", "").strip(),
                duracion=f.get("duracion", "").strip(),
                cantidad_total=f.get("cantidad_total", "").strip(),
                justificacion=f.get("justificacion", "").strip(),
            ),
        )

        # Validaciones ligeras antes del POST
        if not record.numero_solicitud.isdigit() or len(record.numero_solicitud) < 15:
            raise ValueError("N° de prescripción debe ser numérico (15+ dígitos).")
        if not record.paciente.numero_id.isdigit():
            raise ValueError("Número de identidad debe ser numérico.")
        if not record.paciente.tipo_id_corto:
            raise ValueError("Tipo de ID no reconocido.")

        submit_via_requests(record, ambito_priorizacion)

        return page(render_template_string(
            TPL_SUCCESS,
            numero_solicitud=record.numero_solicitud,
            nombre=record.paciente.nombre_completo,
            tipo_id=record.paciente.tipo_id_corto,
            num_id=record.paciente.numero_id,
            ambito_priorizacion=ambito_priorizacion,
        ), title="Enviado")

    except Exception as e:
        log.exception("Error en /submit")
        return page(render_template_string(TPL_ERROR, error=str(e)),
                    title="Error"), 500


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
