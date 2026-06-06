"""
MIPRES (Fórmula Médica)  ->  Drive + Google Form (Aprobados / Rechazados)
=========================================================================
Proceso de ENTREGA: el empleado sube el PDF de la Fórmula Médica aprobada o
rechazada por la junta, la app extrae 4 campos, el empleado verifica y marca
APROBADO o RECHAZADO, y según eso:
  - sube el PDF a una carpeta de Drive (Aprobados o Rechazados)
  - hace POST al Form correspondiente con los 4 campos + el link del PDF

Este parser lee el formato "FÓRMULA MÉDICA" (distinto al "Reporte de
Prescripción / Solicitud de Tecnología" que usa mipres_to_form.py). Reusa el
patrón de dataclasses, build_payload y submit_via_requests del sistema actual.

Campos extraídos del PDF:
  - Nro. Prescripción
  - Paciente (tipo doc pegado al número: "CC33965658", + 4 fragmentos de nombre)

Campo decidido por el empleado en el preview:
  - APROBADO | RECHAZADO  (radio obligatorio sin default; lo determinó la junta)
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mipres-entrega")


# =============================================================================
# CONFIGURACIÓN DE LOS DOS FORMULARIOS
# =============================================================================
# IDs públicos (los del /d/e/<ID>/viewform), NO los del editor.
FORM_APROBADOS_ID = "1FAIpQLSdsgQkLzIo_RBYlbrrk5YkdWmDcM0dRpq2x3VM-exDjxVOPGw"
FORM_RECHAZADOS_ID = "1FAIpQLSc6pfl39ge4UkLiEsQv_sSXik988zjLJCkLGFTosHZAYB2u1A"

# Entry IDs extraídos del HTML público de cada Form.
ENTRY_IDS_APROBADOS = {
    "numero_prescripcion": "entry.1495484197",
    "nombre_completo":     "entry.1993521012",
    "numero_identidad":    "entry.1833989120",
    "tipo_id":             "entry.1119797130",
    "link_pdf":            "entry.1184306419",
}

ENTRY_IDS_RECHAZADOS = {
    "numero_prescripcion": "entry.1610084704",
    "nombre_completo":     "entry.1434995228",
    "numero_identidad":    "entry.828598176",
    "tipo_id":             "entry.1054500099",
    "link_pdf":            "entry.1026662788",
}

# Opciones válidas del radio "Tipo de identificación" en ambos Forms.
# El valor enviado debe coincidir EXACTAMENTE o Google lo descarta en silencio.
TIPOS_ID_VALIDOS = {"CC", "TI", "PT", "RC", "CE", "PA", "OTRO"}


def form_config(decision: str) -> tuple[str, dict]:
    """Devuelve (form_id, entry_ids) según la decisión del empleado."""
    if decision == "APROBADO":
        return FORM_APROBADOS_ID, ENTRY_IDS_APROBADOS
    if decision == "RECHAZADO":
        return FORM_RECHAZADOS_ID, ENTRY_IDS_RECHAZADOS
    raise ValueError(f"decision inválida: {decision!r}. Esperado APROBADO o RECHAZADO.")


# =============================================================================
# MODELO DE DATOS
# =============================================================================
@dataclass
class PersonaRecord:
    tipo_id_corto: str = ""        # ya viene corto en la Fórmula Médica (CC, TI...)
    numero_id: str = ""
    primer_nombre: str = ""
    segundo_nombre: str = ""
    primer_apellido: str = ""
    segundo_apellido: str = ""

    @property
    def nombre_completo(self) -> str:
        parts = [self.primer_nombre, self.segundo_nombre,
                 self.primer_apellido, self.segundo_apellido]
        return " ".join(p for p in (s.strip() for s in parts) if p)

    @property
    def tipo_id_form(self) -> str:
        """Normaliza el tipo de doc a una de las opciones válidas del Form."""
        t = self.tipo_id_corto.upper().strip()
        return t if t in TIPOS_ID_VALIDOS else "OTRO"


@dataclass
class FormulaMedicaRecord:
    numero_prescripcion: str = ""
    fecha_expedicion: str = ""
    codigo_habilitacion: str = ""
    municipio: str = ""
    paciente: PersonaRecord = field(default_factory=PersonaRecord)


# =============================================================================
# PARSER — formato "FÓRMULA MÉDICA"
# =============================================================================
class FormulaMedicaParser:
    """Parser para el PDF 'FÓRMULA MÉDICA' (aprobado/rechazado por la junta)."""

    def __init__(self, pdf_path):
        self.pdf_path = Path(pdf_path)
        if not self.pdf_path.exists():
            raise FileNotFoundError(f"PDF no encontrado: {self.pdf_path}")

    def parse(self) -> FormulaMedicaRecord:
        text = self._extract_text()
        rec = FormulaMedicaRecord()
        rec.numero_prescripcion = self._numero_prescripcion(text)
        rec.fecha_expedicion = self._fecha(text)
        rec.codigo_habilitacion, rec.municipio = self._prestador(text)
        rec.paciente = self._paciente(text)

        log.info(
            "Parseado OK: prescripción=%s | paciente=%s (%s %s)",
            rec.numero_prescripcion,
            rec.paciente.nombre_completo,
            rec.paciente.tipo_id_corto, rec.paciente.numero_id,
        )
        return rec

    def _extract_text(self) -> str:
        try:
            with pdfplumber.open(self.pdf_path) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
        except Exception as e:
            raise RuntimeError(f"No se pudo leer el PDF: {e}") from e
        full = "\n".join(pages)
        if not full.strip():
            raise RuntimeError("El PDF se abrió pero no tiene texto extraíble.")
        return full

    @staticmethod
    def _numero_prescripcion(text: str) -> str:
        m = re.search(r"Nro\.\s*Prescripci[óo]n\s*\n\s*(\d{15,25})", text)
        if not m:
            # fallback: cualquier corrida de 15-25 dígitos tras 'Prescripción'
            m = re.search(r"Prescripci[óo]n[^\n]*\n\s*(\d{15,25})", text)
        if not m:
            raise ValueError("No se encontró 'Nro. Prescripción'.")
        return m.group(1)

    @staticmethod
    def _fecha(text: str) -> str:
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s+\d{1,2}:\d{2}", text)
        return m.group(1) if m else ""

    @staticmethod
    def _prestador(text: str) -> tuple[str, str]:
        """Extrae código de habilitación (12 díg.) y municipio.
        Layout columnar:
            Departamento: Municipio: Código Habilitación:
            RISARALDA PEREIRA 660010233201
        """
        codigo = ""
        m = re.search(r"\b(\d{12})\b", text)  # el código de habilitación
        if m:
            codigo = m.group(1)
        municipio = ""
        # La línea de valores tras "Departamento: Municipio:"
        mm = re.search(
            r"Departamento:\s*Municipio:[^\n]*\n\s*(\S+)\s+(\S+)", text)
        if mm:
            municipio = mm.group(2)
        return codigo, municipio

    @staticmethod
    def _paciente(text: str) -> PersonaRecord:
        """Extrae los datos del paciente del bloque 'DATOS DEL PACIENTE'.

        pdfplumber entrega los labels en una línea y los valores en la
        siguiente, alineados como columnas:
            Documento de Identificación: Primer Apellido: Segundo Apellido: Primer Nombre: Segundo Nombre:
            CC33965658 PAREJA ARENAS GLORIA INES
        El documento viene pegado: dos letras de tipo + dígitos ("CC33965658").
        """
        i = text.find("DATOS DEL PACIENTE")
        if i < 0:
            raise ValueError("No se encontró el bloque 'DATOS DEL PACIENTE'.")
        blk = text[i:i + 600]

        # Busca la línea que arranca con el documento: 2 letras + dígitos.
        m = re.search(r"\b([A-Z]{2})(\d{4,})\b\s+(.+)", blk)
        if not m:
            raise ValueError("No se encontró el documento del paciente.")
        tipo_id = m.group(1)
        numero_id = m.group(2)
        resto = m.group(3).strip()

        # `resto` son los nombres en orden: apellido1 apellido2 nombre1 [nombre2...]
        # Pero pueden venir en una o dos líneas; tomamos solo la primera línea.
        resto = resto.splitlines()[0].strip()
        tokens = resto.split()

        # Orden en la Fórmula Médica: Primer Apellido, Segundo Apellido,
        # Primer Nombre, Segundo Nombre.
        tokens += ["", "", "", ""]
        return PersonaRecord(
            tipo_id_corto=tipo_id,
            numero_id=numero_id,
            primer_apellido=tokens[0],
            segundo_apellido=tokens[1],
            primer_nombre=tokens[2],
            segundo_nombre=" ".join(t for t in tokens[3:] if t).strip(),
        )


# =============================================================================
# PAYLOAD Y ENVÍO
# =============================================================================
def build_payload(rec: FormulaMedicaRecord, decision: str, link_pdf: str) -> dict:
    """Arma el payload para el Form correcto según la decisión."""
    _, entries = form_config(decision)
    return {
        entries["numero_prescripcion"]: rec.numero_prescripcion,
        entries["nombre_completo"]:     rec.paciente.nombre_completo,
        entries["numero_identidad"]:    rec.paciente.numero_id,
        entries["tipo_id"]:             rec.paciente.tipo_id_form,
        entries["link_pdf"]:            link_pdf or "",
    }


def submit_via_requests(rec: FormulaMedicaRecord, decision: str,
                        link_pdf: str, *, dry_run: bool = False) -> int:
    form_id, _ = form_config(decision)
    payload = build_payload(rec, decision, link_pdf)
    log.info("Payload con %d campos para Form %s.", len(payload), decision)
    if dry_run:
        for k, v in payload.items():
            log.info("  %s = %r", k, v)
        log.info("DRY-RUN: no se envía.")
        return 0
    action = f"https://docs.google.com/forms/d/e/{form_id}/formResponse"
    headers = {
        "User-Agent": "Mozilla/5.0 (mipres-entrega-bot)",
        "Referer": f"https://docs.google.com/forms/d/e/{form_id}/viewform",
    }
    try:
        resp = requests.post(action, data=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        log.info("Enviado OK a Form %s. HTTP %s", decision, resp.status_code)
        return resp.status_code
    except requests.RequestException as e:
        raise RuntimeError(f"Falló el envío HTTP: {e}") from e


# =============================================================================
# SUBIDA A DRIVE (service account)
# =============================================================================
def upload_pdf_to_drive(pdf_bytes: bytes, filename: str, decision: str) -> str:
    """Sube el PDF a la carpeta de Drive correspondiente y devuelve el link.
    Requiere las variables de entorno de la service account (ver guía).
    Devuelve '' si Drive no está configurado (la app sigue funcionando)."""
    import io
    import os

    folder_aprobados = os.environ.get("DRIVE_FOLDER_APROBADOS", "")
    folder_rechazados = os.environ.get("DRIVE_FOLDER_RECHAZADOS", "")
    folder_id = folder_aprobados if decision == "APROBADO" else folder_rechazados
    if not folder_id:
        log.warning("Carpeta de Drive no configurada para %s; se omite subida.", decision)
        return ""

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError:
        log.warning("Librerías de Google Drive no instaladas; se omite subida.")
        return ""

    # Las credenciales pueden venir de dos formas:
    #  a) GOOGLE_SERVICE_ACCOUNT_JSON = ruta a un archivo .json (local), o
    #  b) GOOGLE_SERVICE_ACCOUNT_JSON = el CONTENIDO del JSON pegado directo
    #     (caso típico en Railway, donde no se suben archivos).
    import json
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON no configurado; se omite subida.")
        return ""

    scopes = ["https://www.googleapis.com/auth/drive.file"]
    try:
        if raw.startswith("{"):
            # Es el contenido JSON pegado directo.
            info = json.loads(raw)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=scopes)
        elif os.path.exists(raw):
            # Es una ruta a un archivo .json.
            creds = service_account.Credentials.from_service_account_file(
                raw, scopes=scopes)
        else:
            log.warning("GOOGLE_SERVICE_ACCOUNT_JSON no es JSON válido ni una ruta existente.")
            return ""
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("No se pudieron cargar las credenciales: %s", e)
        return ""

    service = build("drive", "v3", credentials=creds)

    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf")
    metadata = {"name": filename, "parents": [folder_id]}
    # supportsAllDrives=True permite subir a Unidades compartidas (Shared Drives).
    # Es obligatorio: las service accounts NO tienen cuota propia y solo pueden
    # ser dueñas de archivos dentro de un Shared Drive, no en "Mi unidad".
    file = service.files().create(
        body=metadata, media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True).execute()

    link = file.get("webViewLink", "")
    log.info("PDF subido a Drive: %s", link)
    return link


# =============================================================================
# CLI (para pruebas)
# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="Fórmula Médica PDF -> Form (entrega)")
    ap.add_argument("pdf", help="Ruta al PDF de Fórmula Médica")
    ap.add_argument("--decision", choices=["APROBADO", "RECHAZADO"], required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        rec = FormulaMedicaParser(args.pdf).parse()
    except Exception as e:
        log.error("Error parseando PDF: %s", e)
        sys.exit(2)
    try:
        submit_via_requests(rec, args.decision, link_pdf="", dry_run=args.dry_run)
    except Exception as e:
        log.error("Error enviando: %s", e)
        sys.exit(3)


if __name__ == "__main__":
    main()
