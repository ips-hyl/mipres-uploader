"""
app_entrega.py — Flask web app para el proceso de ENTREGA de MIPRES.
====================================================================
El empleado:
  1. Abre la URL pública, ingresa el password compartido.
  2. Sube el PDF de la Fórmula Médica (aprobada o rechazada por la junta).
  3. La app extrae 4 campos y muestra un preview editable.
  4. El empleado marca APROBADO o RECHAZADO (radio obligatorio sin default).
  5. Al confirmar: la app sube el PDF a Drive (carpeta según decisión) y hace
     POST al Form correspondiente con los 4 campos + el link del PDF.

Mismo patrón que el sistema actual: password único de env var, PDF en memoria,
verificación humana obligatoria, Form como sink.
"""
import os
import io
import tempfile
import logging

from flask import (Flask, request, render_template_string, redirect,
                   url_for, session, flash)

from mipres_entrega import (
    FormulaMedicaParser, FormulaMedicaRecord, PersonaRecord,
    submit_via_requests, upload_pdf_to_drive, TIPOS_ID_VALIDOS,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app-entrega")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "cambia-esto-en-produccion")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# Guardamos el PDF temporalmente entre el preview y el confirm (en memoria de sesión
# guardamos solo la ruta temp; el archivo se borra tras confirmar).
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB


# =============================================================================
# AUTH
# =============================================================================
def is_authed() -> bool:
    return session.get("authed") is True


LOGIN_HTML = """
<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrega MIPRES — Acceso</title>
<style>
 body{font-family:Arial,sans-serif;background:#f4f6f8;display:flex;
   align-items:center;justify-content:center;height:100vh;margin:0}
 .card{background:#fff;padding:32px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);width:320px}
 h1{font-size:18px;margin:0 0 16px;color:#1a52c2}
 input{width:100%;padding:10px;border:1px solid #dadada;border-radius:8px;font-size:14px;box-sizing:border-box}
 button{width:100%;padding:11px;margin-top:12px;background:#1a52c2;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:bold;cursor:pointer}
 .err{color:#c62828;font-size:13px;margin-top:8px}
</style></head><body>
<div class="card">
  <h1>🏥 Entrega MIPRES</h1>
  <form method="post">
    <input type="password" name="password" placeholder="Contraseña" autofocus>
    <button type="submit">Entrar</button>
  </form>
  {% with msgs = get_flashed_messages() %}{% if msgs %}<div class="err">{{ msgs[0] }}</div>{% endif %}{% endwith %}
</div></body></html>
"""

UPLOAD_HTML = """
<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrega MIPRES — Subir PDF</title>
<style>
 body{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;padding:24px}
 .card{background:#fff;max-width:480px;margin:0 auto;padding:28px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
 h1{font-size:18px;color:#1a52c2;margin:0 0 6px}
 p{color:#666;font-size:13px;margin:0 0 20px}
 input[type=file]{width:100%;padding:14px;border:2px dashed #c5d3f5;border-radius:8px;background:#f8faff;box-sizing:border-box}
 button{width:100%;padding:12px;margin-top:16px;background:#1a52c2;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:bold;cursor:pointer}
 .logout{display:block;text-align:center;margin-top:16px;color:#999;font-size:12px;text-decoration:none}
 .err{color:#c62828;font-size:13px;margin-top:12px}
</style></head><body>
<div class="card">
  <h1>Subir Fórmula Médica</h1>
  <p>Sube el PDF del MIPRES revisado por la junta. La app leerá los datos
     y podrás clasificarlo como aprobado o rechazado.</p>
  <form method="post" action="{{ url_for('preview') }}" enctype="multipart/form-data">
    <input type="file" name="pdf" accept="application/pdf" required>
    <button type="submit">Leer PDF</button>
  </form>
  {% with msgs = get_flashed_messages() %}{% if msgs %}<div class="err">{{ msgs[0] }}</div>{% endif %}{% endwith %}
  <a class="logout" href="{{ url_for('logout') }}">Salir</a>
</div></body></html>
"""

PREVIEW_HTML = """
<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrega MIPRES — Verificar</title>
<style>
 body{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;padding:24px}
 .card{background:#fff;max-width:520px;margin:0 auto;padding:28px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
 h1{font-size:18px;color:#1a52c2;margin:0 0 4px}
 .sub{color:#666;font-size:13px;margin:0 0 20px}
 label{display:block;font-size:11px;font-weight:bold;color:#888;text-transform:uppercase;letter-spacing:.04em;margin:14px 0 4px}
 input,select{width:100%;padding:10px;border:1px solid #dadada;border-radius:8px;font-size:14px;box-sizing:border-box}
 .decision{margin:24px 0 8px;padding:16px;border:2px solid #ffcc80;border-radius:10px;background:#fff8ec}
 .decision .q{font-size:14px;font-weight:bold;color:#854f0b;margin-bottom:10px}
 .radio-row{display:flex;gap:10px}
 .radio-opt{flex:1}
 .radio-opt input{display:none}
 .radio-opt label{display:block;text-align:center;padding:14px;border:2px solid #dadada;border-radius:8px;cursor:pointer;margin:0;font-size:14px;text-transform:none;letter-spacing:0;color:#333;font-weight:bold}
 .radio-opt input:checked + label.aprob{border-color:#2e7d32;background:#e8f5e9;color:#2e7d32}
 .radio-opt input:checked + label.rech{border-color:#c62828;background:#fdecea;color:#c62828}
 button{width:100%;padding:13px;margin-top:20px;background:#1a52c2;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:bold;cursor:pointer}
 button:disabled{opacity:.5;cursor:not-allowed}
 details{margin-top:16px}
 summary{font-size:13px;color:#1a52c2;cursor:pointer}
 .back{display:block;text-align:center;margin-top:14px;color:#999;font-size:12px;text-decoration:none}
</style></head><body>
<div class="card">
  <h1>Verifica los datos</h1>
  <p class="sub">Extraídos del PDF. Corrige si algo está mal, marca la decisión de la junta y confirma.</p>
  <form method="post" action="{{ url_for('confirm') }}" id="f">
    <label>Número de MIPRES</label>
    <input name="numero_prescripcion" value="{{ rec.numero_prescripcion }}" required>

    <label>Nombre y apellidos del paciente</label>
    <input name="nombre_completo" value="{{ rec.paciente.nombre_completo }}" required>

    <label>Número de identidad</label>
    <input name="numero_identidad" value="{{ rec.paciente.numero_id }}" required>

    <label>Tipo de identificación</label>
    <select name="tipo_id">
      {% for t in tipos %}
      <option value="{{ t }}" {% if t == rec.paciente.tipo_id_form %}selected{% endif %}>{{ t }}</option>
      {% endfor %}
    </select>

    <div class="decision">
      <div class="q">¿Cuál fue la decisión de la junta?</div>
      <div class="radio-row">
        <div class="radio-opt">
          <input type="radio" name="decision" value="APROBADO" id="r-aprob" required onchange="enable()">
          <label class="aprob" for="r-aprob">✅ Aprobado</label>
        </div>
        <div class="radio-opt">
          <input type="radio" name="decision" value="RECHAZADO" id="r-rech" onchange="enable()">
          <label class="rech" for="r-rech">❌ Rechazado</label>
        </div>
      </div>
    </div>

    <button type="submit" id="btn" disabled>Confirmar y registrar</button>
  </form>
  <a class="back" href="{{ url_for('upload') }}">← Subir otro PDF</a>
</div>
<script>
function enable(){ document.getElementById('btn').disabled = false; }
</script>
</body></html>
"""

RESULT_HTML = """
<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrega MIPRES — Resultado</title>
<style>
 body{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;padding:24px}
 .card{background:#fff;max-width:460px;margin:0 auto;padding:32px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);text-align:center}
 .icon{font-size:42px}
 h1{font-size:18px;margin:12px 0 6px}
 .ok{color:#2e7d32}.rech{color:#c62828}
 p{color:#666;font-size:14px;line-height:1.5}
 .warn{background:#fff8ec;border:1px solid #ffcc80;border-radius:8px;padding:10px;font-size:12px;color:#854f0b;margin-top:16px}
 a{display:inline-block;margin-top:20px;padding:11px 24px;background:#1a52c2;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;font-size:14px}
</style></head><body>
<div class="card">
  <div class="icon">{{ '✅' if decision == 'APROBADO' else '❌' }}</div>
  <h1 class="{{ 'ok' if decision == 'APROBADO' else 'rech' }}">
    Registrado como {{ decision | lower }}
  </h1>
  <p><b>{{ nombre }}</b><br>MIPRES {{ numero }}</p>
  {% if not link %}
  <div class="warn">El PDF no se guardó en Drive (Drive no configurado). Los datos sí se registraron.</div>
  {% endif %}
  <a href="{{ url_for('upload') }}">Registrar otro</a>
</div></body></html>
"""


# =============================================================================
# RUTAS
# =============================================================================
@app.route("/", methods=["GET", "POST"])
def login():
    if is_authed():
        return redirect(url_for("upload"))
    if request.method == "POST":
        if APP_PASSWORD and request.form.get("password") == APP_PASSWORD:
            session["authed"] = True
            return redirect(url_for("upload"))
        flash("Contraseña incorrecta.")
    return render_template_string(LOGIN_HTML)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/upload")
def upload():
    if not is_authed():
        return redirect(url_for("login"))
    return render_template_string(UPLOAD_HTML)


@app.route("/preview", methods=["POST"])
def preview():
    if not is_authed():
        return redirect(url_for("login"))
    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        flash("Sube un archivo PDF válido.")
        return redirect(url_for("upload"))

    # Guarda el PDF en un temp file y la ruta en sesión para el confirm.
    pdf_bytes = f.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(pdf_bytes)
    tmp.close()
    session["pdf_path"] = tmp.name
    session["pdf_name"] = f.filename

    try:
        rec = FormulaMedicaParser(tmp.name).parse()
    except Exception as e:
        log.error("Error parseando: %s", e)
        flash(f"No se pudo leer el PDF: {e}")
        return redirect(url_for("upload"))

    return render_template_string(
        PREVIEW_HTML, rec=rec, tipos=sorted(TIPOS_ID_VALIDOS))


@app.route("/confirm", methods=["POST"])
def confirm():
    if not is_authed():
        return redirect(url_for("login"))

    decision = request.form.get("decision", "")
    if decision not in ("APROBADO", "RECHAZADO"):
        flash("Debes marcar aprobado o rechazado.")
        return redirect(url_for("upload"))

    # Reconstruye el record con los valores (posiblemente editados) del preview.
    rec = FormulaMedicaRecord(
        numero_prescripcion=request.form.get("numero_prescripcion", "").strip(),
        paciente=PersonaRecord(
            tipo_id_corto=request.form.get("tipo_id", "").strip(),
            numero_id=request.form.get("numero_identidad", "").strip(),
        ),
    )
    # El nombre se editó como un solo campo; lo guardamos directo.
    nombre_completo = request.form.get("nombre_completo", "").strip()
    # Sobreescribimos la property poniéndolo en primer_nombre (el Form pide un solo campo).
    rec.paciente.primer_nombre = nombre_completo
    rec.paciente.segundo_nombre = ""
    rec.paciente.primer_apellido = ""
    rec.paciente.segundo_apellido = ""

    # Sube el PDF a Drive (si está configurado).
    pdf_path = session.get("pdf_path", "")
    link = ""
    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()
        filename = f"{decision}_{rec.numero_prescripcion}_{rec.paciente.numero_id}.pdf"
        try:
            link = upload_pdf_to_drive(pdf_bytes, filename, decision)
        except Exception as e:
            log.warning("Subida a Drive falló: %s", e)
        # Borra el temp file (no persistir PII en disco).
        try:
            os.unlink(pdf_path)
        except OSError:
            pass

    # POST al Form correspondiente.
    try:
        submit_via_requests(rec, decision, link_pdf=link)
    except Exception as e:
        log.error("Error enviando al Form: %s", e)
        flash(f"Error registrando: {e}")
        return redirect(url_for("upload"))

    session.pop("pdf_path", None)
    return render_template_string(
        RESULT_HTML, decision=decision, nombre=nombre_completo,
        numero=rec.numero_prescripcion, link=link)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
