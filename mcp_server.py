"""
mcp_server.py

MCP server locale per gestione Ordini / info commerciali via REST.

- Legge configurazione dei servizi REST da un file XML.
- Espone due tool MCP:
    1) list_rest_services() -> lista dei servizi e dei parametri (con schema JSON annidato opzionale)
    2) call_rest_service(service_name, arguments) -> chiama il REST corrispondente
"""

import logging
import os
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
import xml.etree.ElementTree as ET

from mcp.server.fastmcp import FastMCP, Context

# ===================== LOGGING =====================

logger = logging.getLogger("orders_mcp_server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)


# ==================== CONFIG DATA CLASSES =====================

@dataclass
class JsonSchemaField:
    """
    Descrive un campo JSON annidato per i parametri di tipo object/array.

    Esempi:
        - object con sotto-campi: type="object", fields=[...]
        - array di oggetti:       type="array",  items=JsonSchemaField(...)
    """
    name: Optional[str] = None
    type: str = "string"   # string, int, bool, object, array, ...
    required: bool = False
    fields: List["JsonSchemaField"] = field(default_factory=list)  # per object
    items: Optional["JsonSchemaField"] = None                      # per array


@dataclass
class ParamConfig:
    """Descrive un singolo parametro di un servizio REST."""

    name: str
    required: bool
    location: str   # "path" | "query" | "body"
    type: str       # descrittivo
    schema: Optional[JsonSchemaField] = None  # opzionale, solo per JSON complessi


@dataclass
class HeaderConfig:
    """Descrive un header HTTP configurabile per un servizio REST."""

    name: str
    value: Optional[str] = None   # valore letterale da XML
    env: Optional[str] = None     # nome di variabile d'ambiente da cui leggere il valore


@dataclass
class ServiceConfig:
    """Descrive un singolo servizio REST (endpoint) configurato via XML."""

    name: str
    method: str
    path: str
    params: List[ParamConfig] = field(default_factory=list)
    headers: List[HeaderConfig] = field(default_factory=list)
    base_url_override: Optional[str] = None


@dataclass
class RestConfig:
    """Configurazione globale: base URL + dizionario dei servizi."""

    base_url: str
    services: Dict[str, ServiceConfig]


# ===================== PARSING XML (schema compreso) =====================

def _parse_json_field(field_el: ET.Element) -> JsonSchemaField:
    """
    Parsifica ricorsivamente un <Field> (e l'eventuale <Item>) in JsonSchemaField.

    Esempio:

      <Field name="Indirizzi" type="array" required="true">
        <Item type="object">
          <Field name="NumProgr" type="int" required="true"/>
          ...
        </Item>
      </Field>
    """
    name = field_el.attrib.get("name")
    f_type = field_el.attrib.get("type", "string")
    required = (field_el.attrib.get("required", "false").lower() == "true")

    field_schema = JsonSchemaField(
        name=name,
        type=f_type,
        required=required,
        fields=[],
        items=None,
    )

    # Se è un object, guarda eventuali sotto-Field
    if f_type == "object":
        for child_field_el in field_el.findall("Field"):
            field_schema.fields.append(_parse_json_field(child_field_el))

    # Se è un array, guarda l'eventuale <Item>
    if f_type == "array":
        item_el = field_el.find("Item")
        if item_el is not None:
            item_type = item_el.attrib.get("type", "object")
            item_schema = JsonSchemaField(
                name=None,
                type=item_type,
                required=False,
                fields=[],
                items=None,
            )
            if item_type == "object":
                for child_field_el in item_el.findall("Field"):
                    item_schema.fields.append(_parse_json_field(child_field_el))
            field_schema.items = item_schema

    return field_schema


def _parse_json_schema_from_param(param_el: ET.Element, param_type: str) -> Optional[JsonSchemaField]:
    """
    Se il <Param> contiene sotto-elementi <Field>, costruisce uno schema JSON
    strutturato (JsonSchemaField). Altrimenti restituisce None.

    Esempio XML:

      <Param name="Parametri" required="true" location="body" type="object">
        <Field name="TipoAnagrafica" type="int" required="true"/>
        <Field name="Indirizzi" type="array" required="true">
          <Item type="object">
            <Field name="NumProgr" type="int" required="true"/>
            ...
          </Item>
        </Field>
      </Param>
    """
    field_els = list(param_el.findall("Field"))
    if not field_els:
        # Nessuno schema annidato definito
        return None

    root_type = param_type or "object"
    root = JsonSchemaField(
        name=None,
        type=root_type,
        required=False,
        fields=[],
        items=None,
    )

    for field_el in field_els:
        root.fields.append(_parse_json_field(field_el))

    return root


def load_rest_config_from_xml(path: str) -> RestConfig:
    """
    Carica la configurazione REST da un file XML.

    Root attesa:
    <RestServices baseUrl="http://127.0.0.1:8001">
      <Service .>
        <Header . />
        <Param . />   (eventualmente con Field/Item per lo schema annidato)
      </Service>
      .
    </RestServices>
    """
    if not os.path.isfile(path):
        raise ValueError(f"File XML non trovato: {path}")

    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as exc:
        raise ValueError(f"Errore nel parsing XML '{path}': {exc}") from exc

    if root.tag != "RestServices":
        raise ValueError("Root XML deve essere <RestServices>")

    base_url = root.attrib.get("baseUrl", "").rstrip("/")
    if not base_url:
        raise ValueError("Attributo baseUrl mancante su <RestServices>")

    services: Dict[str, ServiceConfig] = {}

    for svc_el in root.findall("Service"):
        name = svc_el.attrib.get("name")
        method = (svc_el.attrib.get("method") or "GET").upper()
        path_attr = svc_el.attrib.get("path", "")

        if not name or not path_attr:
            logger.warning("Service senza name/path nel file XML, ignorato: %s", svc_el.attrib)
            continue

        # baseUrlOverride opzionale sul singolo service
        base_url_override = svc_el.attrib.get("baseUrlOverride")

        # Parametri
        params_cfg: List[ParamConfig] = []
        for param_el in svc_el.findall("Param"):
            p_name = param_el.attrib.get("name")
            if not p_name:
                logger.warning("Param senza name nel service '%s', ignorato", name)
                continue

            required = (param_el.attrib.get("required", "false").lower() == "true")
            location = (param_el.attrib.get("location") or "query").lower()
            if location not in {"path", "query", "body"}:
                logger.warning(
                    "location '%s' non valida per param '%s' in service '%s', uso 'query' di default",
                    location,
                    p_name,
                    name,
                )
                location = "query"

            p_type = param_el.attrib.get("type", "string")

            # NUOVO: se nel Param ci sono <Field>, costruisco lo schema JSON annidato
            schema = _parse_json_schema_from_param(param_el, p_type)

            params_cfg.append(
                ParamConfig(
                    name=p_name,
                    required=required,
                    location=location,
                    type=p_type,
                    schema=schema,
                )
            )

        # Header configurabili
        headers_cfg: List[HeaderConfig] = []
        for hdr_el in svc_el.findall("Header"):
            h_name = hdr_el.attrib.get("name")
            if not h_name:
                logger.warning("Header senza name nel service '%s', ignorato", name)
                continue

            h_value = hdr_el.attrib.get("value")
            h_env = hdr_el.attrib.get("env")

            headers_cfg.append(
                HeaderConfig(
                    name=h_name,
                    value=h_value,
                    env=h_env,
                )
            )

        if name in services:
            logger.warning("Service duplicato '%s' nel file XML, sovrascrivo il precedente", name)

        services[name] = ServiceConfig(
            name=name,
            method=method,
            path=path_attr,
            params=params_cfg,
            headers=headers_cfg,
            base_url_override=base_url_override,
        )

    if not services:
        logger.warning("Nessun service definito nel file XML '%s'", path)

    logger.info("Caricata REST config: base_url=%s, services=%d", base_url, len(services))
    return RestConfig(base_url=base_url, services=services)


# ===================== MCP SERVER (FastMCP) =====================

REST_XML_PATH = os.getenv("ORDERS_REST_XML_PATH", "my_services.xml")

try:
    REST_CONFIG = load_rest_config_from_xml(REST_XML_PATH)
    logger.info("Servizi MCP disponibili: %s", list(REST_CONFIG.services.keys()))
except Exception as exc:
    logger.exception("Impossibile caricare la configurazione REST: %s", exc)
    raise

mcp = FastMCP("OrdersMCPServer")


@mcp.tool()
def list_rest_services() -> list[dict]:
    """
    Restituisce la lista dei servizi REST configurati, con i relativi parametri,
    headers e (se presente) lo schema JSON annidato dei parametri body.
    """

    def _field_to_dict(f: JsonSchemaField) -> dict:
        data: dict[str, Any] = {
            "name": f.name,
            "type": f.type,
            "required": f.required,
        }
        if f.fields:
            data["fields"] = [_field_to_dict(ch) for ch in f.fields]
        if f.items is not None:
            data["items"] = {
                "type": f.items.type,
                "required": f.items.required,
            }
            if f.items.fields:
                data["items"]["fields"] = [
                    _field_to_dict(ch) for ch in f.items.fields
                ]
        return data

    def _schema_to_dict(root: Optional[JsonSchemaField]) -> Optional[dict]:
        if root is None:
            return None

        out: dict[str, Any] = {
            "type": root.type,
        }
        if root.fields:
            out["fields"] = [_field_to_dict(ch) for ch in root.fields]
        if root.items is not None:
            out["items"] = {
                "type": root.items.type,
                "required": root.items.required,
            }
            if root.items.fields:
                out["items"]["fields"] = [
                    _field_to_dict(ch) for ch in root.items.fields
                ]
        return out

    items: list[dict] = []
    for svc in REST_CONFIG.services.values():
        items.append(
            {
                "name": svc.name,
                "method": svc.method,
                "path": svc.path,
                "base_url_override": svc.base_url_override,
                "params": [
                    {
                        "name": p.name,
                        "required": p.required,
                        "location": p.location,
                        "type": p.type,
                        "schema": _schema_to_dict(p.schema),
                    }
                    for p in svc.params
                ],
                "headers": [
                    {
                        "name": h.name,
                        "value": h.value,
                        "env": h.env,
                    }
                    for h in svc.headers
                ],
            }
        )
    return items


@mcp.tool()
async def call_rest_service(
    service_name: str,
    arguments: dict,
    ctx: Context,
) -> dict:
    """
    Chiama un servizio REST configurato via XML.

    :param service_name: nome logico del service (attributo 'name' nel file XML).
    :param arguments: dizionario con i parametri (chiave = nome param).
    :param ctx: contesto MCP.
    """

    logger.info("call_rest_service: service_name=%s, arguments=%s", service_name, arguments)

    svc = REST_CONFIG.services.get(service_name)
    if not svc:
        err = f"Servizio '{service_name}' non definito nella configurazione."
        logger.error(err)
        return {
            "ok": False,
            "status_code": None,
            "service": service_name,
            "request": {},
            "response_json": None,
            "response_text": None,
            "error": err,
        }

    # Validazione parametri richiesti
    missing_required: list[str] = []
    for p in svc.params:
        if p.required and p.name not in arguments:
            missing_required.append(p.name)

    if missing_required:
        err = f"Parametri obbligatori mancanti per service '{service_name}': {', '.join(missing_required)}"
        logger.warning(err)
        return {
            "ok": False,
            "status_code": None,
            "service": service_name,
            "request": {
                "missing_required_params": missing_required,
            },
            "response_json": None,
            "response_text": None,
            "error": err,
        }

    # Prepara URL, query, body e header
    base_url = (svc.base_url_override or REST_CONFIG.base_url).rstrip("/")
    url = base_url + svc.path
    query_params: dict[str, Any] = {}
    body_payload: dict[str, Any] = {}

    # 1) Parametri path: rimpiazza {name} nel path
    for p in svc.params:
        if p.location == "path" and p.name in arguments:
            placeholder = "{" + p.name + "}"
            value = str(arguments[p.name])
            url = url.replace(placeholder, value)

    # 2) Parametri query
    for p in svc.params:
        if p.location == "query" and p.name in arguments:
            query_params[p.name] = arguments[p.name]

    # 3) Parametri body
    for p in svc.params:
        if p.location == "body" and p.name in arguments:
            body_payload[p.name] = arguments[p.name]

    # 4) Header (da configurazione + variabili d'ambiente)
    headers: dict[str, str] = {}
    for h in svc.headers:
        # Se è definita 'env', prova a leggerla dalle variabili d'ambiente
        if h.env:
            env_val = os.getenv(h.env)
            if env_val:
                headers[h.name] = env_val

        # Se c'è un valore letterale, usalo se non già impostato
        if h.value and h.name not in headers:
            headers[h.name] = h.value

    # Header di default: Accept: application/json se non specificato
    if "Accept" not in headers:
        headers["Accept"] = "application/json"

    method = svc.method.upper()
    timeout = httpx.Timeout(10.0, connect=5.0)

    # Costruzione URL completo per il CURL (con query string)
    if query_params:
        qs = urlencode(query_params, doseq=True)
        full_url = f"{url}?{qs}"
    else:
        full_url = url

    # JSON del body per il CURL
    body_json_str: Optional[str] = None
    if body_payload:
        body_json_str = json.dumps(body_payload, ensure_ascii=False)

    # Costruisco il CURL equivalente
    curl_parts: list[str] = [f"curl -X {method} '{full_url}'"]

    for h_name, h_value in headers.items():
        # Se vuoi mascherare i token, puoi intervenire qui
        curl_parts.append(f"-H '{h_name}: {h_value}'")

    if body_json_str is not None:
        curl_parts.append(f"-d '{body_json_str}'")

    curl_cmd = curl_parts[0]
    if len(curl_parts) > 1:
        curl_cmd += " \\\n  " + " \\\n  ".join(curl_parts[1:])

    logger.info("CURL equivalente per '%s':\n%s", service_name, curl_cmd)

    request_info = {
        "method": method,
        "url": url,
        "query": query_params if query_params else None,
        "body": body_payload if body_payload else None,
        "headers": headers or None,
        "curl": curl_cmd,
    }

    logger.info("Effettuo richiesta REST: %s", request_info)
    await ctx.report_progress(0, 1)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                resp = await client.get(
                    url,
                    params=query_params,
                    headers=headers or None,
                )
            elif method == "POST":
                resp = await client.post(
                    url,
                    params=query_params,
                    json=body_payload or None,
                    headers=headers or None,
                )
            elif method == "PUT":
                resp = await client.put(
                    url,
                    params=query_params,
                    json=body_payload or None,
                    headers=headers or None,
                )
            elif method == "DELETE":
                resp = await client.delete(
                    url,
                    params=query_params,
                    json=body_payload or None,
                    headers=headers or None,
                )
            else:
                err = f"Metodo HTTP '{method}' non supportato dal server MCP."
                logger.error(err)
                return {
                    "ok": False,
                    "status_code": None,
                    "service": service_name,
                    "request": request_info,
                    "response_json": None,
                    "response_text": None,
                    "error": err,
                }

        # ===== LOG RISPOSTA =====
        response_text = resp.text
        try:
            response_json = resp.json()
        except Exception:
            response_json = None

        max_len = 2000  # per non inondare il log
        if response_json is not None:
            try:
                pretty_json = json.dumps(response_json, ensure_ascii=False)
            except Exception:
                pretty_json = str(response_json)
            snippet = pretty_json if len(pretty_json) <= max_len else pretty_json[:max_len] + ". [troncato]"
            logger.info(
                "Risposta REST per '%s' (status %s, JSON): %s",
                service_name,
                resp.status_code,
                snippet,
            )
        else:
            snippet = response_text if len(response_text) <= max_len else response_text[:max_len] + ". [troncato]"
            logger.info(
                "Risposta REST per '%s' (status %s, text): %s",
                service_name,
                resp.status_code,
                snippet,
            )
        # ========================

        ok = 200 <= resp.status_code < 300

        return {
            "ok": ok,
            "status_code": resp.status_code,
            "service": service_name,
            "request": request_info,
            "response_json": response_json,
            "response_text": response_text,
            "error": None if ok else f"HTTP {resp.status_code}",
        }

    except httpx.RequestError as exc:
        err = f"Errore di rete verso il servizio '{service_name}': {exc}"
        logger.error(err)
        return {
            "ok": False,
            "status_code": None,
            "service": service_name,
            "request": request_info,
            "response_json": None,
            "response_text": None,
            "error": err,
        }
    except Exception as exc:
        err = f"Errore imprevisto nella chiamata a '{service_name}': {exc}"
        logger.exception(err)
        return {
            "ok": False,
            "status_code": None,
            "service": service_name,
            "request": request_info,
            "response_json": None,
            "response_text": None,
            "error": err,
        }


# ===================== ENTRYPOINT STDIO =====================

if __name__ == "__main__":
    logger.info("Avvio OrdersMCPServer in modalità stdio")
    mcp.run()
