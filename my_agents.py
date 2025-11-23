import logging
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from agents import Agent
from agents.mcp import MCPServerStdio

logger = logging.getLogger(__name__)


class AgentsConfigError(Exception):
    """Errore nella configurazione degli agent (file XML)."""
    pass


# Configurazione caricata da XML (lazy, solo al primo utilizzo)
# Struttura:
# {
#   "orders": {
#       "name": "OrdersAgent",
#       "description": "...",
#       "instructions": "..."
#   },
#   "customers": {
#       "name": "CustomersAgent",
#       ...
#   },
# }
_AGENTS_BY_ID: Optional[Dict[str, Dict[str, str]]] = None


def _load_agents_from_xml() -> Dict[str, Dict[str, str]]:
    """
    Legge la configurazione degli agent da my_agents.xlm (o, in fallback,
    my_agents.xml) e restituisce un dizionario indicizzato per 'id'.
    """
    primary = Path("my_agents.xml")

    path: Optional[Path] = None

    if primary.exists():
        path = primary

    if path is None:
        raise AgentsConfigError(
            "File XML degli agent non trovato. "
            f"Ho cercato: {primary.resolve()}"
        )

    tree = ET.parse(path)
    root = tree.getroot()

    agents_by_id: Dict[str, Dict[str, str]] = {}

    for agent_el in root.findall("Agent"):
        agent_id = (agent_el.get("id") or "").strip()
        name = (agent_el.get("name") or "").strip()

        if not agent_id:
            logger.warning(
                "Trovato un <Agent> senza attributo 'id' nel file %s",
                path,
            )
            continue

        desc_el = agent_el.find("Description")
        instr_el = agent_el.find("Instructions")

        description = (desc_el.text or "").strip() if desc_el is not None else ""
        instructions = (instr_el.text or "").strip() if instr_el is not None else ""

        if not instructions:
            logger.warning(
                "L'Agent con id='%s' nel file %s non ha istruzioni (Instructions vuoto).",
                agent_id,
                path,
            )

        agents_by_id[agent_id] = {
            "name": name,  # etichetta "umana", opzionale
            "description": description,
            "instructions": instructions,
        }

    if not agents_by_id:
        raise AgentsConfigError(
            f"Nessun <Agent> valido trovato nel file XML: {path.resolve()}"
        )

    logger.info(
        "Caricati %d agent dal file di configurazione: %s",
        len(agents_by_id),
        path.resolve(),
    )
    logger.info("ID agent caricati: %s", list(agents_by_id.keys()))
    return agents_by_id


def _get_agents_by_id() -> Dict[str, Dict[str, str]]:
    """
    Ritorna la configurazione degli agent, caricandola da XML alla prima chiamata.
    """
    global _AGENTS_BY_ID
    if _AGENTS_BY_ID is None:
        _AGENTS_BY_ID = _load_agents_from_xml()
    return _AGENTS_BY_ID


# ================== API PUBBLICA ==================


def create_agent_by_id(agent_id: str, mcp_server: MCPServerStdio) -> Agent:
    """
    Crea un Agent generico a partire dalla configurazione letta dal file XML,
    identificandolo tramite 'id'.

    Esempi:
        create_agent_by_id("orders", mcp_server)
        create_agent_by_id("customers", mcp_server)
    """
    agents_by_id = _get_agents_by_id()

    if agent_id not in agents_by_id:
        raise AgentsConfigError(
            f"Nessun agent con id='{agent_id}' definito nel file XML. "
            "Controlla <Agent id=\"...\"> in my_agents.xlm / my_agents.xml."
        )

    cfg = agents_by_id[agent_id]
    name = cfg.get("name") or agent_id  # se manca name, uso l'id come nome interno
    instructions = cfg.get("instructions", "").strip()

    if not instructions:
        logger.warning(
            "L'Agent con id='%s' è presente nel file XML ma senza istruzioni.",
            agent_id,
        )

    logger.info(
        "Creo l'Agent con id='%s' (name='%s') a partire dalla configurazione XML...",
        agent_id,
        name,
    )

    return Agent(
        name=name,
        instructions=instructions,
        mcp_servers=[mcp_server],
    )


def get_available_agent_ids() -> List[str]:
    """
    Ritorna l'elenco degli ID agent definiti nel file XML.
    """
    return list(_get_agents_by_id().keys())


def load_bot_agents(mcp_server: MCPServerStdio) -> Tuple[Agent, Optional[Agent]]:
    """
    Crea gli agent principali per il bot Telegram, guidato dal file XML.

    - Cerca un agent con id="orders"    -> diventa l'orders_agent (OBBLIGATORIO)
    - Cerca un agent con id="customers" -> diventa il customers_agent (OPZIONALE)

    Non usa NESSUN nome statico tipo 'OrdersAgent' o 'CustomersAgent':
    si basa solo sugli id definiti nell'XML.
    """
    agents_by_id = _get_agents_by_id()
    ids = list(agents_by_id.keys())
    logger.info("load_bot_agents: agent IDs disponibili da XML: %s", ids)

    # ORDERS: obbligatorio
    if "orders" not in agents_by_id:
        raise AgentsConfigError(
            "Nessun agent con id='orders' trovato nel file XML. "
            "Definisci ad esempio: <Agent id=\"orders\" name=\"Qualcosa\">..."
        )

    orders_agent = create_agent_by_id("orders", mcp_server)

    # CUSTOMERS: opzionale
    customers_agent: Optional[Agent] = None
    if "customers" in agents_by_id:
        customers_agent = create_agent_by_id("customers", mcp_server)

    logger.info(
        "load_bot_agents: creati orders_agent (id='orders') "
        "e customers_agent (id='customers' presente=%s)",
        "customers" in agents_by_id,
    )

    return orders_agent, customers_agent
