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
#   "OrdersAgent": {
#       "id": "orders",
#       "description": "...",
#       "instructions": "..."
#   },
#   "CustomersAgent": {
#       "id": "customers",
#       ...
#   },
# }
_AGENTS_CONFIG: Optional[Dict[str, Dict[str, str]]] = None


def _load_agents_from_xml() -> Dict[str, Dict[str, str]]:
    """
    Legge la configurazione degli agent da my_agents.xlm (o, in fallback, my_agents.xml)
    e restituisce un dizionario keyed per 'name' (attributo name dell'XML).
    """
    primary = Path("my_agents.xlm")
    fallback = Path("my_agents.xml")

    path: Optional[Path] = None

    if primary.exists():
        path = primary
    elif fallback.exists():
        path = fallback

    if path is None:
        raise AgentsConfigError(
            "File XML degli agent non trovato. "
            f"Ho cercato: {primary.resolve()} e {fallback.resolve()}"
        )

    tree = ET.parse(path)
    root = tree.getroot()

    agents_config: Dict[str, Dict[str, str]] = {}

    for agent_el in root.findall("Agent"):
        name = agent_el.get("name")
        agent_id = agent_el.get("id")

        if not name:
            logger.warning(
                "Trovato un <Agent> senza attributo 'name' nel file %s",
                path,
            )
            continue

        desc_el = agent_el.find("Description")
        instr_el = agent_el.find("Instructions")

        description = (desc_el.text or "").strip() if desc_el is not None else ""
        instructions = (instr_el.text or "").strip() if instr_el is not None else ""

        if not instructions:
            logger.warning(
                "L'Agent '%s' nel file %s non ha istruzioni (Instructions vuoto).",
                name,
                path,
            )

        agents_config[name] = {
            "id": agent_id or "",
            "description": description,
            "instructions": instructions,
        }

    if not agents_config:
        raise AgentsConfigError(
            f"Nessun <Agent> valido trovato nel file XML: {path.resolve()}"
        )

    logger.info(
        "Caricati %d agent dal file di configurazione: %s",
        len(agents_config),
        path.resolve(),
    )
    logger.info("Nomi agent caricati: %s", list(agents_config.keys()))
    return agents_config


def _get_agents_config() -> Dict[str, Dict[str, str]]:
    """
    Ritorna la configurazione degli agent, caricandola da XML alla prima chiamata.
    """
    global _AGENTS_CONFIG
    if _AGENTS_CONFIG is None:
        _AGENTS_CONFIG = _load_agents_from_xml()
    return _AGENTS_CONFIG


# ================== API PUBBLICA ==================


def create_agent(agent_name: str, mcp_server: MCPServerStdio) -> Agent:
    """
    Crea un Agent generico a partire dalla configurazione letta dal file XML.

    Esempi:
        create_agent("OrdersAgent", mcp_server)
        create_agent("CustomersAgent", mcp_server)
    """
    agents_config = _get_agents_config()

    if agent_name not in agents_config:
        raise AgentsConfigError(
            f"Agent '{agent_name}' non presente nel file XML. "
            f"Controlla <Agent name=\"{agent_name}\"> in my_agents.xlm / my_agents.xml."
        )

    cfg = agents_config[agent_name]
    instructions = cfg.get("instructions", "").strip()

    if not instructions:
        logger.warning(
            "L'Agent '%s' è presente nel file XML ma senza istruzioni.",
            agent_name,
        )

    logger.info("Creo l'Agent '%s' a partire dalla configurazione XML...", agent_name)

    return Agent(
        name=agent_name,
        instructions=instructions,
        mcp_servers=[mcp_server],
    )


def get_available_agent_names() -> List[str]:
    """
    Ritorna l'elenco dei nomi agent definiti nel file XML.
    Utile per debug o per caricare agent in modo dinamico.
    """
    return list(_get_agents_config().keys())


def load_bot_agents(mcp_server: MCPServerStdio) -> Tuple[Agent, Optional[Agent]]:
    """
    Crea gli agent principali per il bot Telegram, guidato dal file XML.

    - Cerca un agent con id="orders"  -> diventa l'orders_agent (OBBLIGATORIO)
    - Cerca un agent con id="customers" -> diventa il customers_agent (OPZIONALE)

    Se id non sono presenti, prova a usare i name di default:
        "OrdersAgent" / "CustomersAgent".
    """
    cfg = _get_agents_config()

    # Mappa id -> name
    id_to_name: Dict[str, str] = {}
    for name, data in cfg.items():
        agent_id = (data.get("id") or "").strip()
        if agent_id:
            id_to_name[agent_id] = name

    # Determina name dell'agent ordini
    orders_name = id_to_name.get("orders")
    if orders_name is None and "OrdersAgent" in cfg:
        orders_name = "OrdersAgent"

    if not orders_name:
        raise AgentsConfigError(
            "Nessun agent con id='orders' (o name='OrdersAgent') trovato nel file XML. "
            "Definisci ad esempio: <Agent id=\"orders\" name=\"OrdersAgent\">..."
        )

    # Determina name dell'agent clienti (opzionale)
    customers_name = id_to_name.get("customers")
    if customers_name is None and "CustomersAgent" in cfg:
        customers_name = "CustomersAgent"

    orders_agent = create_agent(orders_name, mcp_server)

    customers_agent: Optional[Agent] = None
    if customers_name:
        customers_agent = create_agent(customers_name, mcp_server)

    logger.info(
        "load_bot_agents: orders_agent=%s, customers_agent=%s",
        orders_name,
        customers_name,
    )

    return orders_agent, customers_agent
