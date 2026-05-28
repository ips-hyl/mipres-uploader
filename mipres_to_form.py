"""
MIPRES PDF  ->  Google Form de Solicitud de Tecnología en Salud
================================================================
Versión extendida: extrae ~20 campos del PDF de prescripción y los manda al
Google Form de Solicitud (28 preguntas, una fila por caso en la Sheet).

Campos hardcodeados en el payload (no se preguntan al empleado):
  - ¿Enfermedad huérfana?  → "No"
  - ¿Caso COVID-19?        → "No"

Campos pedidos al empleado en el preview de la app:
  - Ámbito (priorizado | no priorizado)  → arma "Ambulatorio - <choice>"

Campos auto-extraídos del PDF:
  - Solicitud (n°, fecha)
  - IPS (código, municipio, razón social)
  - Médico (nombre completo, cédula)
  - Paciente (tipo doc, núm doc, 4 fragmentos de nombre)
  - Diagnóstico principal y hasta 2 relacionados (CIE-10 + descripción)
  - Tecnología (tipo, nombre, forma, vía, dosis, frecuencia, duración,
                cantidad, justificación)
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
log = logging.getLogger("mipres")


# =============================================================================
# CONFIGURACIÓN DEL FORMULARIO
# =============================================================================
PUBLIC_FORM_ID = "1FAIpQLSfftbwb6LqZKVy4xCeMqdhhve-AYy-Wqzw1g5zw8m6e70Ec0A"
FORM_ACTION_URL = (
    f"https://docs.google.com/forms/d/e/{PUBLIC_FORM_ID}/formResponse"
)

# Cómo obtener los entry IDs: editor del form → menú 3 puntos → "Obtener
# enlace prerrellenado" → llena cada campo con un valor único (SEDE_TEST,
# 111111, NOMBRE_TEST, etc.) → "Obtener enlace" → cada entry.XXX de la URL
# resultante te dice a qué campo corresponde.
ENTRY_IDS: dict[str, str] = {
    # --- los 5 críticos visibles en el preview ---
    "sede":                "entry.652302048",
    "numero_prescripcion": "entry.1288285391",
    "tipo_id":             "entry.490257702",
    "numero_identidad":    "entry.868885480",
    # nombres del paciente (el form los pide separados)
    "primer_nombre":       "entry.781080235",
    "segundo_nombre":      "entry.2072142761",
    "primer_apellido":     "entry.767012250",
    "segundo_apellido":    "entry.669624260",
    # --- el campo que sí pide elección al empleado ---
    "ambito_atencion":     "entry.398677970",
    # --- hardcodeados ---
    "enfermedad_huerfana": "entry.131110802",
    "caso_covid":          "entry.2093491339",
    # --- avanzados (auto, colapsados en preview) ---
    "fecha_prescripcion":  "entry.17977625",
    "medico_nombre":       "entry.1652705970",
    "medico_cedula":       "entry.653133533",
    "cie10_principal":     "entry.2041481629",
    "diag_principal_desc": "entry.1990289046",
    "cie10_relacionado_1": "entry.1733932910",
    "cie10_relacionado_2": "entry.1542790094",
    "tipo_tecnologia":     "entry.1304745359",
    "nombre_producto":     "entry.357641503",
    "forma":               "entry.1667943205",
    "via_administracion":  "entry.1023808841",
    "dosis":               "entry.2122264111",
    "frecuencia":          "entry.241737275",
    "duracion":            "entry.1828900183",
    "cantidad_total":      "entry.704094637",
    "justificacion":       "entry.99798445",
}


# =============================================================================
# MODELO DE DATOS
# =============================================================================
@dataclass
class PersonaRecord:
    """Médico o paciente."""
    tipo_id_raw: str = ""
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
    def tipo_id_corto(self) -> str:
        raw = self.tipo_id_raw.lower()
        norm = (raw.replace("á", "a").replace("é", "e").replace("í", "i")
                   .replace("ó", "o").replace("ú", "u").replace("ñ", "n"))
        mapping = {
            "cedula de ciudadania":            "CC",
            "cedula de extranjeria":           "CE",
            "permiso por proteccion temporal": "PT",
            "permiso de proteccion temporal":  "PT",
            "registro civil":                  "RC",
            "tarjeta de identidad":            "TI",
            "pasaporte":                       "PA",
            "menor sin identificacion":        "MS",
            "adulto sin identificacion":       "AS",
        }
        for k, v in mapping.items():
            if k in norm:
                return v
        return ""


@dataclass
class DiagnosticoRecord:
    descripcion: str = ""
    cie10: str = ""

    @property
    def existe(self) -> bool:
        return bool(self.cie10 or self.descripcion)


@dataclass
class TecnologiaRecord:
    """Datos del producto, medicamento, procedimiento, etc. prescrito."""
    tipo: str = ""
    nombre: str = ""
    forma: str = ""
    via_administracion: str = ""
    dosis: str = ""
    frecuencia: str = ""
    duracion: str = ""
    cantidad_total: str = ""
    justificacion: str = ""
    indicaciones: str = ""


@dataclass
class MipresRecord:
    """Registro completo de una prescripción MIPRES."""
    numero_solicitud: str = ""
    fecha_prescripcion: str = ""
    codigo_ips: str = ""
    municipio: str = ""
    razon_social_ips: str = ""
    medico: PersonaRecord = field(default_factory=PersonaRecord)
    paciente: PersonaRecord = field(default_factory=PersonaRecord)
    diag_principal: DiagnosticoRecord = field(default_factory=DiagnosticoRecord)
    diag_relacionado_1: DiagnosticoRecord = field(default_factory=DiagnosticoRecord)
    diag_relacionado_2: DiagnosticoRecord = field(default_factory=DiagnosticoRecord)
    tecnologia: TecnologiaRecord = field(default_factory=TecnologiaRecord)

    @property
    def sede_form_value(self) -> str:
        mapping = {
            "660010233201": "660010233201 - PEREIRA",
            "761470934201": "761470934201 - CARTAGO",
        }
        if self.codigo_ips not in mapping:
            raise ValueError(f"Código IPS no reconocido: {self.codigo_ips!r}")
        return mapping[self.codigo_ips]


# =============================================================================
# PARSER
# =============================================================================
TIPOS_DOC_CONOCIDOS = [
    "Cédula de ciudadanía",
    "Cédula de extranjería",
    "Permiso por Protección Temporal",
    "Permiso Especial de Permanencia",
    "Registro civil",
    "Tarjeta de identidad",
    "Pasaporte",
]

SECCIONES_TECNOLOGIA = {
    "MEDICAMENTOS":                       "Medicamento",
    "PROCEDIMIENTOS":                     "Procedimiento",
    "DISPOSITIVOS MÉDICOS":               "Dispositivo Médico",
    "PRODUCTOS PARA SOPORTE NUTRICIONAL": "Producto para Soporte Nutricional",
    "SERVICIOS COMPLEMENTARIOS":          "Servicio Complementario",
}


class MipresPdfParser:
    """Parser tolerante para reportes MIPRES; extrae datos clínicos y admin."""

    def __init__(self, pdf_path):
        self.pdf_path = Path(pdf_path)
        if not self.pdf_path.exists():
            raise FileNotFoundError(f"PDF no encontrado: {self.pdf_path}")

    # ------------------------------------------------------------------ API
    def parse(self) -> MipresRecord:
        text, tables = self._extract_text_and_tables()
        record = MipresRecord()

        record.numero_solicitud = self._numero_solicitud(text)
        record.fecha_prescripcion = self._fecha(text)
        record.codigo_ips, record.municipio, record.razon_social_ips = self._ips(text)

        record.medico = self._persona(
            text, "DATOS DEL MÉDICO PRESCRIPTOR", "DATOS DEL PACIENTE")
        record.paciente = self._persona(
            text, "DATOS DEL PACIENTE", "AMBITO DE ATENCIÓN")

        diags = self._diagnosticos(text)
        if len(diags) >= 1: record.diag_principal = diags[0]
        if len(diags) >= 2: record.diag_relacionado_1 = diags[1]
        if len(diags) >= 3: record.diag_relacionado_2 = diags[2]

        record.tecnologia = self._tecnologia(text, tables)

        log.info(
            "Parseado OK: solicitud=%s | paciente=%s (%s %s) | tec=%s/%s",
            record.numero_solicitud,
            record.paciente.nombre_completo,
            record.paciente.tipo_id_corto, record.paciente.numero_id,
            record.tecnologia.tipo, record.tecnologia.nombre[:40],
        )
        return record

    # ------------------------------------------------------------- helpers
    def _extract_text_and_tables(self):
        try:
            with pdfplumber.open(self.pdf_path) as pdf:
                text_pages = [p.extract_text() or "" for p in pdf.pages]
                table_pages = [p.extract_tables() or [] for p in pdf.pages]
        except Exception as e:
            raise RuntimeError(f"No se pudo leer el PDF: {e}") from e
        full_text = "\n".join(text_pages)
        all_tables = [t for page_tables in table_pages for t in page_tables]
        if not full_text.strip():
            raise RuntimeError("El PDF se abrió pero no tiene texto extraíble.")
        return full_text, all_tables

    @staticmethod
    def _numero_solicitud(text):
        m = re.search(
            r"Número\s+de\s+Solicitud[^\n]*\n\s*(\d{15,25})",
            text, flags=re.IGNORECASE)
        if not m:
            raise ValueError("No se encontró 'Número de Solicitud'.")
        return m.group(1)

    @staticmethod
    def _fecha(text):
        m = re.search(r"\d{15,}\s+(\d{4}-\d{2}-\d{2})\s+\d{1,2}:\d{2}", text)
        return m.group(1) if m else ""

    @staticmethod
    def _ips(text):
        codigo = ""
        m_cod = re.search(r"(\d{12})\s+IPS", text)
        if not m_cod:
            m_cod = re.search(
                r"Código:[^\n]*\n[^\n]*?(\d{12})",
                text, flags=re.IGNORECASE | re.DOTALL)
        if m_cod:
            codigo = m_cod.group(1)
        razon = ""
        m_razon = re.search(r"\d{12}\s+(.+?)\n", text)
        if m_razon:
            razon = m_razon.group(1).strip()
        municipio = ""
        m_mun = re.search(
            r"Departamento:\s*Municipio:\s*\n([A-ZÁÉÍÓÚÑa-záéíóúñ ]+)", text)
        if m_mun:
            partes = m_mun.group(1).strip().split()
            if len(partes) > 1:
                municipio = " ".join(partes[1:])
        if not codigo:
            raise ValueError("No se encontró el Código de IPS (12 dígitos).")
        return codigo, municipio, razon

    @classmethod
    def _persona(cls, text, start, end):
        bloque = cls._slice_between(text, start, end)
        if not bloque:
            raise ValueError(f"Bloque no encontrado: {start!r} → {end!r}.")
        for tipo in TIPOS_DOC_CONOCIDOS:
            m = re.search(
                rf"^\s*({re.escape(tipo)})\s+(.+)$",
                bloque, flags=re.IGNORECASE | re.MULTILINE)
            if m:
                tipo_match, valores = m.group(1), m.group(2).strip()
                break
        else:
            raise ValueError(f"Línea de valores no identificada en {start!r}.")
        tokens = valores.split()
        if len(tokens) < 2 or not tokens[0].isdigit():
            raise ValueError(f"Línea inválida en {start!r}: {valores!r}")
        nombres = tokens[1:] + ["", "", "", ""]
        return PersonaRecord(
            tipo_id_raw=tipo_match,
            numero_id=tokens[0],
            primer_apellido=nombres[0],
            segundo_apellido=nombres[1],
            primer_nombre=nombres[2],
            segundo_nombre=" ".join(nombres[3:]).strip(),
        )

    @staticmethod
    def _diagnosticos(text):
        """Cada diagnóstico: 'Diagnóstico XXX: <descripción> <CIE10>'."""
        diags = []
        labels = [
            r"Diagn[óo]stico\s+Principal:\s*(.+)",
            r"Diagn[óo]stico\s+Relacionado\s+1:\s*(.+)",
            r"Diagn[óo]stico\s+Relacionado\s+2:\s*(.+)",
        ]
        for pat in labels:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if not m:
                diags.append(DiagnosticoRecord())
                continue
            linea = m.group(1).strip()
            if not linea:
                diags.append(DiagnosticoRecord())
                continue
            m_cie = re.search(r"\s+([A-Z]\d{2,4})\s*$", linea)
            if m_cie:
                cie = m_cie.group(1)
                desc = linea[:m_cie.start()].strip().rstrip(",")
            else:
                cie, desc = "", linea
            diags.append(DiagnosticoRecord(descripcion=desc, cie10=cie))
        return diags

    @staticmethod
    def _tecnologia(text, tables):
        """Detecta qué sección tiene >0 registros y extrae los datos
        de la tabla correspondiente. Funciona para las 5 secciones porque
        las columnas relevantes están en posiciones similares."""
        tipo_detectado = ""
        for seccion, label in SECCIONES_TECNOLOGIA.items():
            m = re.search(
                rf"{re.escape(seccion)}\s*\n\s*(\d+)\s+Registro\(s\)", text)
            if m and int(m.group(1)) > 0:
                tipo_detectado = label
                break
        if not tipo_detectado:
            log.warning("Ninguna sección de tecnología tiene registros.")
            return TecnologiaRecord()
        # Buscamos la primera tabla con >=10 columnas (la del producto).
        tabla_producto = None
        for t in tables:
            if t and len(t[0]) >= 10:
                tabla_producto = t
                break
        if not tabla_producto or len(tabla_producto) < 2:
            log.warning("Tabla de producto no encontrada.")
            return TecnologiaRecord(tipo=tipo_detectado)
        headers = [(h or "").replace("\n", " ").strip().lower()
                   for h in tabla_producto[0]]
        valores = [(v or "").replace("\n", " ").strip()
                   for v in tabla_producto[1]]

        def col(*keywords, exclude=()):
            """Devuelve el valor de la primera columna cuyo header contiene
            alguno de los `keywords` y ninguno de los `exclude`."""
            for kw in keywords:
                for i, h in enumerate(headers):
                    if kw in h and not any(ex in h for ex in exclude):
                        return valores[i] if i < len(valores) else ""
            return ""

        return TecnologiaRecord(
            tipo=tipo_detectado,
            # La columna "Producto" se distingue de "Tipo de Producto" excluyendo
            # headers que arrancan con "tipo de".
            nombre=col("producto para", "medicamento", "procedimiento",
                       "dispositivo", "servicio", exclude=("tipo de",)),
            forma=col("forma"),
            via_administracion=col("vía", "via"),
            dosis=col("dosis"),
            frecuencia=col("frecuencia"),
            duracion=col("duración", "duracion"),
            cantidad_total=col("cantidad"),
            justificacion=col("justificación", "justificacion"),
            indicaciones=col("indicaciones/recomendaciones",
                             "indicaciones / recomendaciones"),
        )

    @staticmethod
    def _slice_between(text, start, end):
        i = text.find(start)
        if i < 0:
            return ""
        j = text.find(end, i + len(start))
        return text[i + len(start): j if j >= 0 else len(text)]


# =============================================================================
# PAYLOAD Y ENVÍO
# =============================================================================
def build_payload(record: MipresRecord, ambito_priorizacion: str) -> dict:
    """Arma el payload final. `ambito_priorizacion` viene del preview."""
    if ambito_priorizacion not in ("priorizado", "no priorizado"):
        raise ValueError(
            f"ambito_priorizacion inválido: {ambito_priorizacion!r}. "
            "Esperado 'priorizado' o 'no priorizado'.")
    ambito_completo = f"Ambulatorio - {ambito_priorizacion}"

    return {
        ENTRY_IDS["sede"]:                record.sede_form_value,
        ENTRY_IDS["numero_prescripcion"]: record.numero_solicitud,
        ENTRY_IDS["tipo_id"]:             record.paciente.tipo_id_corto,
        ENTRY_IDS["numero_identidad"]:    record.paciente.numero_id,
        ENTRY_IDS["primer_nombre"]:       record.paciente.primer_nombre,
        ENTRY_IDS["segundo_nombre"]:      record.paciente.segundo_nombre,
        ENTRY_IDS["primer_apellido"]:     record.paciente.primer_apellido,
        ENTRY_IDS["segundo_apellido"]:    record.paciente.segundo_apellido,
        ENTRY_IDS["ambito_atencion"]:     ambito_completo,
        ENTRY_IDS["enfermedad_huerfana"]: "No",
        ENTRY_IDS["caso_covid"]:          "No",
        ENTRY_IDS["fecha_prescripcion"]:  record.fecha_prescripcion,
        ENTRY_IDS["medico_nombre"]:       record.medico.nombre_completo,
        ENTRY_IDS["medico_cedula"]:       record.medico.numero_id,
        ENTRY_IDS["cie10_principal"]:     record.diag_principal.cie10,
        ENTRY_IDS["diag_principal_desc"]: record.diag_principal.descripcion,
        ENTRY_IDS["cie10_relacionado_1"]: record.diag_relacionado_1.cie10,
        ENTRY_IDS["cie10_relacionado_2"]: record.diag_relacionado_2.cie10,
        ENTRY_IDS["tipo_tecnologia"]:     record.tecnologia.tipo,
        ENTRY_IDS["nombre_producto"]:     record.tecnologia.nombre,
        ENTRY_IDS["forma"]:               record.tecnologia.forma,
        ENTRY_IDS["via_administracion"]:  record.tecnologia.via_administracion,
        ENTRY_IDS["dosis"]:               record.tecnologia.dosis,
        ENTRY_IDS["frecuencia"]:          record.tecnologia.frecuencia,
        ENTRY_IDS["duracion"]:            record.tecnologia.duracion,
        ENTRY_IDS["cantidad_total"]:      record.tecnologia.cantidad_total,
        ENTRY_IDS["justificacion"]:       record.tecnologia.justificacion,
    }


def _check_entry_ids_configured() -> None:
    pendientes = [k for k, v in ENTRY_IDS.items() if "PENDIENTE" in v]
    if pendientes:
        raise RuntimeError(
            f"Hay {len(pendientes)} entry IDs sin configurar: {pendientes[:3]}..."
            " Aplicalos en ENTRY_IDS antes de enviar.")


def submit_via_requests(record, ambito_priorizacion, *, dry_run=False):
    payload = build_payload(record, ambito_priorizacion)
    log.info("Payload con %d campos.", len(payload))
    if dry_run:
        for k, v in payload.items():
            log.info("  %s = %r", k, v[:80] if isinstance(v, str) else v)
        log.info("DRY-RUN activado, no se envía la petición.")
        return 0
    _check_entry_ids_configured()
    if PUBLIC_FORM_ID.startswith("REEMPLAZAR"):
        raise RuntimeError("PUBLIC_FORM_ID sin configurar.")
    headers = {
        "User-Agent": "Mozilla/5.0 (mipres-bot)",
        "Referer": f"https://docs.google.com/forms/d/e/{PUBLIC_FORM_ID}/viewform",
    }
    try:
        resp = requests.post(FORM_ACTION_URL, data=payload,
                             headers=headers, timeout=30)
        resp.raise_for_status()
        log.info("Enviado OK. HTTP %s", resp.status_code)
        return resp.status_code
    except requests.RequestException as e:
        raise RuntimeError(f"Falló el envío HTTP: {e}") from e


# =============================================================================
# CLI
# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="MIPRES PDF -> Google Form (Solicitud)")
    ap.add_argument("pdf", help="Ruta al PDF MIPRES")
    ap.add_argument("--ambito", choices=["priorizado", "no priorizado"],
                    help="Priorización del caso")
    ap.add_argument("--dry-run", action="store_true",
                    help="Solo parsea y muestra el payload, no envía.")
    args = ap.parse_args()
    try:
        record = MipresPdfParser(args.pdf).parse()
    except Exception as e:
        log.error("Error parseando PDF: %s", e)
        sys.exit(2)
    ambito = args.ambito or "no priorizado"
    if not args.dry_run and not args.ambito:
        log.error("Para envío real hay que pasar --ambito")
        sys.exit(1)
    try:
        submit_via_requests(record, ambito, dry_run=args.dry_run)
    except Exception as e:
        log.error("Error enviando: %s", e)
        sys.exit(3)


if __name__ == "__main__":
    main()
