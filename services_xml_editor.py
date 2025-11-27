import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import xml.etree.ElementTree as ET

XML_FILE = Path("my_services.xml")


class MyServicesEditor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Editor my_services.xml")
        self.geometry("1100x650")

        self.xml_tree = None  # ET.ElementTree
        self.xml_root = None  # <RestServices>

        self.element_by_iid = {}  # tree iid -> Element
        self.dirty = False

        self._create_widgets()
        self._load_xml()
        self._build_tree()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI SETUP ----------

    def _create_widgets(self):
        # Toolbar
        toolbar = tk.Frame(self)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        tk.Button(toolbar, text="Nuovo Service", command=self.add_service).pack(
            side=tk.LEFT, padx=2, pady=2
        )
        tk.Button(toolbar, text="Aggiungi Header", command=self.add_header).pack(
            side=tk.LEFT, padx=2, pady=2
        )
        tk.Button(toolbar, text="Aggiungi Param", command=self.add_param).pack(
            side=tk.LEFT, padx=2, pady=2
        )
        tk.Button(toolbar, text="Aggiungi Field", command=self.add_field).pack(
            side=tk.LEFT, padx=2, pady=2
        )
        tk.Button(toolbar, text="Aggiungi Item", command=self.add_item).pack(
            side=tk.LEFT, padx=2, pady=2
        )
        tk.Button(toolbar, text="Elimina selezionato", command=self.delete_selected).pack(
            side=tk.LEFT, padx=2, pady=2
        )
        tk.Button(toolbar, text="Salva su file", command=self.save_xml).pack(
            side=tk.LEFT, padx=2, pady=2
        )

        # Paned window
        paned = tk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # Left: tree
        left_frame = tk.Frame(paned)
        paned.add(left_frame, minsize=250)  # sinistra ~1/3

        self.tree = ttk.Treeview(left_frame, show="tree")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        vsb = ttk.Scrollbar(left_frame, orient="vertical", command=self.tree.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        # Right: detail (più largo)
        right_frame = tk.Frame(paned)
        paned.add(right_frame, minsize=400)

        self.detail_frame = tk.Frame(right_frame, borderwidth=1, relief=tk.SUNKEN)
        self.detail_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.detail_widgets = {}  # field_name -> widget

        self._show_info_message("Seleziona un nodo a sinistra per modificarne i dettagli.")

    # ---------- XML LOAD/SAVE ----------

    def _load_xml(self):
        if XML_FILE.exists():
            try:
                self.xml_tree = ET.parse(XML_FILE)
                self.xml_root = self.xml_tree.getroot()
            except Exception as e:
                messagebox.showerror("Errore XML", f"Errore nel parsing di {XML_FILE}:\n{e}")
                # crea struttura base
                self.xml_root = ET.Element("RestServices", {"baseUrl": "http://localhost"})
                self.xml_tree = ET.ElementTree(self.xml_root)
        else:
            # crea nuovo file base
            self.xml_root = ET.Element("RestServices", {"baseUrl": "http://localhost"})
            self.xml_tree = ET.ElementTree(self.xml_root)

    def save_xml(self):
        if self.xml_tree is None:
            return
        try:
            self.xml_tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
            self.dirty = False
            messagebox.showinfo("Salvataggio", f"File salvato:\n{XML_FILE}")
        except Exception as e:
            messagebox.showerror("Errore salvataggio", str(e))

    # ---------- TREE BUILDING ----------

    def _build_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.element_by_iid.clear()

        # root visual node
        root_text = "RestServices"
        base_url = self.xml_root.get("baseUrl")
        if base_url:
            root_text += f" (baseUrl={base_url})"

        root_iid = self.tree.insert("", "end", text=root_text, open=True)
        self.element_by_iid[root_iid] = self.xml_root

        # children: Service
        for srv in self.xml_root.findall("Service"):
            self._add_service_to_tree(root_iid, srv)

    def _add_service_to_tree(self, parent_iid, service_elem):
        name = service_elem.get("name", "<senza_nome>")
        method = service_elem.get("method", "")
        path = service_elem.get("path", "")
        text = f"Service: {name} [{method} {path}]".strip()

        srv_iid = self.tree.insert(parent_iid, "end", text=text, open=False)
        self.element_by_iid[srv_iid] = service_elem

        # Headers
        for hdr in service_elem.findall("Header"):
            self._add_header_to_tree(srv_iid, hdr)

        # Params
        for prm in service_elem.findall("Param"):
            self._add_param_to_tree(srv_iid, prm)

    def _add_header_to_tree(self, parent_iid, header_elem):
        name = header_elem.get("name", "<senza_nome>")
        value = header_elem.get("value", "")
        text = f"Header: {name} = {value}"
        hdr_iid = self.tree.insert(parent_iid, "end", text=text, open=False)
        self.element_by_iid[hdr_iid] = header_elem

    def _add_param_to_tree(self, parent_iid, param_elem):
        name = param_elem.get("name", "<senza_nome>")
        ptype = param_elem.get("type", "")
        text = f"Param: {name} ({ptype})"
        prm_iid = self.tree.insert(parent_iid, "end", text=text, open=False)
        self.element_by_iid[prm_iid] = param_elem

        # Children Fields
        for fld in param_elem.findall("Field"):
            self._add_field_to_tree(prm_iid, fld)

    def _add_field_to_tree(self, parent_iid, field_elem):
        name = field_elem.get("name", "<senza_nome>")
        ftype = field_elem.get("type", "")
        text = f"Field: {name} ({ftype})"
        fld_iid = self.tree.insert(parent_iid, "end", text=text, open=False)
        self.element_by_iid[fld_iid] = field_elem

        # Item child (if any)
        for item in field_elem.findall("Item"):
            self._add_item_to_tree(fld_iid, item)

    def _add_item_to_tree(self, parent_iid, item_elem):
        itype = item_elem.get("type", "")
        text = f"Item ({itype})"
        item_iid = self.tree.insert(parent_iid, "end", text=text, open=False)
        self.element_by_iid[item_iid] = item_elem

        # Nested fields under item
        for fld in item_elem.findall("Field"):
            self._add_field_to_tree(item_iid, fld)

    # ---------- TREE EVENTS ----------

    def on_tree_select(self, event=None):
        selection = self.tree.selection()
        if not selection:
            return
        iid = selection[0]
        elem = self.element_by_iid.get(iid)
        if elem is None:
            return
        self._show_detail_for_element(iid, elem)

    # ---------- DETAIL PANEL ----------

    def _clear_detail(self):
        for widget in self.detail_frame.winfo_children():
            widget.destroy()
        self.detail_widgets.clear()

    def _show_info_message(self, msg):
        self._clear_detail()
        lbl = tk.Label(self.detail_frame, text=msg, anchor="nw", justify="left")
        lbl.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def _show_detail_for_element(self, iid, elem):
        self._clear_detail()

        tag = elem.tag

        title = tk.Label(
            self.detail_frame,
            text=f"Dettaglio: <{tag}>",
            font=("TkDefaultFont", 12, "bold"),
        )
        title.pack(anchor="w", padx=10, pady=(10, 5))

        form = tk.Frame(self.detail_frame)
        form.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        row = 0

        def add_entry(label_text, attr_name, default="", widget_type="entry", values=None):
            nonlocal row
            tk.Label(form, text=label_text).grid(row=row, column=0, sticky="w", pady=2)

            current = elem.get(attr_name, default)

            if widget_type == "entry":
                var = tk.StringVar(value=current)
                entry = tk.Entry(form, textvariable=var, width=60)
                entry.grid(row=row, column=1, sticky="we", pady=2)
                self.detail_widgets[attr_name] = var
            elif widget_type == "bool":
                var = tk.BooleanVar(value=(str(current).lower() == "true"))
                cb = tk.Checkbutton(form, variable=var)
                cb.grid(row=row, column=1, sticky="w", pady=2)
                self.detail_widgets[attr_name] = var
            elif widget_type == "combo":
                var = tk.StringVar(value=current)
                cb = ttk.Combobox(form, textvariable=var, values=values or [], width=20)
                cb.grid(row=row, column=1, sticky="w", pady=2)
                self.detail_widgets[attr_name] = var

            row += 1

        # Editor per tag
        if tag == "RestServices":
            add_entry("baseUrl", "baseUrl", default="http://localhost")
        elif tag == "Service":
            add_entry("name", "name", default="")
            add_entry("method", "method", default="GET")
            add_entry("path", "path", default="/")
            add_entry("baseUrlOverride", "baseUrlOverride", default="")
        elif tag == "Header":
            add_entry("name", "name", default="")
            add_entry("value", "value", default="")
            add_entry("env (opzionale)", "env", default="")
        elif tag == "Param":
            add_entry("name", "name", default="")
            add_entry("required", "required", default="true", widget_type="bool")
            add_entry(
                "location",
                "location",
                default="body",
                widget_type="combo",
                values=["path", "query", "body"],
            )
            add_entry(
                "type",
                "type",
                default="string",
                widget_type="combo",
                values=["string", "int", "number", "json", "object", "array"],
            )
        elif tag == "Field":
            add_entry(
                "name",
                "name",
                default="",
            )
            add_entry(
                "type",
                "type",
                default="string",
                widget_type="combo",
                values=["string", "int", "number", "json", "object", "array"],
            )
            add_entry("required", "required", default="true", widget_type="bool")
        elif tag == "Item":
            add_entry(
                "type",
                "type",
                default="object",
                widget_type="combo",
                values=["object", "string", "int", "number", "json"],
            )
        else:
            # generico per tag sconosciuti
            for attr_name, value in elem.attrib.items():
                add_entry(attr_name, attr_name, default=value)

        # Pulsante applica
        btn_frame = tk.Frame(self.detail_frame)
        btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        tk.Button(
            btn_frame,
            text="Applica modifiche",
            command=lambda: self._apply_detail_changes(iid, elem),
        ).pack(side=tk.LEFT)

    def _apply_detail_changes(self, iid, elem):
        # Update attributi da widgets
        for attr_name, widget in self.detail_widgets.items():
            if isinstance(widget, tk.BooleanVar):
                elem.set(attr_name, "true" if widget.get() else "false")
            else:
                value = widget.get()
                if value == "":
                    # rimuovi se vuoto
                    if attr_name in elem.attrib:
                        del elem.attrib[attr_name]
                else:
                    elem.set(attr_name, value)

        # Aggiorna il testo del nodo nell'albero
        tag = elem.tag
        if tag == "RestServices":
            base_url = elem.get("baseUrl", "")
            text = "RestServices"
            if base_url:
                text += f" (baseUrl={base_url})"
            self.tree.item(iid, text=text)
        elif tag == "Service":
            name = elem.get("name", "<senza_nome>")
            method = elem.get("method", "")
            path = elem.get("path", "")
            text = f"Service: {name} [{method} {path}]".strip()
            self.tree.item(iid, text=text)
        elif tag == "Header":
            name = elem.get("name", "<senza_nome>")
            value = elem.get("value", "")
            text = f"Header: {name} = {value}"
            self.tree.item(iid, text=text)
        elif tag == "Param":
            name = elem.get("name", "<senza_nome>")
            ptype = elem.get("type", "")
            text = f"Param: {name} ({ptype})"
            self.tree.item(iid, text=text)
        elif tag == "Field":
            name = elem.get("name", "<senza_nome>")
            ftype = elem.get("type", "")
            text = f"Field: {name} ({ftype})"
            self.tree.item(iid, text=text)
        elif tag == "Item":
            itype = elem.get("type", "")
            text = f"Item ({itype})"
            self.tree.item(iid, text=text)

        self.dirty = True

    # ---------- ADD / DELETE NODES ----------

    def _get_selected_element(self):
        selection = self.tree.selection()
        if not selection:
            return None, None
        iid = selection[0]
        elem = self.element_by_iid.get(iid)
        return iid, elem

    def add_service(self):
        # Service sempre figlio di RestServices
        parent_elem = self.xml_root

        new_service = ET.Element(
            "Service",
            {
                "name": "new_service",
                "method": "POST",
                "path": "/path",
            },
        )
        parent_elem.append(new_service)

        # aggiungi all'albero sotto il nodo root
        root_iid = self.tree.get_children("")[0]
        self._add_service_to_tree(root_iid, new_service)
        self.dirty = True

    def add_header(self):
        iid, elem = self._get_selected_element()
        if elem is None:
            messagebox.showwarning("Selezione mancante", "Seleziona prima un Service.")
            return

        # header solo sotto Service
        if elem.tag != "Service":
            messagebox.showwarning(
                "Nodo non valido",
                "Gli Header possono essere aggiunti solo ad un Service.",
            )
            return

        new_hdr = ET.Element(
            "Header",
            {
                "name": "X-Header-Name",
                "value": "",
            },
        )
        elem.append(new_hdr)
        self._add_header_to_tree(iid, new_hdr)
        self.dirty = True

    def add_param(self):
        iid, elem = self._get_selected_element()
        if elem is None:
            messagebox.showwarning("Selezione mancante", "Seleziona prima un Service.")
            return

        if elem.tag != "Service":
            messagebox.showwarning(
                "Nodo non valido",
                "I Param possono essere aggiunti solo ad un Service.",
            )
            return

        new_param = ET.Element(
            "Param",
            {
                "name": "new_param",
                "required": "true",
                "location": "body",
                "type": "string",
            },
        )
        elem.append(new_param)
        self._add_param_to_tree(iid, new_param)
        self.dirty = True

    def add_field(self):
        iid, elem = self._get_selected_element()
        if elem is None:
            messagebox.showwarning(
                "Selezione mancante",
                "Seleziona un Param, un Field (per sotto-campi) o un Item.",
            )
            return

        parent_elem = None
        parent_iid = None

        if elem.tag == "Param":
            parent_elem = elem
            parent_iid = iid
        elif elem.tag == "Item":
            parent_elem = elem
            parent_iid = iid
        elif elem.tag == "Field":
            # Field->Item->Field: se il Field ha un Item figlio, aggiungi sotto l'Item
            item_child = elem.find("Item")
            if item_child is None:
                messagebox.showwarning(
                    "Nodo non valido",
                    "Per aggiungere Field sotto un Field, questo deve avere un figlio <Item> (array).",
                )
                return
            parent_elem = item_child
            # trova iid dell'Item nel tree
            for child_iid in self.tree.get_children(iid):
                child_elem = self.element_by_iid.get(child_iid)
                if child_elem is item_child:
                    parent_iid = child_iid
                    break
            if parent_iid is None:
                return
        else:
            messagebox.showwarning(
                "Nodo non valido",
                "Seleziona un Param, un Field (con Item) o un Item.",
            )
            return

        new_field = ET.Element(
            "Field",
            {
                "name": "new_field",
                "type": "string",
                "required": "true",
            },
        )
        parent_elem.append(new_field)
        self._add_field_to_tree(parent_iid, new_field)
        self.dirty = True

    def add_item(self):
        iid, elem = self._get_selected_element()
        if elem is None:
            messagebox.showwarning("Selezione mancante", "Seleziona prima un Field.")
            return

        if elem.tag != "Field":
            messagebox.showwarning(
                "Nodo non valido",
                "Gli Item possono essere aggiunti solo ad un Field.",
            )
            return

        # evita più Item sotto lo stesso Field
        existing_item = elem.find("Item")
        if existing_item is not None:
            messagebox.showwarning("Già presente", "Questo Field ha già un Item.")
            return

        new_item = ET.Element(
            "Item",
            {
                "type": "object",
            },
        )
        elem.append(new_item)
        self._add_item_to_tree(iid, new_item)
        self.dirty = True

    def delete_selected(self):
        iid, elem = self._get_selected_element()
        if elem is None:
            return
        if elem is self.xml_root:
            messagebox.showwarning(
                "Operazione non permessa",
                "Non puoi eliminare il nodo root <RestServices>.",
            )
            return

        if not messagebox.askyesno(
            "Conferma eliminazione",
            "Vuoi eliminare il nodo selezionato e tutti i suoi figli?",
        ):
            return

        # remove from XML
        parent_elem = self._find_parent(self.xml_root, elem)
        if parent_elem is not None:
            parent_elem.remove(elem)

        # remove from tree (ricorsivo)
        self._remove_tree_node_recursive(iid)

        self.dirty = True

    def _remove_tree_node_recursive(self, iid):
        # remove children first
        for child_iid in list(self.tree.get_children(iid)):
            self._remove_tree_node_recursive(child_iid)
        # remove from map and tree
        self.element_by_iid.pop(iid, None)
        self.tree.delete(iid)

    def _find_parent(self, root, target):
        # depth-first search
        for child in root:
            if child is target:
                return root
            res = self._find_parent(child, target)
            if res is not None:
                return res
        return None

    # ---------- CLOSE ----------

    def on_close(self):
        if self.dirty:
            res = messagebox.askyesnocancel(
                "Modifiche non salvate",
                "Vuoi salvare le modifiche prima di uscire?",
            )
            if res:  # Yes
                self.save_xml()
                self.destroy()
            elif res is False:  # No
                self.destroy()
            else:  # Cancel
                return
        else:
            self.destroy()


if __name__ == "__main__":
    app = MyServicesEditor()
    app.mainloop()
