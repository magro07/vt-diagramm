"""Erzeugt Geschwindigkeit-Zeit-Diagramme mit Grauzone.

Eingabe: eine TOML-Konfiguration. Die Stützpunkte kommen aus einer CSV-Datei,
Vorwissen (Beschleunigungs- und Höhenangaben) steht in der TOML. Die Grauzone
entsteht per Monte-Carlo-Simulation aus den angegebenen Unsicherheiten.
Nichts wird aus einem Video automatisch ausgelesen.

Aufruf: .venv/bin/python vt_diagramm.py beispiel.toml
"""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
import tomllib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, MultipleLocator
import numpy as np
from scipy.interpolate import PchipInterpolator


G = 9.81
KMH = 3.6
RASTER_PUNKTE = 1200
FARBEN = {
    "hintergrund": "#f7faff",
    "flaeche": "white",
    "linie": "#176cb5",
    "band": "#176cb5",
    "text": "#132c42",
    "akzent": "#176cb5",
    "grau": "#4d6275",
    "gitter": "#dfe8f1",
    "abgeleitet": "#0f766e",
    "marke": "#8aa0b5",
}


def zahl(wert, standard=None):
    if wert is None or (isinstance(wert, str) and not wert.strip()):
        if standard is None:
            raise ValueError("Zahlenwert fehlt")
        return float(standard)
    if isinstance(wert, bool):
        raise ValueError(f"ungültiger Zahlenwert: {wert!r}")
    if isinstance(wert, (int, float)):
        return float(wert)
    return float(str(wert).strip().replace(",", "."))


def hole_zahl(eintrag, schluessel, standard=None):
    wert = eintrag.get(schluessel)
    if wert is None or (isinstance(wert, str) and not wert.strip()):
        if standard is None:
            raise SystemExit(f"Fehler: Pflichtfeld '{schluessel}' fehlt")
        return float(standard)
    try:
        return zahl(wert)
    except (TypeError, ValueError):
        raise SystemExit(f"Fehler: '{schluessel}' ist kein gültiger Zahlenwert: {wert!r}")


def komma(x, stellen=1):
    text = f"{x:.{stellen}f}"
    if stellen > 0:
        text = text.rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def video_sekunden(text):
    text = str(text).strip().replace(",", ".")
    teile = text.split(":")
    if len(teile) == 1:
        return float(teile[0])
    if len(teile) == 2:
        return int(teile[0]) * 60 + float(teile[1])
    if len(teile) == 3:
        return int(teile[0]) * 3600 + int(teile[1]) * 60 + float(teile[2])
    raise SystemExit(f"Fehler: Videozeit nicht lesbar: {text!r} (erwartet z. B. 0:42,5)")


@dataclass
class Punkt:
    name: str
    t: float
    v_ms: float | None
    u_v_ms: float = 0.0
    u_t: float = 0.0
    art: str = "stuetze"
    herkunft: str = "eingabe"
    referenz: str | None = None
    ref_u_ms: float | None = None
    hoehe_m: float = 0.0
    u_hoehe_m: float = 0.0
    verlust_ms: float = 0.0
    u_verlust_ms: float = 0.0
    modus: str = "fortpflanzung"
    u_fix_ms: float = 0.0
    dauer_s: float = 0.0
    u_dauer_s: float = 0.0
    t_start: float = 0.0


