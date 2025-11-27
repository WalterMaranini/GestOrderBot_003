import tkinter as tk
from tkinter import ttk, messagebox
import json
import re
from urllib.parse import urlparse


class CurlToXmlApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Curl → XML servizio (my_services.xml)")
        self.geometry("1100x650")

        # Layout principale: due colonne (curl a sinistra, xml a destra)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        # Label campo curl
        lbl_curl = ttk.Label(self, text="curl")
        lbl_curl.grid(row=0, column=0, sticky="w", padx=5, pady=5)

        # Label campo xml
        lbl_xml = ttk.Label(self, text="xml")
        lbl_xml.grid(row=0, column=1, sticky="w", padx=5, pady=5)

        # Text per curl
        self.txt_curl = tk.Text(self, wrap="word")
        self.txt_curl.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        # Text per xml
        self.txt_xml = tk.Text(self, wrap="word")
        self.txt_xml.grid(row=1, column=1, sticky="nsew", padx=5, pady=5)

        # Scrollbar condivisa no, ne metto una per ognuno
        curl_scroll = ttk.Scrollbar(self, orient="vertical", command=self.txt_curl.yview)
        curl_scroll.grid(row=1, column=0, sticky="nse", padx=(0, 5))
        self.txt_curl.configure(yscrollcommand=curl_scroll.set)

        xml_scroll = ttk.Scrollbar(self, orient="vertical", command=self.txt_xml.yview)
        xml_scroll.grid(row=1, column=1, sticky="nse", padx=(0, 5))
        self.txt_xml.configure(yscrollcommand=xml_scroll.set)

        # Bottone Genera
        btn_generate = ttk.Button(self, text="Genera XML servizio",
                                  command=self.on_generate_xml)
        btn_generate.grid(row=2, column=0, columnspan=2, pady=10)

    # ---------------------- CALLBACK UI ----------------------

    def on_generate_xml(self):
        curl_text = self.txt_curl.get("1.0", "end").strip()
        if not curl_text:
            messagebox.showwarning("Attenzione", "Inserisci un comando curl nel campo 'curl'.")
            return

        try:
            xml_snippet = self.curl_to_service_xml(curl_text)
        except Exception as e:
            messagebox.showerror("Errore", f"Errore nella generazione XML:\n{e}")
            return

        self.txt_xml.delete("1.0", "end")
        self.txt_xml.insert("1.0", xml_snippet)

    # ---------------------- LOGICA DI CONVERSIONE ----------------------

    def curl_to_service_xml(self, curl_str: str) -> str:
        """
        Converte un comando curl in un blocco <Service> per my_services.xml.
        Assunzioni:
          - curl simile a quello fornito (headers con --header, body con -d '...' JSON-like)
          - URL finale tra apici singoli.
        """

        # 1) Metodo HTTP
        method_match = re.search(r"-X\s+(\w+)", curl_str)
        method = method_match.group(1).upper() if method_match else "POST"

        # 2) Headers
        headers = {}
        # --header 'Name: value'
        for m in re.finditer(r"--header\s+'([^']+)'", curl_str):
            hv = m.group(1)
            if ":" in hv:
                name, val = hv.split(":", 1)
                headers[name.strip()] = val.strip()

        # Supporto anche -H "Name: value"
        for m in re.finditer(r"-H\s+\"([^\"]+)\"", curl_str):
            hv = m.group(1)
            if ":" in hv:
                name, val = hv.split(":", 1)
                headers[name.strip()] = val.strip()

        # 3) URL
        url_matches = re.findall(r"'(https?://[^']+)'", curl_str)
        if not url_matches:
            raise ValueError("Impossibile trovare l'URL (es. 'http://...') nel curl.")
        url = url_matches[-1]  # prendo l'ultima occorrenza

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"URL non valido: {url}")

        base_override = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"

        # Nome servizio di default: ultimo pezzo del path, lowercase
        service_name = self._build_service_name_from_path(path)

        # 4) Body JSON (opzionale)
        body_json = None
        data_match = re.search(r"-d\s+'([^']+)'", curl_str, re.DOTALL)
        if not data_match:
            # alcuni curl usano --data-raw
            data_match = re.search(r"--data-raw\s+'([^']+)'", curl_str, re.DOTALL)

        if data_match:
            raw_body = data_match.group(1)

            # pulizia grezza dei "\" e dei ritorni a capo stile documentazione
            cleaned = raw_body.replace("\\\n", " ")
            cleaned = cleaned.replace("\\\r\n", " ")
            cleaned = cleaned.replace("\\", " ")

            # rimozione virgole prima di } o ] (trailing comma tipici degli esempi)
            cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

            # normalizza spazi
            cleaned = re.sub(r"\s+", " ", cleaned).strip()

            try:
                body_json = json.loads(cleaned)
            except json.JSONDecodeError as je:
                raise ValueError(f"Body JSON non valido dopo la pulizia:\n{cleaned}\n\nDettaglio: {je}")

        # 5) Generazione XML
        lines = []

        # N.B. generiamo solo lo snippet <Service>, da incollare in my_services.xml
        lines.append(
            f'<Service name="{self.xml_escape(service_name)}" '
            f'method="{method}" '
            f'path="{self.xml_escape(path)}" '
            f'baseUrlOverride="{self.xml_escape(base_override)}">'
        )
        lines.append("")

        # 5a) Headers coerenti con il tuo formalismo
        # X-BC-Gruppo
        gruppo = headers.get("X-BC-Gruppo") or headers.get("X-BC-gruppo")
        if gruppo:
            lines.append(f'  <Header name="X-BC-Gruppo" value="{self.xml_escape(gruppo.upper())}" />')

        # X-BC-Authorization -> env (NON metto il token in chiaro)
        if "X-BC-Authorization" in headers:
            lines.append('  <Header name="X-BC-Authorization" env="BC_AUTH_TOKEN" />')

        # Content-Type
        ct = headers.get("Content-Type")
        if ct:
            lines.append(f'  <Header name="Content-Type" value="{self.xml_escape(ct)}" />')

        # Accept
        acc = headers.get("Accept")
        if acc:
            lines.append(f'  <Header name="Accept" value="{self.xml_escape(acc)}" />')

        # Altri headers eventuali
        for hname, hval in headers.items():
            if hname in ("X-BC-Gruppo", "X-BC-Authorization", "Content-Type", "Accept"):
                continue
            lines.append(
                f'  <Header name="{self.xml_escape(hname)}" value="{self.xml_escape(hval)}" />'
            )

        lines.append("")

        # 5b) Parametri body come JSON schema (stile create_order)
        if body_json is not None:
            param_name, param_value = self._detect_main_param(body_json)
            lines.append(
                f'  <Param name="{self.xml_escape(param_name)}" '
                f'required="true" location="body" type="object">'
            )
            lines.append("")
            # Oggetto principale -> campi
            fields_lines = self._build_fields_from_json(param_value, indent="    ", top_level=True)
            lines.extend(fields_lines)
            lines.append("  </Param>")

        lines.append("</Service>")

        return "\n".join(lines)

    # ---------------------- HELPERS ----------------------

    def _build_service_name_from_path(self, path: str) -> str:
        """
        Ricava un nome di servizio dal path, es:
          /api/ES_ORV/App-Sistemi/Crea -> crea
        """
        parts = [p for p in path.split("/") if p]
        if not parts:
            return "service_from_curl"
        last = parts[-1]
        # pulizia minima
        last = re.sub(r"[^a-zA-Z0-9_]", "_", last)
        if not last:
            last = "service_from_curl"
        return last.lower()

    def _detect_main_param(self, body_json):
        """
        Se il body è del tipo { "Parametri": { ... } } prende "Parametri" come Param principale.
        Altrimenti usa un generico "body".
        """
        if isinstance(body_json, dict) and len(body_json) == 1:
            key = next(iter(body_json.keys()))
            return key, body_json[key]
        return "body", body_json

    def _build_fields_from_json(self, value, indent="    ", top_level=False):
        """
        Converte una struttura JSON (dict/list/valore) in <Field> annidati.
        - top_level=True: i campi immediatamente sotto <Param> → required="true"
        - nested      : i campi più interni            → required="false"
        """
        lines = []

        if isinstance(value, dict):
            for k, v in value.items():
                req = "true" if top_level else "false"

                if isinstance(v, list):
                    # Array
                    lines.append(
                        f'{indent}<Field name="{self.xml_escape(k)}" type="array" required="{req}">'
                    )
                    item_indent = indent + "  "

                    if v:
                        first = v[0]
                        if isinstance(first, dict):
                            lines.append(f'{item_indent}<Item type="object">')
                            inner_indent = item_indent + "  "
                            inner_lines = self._build_fields_from_json(
                                first, indent=inner_indent, top_level=False
                            )
                            lines.extend(inner_lines)
                            lines.append(f'{item_indent}</Item>')
                        else:
                            item_type = self._infer_type(first)
                            lines.append(
                                f'{item_indent}<Item type="{item_type}" />'
                            )
                    else:
                        # array vuoto -> assumo array di oggetti
                        lines.append(f'{item_indent}<Item type="object" />')

                    lines.append(f'{indent}</Field>')

                elif isinstance(v, dict):
                    # Oggetto annidato
                    lines.append(
                        f'{indent}<Field name="{self.xml_escape(k)}" type="object" required="{req}">'
                    )
                    inner_indent = indent + "  "
                    inner_lines = self._build_fields_from_json(
                        v, indent=inner_indent, top_level=False
                    )
                    lines.extend(inner_lines)
                    lines.append(f'{indent}</Field>')

                else:
                    # Campo semplice
                    ftype = self._infer_type(v)
                    lines.append(
                        f'{indent}<Field name="{self.xml_escape(k)}" '
                        f'type="{ftype}" required="{req}" />'
                    )

        else:
            # Caso limite: il body principale NON è un oggetto
            ftype = self._infer_type(value)
            lines.append(
                f'{indent}<Field name="value" type="{ftype}" required="true" />'
            )

        return lines

    def _infer_type(self, v):
        """Inferenza grezza tipo JSON → tipo XML."""
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "number"
        if isinstance(v, list):
            return "array"
        if isinstance(v, dict):
            return "object"
        return "string"

    def xml_escape(self, s: str) -> str:
        """Escape minimale per uso in attributi XML."""
        return (
            str(s)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )


if __name__ == "__main__":
    app = CurlToXmlApp()
    app.mainloop()
