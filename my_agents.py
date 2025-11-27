import logging
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Dict, Optional

from agents import Agent
from agents.mcp import MCPServerStdio

logger = logging.getLogger(__name__)


class AgentsConfigError(Exception):
    """Errore nella configurazione degli agent (file XML)."""
    pass


# Cache in memoria degli agent caricati da XML
_AGENTS_BY_ID: Optional[Dict[str, Dict[str, str]]] = None

# Percorso del file XML (stessa cartella di esecuzione)
XML_FILE = Path("my_agents.xml")


def _load_agents_from_xml() -> Dict[str, Dict[str, str]]:
    """
    Legge my_agents.xml e restituisce un dizionario:
        { agent_id: { name, description, role, language_tone, tools_usage,
                      tools, main_flows, error_handling, extra_notes,
                      instructions } }

    ADEGUAMENTO:
    - Il vecchio campo <Rules> all'interno di <Tool> NON esiste più.
    - È stato sostituito da:
        <BeforeCallingRules>, <CallingRules>, <AfterCallingRules>
      per ciascun <Tool>.
    - Qui ricombiniamo i tre campi in un unico testo "tools"
      (eventualmente includendo il nome del tool), che va a popolare
      la stessa variabile che prima veniva valorizzata con il solo <Rules>.
    """
    if not XML_FILE.exists():
        raise AgentsConfigError(f"File XML non trovato: {XML_FILE}")

    try:
        tree = ET.parse(XML_FILE)
        root = tree.getroot()
    except Exception as exc:
        logger.exception("Impossibile parsare %s", XML_FILE)
        raise AgentsConfigError(f"Impossibile parsare {XML_FILE}: {exc}") from exc

    if root.tag != "Agents":
        raise AgentsConfigError(f"Root XML inattesa: '{root.tag}', atteso 'Agents'")

    agents_by_id: Dict[str, Dict[str, str]] = {}

    for agent_el in root.findall("Agent"):
        agent_id = (agent_el.get("id") or "").strip()
        name = (agent_el.get("name") or "").strip()

        if not agent_id:
            logger.warning("Trovato <Agent> senza attributo 'id': ignorato.")
            continue

        # Descrizione agente
        desc_el = agent_el.find("Description")
        description = (desc_el.text or "").strip() if desc_el is not None and desc_el.text else ""

        # ----- Instructions strutturate -----
        instr_el = agent_el.find("Instructions")

        role = ""
        language_tone = ""
        tools_usage = ""
        tools = ""          # <-- qui finirà il testo ricomposto da Before/Calling/After
        main_flows = ""
        error_handling = ""
        extra_notes = ""

        # 1) Sottocampi Role / LanguageTone / ToolsUsage / MainFlows / ErrorHandling / ExtraNotes
        if instr_el is not None:
            children = list(instr_el)
            if children:
                # formato nuovo
                def _get(tag: str) -> str:
                    el = instr_el.find(tag)
                    return (el.text or "").strip() if el is not None and el.text else ""

                role = _get("Role")
                language_tone = _get("LanguageTone")
                tools_usage = _get("ToolsUsage")
                # "Tools" dentro <Instructions> è testo libero, diverso dai Tool REST
                tools_text_from_instructions = _get("Tools")
                main_flows = _get("MainFlows")
                error_handling = _get("ErrorHandling")
                extra_notes = _get("ExtraNotes")
            else:
                # formato legacy: tutto il testo dentro <Instructions>
                role = ""
                language_tone = ""
                tools_usage = ""
                tools_text_from_instructions = (instr_el.text or "").strip()
                main_flows = ""
                error_handling = ""
                extra_notes = ""
        else:
            tools_text_from_instructions = ""

        # 2) Ricomposizione dei "Rules" dei singoli Tool REST
        #    (BeforeCallingRules + CallingRules + AfterCallingRules)
        tools_el = agent_el.find("Tools")
        per_tool_blocks = []

        if tools_el is not None:
            for tool_el in tools_el.findall("Tool"):
                tool_name = (tool_el.get("name") or "").strip()

                # campi nuovi
                before_txt = tool_el.findtext("BeforeCallingRules", default="") or ""
                calling_txt = tool_el.findtext("CallingRules", default="") or ""
                after_txt = tool_el.findtext("AfterCallingRules", default="") or ""

                # compatibilità: se i 3 campi sono tutti vuoti, prova il vecchio <Rules>
                if not (before_txt or calling_txt or after_txt):
                    legacy_rules = tool_el.findtext("Rules", default="") or ""
                    calling_txt = legacy_rules

                # Se non c'è niente da dire, salta il tool
                if not (before_txt or calling_txt or after_txt):
                    continue

                # Blocchetto di testo per questo tool
                tool_lines = []
                if tool_name:
                    tool_lines.append(f"Regole per il tool '{tool_name}':")
                else:
                    tool_lines.append("Regole per questo tool:")

                if before_txt.strip():
                    tool_lines.append("")
                    tool_lines.append("Prima della chiamata (Before calling):")
                    tool_lines.append(before_txt.strip())

                if calling_txt.strip():
                    tool_lines.append("")
                    tool_lines.append("Durante la chiamata (Calling):")
                    tool_lines.append(calling_txt.strip())

                if after_txt.strip():
                    tool_lines.append("")
                    tool_lines.append("Dopo la chiamata (After calling):")
                    tool_lines.append(after_txt.strip())

                per_tool_blocks.append("\n".join(tool_lines).strip())

        # Concatenazione finale della sezione "tools":
        # - prima eventuale testo da <Instructions><Tools>
        # - poi, a seguire, i blocchi per ogni <Tool> REST
        parts_for_tools: list[str] = []
        if tools_text_from_instructions.strip():
            parts_for_tools.append(tools_text_from_instructions.strip())
        if per_tool_blocks:
            parts_for_tools.append("\n\n".join(per_tool_blocks))

        tools = "\n\n".join([p for p in parts_for_tools if p])

        # Costruzione campo instructions complessivo da passare all'LLM
        sections = [role, language_tone, tools_usage, tools, main_flows, error_handling, extra_notes]
        instructions = "\n\n".join([s for s in sections if s.strip()])

        agents_by_id[agent_id] = {
            "name": name or agent_id,
            "description": description,
            "role": role,
            "language_tone": language_tone,
            "tools_usage": tools_usage,
            "tools": tools,  # <-- stessa variabile di prima, ora da 3 campi
            "main_flows": main_flows,
            "error_handling": error_handling,
            "extra_notes": extra_notes,
            "instructions": instructions,
        }

    if not agents_by_id:
        raise AgentsConfigError(
            f"Nessun <Agent> valido trovato in {XML_FILE}. Controlla la configurazione."
        )

    logger.info("Caricati %d agent da %s", len(agents_by_id), XML_FILE)
    return agents_by_id