class Modell:
    def __init__(self, pfad, n=None, seed=None, verteilung=None, band=None):
        self.pfad = Path(pfad).expanduser().resolve()
        if not self.pfad.exists():
            raise SystemExit(f"Fehler: Konfiguration nicht gefunden: {self.pfad}")
        with self.pfad.open("rb") as f:
            self.cfg = tomllib.load(f)
        d = self.cfg.get("diagramm", {})
        self.n = int(n if n is not None else d.get("ziehungen", 2000))
        self.seed = seed if seed is not None else d.get("seed")
        self.verteilung = str(verteilung or d.get("verteilung", "uniform")).strip().lower()
        if self.verteilung not in ("uniform", "normal"):
            raise SystemExit("Fehler: 'verteilung' muss 'uniform' oder 'normal' sein")
        self.band_modus = str(band or d.get("band_modus", "monte-carlo")).strip().lower()
        if self.band_modus in ("huelle", "hülle", "envelope"):
            self.band_modus = "huelle"
        elif self.band_modus in ("monte-carlo", "montecarlo", "perzentil"):
            self.band_modus = "monte-carlo"
        else:
            raise SystemExit("Fehler: 'band_modus' muss 'monte-carlo' oder 'huelle' sein")
        if self.n < 10:
            raise SystemExit("Fehler: --n muss mindestens 10 sein")
        self.punkte: list[Punkt] = []
        self.nach_name: dict[str, Punkt] = {}
        self.fakten: list[dict] = []
        self.geklippt: dict[str, int] = {}
        self._lese_stuetzen()
        self._lese_bekannt()
        self.punkte.sort(key=lambda p: p.t)
        self._pruefe()
        self._nominal_aufloesen()
        self.raster = np.linspace(self.punkte[0].t, self.punkte[-1].t, RASTER_PUNKTE)
        self.nom_kurve = PchipInterpolator(
            np.array([p.t for p in self.punkte]),
            np.array([p.v_ms for p in self.punkte]),
        )(self.raster)
        self.weg_nom = float(np.trapezoid(self.nom_kurve, self.raster))
        self.p5 = self.p50 = self.p95 = None
        self.kurven = None
        self.weg = (0.0, 0.0, 0.0)
        self.a_werte: dict[str, tuple] = {}

    def _fuege_hinzu(self, punkt):
        if punkt.name in self.nach_name:
            raise SystemExit(f"Fehler: Name doppelt vergeben: '{punkt.name}'")
        self.punkte.append(punkt)
        self.nach_name[punkt.name] = punkt

    def _lese_stuetzen(self):
        eintrag = self.cfg.get("stuetzen") or {}
        datei = eintrag.get("datei")
        if not datei:
            raise SystemExit("Fehler: [stuetzen] datei fehlt in der Konfiguration")
        pfad = (self.pfad.parent / str(datei)).resolve()
        if not pfad.exists():
            raise SystemExit(f"Fehler: Stützpunktdatei nicht gefunden: {pfad}")
        with pfad.open(encoding="utf-8", newline="") as f:
            leser = csv.DictReader(f)
            felder = leser.fieldnames or []

            def spalte(*kandidaten):
                for kandidat in kandidaten:
                    if kandidat in felder:
                        return kandidat
                return None

            f_t = spalte("zeit_seit_launch_s", "t_s", "zeit_s")
            f_name = spalte("element", "name", "bezeichnung")
            f_v = spalte("geschwindigkeit_kmh", "v_kmh")
            f_u = spalte("unsicherheit_kmh", "u_kmh", "unsicherheit")
            f_ut = spalte("unsicherheit_s", "u_s")
            for feld, was in ((f_t, "Zeit"), (f_name, "Name"), (f_v, "Geschwindigkeit (km/h)")):
                if feld is None:
                    raise SystemExit(
                        f"Fehler: Spalte für {was} fehlt in {pfad.name}. Vorhanden: {felder}"
                    )
            for nummer, zeile in enumerate(leser, start=2):
                name = (zeile.get(f_name) or "").strip()
                if not name:
                    raise SystemExit(f"Fehler: {pfad.name}, Zeile {nummer}: Name fehlt")
                t = hole_zahl(zeile, f_t)
                v = hole_zahl(zeile, f_v)
                u = hole_zahl(zeile, f_u, 0.0) if f_u else 0.0
                ut = hole_zahl(zeile, f_ut, 0.0) if f_ut else 0.0
                if min(t, v, u, ut) < 0:
                    raise SystemExit(
                        f"Fehler: {pfad.name}, Zeile {nummer}: negative Werte sind nicht erlaubt"
                    )
                self._fuege_hinzu(Punkt(name=name, t=t, v_ms=v / KMH, u_v_ms=u / KMH, u_t=ut))

    def _lese_bekannt(self):
        for eintrag in self.cfg.get("bekannt", []):
            art = str(eintrag.get("art", "")).strip().lower()
            name = str(eintrag.get("name", "")).strip()
            if not name:
                raise SystemExit("Fehler: [[bekannt]] ohne 'name'")
            if art.startswith("beschl"):
                self._fakt_beschleunigung(eintrag, name)
            elif art.startswith("höh") or art.startswith("hoeh"):
                self._fakt_hoehe(eintrag, name)
            else:
                raise SystemExit(
                    f"Fehler: unbekannte Art '{art}' bei '{name}' (erlaubt: beschleunigung, hoehe)"
                )

    def _fakt_beschleunigung(self, e, name):
        t0 = hole_zahl(e, "t_s", 0.0)
        dauer = hole_zahl(e, "dauer_s")
        von = hole_zahl(e, "von_kmh")
        nach = hole_zahl(e, "nach_kmh")
        u_von = hole_zahl(e, "u_von_kmh", 0.0)
        u_nach = hole_zahl(e, "u_nach_kmh", 0.0)
        u_d = hole_zahl(e, "u_dauer_s", 0.0)
        u_t0 = hole_zahl(e, "u_t_s", 0.0)
        if dauer <= 0:
            raise SystemExit(f"Fehler: 'dauer_s' muss bei '{name}' größer als 0 sein")
        if min(t0, von, nach, u_von, u_nach, u_d, u_t0) < 0:
            raise SystemExit(f"Fehler: negative Werte bei '{name}' sind nicht erlaubt")
        name_start = str(e.get("name_start", f"{name}-Beginn")).strip()
        self._fuege_hinzu(Punkt(
            name=name_start, t=t0, v_ms=von / KMH, u_v_ms=u_von / KMH, u_t=u_t0,
            art="bek_start", herkunft="beschleunigung",
        ))
        self._fuege_hinzu(Punkt(
            name=name, t=t0 + dauer, v_ms=nach / KMH, u_v_ms=u_nach / KMH,
            art="bek_ende", herkunft="beschleunigung",
            dauer_s=dauer, u_dauer_s=u_d, t_start=t0,
        ))
        self.fakten.append({
            "art": "beschleunigung", "name": name, "name_start": name_start,
            "t": t0, "dauer": dauer, "von": von, "nach": nach,
        })

    def _fakt_hoehe(self, e, name):
        t = hole_zahl(e, "t_s")
        hoehe = hole_zahl(e, "hoehe_m")
        u_h = hole_zahl(e, "u_hoehe_m", 0.0)
        verlust = hole_zahl(e, "verlust_kmh", 0.0)
        u_verlust = hole_zahl(e, "u_verlust_kmh", 0.0)
        u_t = hole_zahl(e, "u_t_s", 0.0)
        referenz = str(e.get("referenz", "")).strip()
        if not referenz:
            raise SystemExit(f"Fehler: 'referenz' fehlt bei '{name}'")
        modus = str(e.get("unsicherheit_modus", "fortpflanzung")).strip().lower()
        if modus not in ("fortpflanzung", "fest"):
            raise SystemExit(
                f"Fehler: 'unsicherheit_modus' muss 'fortpflanzung' oder 'fest' sein ({name})"
            )
        u_fix = hole_zahl(e, "u_v_kmh", 0.0)
        ref_u = e.get("referenz_unsicherheit_kmh")
        ref_u_ms = None if ref_u is None else zahl(ref_u) / KMH
        if min(t, hoehe, u_h, verlust, u_verlust, u_t, u_fix) < 0:
            raise SystemExit(f"Fehler: negative Werte bei '{name}' sind nicht erlaubt")
        if modus == "fest" and u_fix <= 0:
            print(f"Hinweis: '{name}' nutzt 'fest', aber u_v_kmh ist 0; es gibt dort keine Grauzone.")
        self._fuege_hinzu(Punkt(
            name=name, t=t, v_ms=None, u_t=u_t, art="hoehe", herkunft="hoehe",
            referenz=referenz, ref_u_ms=ref_u_ms, hoehe_m=hoehe, u_hoehe_m=u_h,
            verlust_ms=verlust / KMH, u_verlust_ms=u_verlust / KMH,
            modus=modus, u_fix_ms=u_fix / KMH,
        ))
        self.geklippt[name] = 0
        self.fakten.append({
            "art": "hoehe", "name": name, "t": t, "referenz": referenz,
            "hoehe_m": hoehe, "verlust_kmh": verlust, "modus": modus,
        })

    def _pruefe(self):
        if len(self.punkte) < 2:
            raise SystemExit("Fehler: mindestens zwei Stützpunkte nötig")
        for a, b in zip(self.punkte, self.punkte[1:]):
            if abs(a.t - b.t) < 1e-9:
                raise SystemExit(
                    f"Fehler: zwei Stützpunkte zur selben Zeit {a.t:g} s: '{a.name}' und '{b.name}'"
                )
        for p in self.punkte:
            if p.art == "hoehe" and p.referenz not in self.nach_name:
                raise SystemExit(
                    f"Fehler: Referenz '{p.referenz}' von '{p.name}' existiert nicht"
                )

    def _nominal_aufloesen(self):
        for p in self.punkte:
            self._nominal(p, [])

    def _nominal(self, p, kette):
        if p.v_ms is not None:
            return p.v_ms
        if p.name in kette:
            raise SystemExit(f"Fehler: Zirkelbezug beim Vorwissen über '{p.name}'")
        kette.append(p.name)
        vref = self._nominal(self.nach_name[p.referenz], kette)
        q = vref * vref - 2 * G * p.hoehe_m - p.verlust_ms * p.verlust_ms
        if q <= 0:
            print(
                f"Hinweis: '{p.name}' erreicht den Scheitel rechnerisch nicht "
                f"(Energie reicht nicht); es wird 0 km/h angenommen."
            )
        p.v_ms = math.sqrt(q) if q > 0 else 0.0
        kette.pop()
        return p.v_ms

    def _zieh(self, rng, wert, u):
        if u <= 0:
            return float(wert)
        if self.verteilung == "normal":
            return float(rng.normal(wert, u))
        return float(rng.uniform(wert - u, wert + u))

    def _zeit(self, p, rng, ctx):
        if p.art == "bek_ende":
            return p.t_start + max(0.0, self._zieh(rng, p.dauer_s, p.u_dauer_s))
        return p.t + self._zieh(rng, 0.0, p.u_t)

    def _wert(self, p, rng, ctx):
        if p.art == "hoehe":
            if p.modus == "fest":
                return max(0.0, self._zieh(rng, p.v_ms, p.u_fix_ms))
            vref = self._hole(p.referenz, rng, ctx)[1]
            if p.ref_u_ms is not None:
                vref = max(0.0, self._zieh(rng, vref, p.ref_u_ms))
            h = max(0.0, self._zieh(rng, p.hoehe_m, p.u_hoehe_m))
            verlust = max(0.0, self._zieh(rng, p.verlust_ms, p.u_verlust_ms))
            q = vref * vref - 2 * G * h - verlust * verlust
            if q <= 0:
                self.geklippt[p.name] += 1
                return 0.0
            return math.sqrt(q)
        return max(0.0, self._zieh(rng, p.v_ms, p.u_v_ms))

    def _hole(self, name, rng, ctx):
        if name not in ctx:
            p = self.nach_name[name]
            ctx[name] = (self._zeit(p, rng, ctx), self._wert(p, rng, ctx))
        return ctx[name]

    def rechnen(self):
        rng = np.random.default_rng(self.seed)
        namen = [p.name for p in self.punkte]
        raster = self.raster
        kurven = np.empty((self.n, raster.size))
        punktwerte = np.empty((self.n, len(namen)))
        wege = np.empty(self.n)
        beschleunigungen = [f for f in self.fakten if f["art"] == "beschleunigung"]
        a_proben = {f["name"]: np.empty(self.n) for f in beschleunigungen}
        for i in range(self.n):
            ctx = {}
            for name in namen:
                self._hole(name, rng, ctx)
            zeiten = np.array([ctx[k][0] for k in namen])
            werte = np.array([ctx[k][1] for k in namen])
            punktwerte[i] = werte
            reihe = np.argsort(zeiten, kind="stable")
            z = zeiten[reihe] + np.arange(zeiten.size) * 1e-9
            w = werte[reihe]
            kurven[i] = PchipInterpolator(z, w)(raster)
            wege[i] = float(np.trapezoid(kurven[i], raster))
            for f in beschleunigungen:
                t0, v0 = ctx[f["name_start"]]
                t1, v1 = ctx[f["name"]]
                dt = t1 - t0
                a_proben[f["name"]][i] = (v1 - v0) / dt if dt > 0 else np.nan
        self.raster = raster
        self.kurven = kurven
        t_arr = np.array([p.t for p in self.punkte])
        if self.band_modus == "huelle":
            unten_p, oben_p = np.percentile(punktwerte, [5, 95], axis=0)
            self.p5 = PchipInterpolator(t_arr, unten_p)(raster)
            self.p95 = PchipInterpolator(t_arr, oben_p)(raster)
            self.p50 = None
            self.weg = (self.weg_nom, self.weg_nom, self.weg_nom)
        else:
            self.p5, self.p50, self.p95 = np.percentile(kurven, [5, 50, 95], axis=0)
            self.weg = tuple(float(x) for x in np.percentile(wege, [50, 5, 95]))
        self.a_werte = {
            name: tuple(float(x) for x in np.nanpercentile(werte, [50, 5, 95]))
            for name, werte in a_proben.items()
        }


