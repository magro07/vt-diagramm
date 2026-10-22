"""Web-Interface für vt-diagramm.

Starten: .venv/bin/python vt_diagramm_gui.py
Öffnet einen lokalen Browser auf http://127.0.0.1:7860

Die GUI deckt den häufigsten Fall ab: CSV-Stützpunkte hochladen, Titel und
Optionen wählen, Vorschau sehen, Dateien herunterladen. Die vollständige
TOML-Konfiguration (Vorwissen, Phasen, Beschriftungen) bleibt der
Expertenmodus über die Kommandozeile.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace

import gradio as gr

import vt_diagramm as v

REPO = Path(__file__).resolve().parent
BEISPIEL_CSV = REPO / "beispiel_stuetzpunkte.csv"
BEISPIEL_TOML = REPO / "beispiel.toml"


def _args(ausgabe, formate, kein_band):
    return SimpleNamespace(kein_band=bool(kein_band), ausgabe=ausgabe, formate=formate)


def _toml_text(wert):
    einzeilig = str(wert).replace("\r", " ").replace("\n", " ")
    return '"' + einzeilig.replace("\\", "\\\\").replace('"', '\\"') + '"'


def bericht_text(m):
    puffer = io.StringIO()
    with contextlib.redirect_stdout(puffer):
        v.bericht(m)
    return puffer.getvalue()


def _lade_beispiel_csv():
    return BEISPIEL_CSV.read_text(encoding="utf-8")


def _lade_beispiel_titel():
    import tomllib
    with BEISPIEL_TOML.open("rb") as f:
        cfg = tomllib.load(f)
    d = cfg.get("diagramm", {})
    return (
        str(d.get("titel", "")),
        str(d.get("untertitel", "")),
        str(d.get("beschreibung", "")),
    )


def erzeugen(csv_text, titel, untertitel, beschreibung, art, band, ziehungen, seed,
             verteilung, kein_band, formate):
    if not csv_text or not csv_text.strip():
        raise gr.Error("Bitte zuerst eine CSV-Datei mit Stützpunkten hochladen oder laden.")
    try:
        ziehungen = int(ziehungen)
    except (TypeError, ValueError):
        raise gr.Error("Ziehungen muss eine ganze Zahl sein.")
    if ziehungen < 10:
        raise gr.Error("Ziehungen müssen mindestens 10 sein.")
    seed_wert = None
    if seed is not None and str(seed).strip() not in ("", "zufällig"):
        try:
            seed_wert = int(seed)
        except (TypeError, ValueError):
            raise gr.Error("Seed muss eine ganze Zahl oder leer sein.")

    art_kurz = {"Geschwindigkeit v(t)": "v", "Weg s(t)": "s", "Beschleunigung a(t)": "a"}[art]
    band_kurz = {"Monte-Carlo (5–95 %-Band)": "monte-carlo", "Hülle (v ± u)": "huelle"}[band]
    formate_str = ",".join(f.strip() for f in formate if f and f.strip()) or "png"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        csv_pfad = tmp / "stuetzpunkte.csv"
        csv_pfad.write_text(csv_text, encoding="utf-8")
        toml_pfad = tmp / "konfig.toml"
        toml_pfad.write_text(
            "[diagramm]\n"
            f"titel = {_toml_text(str(titel).strip() or 'DIAGRAMM')}\n"
            f"untertitel = {_toml_text(str(untertitel).strip())}\n"
            f"beschreibung = {_toml_text(str(beschreibung).strip())}\n"
            f'art = "{art_kurz}"\n'
            f'band_modus = "{band_kurz}"\n'
            f'ziehungen = {ziehungen}\n'
            f'verteilung = "{verteilung}"\n'
            f'weg_anzeigen = true\n'
            f'beschriften = "auto"\n'
            f'formate = "{formate_str}"\n'
            + (f'seed = {seed_wert}\n' if seed_wert is not None else "")
            + "[achsen]\n"
            'x_label = "Zeit seit Beginn [s]"\n'
            "[stuetzen]\n"
            'datei = "stuetzpunkte.csv"\n',
            encoding="utf-8",
        )
        ausgabe = tmp / "diagramm"
        try:
            m = v.Modell(str(toml_pfad), art=art_kurz, band=band_kurz,
                         n=ziehungen, seed=seed_wert, verteilung=verteilung)
            m.rechnen()
            dateien = v.zeichne(m, _args(str(ausgabe), formate_str, kein_band))
        except SystemExit as e:
            raise gr.Error(str(e))
        text = bericht_text(m)
        text += f"\nZiehungen: {m.n} · Verteilung: {m.verteilung} · Seed: {m.seed if m.seed is not None else 'zufällig'}\n"
        text += "Ausgabe: " + ", ".join(dateien) + "\n"

        bilder = [str(tmp / f"{ausgabe.name}.{ext}") for ext in ("png", "svg", "pdf")
                  if (tmp / f"{ausgabe.name}.{ext}").exists()]
        png = next((b for b in bilder if b.endswith(".png")), None)
        downloads = [b for b in bilder if not b.endswith(".png")] or bilder
        return png, text, gr.update(value=downloads, visible=True)


with gr.Blocks(title="vt-diagramm") as demo:
    gr.Markdown(
        "# vt-diagramm\n"
        "Diagramm mit Grauzone aus manuell eingetragenen Stützpunkten — "
        "**v(t)**, **s(t)** oder **a(t)**. Für Vorwissen (Beschleunigung, Höhen), "
        "Phasen und Beschriftungen bitte die TOML-Konfiguration auf der "
        "Kommandozeile verwenden (`vt_diagramm.py konfig.toml`)."
    )
    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("### 1. Stützpunkte (CSV)")
            csv_datei = gr.UploadButton(
                label="CSV-Datei hochladen",
                file_types=[".csv"],
            )
            csv_text = gr.Textbox(
                label="CSV-Inhalt (editierbar)",
                lines=12,
                placeholder="t_s,name,v_kmh,u_kmh\n8.0,Tal,75,6",
            )
            with gr.Row():
                beispiel_btn = gr.Button("Beispieldaten laden")
            gr.Markdown("### 2. Diagramm")
            titel = gr.Textbox(label="Titel (kleine Zeile oben)", value="MEINE FAHRT")
            untertitel = gr.Textbox(label="Untertitel (große Überschrift)", value="")
            beschreibung = gr.Textbox(label="Beschreibung (Zeile unter der Überschrift)", value="")
            art = gr.Radio(
                label="Diagrammtyp",
                choices=["Geschwindigkeit v(t)", "Weg s(t)", "Beschleunigung a(t)"],
                value="Geschwindigkeit v(t)",
            )
            with gr.Accordion("Grauzone und Monte-Carlo", open=True):
                band = gr.Radio(
                    label="Band-Modus",
                    choices=["Monte-Carlo (5–95 %-Band)", "Hülle (v ± u)"],
                    value="Monte-Carlo (5–95 %-Band)",
                )
                ziehungen = gr.Number(label="Monte-Carlo-Ziehungen", value=2000, precision=0)
                seed = gr.Textbox(label="Seed (leer = zufällig)", value="")
                verteilung = gr.Radio(
                    label="Verteilung der Unsicherheiten",
                    choices=["uniform", "normal"],
                    value="uniform",
                )
                kein_band = gr.Checkbox(label="Grauzone nicht zeichnen", value=False)
            with gr.Accordion("Ausgabe", open=False):
                formate = gr.CheckboxGroup(
                    label="Dateiformate",
                    choices=["png", "svg", "pdf"],
                    value=["png", "svg", "pdf"],
                )
            erzeugen_btn = gr.Button("Diagramm erzeugen", variant="primary", size="lg")
        with gr.Column(scale=2):
            gr.Markdown("### Vorschau")
            bild = gr.Image(label="Diagramm", type="filepath")
            bericht = gr.Code(label="Bericht", language="markdown", lines=14)
            downloads = gr.Files(label="Download", visible=False)

    def _lese_datei(dateien):
        if not dateien:
            return gr.update(), gr.update()
        return Path(dateien[0]).read_text(encoding="utf-8"), gr.update()

    csv_datei.change(_lese_datei, inputs=csv_datei, outputs=[csv_text, bericht])

    def _lade_beispiel():
        titel, untertitel, beschreibung = _lade_beispiel_titel()
        return _lade_beispiel_csv(), titel, untertitel, beschreibung

    beispiel_btn.click(
        _lade_beispiel,
        outputs=[csv_text, titel, untertitel, beschreibung],
    )
    erzeugen_btn.click(
        erzeugen,
        inputs=[csv_text, titel, untertitel, beschreibung, art, band, ziehungen, seed,
                verteilung, kein_band, formate],
        outputs=[bild, bericht, downloads],
    )


if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7860, show_error=True)
