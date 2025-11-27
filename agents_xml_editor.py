import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import xml.etree.ElementTree as ET

XML_FILE = Path("my_agents.xml")
SERVICES_XML = Path("my_services.xml")


class AgentsXmlEditor(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Editor my_agents.xml")
        self.geometry("1100x650")

        # Dati in memoria:
        # self.agents = [
        #   {
        #       "id": "...",
        #       "name": "...",
        #       "description": "...",
        #       "role": "...",
        #       "language_tone": "...",
        #       "tools_usage": "...",
        #       "main_flows": "...",
        #       "error_handling": "...",
        #       "extra_notes": "...",
        #       "tools": [
        #           {
        #               "name": "...",
        #               "description": "...",
        #               "before_calling_rules": "...",
        #               "calling_rules": "...",
        #               "after_calling_rules": "..."
        #           },
        #           ...
        #       ]
        #   },
        #   ...
        # ]
        self.agents = []
        self.current_agent_index = None   # indice agente selezionato
        self.current_tool_index = None    # indice tool selezionato all'interno dell'agente
        self.current_selection_kind = None  # "agent" | "tool" | None
        self.dirty = False

        # mappa iid Treeview -> ("agent", idx) | ("tools_folder", idx) | ("tool", a_idx, t_idx)
        self.tree_item_map = {}

        self._create_widgets()
        self._load_agents()
        self._refresh_tree()

        if self.agents:
            self.tree.selection_set("agent_0")
            self.tree.focus("agent_0")
            self._on_tree_select()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ===================== CREAZIONE UI =====================

    def _create_widgets(self):
        main_frame = ttk.Frame(self, padding=5)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Barra comandi superiore
        toolbar = ttk.Frame(main_frame)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        btn_apply = ttk.Button(toolbar, text="Varia", command=self._on_apply)
        btn_apply.pack(side=tk.LEFT, padx=3)

        btn_save = ttk.Button(toolbar, text="Salva", command=self._on_save)
        btn_save.pack(side=tk.LEFT, padx=3)

        btn_exit = ttk.Button(toolbar, text="Abbandona", command=self._on_exit)
        btn_exit.pack(side=tk.LEFT, padx=3)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=5)

        btn_new_agent = ttk.Button(toolbar, text="Nuovo Agente", command=self._on_new_agent)
        btn_new_agent.pack(side=tk.LEFT, padx=3)

        btn_del_agent = ttk.Button(toolbar, text="Elimina Agente", command=self._on_delete_agent)
        btn_del_agent.pack(side=tk.LEFT, padx=3)

        # Corpo: sinistra Treeview, destra dettaglio
        body = ttk.PanedWindow(main_frame, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, pady=5)

        # Sinistra: tree di agent/tool
        left_frame = ttk.Frame(body, padding=5, relief="groove")
        body.add(left_frame, weight=1)

        self.tree = ttk.Treeview(left_frame, selectmode="browse")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tree_scroll = ttk.Scrollbar(left_frame, orient="vertical", command=self.tree.yview)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=tree_scroll.set)

        self.tree["columns"] = ("desc",)
        self.tree.heading("#0", text="ID / Tools")
        self.tree.heading("desc", text="Descrizione")
        self.tree.column("#0", width=220, anchor="w")
        self.tree.column("desc", width=220, anchor="w")

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Return>", self._on_tree_edit_request)
        self.tree.bind("<Double-1>", self._on_tree_edit_request)

        # Destra: Notebook con tab "Agente" e "Tool"
        right_frame = ttk.Frame(body, padding=5, relief="groove")
        body.add(right_frame, weight=3)

        self.notebook = ttk.Notebook(right_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # ---- Tab Agente ----
        self.agent_frame = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.agent_frame, text="Agente")

        row = 0
        ttk.Label(self.agent_frame, text="ID agente:").grid(row=row, column=0, sticky="e", padx=5, pady=2)
        self.var_agent_id = tk.StringVar()
        self.entry_agent_id = ttk.Entry(self.agent_frame, textvariable=self.var_agent_id)
        self.entry_agent_id.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        row += 1

        ttk.Label(self.agent_frame, text="Nome agente:").grid(row=row, column=0, sticky="e", padx=5, pady=2)
        self.var_agent_name = tk.StringVar()
        self.entry_agent_name = ttk.Entry(self.agent_frame, textvariable=self.var_agent_name)
        self.entry_agent_name.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        row += 1

        ttk.Label(self.agent_frame, text="Descrizione:").grid(row=row, column=0, sticky="ne", padx=5, pady=2)
        self.txt_agent_description = tk.Text(self.agent_frame, height=4, wrap="word")
        self.txt_agent_description.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        row += 1

        ttk.Label(self.agent_frame, text="Role:").grid(row=row, column=0, sticky="ne", padx=5, pady=2)
        self.txt_role = tk.Text(self.agent_frame, height=4, wrap="word")
        self.txt_role.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        row += 1

        ttk.Label(self.agent_frame, text="Language tone:").grid(row=row, column=0, sticky="ne", padx=5, pady=2)
        self.txt_language_tone = tk.Text(self.agent_frame, height=4, wrap="word")
        self.txt_language_tone.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        row += 1

        ttk.Label(self.agent_frame, text="Tools usage:").grid(row=row, column=0, sticky="ne", padx=5, pady=2)
        self.txt_tools_usage = tk.Text(self.agent_frame, height=4, wrap="word")
        self.txt_tools_usage.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        row += 1

        ttk.Label(self.agent_frame, text="Main flows:").grid(row=row, column=0, sticky="ne", padx=5, pady=2)
        self.txt_main_flows = tk.Text(self.agent_frame, height=6, wrap="word")
        self.txt_main_flows.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        row += 1

        ttk.Label(self.agent_frame, text="Error handling:").grid(row=row, column=0, sticky="ne", padx=5, pady=2)
        self.txt_error_handling = tk.Text(self.agent_frame, height=4, wrap="word")
        self.txt_error_handling.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        row += 1

        ttk.Label(self.agent_frame, text="Extra notes:").grid(row=row, column=0, sticky="ne", padx=5, pady=2)
        self.txt_extra_notes = tk.Text(self.agent_frame, height=4, wrap="word")
        self.txt_extra_notes.grid(row=row, column=1, sticky="ew", padx=5, pady=2)
        row += 1

        self.agent_frame.columnconfigure(1, weight=1)

        # ---- Tab Tool ----
        self.tool_frame = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.tool_frame, text="Tool")

        ttk.Label(self.tool_frame, text="Agente:").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        self.var_tool_agent_info = tk.StringVar()
        lbl_agent_info = ttk.Label(self.tool_frame, textvariable=self.var_tool_agent_info)
        lbl_agent_info.grid(row=0, column=1, sticky="w", padx=5, pady=2)

        ttk.Label(self.tool_frame, text="Nome tool:").grid(row=1, column=0, sticky="e", padx=5, pady=2)
        self.var_tool_name = tk.StringVar()
        self.entry_tool_name = ttk.Entry(self.tool_frame, textvariable=self.var_tool_name)
        self.entry_tool_name.grid(row=1, column=1, sticky="ew", padx=5, pady=2)

        ttk.Label(self.tool_frame, text="Description:").grid(row=2, column=0, sticky="ne", padx=5, pady=2)
        self.txt_tool_description = tk.Text(self.tool_frame, height=4, wrap="word")
        self.txt_tool_description.grid(row=2, column=1, columnspan=2, sticky="ew", padx=5, pady=2)

        ttk.Label(self.tool_frame, text="Before calling rules:").grid(row=3, column=0, sticky="ne", padx=5, pady=2)
        self.txt_tool_before_rules = tk.Text(self.tool_frame, height=4, wrap="word")
        self.txt_tool_before_rules.grid(row=3, column=1, sticky="nsew", padx=5, pady=2)
        br_scroll = ttk.Scrollbar(self.tool_frame, orient="vertical", command=self.txt_tool_before_rules.yview)
        br_scroll.grid(row=3, column=2, sticky="ns")
        self.txt_tool_before_rules.configure(yscrollcommand=br_scroll.set)

        ttk.Label(self.tool_frame, text="Calling rules:").grid(row=4, column=0, sticky="ne", padx=5, pady=2)
        self.txt_tool_calling_rules = tk.Text(self.tool_frame, height=4, wrap="word")
        self.txt_tool_calling_rules.grid(row=4, column=1, sticky="nsew", padx=5, pady=2)
        cr_scroll = ttk.Scrollbar(self.tool_frame, orient="vertical", command=self.txt_tool_calling_rules.yview)
        cr_scroll.grid(row=4, column=2, sticky="ns")
        self.txt_tool_calling_rules.configure(yscrollcommand=cr_scroll.set)

        ttk.Label(self.tool_frame, text="After calling rules:").grid(row=5, column=0, sticky="ne", padx=5, pady=2)
        self.txt_tool_after_rules = tk.Text(self.tool_frame, height=4, wrap="word")
        self.txt_tool_after_rules.grid(row=5, column=1, sticky="nsew", padx=5, pady=2)
        ar_scroll = ttk.Scrollbar(self.tool_frame, orient="vertical", command=self.txt_tool_after_rules.yview)
        ar_scroll.grid(row=5, column=2, sticky="ns")
        self.txt_tool_after_rules.configure(yscrollcommand=ar_scroll.set)

        # Bottoni per Tool
        tool_btn_frame = ttk.Frame(self.tool_frame)
        tool_btn_frame.grid(row=6, column=0, columnspan=3, sticky="w", padx=5, pady=5)

        btn_new_tool = ttk.Button(tool_btn_frame, text="Nuovo Tool", command=self._on_new_tool)
        btn_new_tool.pack(side=tk.LEFT, padx=3)

        btn_del_tool = ttk.Button(tool_btn_frame, text="Elimina Tool", command=self._on_delete_tool)
        btn_del_tool.pack(side=tk.LEFT, padx=3)

        btn_gen_rules = ttk.Button(
            tool_btn_frame,
            text="Deriva Calling rules da my_services.xml",
            command=self._on_generate_tool_rules_from_services,
        )
        btn_gen_rules.pack(side=tk.LEFT, padx=8)

        self.tool_frame.columnconfigure(1, weight=1)
        self.tool_frame.rowconfigure(3, weight=1)
        self.tool_frame.rowconfigure(4, weight=1)
        self.tool_frame.rowconfigure(5, weight=1)

    # ===================== CARICAMENTO / SALVATAGGIO XML =====================

    def _load_agents(self):
        """Carica my_agents.xml in self.agents."""
        self.agents = []

        if not XML_FILE.exists():
            messagebox.showwarning(
                "File non trovato",
                f"Il file {XML_FILE} non esiste. Verrà creato al primo salvataggio.",
            )
            self.dirty = False
            return

        try:
            tree = ET.parse(XML_FILE)
            root = tree.getroot()
        except Exception as e:
            messagebox.showerror("Errore XML", f"Impossibile leggere {XML_FILE}:\n{e}")
            return

        if root.tag != "Agents":
            messagebox.showerror("Errore XML", "Root del file XML diversa da 'Agents'.")
            return

        agents = []

        for agent_el in root.findall("Agent"):
            agent_id = (agent_el.get("id") or "").strip()
            name = (agent_el.get("name") or "").strip()

            desc_el = agent_el.find("Description")
            description = desc_el.text if desc_el is not None and desc_el.text else ""

            instr_el = agent_el.find("Instructions")
            role = ""
            language_tone = ""
            tools_usage = ""
            main_flows = ""
            error_handling = ""
            extra_notes = ""

            if instr_el is not None:
                children = list(instr_el)
                if children:
                    def _get(tag: str) -> str:
                        el = instr_el.find(tag)
                        return el.text if el is not None and el.text else ""

                    role = _get("Role")
                    language_tone = _get("LanguageTone")
                    tools_usage = _get("ToolsUsage")
                    main_flows = _get("MainFlows")
                    error_handling = _get("ErrorHandling")
                    extra_notes = _get("ExtraNotes")
                else:
                    main_flows = instr_el.text or ""

            tools_list = []
            tools_el = agent_el.find("Tools")
            if tools_el is not None:
                for tool_el in tools_el.findall("Tool"):
                    t_name = (tool_el.get("name") or "").strip()
                    t_desc = tool_el.findtext("Description", default="") or ""

                    # Nuova struttura: 3 campi di regole
                    t_before = tool_el.findtext("BeforeCallingRules", default="") or ""
                    t_calling = tool_el.findtext("CallingRules", default="") or ""
                    t_after = tool_el.findtext("AfterCallingRules", default="") or ""

                    # Retrocompatibilità: se non ci sono i 3 tag ma solo Rules,
                    # mettiamo il contenuto in Calling rules
                    if not (t_before or t_calling or t_after):
                        t_legacy = tool_el.findtext("Rules", default="") or ""
                        t_calling = t_legacy

                    tools_list.append(
                        {
                            "name": t_name,
                            "description": t_desc,
                            "before_calling_rules": t_before,
                            "calling_rules": t_calling,
                            "after_calling_rules": t_after,
                        }
                    )

            agents.append(
                {
                    "id": agent_id,
                    "name": name,
                    "description": description,
                    "role": role,
                    "language_tone": language_tone,
                    "tools_usage": tools_usage,
                    "main_flows": main_flows,
                    "error_handling": error_handling,
                    "extra_notes": extra_notes,
                    "tools": tools_list,
                }
            )

        self.agents = agents
        self.dirty = False

    def _save_agents(self):
        """Scrive self.agents in my_agents.xml."""
        root = ET.Element("Agents")

        for agent in self.agents:
            agent_id = (agent.get("id") or "").strip()
            name = (agent.get("name") or "").strip()
            description = agent.get("description", "")

            role = agent.get("role", "")
            language_tone = agent.get("language_tone", "")
            tools_usage = agent.get("tools_usage", "")
            main_flows = agent.get("main_flows", "")
            error_handling = agent.get("error_handling", "")
            extra_notes = agent.get("extra_notes", "")

            tools_list = agent.get("tools", []) or []

            agent_el = ET.SubElement(root, "Agent")
            if agent_id:
                agent_el.set("id", agent_id)
            if name:
                agent_el.set("name", name)

            desc_el = ET.SubElement(agent_el, "Description")
            desc_el.text = description

            instr_el = ET.SubElement(agent_el, "Instructions")

            def _add_if_not_empty(tag: str, value: str):
                value = (value or "").strip()
                if value:
                    el = ET.SubElement(instr_el, tag)
                    el.text = value

            _add_if_not_empty("Role", role)
            _add_if_not_empty("LanguageTone", language_tone)
            _add_if_not_empty("ToolsUsage", tools_usage)
            _add_if_not_empty("MainFlows", main_flows)
            _add_if_not_empty("ErrorHandling", error_handling)
            _add_if_not_empty("ExtraNotes", extra_notes)

            if tools_list:
                tools_el = ET.SubElement(agent_el, "Tools")
                for t in tools_list:
                    t_name = (t.get("name") or "").strip()
                    t_desc = (t.get("description") or "").strip()
                    t_before = (t.get("before_calling_rules") or "").strip()
                    t_calling = (t.get("calling_rules") or "").strip()
                    t_after = (t.get("after_calling_rules") or "").strip()

                    tool_el = ET.SubElement(tools_el, "Tool")
                    if t_name:
                        tool_el.set("name", t_name)
                    if t_desc:
                        d_el = ET.SubElement(tool_el, "Description")
                        d_el.text = t_desc

                    # Scriviamo sempre i 3 tag per coerenza di struttura
                    b_el = ET.SubElement(tool_el, "BeforeCallingRules")
                    b_el.text = t_before if t_before else ""
                    c_el = ET.SubElement(tool_el, "CallingRules")
                    c_el.text = t_calling if t_calling else ""
                    a_el = ET.SubElement(tool_el, "AfterCallingRules")
                    a_el.text = t_after if t_after else ""

        tree = ET.ElementTree(root)
        try:
            tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
        except Exception as e:
            messagebox.showerror("Errore salvataggio", f"Impossibile scrivere {XML_FILE}:\n{e}")
            return False

        self.dirty = False
        return True

    # ===================== GESTIONE TREEVIEW =====================

    def _refresh_tree(self):
        self.tree_item_map.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)

        for i, agent in enumerate(self.agents):
            aid = agent.get("id", "") or "(senza id)"
            name = agent.get("name", "")
            desc = agent.get("description", "")

            if name and name != aid:
                text = f"{aid} ({name})"
            else:
                text = aid

            agent_iid = f"agent_{i}"
            self.tree.insert("", "end", iid=agent_iid, text=text, values=(desc,))
            self.tree_item_map[agent_iid] = ("agent", i)

            # nodo Tools (folder)
            tools_iid = f"agent_{i}_tools"
            self.tree.insert(agent_iid, "end", iid=tools_iid, text="Tools", values=("",))
            self.tree_item_map[tools_iid] = ("tools_folder", i)

            for j, tool in enumerate(agent.get("tools", []) or []):
                t_name = tool.get("name", "") or "(senza nome)"
                t_desc = tool.get("description", "")
                tool_iid = f"agent_{i}_tool_{j}"
                self.tree.insert(tools_iid, "end", iid=tool_iid, text=t_name, values=(t_desc,))
                self.tree_item_map[tool_iid] = ("tool", i, j)

    def _on_tree_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            self.current_agent_index = None
            self.current_tool_index = None
            self.current_selection_kind = None
            return

        iid = sel[0]
        info = self.tree_item_map.get(iid)
        if not info:
            self.current_agent_index = None
            self.current_tool_index = None
            self.current_selection_kind = None
            return

        if info[0] == "agent":
            _, a_idx = info
            self.current_agent_index = a_idx
            self.current_tool_index = None
            self.current_selection_kind = "agent"
            self._load_agent_to_form(a_idx)
            self.notebook.select(self.agent_frame)
        elif info[0] == "tool":
            _, a_idx, t_idx = info
            self.current_agent_index = a_idx
            self.current_tool_index = t_idx
            self.current_selection_kind = "tool"
            self._load_tool_to_form(a_idx, t_idx)
            self.notebook.select(self.tool_frame)
        else:
            # folder Tools
            _, a_idx = info
            self.current_agent_index = a_idx
            self.current_tool_index = None
            self.current_selection_kind = "agent"
            self._load_agent_to_form(a_idx)
            self.notebook.select(self.agent_frame)

    def _on_tree_edit_request(self, event=None):
        self._on_tree_select()

    # ===================== GESTIONE FORM AGENTE =====================

    def _clear_agent_form(self):
        self.var_agent_id.set("")
        self.var_agent_name.set("")
        self.txt_agent_description.delete("1.0", tk.END)
        self.txt_role.delete("1.0", tk.END)
        self.txt_language_tone.delete("1.0", tk.END)
        self.txt_tools_usage.delete("1.0", tk.END)
        self.txt_main_flows.delete("1.0", tk.END)
        self.txt_error_handling.delete("1.0", tk.END)
        self.txt_extra_notes.delete("1.0", tk.END)

    def _load_agent_to_form(self, index: int):
        self._clear_agent_form()

        if index < 0 or index >= len(self.agents):
            return

        agent = self.agents[index]
        self.var_agent_id.set(agent.get("id", ""))
        self.var_agent_name.set(agent.get("name", ""))
        self.txt_agent_description.insert("1.0", agent.get("description", "") or "")
        self.txt_role.insert("1.0", agent.get("role", "") or "")
        self.txt_language_tone.insert("1.0", agent.get("language_tone", "") or "")
        self.txt_tools_usage.insert("1.0", agent.get("tools_usage", "") or "")
        self.txt_main_flows.insert("1.0", agent.get("main_flows", "") or "")
        self.txt_error_handling.insert("1.0", agent.get("error_handling", "") or "")
        self.txt_extra_notes.insert("1.0", agent.get("extra_notes", "") or "")

    def _apply_agent_form_to_data(self):
        if self.current_agent_index is None:
            return False
        if not (0 <= self.current_agent_index < len(self.agents)):
            return False

        agent = self.agents[self.current_agent_index]
        agent["id"] = self.var_agent_id.get().strip()
        agent["name"] = self.var_agent_name.get().strip()
        agent["description"] = self.txt_agent_description.get("1.0", tk.END).rstrip("\n")
        agent["role"] = self.txt_role.get("1.0", tk.END).rstrip("\n")
        agent["language_tone"] = self.txt_language_tone.get("1.0", tk.END).rstrip("\n")
        agent["tools_usage"] = self.txt_tools_usage.get("1.0", tk.END).rstrip("\n")
        agent["main_flows"] = self.txt_main_flows.get("1.0", tk.END).rstrip("\n")
        agent["error_handling"] = self.txt_error_handling.get("1.0", tk.END).rstrip("\n")
        agent["extra_notes"] = self.txt_extra_notes.get("1.0", tk.END).rstrip("\n")

        self.dirty = True
        self._refresh_tree()
        return True

    # ===================== GESTIONE FORM TOOL =====================

    def _clear_tool_form(self):
        self.var_tool_agent_info.set("")
        self.var_tool_name.set("")
        self.txt_tool_description.delete("1.0", tk.END)
        self.txt_tool_before_rules.delete("1.0", tk.END)
        self.txt_tool_calling_rules.delete("1.0", tk.END)
        self.txt_tool_after_rules.delete("1.0", tk.END)

    def _load_tool_to_form(self, agent_index: int, tool_index: int):
        self._clear_tool_form()

        if agent_index < 0 or agent_index >= len(self.agents):
            return
        agent = self.agents[agent_index]
        tools = agent.get("tools", []) or []
        if tool_index < 0 or tool_index >= len(tools):
            return

        tool = tools[tool_index]
        aid = agent.get("id", "") or "(senza id)"
        aname = agent.get("name", "")
        if aname and aname != aid:
            info = f"{aid} ({aname})"
        else:
            info = aid
        self.var_tool_agent_info.set(info)

        self.var_tool_name.set(tool.get("name", ""))
        self.txt_tool_description.insert("1.0", tool.get("description", "") or "")
        self.txt_tool_before_rules.insert("1.0", tool.get("before_calling_rules", "") or "")
        self.txt_tool_calling_rules.insert("1.0", tool.get("calling_rules", "") or "")
        self.txt_tool_after_rules.insert("1.0", tool.get("after_calling_rules", "") or "")

    def _apply_tool_form_to_data(self):
        if self.current_agent_index is None or self.current_tool_index is None:
            return False
        if not (0 <= self.current_agent_index < len(self.agents)):
            return False

        agent = self.agents[self.current_agent_index]
        tools = agent.get("tools", []) or []
        if not (0 <= self.current_tool_index < len(tools)):
            return False

        tool = tools[self.current_tool_index]
        tool["name"] = self.var_tool_name.get().strip()
        tool["description"] = self.txt_tool_description.get("1.0", tk.END).rstrip("\n")
        tool["before_calling_rules"] = self.txt_tool_before_rules.get("1.0", tk.END).rstrip("\n")
        tool["calling_rules"] = self.txt_tool_calling_rules.get("1.0", tk.END).rstrip("\n")
        tool["after_calling_rules"] = self.txt_tool_after_rules.get("1.0", tk.END).rstrip("\n")

        agent["tools"] = tools
        self.dirty = True
        self._refresh_tree()
        return True

    # ===================== GENERAZIONE CALLING RULES DA my_services.xml =====================

    # ===================== SUPPORTO: DESCRIZIONE RICORSIVA CAMPI =====================

    def _describe_field_tree(self, el: ET.Element, prefix: str = "") -> list[str]:
        """
        Ritorna un elenco di righe di testo che descrivono ricorsivamente un campo
        (Param o altro elemento con sotto-campi) fino all'ultimo livello.
        """
        tag = el.tag
        name = (el.get("name") or "").strip()
        field_name = name or tag

        # path logico del campo (per es. "lines", "lines.item_code", ecc.)
        path = f"{prefix}.{field_name}" if prefix else field_name

        required_attr = el.get("required")
        if required_attr is None:
            required_txt = "obbligatorietà non specificata"
        else:
            required_txt = "obbligatorio" if required_attr.lower() == "true" else "opzionale"

        location = (el.get("location") or "").strip()
        type_ = (el.get("type") or "").strip()

        # descrizione: preferisco un eventuale sotto-tag <Description>, altrimenti testo diretto
        desc_el = el.find("Description")
        if desc_el is not None and desc_el.text and desc_el.text.strip():
            description = desc_el.text.strip()
        else:
            description = (el.text or "").strip() if (el.text and el.text.strip()) else ""

        # altri attributi generici
        extra_attrs = []
        for k, v in el.attrib.items():
            if k in ("name", "required", "location", "type"):
                continue
            extra_attrs.append(f"{k}={v}")

        parts = []
        if type_:
            parts.append(f"tipo={type_}")
        else:
            parts.append("tipo non specificato")

        if location:
            parts.append(f"posizione={location}")

        if required_attr is not None:
            parts.append(required_txt)

        if extra_attrs:
            parts.append("altri attributi: " + ", ".join(extra_attrs))

        if description:
            parts.append("descrizione: " + description)

        line = f"- {path}: " + ", ".join(parts)
        lines = [line]

        # figli (sotto-campi) – proseguo fino all'ultimo livello
        for child in el:
            if child.tag == "Description":
                continue
            lines.extend(self._describe_field_tree(child, prefix=path))

        return lines


    # ===================== GENERAZIONE CALLING RULES DA my_services.xml =====================

    def _on_generate_tool_rules_from_services(self):
        """
        Legge my_services.xml e genera una descrizione dettagliata per 'Calling rules'
        del tool corrente, basata sul Service con lo stesso name del tool.
        La descrizione include:
        - baseUrl, metodo HTTP, path
        - elenco dei parametri diretti
        - dettaglio ricorsivo della struttura dei campi (fino all'ultimo livello)
        - linee guida per l'LLM su come impostare gli arguments nel function calling
        """
        if self.current_agent_index is None or self.current_tool_index is None:
            messagebox.showwarning(
                "Nessun tool selezionato",
                "Seleziona prima un tool dall'albero a sinistra.",
            )
            return

        if not SERVICES_XML.exists():
            messagebox.showerror(
                "my_services.xml non trovato",
                f"Il file {SERVICES_XML} non esiste.\nCrealo prima o posizionalo nella stessa cartella di my_agents.xml.",
            )
            return

        # Determina il nome del tool
        tool_name = self.var_tool_name.get().strip()
        if not tool_name:
            agent = self.agents[self.current_agent_index]
            tools = agent.get("tools", []) or []
            if 0 <= self.current_tool_index < len(tools):
                tool_name = (tools[self.current_tool_index].get("name") or "").strip()

        if not tool_name:
            messagebox.showwarning(
                "Nome tool mancante",
                "Imposta prima il nome del tool (deve coincidere con il name del Service in my_services.xml).",
            )
            return

        # Carica my_services.xml
        try:
            tree = ET.parse(SERVICES_XML)
            root = tree.getroot()
        except Exception as e:
            messagebox.showerror("Errore XML", f"Impossibile leggere {SERVICES_XML}:\n{e}")
            return

        base_url = (root.get("baseUrl") or "").strip()

        # Cerca il Service corrispondente
        service_el = None
        for s in root.findall("Service"):
            s_name = (s.get("name") or "").strip()
            if s_name == tool_name:
                service_el = s
                break

        if service_el is None:
            messagebox.showwarning(
                "Service non trovato",
                f"In {SERVICES_XML} non è stato trovato nessun Service con name='{tool_name}'.",
            )
            return

        method = (service_el.get("method") or "GET").strip()
        path = (service_el.get("path") or "").strip()
        service_desc = service_el.findtext("Description", default="") or ""

        # Parametri diretti (livello immediato)
        params = []
        param_elements = []
        for p in service_el.findall("Param"):
            p_name = (p.get("name") or "").strip()
            p_required = (p.get("required") or "").strip().lower() == "true"
            p_location = (p.get("location") or "").strip() or "body"
            p_type = (p.get("type") or "").strip() or "string"
            if not p_name:
                continue
            params.append(
                {
                    "name": p_name,
                    "required": p_required,
                    "location": p_location,
                    "type": p_type,
                }
            )
            param_elements.append(p)

        lines = []
        lines.append(f"Questo tool corrisponde al servizio REST '{tool_name}'.")
        if service_desc:
            lines.append("")
            lines.append("Descrizione del servizio:")
            lines.append(service_desc.strip())

        lines.append("")
        lines.append("Dettagli tecnici del servizio REST:")
        if base_url:
            lines.append(f"- Base URL: {base_url}")
        if path:
            lines.append(f"- Path: {path}")
        lines.append(f"- Metodo HTTP: {method}")

        # riepilogo parametri di primo livello
        if params:
            lines.append("")
            lines.append("Parametri principali (primo livello) da fornire all'endpoint REST:")
            for p in params:
                obbl = "obbligatorio" if p["required"] else "opzionale"
                loc = p["location"]
                tipo = p["type"]
                lines.append(
                    f"- {p['name']} ({tipo}, {obbl}, posizione: {loc})"
                )

        # descrizione ricorsiva di tutta la struttura
        if param_elements:
            lines.append("")
            lines.append("Dettaglio completo della struttura dei campi (inclusi eventuali campi annidati):")
            for p_el in param_elements:
                lines.extend(self._describe_field_tree(p_el))

        lines.append("")
        lines.append("Regole per l'LLM per la chiamata tramite function calling:")
        lines.append(
            f"- Usa il tool MCP che invoca i servizi REST passando il nome del servizio '{tool_name}'."
        )
        lines.append(
            "- Prepara un oggetto 'arguments' con una chiave per ciascun parametro principale "
            "(vedi elenco dei parametri di primo livello) e, se presenti, popola correttamente "
            "i sotto-campi secondo la struttura dettagliata sopra."
        )
        lines.append(
            "- Prima di chiamare il tool, assicurati di aver raccolto dall'utente tutti i parametri obbligatori, "
            "inclusi quelli annidati nei sotto-oggetti."
        )
        lines.append(
            "- Non inventare valori mancanti: se un dato non è disponibile, chiedilo esplicitamente all'utente."
        )

        # Schema di chiamata (esempio generico)
        lines.append("")
        lines.append("Schema di chiamata suggerito (pseudocodice):")
        lines.append(f'service_name = "{tool_name}"')
        if params:
            lines.append("arguments = {")
            for p in params:
                lines.append(f'    "{p["name"]}": VALORE_{p["name"].upper()},  # vedi struttura dettagliata per eventuali sotto-campi')
            lines.append("}")
        else:
            lines.append("arguments = {}  # questo servizio non dichiara parametri di primo livello")

        new_text = "\n".join(lines)

        # Gestione del testo esistente nel campo Calling rules
        existing = self.txt_tool_calling_rules.get("1.0", tk.END).strip()
        if existing:
            res = messagebox.askyesno(
                "Sostituire il testo esistente?",
                "Il campo 'Calling rules' contiene già del testo.\n"
                "Vuoi sostituirlo con il testo generato da my_services.xml?",
            )
            if res:
                self.txt_tool_calling_rules.delete("1.0", tk.END)
                self.txt_tool_calling_rules.insert("1.0", new_text)
            else:
                self.txt_tool_calling_rules.insert("end", "\n\n" + new_text)
        else:
            self.txt_tool_calling_rules.delete("1.0", tk.END)
            self.txt_tool_calling_rules.insert("1.0", new_text)

        self.dirty = True



    # ===================== COMANDI TOOLBAR =====================

    def _on_apply(self):
        if self.current_selection_kind == "agent":
            if not self._apply_agent_form_to_data():
                return
        elif self.current_selection_kind == "tool":
            if not self._apply_tool_form_to_data():
                return
        else:
            return

    def _on_save(self):
        self._on_apply()
        if not self._save_agents():
            return

    def _on_exit(self):
        self._on_close()

    def _on_new_agent(self):
        new_agent = {
            "id": "",
            "name": "",
            "description": "",
            "role": "",
            "language_tone": "",
            "tools_usage": "",
            "main_flows": "",
            "error_handling": "",
            "extra_notes": "",
            "tools": [],
        }
        self.agents.append(new_agent)
        self.current_agent_index = len(self.agents) - 1
        self.current_tool_index = None
        self.current_selection_kind = "agent"
        self.dirty = True
        self._refresh_tree()

        iid = f"agent_{self.current_agent_index}"
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._load_agent_to_form(self.current_agent_index)
        self.notebook.select(self.agent_frame)

    def _on_delete_agent(self):
        if self.current_agent_index is None:
            return
        if not (0 <= self.current_agent_index < len(self.agents)):
            return

        agent = self.agents[self.current_agent_index]
        aid = agent.get("id", "") or "(senza id)"
        name = agent.get("name", "")

        if name and name != aid:
            label = f"{aid} ({name})"
        else:
            label = aid

        res = messagebox.askyesno(
            "Conferma eliminazione", f"Vuoi davvero eliminare l'agente '{label}'?"
        )
        if not res:
            return

        del self.agents[self.current_agent_index]
        self.current_agent_index = None
        self.current_tool_index = None
        self.current_selection_kind = None
        self.dirty = True
        self._refresh_tree()

    def _on_new_tool(self):
        if self.current_agent_index is None:
            messagebox.showwarning(
                "Nessun agente selezionato",
                "Seleziona prima un agente a cui aggiungere il tool.",
            )
            return
        if not (0 <= self.current_agent_index < len(self.agents)):
            return

        agent = self.agents[self.current_agent_index]
        tools = agent.get("tools", []) or []

        new_tool = {
            "name": "",
            "description": "",
            "before_calling_rules": "",
            "calling_rules": "",
            "after_calling_rules": "",
        }
        tools.append(new_tool)
        agent["tools"] = tools
        self.dirty = True

        self.current_tool_index = len(tools) - 1
        self.current_selection_kind = "tool"
        self._refresh_tree()

        # seleziona il nuovo tool
        iid = f"agent_{self.current_agent_index}_tool_{self.current_tool_index}"
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._load_tool_to_form(self.current_agent_index, self.current_tool_index)
        self.notebook.select(self.tool_frame)

    def _on_delete_tool(self):
        if self.current_agent_index is None or self.current_tool_index is None:
            return
        if not (0 <= self.current_agent_index < len(self.agents)):
            return

        agent = self.agents[self.current_agent_index]
        tools = agent.get("tools", []) or []
        if not (0 <= self.current_tool_index < len(tools)):
            return

        tool = tools[self.current_tool_index]
        tname = tool.get("name", "") or "(senza nome)"

        res = messagebox.askyesno(
            "Conferma eliminazione", f"Vuoi davvero eliminare il tool '{tname}'?"
        )
        if not res:
            return

        del tools[self.current_tool_index]
        agent["tools"] = tools
        self.current_tool_index = None
        self.current_selection_kind = "agent"
        self.dirty = True
        self._refresh_tree()

    # ===================== CHIUSURA APP =====================

    def _on_close(self):
        if self.dirty:
            res = messagebox.askyesnocancel(
                "Modifiche non salvate",
                "Ci sono modifiche non salvate.\nVuoi salvarle prima di uscire?",
            )
            if res is None:
                return
            elif res:
                if not self._save_agents():
                    return
        self.destroy()


def main():
    app = AgentsXmlEditor()
    app.mainloop()


if __name__ == "__main__":
    main()