def bericht(m):
    herkunft = {"eingabe": "CSV", "beschleunigung": "Vorwissen", "hoehe": "Vorwissen"}
    print(f"Konfiguration: {m.pfad.name}")
    print(f"Stützpunkte: {len(m.punkte)} ({sum(1 for p in m.punkte if p.herkunft != 'eingabe')} aus Vorwissen)")
    for p in m.punkte:
        if p.v_ms is None:
            continue
        u_kmh = p.u_fix_ms * KMH if (p.art == "hoehe" and p.modus == "fest") else p.u_v_ms * KMH
        print(
            f"  {p.t:7.2f} s  {p.v_ms * KMH:8.2f} km/h  "
            f"±{u_kmh:5.2f} km/h  {p.name} [{herkunft[p.herkunft]}]"
        )
    for f in m.fakten:
        if f["art"] == "beschleunigung":
            a = m.a_werte.get(f["name"])
            zusatz = ""
            if a:
                zusatz = f" → ā = {komma(a[0], 2)} m/s² = {komma(a[0] / G, 2)} g (5–95 %: {komma(a[1], 2)}–{komma(a[2], 2)} m/s²)"
            print(f"  Vorwissen: {f['von']:g}–{f['nach']:g} km/h in {f['dauer']:g} s ({f['name']}){zusatz}")
        else:
            punkt = m.nach_name[f["name"]]
            print(
                f"  Vorwissen: {f['hoehe_m']:g} m über '{f['referenz']}' "
                f"→ {komma((punkt.v_ms or 0) * KMH, 1)} km/h bei '{f['name']}' ({f['modus']})"
                + (
                    f", {m.geklippt[f['name']]}/{m.n} Ziehungen reichten nicht bis zum Scheitel"
                    if m.geklippt.get(f["name"])
                    else ""
                )
            )
    print(f"Weg (beste Schätzung): {m.weg_nom:.0f} m", end="")
    if m.raster is not None and m.p50 is not None:
        print(f" · Grauzone 5–95 %: {m.weg[1]:.0f}–{m.weg[2]:.0f} m (Median {m.weg[0]:.0f} m)")
    else:
        print()