def _get_agents_by_id() -> Dict[str, Dict[str, str]]:
    """Ritorna la cache degli agent; se vuota li carica dal file XML."""
    global _AGENTS_BY_ID
    if _AGENTS_BY_ID is None:
        _AGENTS_BY_ID = _load_agents_from_xml()
    return _AGENTS_BY_ID


def create_agent_by_id(agent_id: str, mcp_server: MCPServerStdio) -> Agent:
    """
    Crea un oggetto Agent (usato dal bot) a partire dall'id definito in my_agents.xml.
    """
    agents_by_id = _get_agents_by_id()

    if agent_id not in agents_by_id:
        raise AgentsConfigError(f"Agent con id '{agent_id}' non definito in {XML_FILE}")

    cfg = agents_by_id[agent_id]
    name = cfg.get("name") or agent_id
    instructions = (cfg.get("instructions") or "").strip()

    if not instructions:
        logger.warning(
            "Agent '%s' ha instructions vuote: verifica la configurazione XML.",
            agent_id,
        )

    logger.info("Creazione Agent '%s' (model=gpt-4.1-mini)", agent_id)

    # Qui si può parametrizzare il modello se in futuro vuoi farlo leggere dall'XML
    return Agent(
        name=name,
        instructions=instructions,
        model="gpt-4.1-mini",
        mcp_servers=[mcp_server],
    )


def get_available_agent_ids() -> list[str]:
    """Ritorna la lista degli id di agent disponibili nel file XML."""
    return list(_get_agents_by_id().keys())


def load_bot_agents(mcp_server: MCPServerStdio) -> tuple[Agent, Optional[Agent]]:
    """
    Funzione helper usata dal bot Telegram:
    - crea SEMPRE l'agent 'orders' (obbligatorio)
    - crea, se presente, l'agent 'customers' (opzionale)
    """
    agents_by_id = _get_agents_by_id()
    ids = list(agents_by_id.keys())
    logger.info("load_bot_agents: agent disponibili: %s", ids)

    if "orders" not in agents_by_id:
        raise AgentsConfigError(
            "Nessun agent con id='orders' definito in my_agents.xml: "
            "è obbligatorio per il funzionamento del bot."
        )

    orders_agent = create_agent_by_id("orders", mcp_server)

    customers_agent: Optional[Agent] = None
    if "customers" in agents_by_id:
        customers_agent = create_agent_by_id("customers", mcp_server)

    logger.info(
        "load_bot_agents: creati orders_agent (id='orders') "
        "e customers_agent (id='customers' presente=%s)",
        "customers" in agents_by_id,
    )

    return orders_agent, customers_agent