def label_optionen(eintrag, ueberschreibung, standard_text):
    basis = {}
    if isinstance(ueberschreibung, str):
        basis["text"] = ueberschreibung
    elif isinstance(ueberschreibung, dict):
        basis.update(ueberschreibung)
    elif ueberschreibung is not None:
        raise SystemExit(f"Fehler: Beschriftung muss Text oder Tabelle sein: {ueberschreibung!r}")
    basis.update(eintrag)
    text = str(basis.get("text", standard_text))
    seite = basis.get("seite")
    if seite is not None:
        seite = str(seite).strip().lower()
        if seite not in ("oben", "unten", "links", "rechts"):
            raise SystemExit("Fehler: 'seite' muss oben, unten, links oder rechts sein")
    versatz = basis.get("versatz")
    if versatz is not None:
        if not isinstance(versatz, (list, tuple)) or len(versatz) != 2:
            raise SystemExit("Fehler: 'versatz' muss zwei Zahlen enthalten, z. B. [0, 14]")
        versatz = (zahl(versatz[0]), zahl(versatz[1]))
    ausrichtung = basis.get("ausrichtung")
    if ausrichtung is not None:
        ausrichtung = str(ausrichtung).strip().lower()
        if ausrichtung not in ("links", "mitte", "rechts"):
            raise SystemExit("Fehler: 'ausrichtung' muss links, mitte oder rechts sein")
    vertikal = basis.get("vertikal")
    if vertikal is not None:
        vertikal = str(vertikal).strip().lower()
        if vertikal not in ("oben", "mitte", "unten"):
            raise SystemExit("Fehler: 'vertikal' muss oben, mitte oder unten sein")
    anker = basis.get("punkt")
    if anker is not None:
        if not isinstance(anker, (list, tuple)) or len(anker) != 2:
            raise SystemExit("Fehler: 'punkt' muss zwei Zahlen enthalten, z. B. [24.5, 51]")
        anker = (zahl(anker[0]), zahl(anker[1]))
    return text, seite, versatz, ausrichtung, vertikal, anker


def label_platz(wert_kmh, y_max, seite, versatz, ausrichtung, vertikal):
    ha_map = {"links": "left", "rechts": "right", "mitte": "center"}
    va_map = {"oben": "top", "unten": "bottom", "mitte": "center"}
    if versatz is not None:
        dx, dy = versatz
        ha = ha_map.get(ausrichtung or "", "center")
        if ausrichtung is None:
            ha = "left" if dx > 0 else ("right" if dx < 0 else "center")
        va = va_map.get(vertikal or "", "center")
        if vertikal is None:
            va = "bottom" if dy >= 0 else "top"
        return dx, dy, ha, va
    if seite == "links":
        return -12, 2, "right", "center"
    if seite == "rechts":
        return 12, 2, "left", "center"
    if seite == "unten":
        return 0, -12, "center", "top"
    if seite == "oben":
        return 0, 12, "center", "bottom"
    if (ausrichtung is not None or vertikal is not None):
        return 0, 12, ha_map.get(ausrichtung or "mitte", "center"), va_map.get(vertikal or "unten", "bottom")
    if wert_kmh >= 0.85 * y_max:
        return -10, 0, "right", "center"
    return 0, 12, "center", "bottom"


def setze_platzhalter(text, m):
    return (
        text.replace("{weg}", komma(m.weg_nom, 0))
        .replace("{weg_unten}", komma(m.weg[1], 0))
        .replace("{weg_oben}", komma(m.weg[2], 0))
    )


def zeichne(m, args):
    d = m.cfg.get("diagramm", {})
    achsen = m.cfg.get("achsen", {})
    farben = dict(FARBEN)
    for schluessel, wert in (d.get("farben") or {}).items():
        farben[str(schluessel)] = str(wert)

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "svg.fonttype": "none",
    })

    raster = m.raster
    v_nom = np.array([p.v_ms for p in m.punkte]) * KMH
    y_max = achsen.get("y_max")
    y_max = zahl(y_max) if y_max is not None else math.ceil((float(v_nom.max()) + 5) / 20) * 20
    x_max = achsen.get("x_max")
    x_max = zahl(x_max) if x_max is not None else m.punkte[-1].t * 1.06
    x_tick = hole_zahl(achsen, "x_tick", 5)
    y_tick = hole_zahl(achsen, "y_tick", 20)
    rand_rechts = hole_zahl(achsen, "rand_rechts", 0.93)
    band_alpha = zahl(farben.get("band_alpha", 0.18))
    band_max = d.get("band_max")
    band_max = zahl(band_max) if band_max is not None else None

    weg_text = None
    if d.get("weg_anzeigen", True):
        weg_text = (
            f"Plausibilitätsprüfung: Fläche unter der Kurve ≈ {komma(m.weg[0], 0)} m "
            f"(5–95 %: {komma(m.weg[1], 0)}–{komma(m.weg[2], 0)} m)."
        )
    fusszeilen = [setze_platzhalter(str(z), m) for z in (d.get("fusszeilen") or [])]
    bloecke = ([weg_text] if weg_text else []) + fusszeilen
    if d.get("quellen"):
        bloecke.append(setze_platzhalter(str(d["quellen"]), m))
    fuss_y = d.get("fuss_y")
    if fuss_y is not None:
        if not isinstance(fuss_y, (list, tuple)) or len(fuss_y) < len(bloecke):
            raise SystemExit("Fehler: 'fuss_y' braucht eine Position je Textblock als Liste")
        unten = 0.20
    else:
        zeilen_gesamt = sum(str(b).count("\n") + 1 for b in bloecke)
        unten = 0.20 + 0.0425 * max(0, zeilen_gesamt - 3)

    fig, ax = plt.subplots(figsize=(14, 8.2), dpi=180)
    fig.patch.set_facecolor(farben["hintergrund"])
    ax.set_facecolor(farben["flaeche"])
    fig.subplots_adjust(left=.08, right=rand_rechts, top=.79, bottom=unten)

    fig.text(.08, .945, str(d.get("titel", "GESCHWINDIGKEIT–ZEIT-DIAGRAMM")),
             color=farben["akzent"], weight="bold", fontsize=13)
    if d.get("untertitel"):
        fig.text(.08, .885, str(d["untertitel"]), color=farben["text"],
                 weight="bold", fontsize=25)
    if d.get("beschreibung"):
        fig.text(.08, .835, str(d["beschreibung"]), color=farben["grau"], fontsize=11)

    for phase in m.cfg.get("phase", []):
        ax.axvspan(
            zahl(phase["von_s"]), zahl(phase["bis_s"]),
            color=str(phase.get("farbe", "#f59e0b")),
            alpha=zahl(phase.get("alpha", 0.1)), linewidth=0,
        )

    if not args.kein_band:
        unten_band = np.clip(m.p5, 0, None) * KMH
        oben_band = m.p95 * KMH
        if band_max is not None:
            oben_band = np.minimum(oben_band, band_max)
        ax.fill_between(raster, unten_band, oben_band,
                        color=farben["band"], alpha=band_alpha, linewidth=0)
    ax.plot(raster, m.nom_kurve * KMH, color=farben["linie"], linewidth=3.2, zorder=3)

    sichere = {str(s) for s in (d.get("sichere_punkte") or [])}
    for p in m.punkte:
        if p.name in sichere:
            ax.scatter([p.t], [p.v_ms * KMH], s=62, color=farben["text"], zorder=5)
        elif p.herkunft in ("beschleunigung", "hoehe"):
            ax.scatter([p.t], [p.v_ms * KMH], s=44, marker="D", facecolor="white",
                       edgecolor=farben["abgeleitet"], linewidth=1.6, zorder=4)
        else:
            ax.scatter([p.t], [p.v_ms * KMH], s=34, facecolor="white",
                       edgecolor=farben["linie"], linewidth=1.5, zorder=4)

    beschriften = d.get("beschriften", "keine")
    if isinstance(beschriften, str):
        if beschriften.strip().lower() in ("auto", "alle"):
            namen = []
            for i in range(1, len(m.punkte) - 1):
                v = [m.punkte[k].v_ms for k in (i - 1, i, i + 1)]
                if (v[1] <= v[0] and v[1] <= v[2]) or (v[1] >= v[0] and v[1] >= v[2]):
                    namen.append(m.punkte[i].name)
            beschriften = namen
        elif beschriften.strip().lower() in ("keine", ""):
            beschriften = []
        else:
            beschriften = [beschriften]
    texte = d.get("beschriftungen") or m.cfg.get("beschriftungen") or {}
    for eintrag in beschriften:
        if isinstance(eintrag, str):
            inline = {"name": eintrag}
        elif isinstance(eintrag, dict):
            inline = dict(eintrag)
        else:
            raise SystemExit(f"Fehler: Beschriftung muss Name oder Tabelle sein: {eintrag!r}")
        name = str(inline.get("name", "")).strip()
        p = m.nach_name.get(name) if name else None
        if p is None and inline.get("punkt") is None:
            print(f"Warnung: Beschriftung für unbekannten Punkt '{name}' übersprungen")
            continue
        text, seite, versatz, ausrichtung, vertikal, anker = label_optionen(
            inline, texte.get(name), p.name if p is not None else "",
        )
        xy = anker if anker is not None else (p.t, p.v_ms * KMH)
        dx, dy, ha, va = label_platz(xy[1], y_max, seite, versatz, ausrichtung, vertikal)
        ax.annotate(text, xy=xy, xytext=(dx, dy), textcoords="offset points",
                    ha=ha, va=va, color=farben["text"], fontsize=9.5,
                    arrowprops={"arrowstyle": "-", "color": farben["marke"], "lw": .8})

    for notiz in m.cfg.get("notiz", []):
        notiz_ha = {"links": "left", "mitte": "center", "rechts": "right"}
        ausrichtung = str(notiz.get("ausrichtung", "links")).strip().lower()
        if ausrichtung not in notiz_ha:
            raise SystemExit("Fehler: Notiz-Ausrichtung muss links, mitte oder rechts sein")
        ax.text(zahl(notiz["x"]), zahl(notiz["y"]), str(notiz["text"]),
                fontsize=zahl(notiz.get("groesse", 10)), color=farben["text"],
                ha=notiz_ha[ausrichtung], va=str(notiz.get("vertikal", "baseline")),
                bbox={"boxstyle": "round,pad=.6", "facecolor": "#eef5fc", "edgecolor": "none"})

    for f in (f for f in m.fakten if f["art"] == "beschleunigung"):
        a = m.a_werte.get(f["name"])
        if not a or d.get("notizen", True) is False:
            continue
        text = f"ā ≈ {komma(a[0], 2)} m/s² = {komma(a[0] / G, 2)} g"
        x = f["t"] + 0.2 if f["dauer"] <= 4 else f["t"] + f["dauer"] / 2
        ax.text(x, 0.12 * y_max, text, fontsize=10, color=farben["text"], ha="left", va="center",
                bbox={"boxstyle": "round,pad=.55", "facecolor": "#eef5fc", "edgecolor": "none"})

    ax.set_xlim(0, x_max)
    ax.set_ylim(0, y_max)
    ax.set_xlabel(str(achsen.get("x_label", "Zeit seit Beginn des Katapultstarts [s]")), labelpad=12)
    ax.set_ylabel(str(achsen.get("y_label", "Geschwindigkeit [km/h]")), labelpad=12)
    ax.xaxis.set_major_locator(MultipleLocator(x_tick))
    ax.yaxis.set_major_locator(MultipleLocator(y_tick))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, pos: komma(x, 1)))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, pos: komma(x, 0)))
    ax.grid(color=farben["gitter"], linewidth=.8)
    ax.set_axisbelow(True)

    if achsen.get("y2_achse", True):
        sec = ax.secondary_yaxis("right", functions=(lambda x: x / KMH, lambda x: x * KMH))
        sec.set_ylabel(str(achsen.get("y2_label", "Geschwindigkeit [m/s]")), labelpad=12)
        sec.yaxis.set_major_locator(MultipleLocator(5))
        sec.yaxis.set_major_formatter(FuncFormatter(lambda x, pos: komma(x, 0)))
        sec.spines["right"].set_visible(False)

    if d.get("video_start"):
        versatz = video_sekunden(d["video_start"])

        def video_format(x, pos=None):
            gesamt = x + versatz
            return f"{int(gesamt // 60)}:{gesamt % 60:04.1f}".replace(".", ",")

        top = ax.secondary_xaxis("top", functions=(lambda x: x, lambda x: x))
        top.xaxis.set_major_locator(MultipleLocator(10))
        top.xaxis.set_major_formatter(FuncFormatter(video_format))

    legende = d.get("legende") or {}
    band_standard = "Grauzone (5–95 %)" if m.band_modus == "monte-carlo" else "geschätzter Bereich"
    handles = [Line2D([0], [0], color=farben["linie"], lw=3,
                      label=str(legende.get("linie", "beste Schätzung")))]
    if not args.kein_band:
        handles.append(Patch(facecolor=farben["band"], alpha=band_alpha, edgecolor="none",
                             label=str(legende.get("band", band_standard))))
    if sichere:
        handles.append(Line2D([0], [0], marker="o", color="none", markerfacecolor=farben["text"],
                              markeredgecolor=farben["text"], markersize=7,
                              label=str(legende.get("feste", "feste Eckwerte"))))
    handles.append(Line2D([0], [0], marker="o", color="none", markerfacecolor="white",
                          markeredgecolor=farben["linie"], markersize=7,
                          label=str(legende.get("geschaetzt", "geschätzte Stützpunkte"))))
    if any(p.herkunft in ("beschleunigung", "hoehe") for p in m.punkte):
        handles.append(Line2D([0], [0], marker="D", color="none", markerfacecolor="white",
                              markeredgecolor=farben["abgeleitet"], markersize=6.5,
                              label=str(legende.get("vorwissen", "aus Vorwissen berechnet"))))
    ax.legend(handles=handles, loc="upper right", frameon=False, ncol=2,
              columnspacing=1.3, handlelength=2.2)

    if fuss_y is not None:
        positionen = [zahl(v) for v in fuss_y[:len(bloecke)]]
    else:
        positionen = []
        y = unten - 0.095
        for block in bloecke:
            positionen.append(y)
            y -= 0.0425 + 0.021 * str(block).count("\n")
    for i, block in enumerate(bloecke):
        ist_quelle = i == len(bloecke) - 1 and d.get("quellen")
        farbe = farben.get("fuss_erste", "#344b5f") if i == 0 else farben["grau"]
        groesse = 9 if ist_quelle else (10 if i == 0 else 9.5)
        fig.text(.08, positionen[i], block, color=farbe, fontsize=groesse,
                 linespacing=zahl(d.get("fuss_zeilenabstand", 1.2)))

    ausgabe = args.ausgabe or d.get("ausgabe") or m.pfad.stem
    formate = args.formate or str(d.get("formate", "png,svg,pdf"))
    ziel = m.pfad.parent / str(ausgabe)
    erzeugt = []
    for ext in (f.strip().lower() for f in formate.split(",") if f.strip()):
        fig.savefig(ziel.with_suffix("." + ext), facecolor=fig.get_facecolor())
        erzeugt.append(str(ziel.with_suffix("." + ext).name))
    plt.close(fig)
    return erzeugt


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="vt_diagramm.py",
        description="Erzeugt aus manuell eingegebenen Daten ein Geschwindigkeit-Zeit-Diagramm "
                    "mit Grauzone (Monte-Carlo-Band oder Hülle aus den angegebenen Unsicherheiten).",
    )
    p.add_argument("config", help="TOML-Konfiguration, z. B. beispiel.toml")
    p.add_argument("--n", type=int, default=None, help="Anzahl Monte-Carlo-Ziehungen (Standard 2000)")
    p.add_argument("--seed", type=int, default=None, help="Zufallsstartwert für reproduzierbare Bänder")
    p.add_argument("--verteilung", choices=("uniform", "normal"), default=None,
                   help="Verteilung der Unsicherheiten (Standard uniform)")
    p.add_argument("--band", choices=("monte-carlo", "huelle"), default=None,
                   help="Bandberechnung: monte-carlo oder huelle (v ± u)")
    p.add_argument("--ausgabe", default=None, help="Basisname der Ausgabedateien ohne Endung")
    p.add_argument("--formate", default=None, help="Kommagetrennte Dateiformate, z. B. png,svg,pdf")
    p.add_argument("--kein-band", action="store_true", help="Grauzone nicht zeichnen")
    p.add_argument("--pruefen", action="store_true",
                   help="Konfiguration und Werte prüfen, ohne Monte-Carlo oder Dateien")
    args = p.parse_args(argv)

    m = Modell(args.config, n=args.n, seed=args.seed, verteilung=args.verteilung, band=args.band)
    if args.pruefen:
        bericht(m)
        print("Konfiguration in Ordnung.")
        return 0
    m.rechnen()
    dateien = zeichne(m, args)
    bericht(m)
    if m.band_modus == "huelle":
        print(f"Band: Hülle aus v ± u · Verteilung: {m.verteilung}")
    else:
        print(f"Ziehungen: {m.n} · Verteilung: {m.verteilung} · "
              f"Seed: {m.seed if m.seed is not None else 'zufällig'}")
    print("Ausgabe: " + ", ".join(dateien))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
