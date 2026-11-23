#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Genera una pagina web estatica e interactiva con las vistas del simulador.

Usa el motor de simulacion y los datos sinteticos incluidos en el repositorio para
producir un unico archivo HTML autocontenido (sin servidor) donde los graficos se
pueden explorar con zoom, paneo y hover.

Salida: docs/index.html

Uso:
    python generar_pagina_estatica.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots

import motor_autonomia as motor

RAIZ = Path(__file__).parent
DATA = RAIZ / "data_sintetica"
SALIDA = RAIZ / "docs"

# paleta consistente con la aplicacion
COL_EVENTO = {"C01": "#1d4ed8", "V01": "#0891b2", "V02": "#0891b2", "R01": "#16a34a"}
NOM_EVENTO = {"C01": "Comercial", "V01": "Vacío (ida)", "V02": "Vacío (vuelta)",
              "R01": "Recarga"}
TIPO_DIA = "Laboral"


def fmt_min(m: float) -> str:
    m = int(m) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


# ---------------------------------------------------------------------------
# 1. Simulacion
# ---------------------------------------------------------------------------
print("Cargando configuracion del escenario...")
cfg = json.load(open(DATA / "config_terminales.json", encoding="utf-8"))
SERV_TERMINAL = {s: t for t, ss in cfg["terminales"].items() for s in ss}

print(f"Simulando dia {TIPO_DIA}...")
eventos, kpi = motor.simular(TIPO_DIA)
df = pd.DataFrame(eventos)
if "terminal" in df.columns:
    df["terminal"] = df["terminal"].fillna(df["servicio"].map(SERV_TERMINAL).fillna(""))

resultado = motor.iteracion2(df.to_dict("records"))
df_soc = pd.DataFrame(resultado["eventos_soc"])
kpis = resultado.get("kpis", {})

print(f"  eventos: {len(df):,}  buses: {df['bus'].nunique()}")

# ---------------------------------------------------------------------------
# 2. Figuras
# ---------------------------------------------------------------------------
figuras: list[tuple[str, str, go.Figure]] = []

# --- 2.1 Linea de tiempo por bus (primeros 40 buses) ---
buses = sorted(df["bus"].unique())[:40]
sub = df[df["bus"].isin(buses)].sort_values(["bus", "inicio"])
fig_tl = go.Figure()
for ev, color in COL_EVENTO.items():
    d = sub[sub["evento"] == ev]
    if d.empty:
        continue
    fig_tl.add_trace(go.Bar(
        x=(d["fin"] - d["inicio"]).clip(lower=0.5),
        y=d["bus"],
        base=d["inicio"],
        orientation="h",
        marker_color=color,
        name=NOM_EVENTO[ev],
        hovertemplate=(f"<b>{NOM_EVENTO[ev]}</b><br>Bus %{{y}}<br>"
                       "Servicio: " + d["servicio"].astype(str) +
                       "<br>Inicio: %{base:.0f} min<br>Duración: %{x:.0f} min<extra></extra>"),
    ))
fig_tl.update_layout(
    barmode="overlay",
    title="Línea de tiempo por bus (primeros 40 buses)",
    xaxis_title="Minuto del día",
    yaxis_title="Bus",
    height=920,
    margin=dict(l=60, r=20, t=60, b=120),
    legend=dict(orientation="h", yanchor="top", y=-0.045, x=0, xanchor="left"),
)
fig_tl.update_xaxes(tickvals=[0, 180, 360, 540, 720, 900, 1080, 1260, 1440],
                    ticktext=["00:00", "03:00", "06:00", "09:00", "12:00",
                              "15:00", "18:00", "21:00", "24:00"])
fig_tl.update_yaxes(categoryorder="category ascending")
figuras.append(("timeline", "Línea de tiempo", fig_tl))

# --- 2.2 Rejilla tipo Marey: un servicio por fila ---
servicios = sorted(df["servicio"].unique())[:14]
fig_marey = go.Figure()
for i, serv in enumerate(servicios):
    d = df[(df["servicio"] == serv) & (df["evento"] == "C01")]
    if d.empty:
        continue
    # trazo diagonal: inicio de la vuelta (y=i) hasta su fin (y=i)
    for _, r in d.iterrows():
        fig_marey.add_trace(go.Scatter(
            x=[r["inicio"], r["fin"]],
            y=[i, i],
            mode="lines",
            line=dict(color=COL_EVENTO["C01"], width=3),
            opacity=0.55,
            hovertemplate=(f"<b>{serv}</b> · {r['sentido']}<br>"
                           f"Bus {r['bus']}<br>"
                           f"{fmt_min(r['inicio'])} a {fmt_min(r['fin'])}<br>"
                           f"{r['km']:.1f} km<extra></extra>"),
            showlegend=False,
        ))
fig_marey.update_layout(
    title="Rejilla por servicio (una fila por servicio)",
    xaxis_title="Minuto del día",
    yaxis_title="Servicio",
    height=650,
    margin=dict(l=80, r=20, t=60, b=50),
)
fig_marey.update_yaxes(tickmode="array", tickvals=list(range(len(servicios))),
                       ticktext=servicios)
fig_marey.update_xaxes(tickvals=[0, 360, 720, 1080, 1440],
                       ticktext=["00:00", "06:00", "12:00", "18:00", "24:00"])
figuras.append(("marey", "Rejilla por servicio", fig_marey))

# --- 2.3 Estado de carga (SOC): margen mínimo por bus ---
if not df_soc.empty and "soc_fin" in df_soc.columns:
    piso = motor.ITER2["soc_piso_kwh"]
    solo_op = df_soc[df_soc["evento"].isin(["C01", "V01", "V02"])]
    soc_min = solo_op.groupby("bus")["soc_fin"].min()
    soc_fin = pd.Series({b: v["soc_fin"] for b, v in resultado["fin_por_bus"].items()
                         if not v.get("sin_soc")})
    fig_soc = go.Figure()
    fig_soc.add_trace(go.Histogram(
        x=soc_min.values, nbinsx=40, marker_color="#0f766e", opacity=0.75,
        name="SOC mínimo alcanzado",
        hovertemplate="SOC %{x:.0f} kWh<br>%{y} buses<extra></extra>",
    ))
    fig_soc.add_vline(x=piso, line_dash="dash", line_color="#b91c1c",
                      annotation_text=f"piso {piso:.0f} kWh", annotation_position="top")
    fig_soc.update_layout(
        title="Margen de carga: SOC mínimo alcanzado por cada bus eléctrico",
        xaxis_title="SOC mínimo en el día (kWh)", yaxis_title="Buses",
        height=500, margin=dict(l=60, r=20, t=70, b=60), bargap=0.05,
    )
    figuras.append(("soc", "Margen de carga", fig_soc))

# --- 2.4 Recarga nocturna ---
prog = resultado.get("programa_nocturno") or []
if prog:
    df_prog = pd.DataFrame(prog)
    if "terminal" in df_prog.columns and "kwh" in df_prog.columns:
        agg = df_prog.groupby("terminal").agg(buses=("bus", "nunique"),
                                              kwh=("kwh", "sum")).reset_index()
        fig_rec = go.Figure()
        fig_rec.add_trace(go.Bar(x=agg["terminal"], y=agg["kwh"],
                                 marker_color="#0ea5e9",
                                 hovertemplate="%{x}<br>%{y:,.0f} kWh<extra></extra>",
                                 text=agg["buses"], texttemplate="%{text} buses",
                                 textposition="outside"))
        fig_rec.update_layout(title="Recarga nocturna por terminal",
                              xaxis_title="Terminal", yaxis_title="kWh",
                              height=480, margin=dict(l=60, r=20, t=60, b=80))
        figuras.append(("recarga", "Recarga nocturna", fig_rec))

# --- 2.5 Kilometros por servicio ---
km_serv = (df[df["evento"] == "C01"].groupby("servicio")["km"].sum()
           .sort_values(ascending=False).head(20).reset_index())
fig_km = go.Figure(go.Bar(x=km_serv["servicio"], y=km_serv["km"],
                          marker_color="#334155",
                          hovertemplate="%{x}<br>%{y:,.0f} km<extra></extra>"))
fig_km.update_layout(title="Kilómetros comerciales por servicio (20 mayores)",
                     xaxis_title="Servicio", yaxis_title="km",
                     height=460, margin=dict(l=60, r=20, t=60, b=70))
figuras.append(("km", "Kilómetros por servicio", fig_km))

# ---------------------------------------------------------------------------
# 3. Ensamblado del HTML
# ---------------------------------------------------------------------------
print("Generando HTML...")
SALIDA.mkdir(exist_ok=True)

bloques = []
for i, (ident, titulo, fig) in enumerate(figuras):
    incluir = "cdn" if i == 0 else False
    bloques.append({
        "id": ident,
        "titulo": titulo,
        "html": pio.to_html(fig, full_html=False, include_plotlyjs=incluir,
                            div_id=f"grafico_{ident}", config={"responsive": True}),
    })

tarjetas = "\n".join(
    f'<div class="kpi"><span class="valor">{v}</span><span class="etiqueta">{e}</span></div>'
    for e, v in [
        ("Buses", f"{df['bus'].nunique():,}"),
        ("Expediciones", f"{int((df['evento'] == 'C01').sum()):,}"),
        ("Km comerciales", f"{df[df['evento'] == 'C01']['km'].sum():,.0f}"),
        ("Energía diurna", f"{kpis.get('energia_dia_kwh', 0):,.0f} kWh"),
        ("Energía nocturna", f"{kpis.get('energia_noche_kwh', 0):,.0f} kWh"),
        ("Factible", "Sí" if kpis.get("factible") else "No"),
    ]
)

botones = "\n".join(
    f'<button class="tab{" activo" if i == 0 else ""}" data-tab="{b["id"]}">{b["titulo"]}</button>'
    for i, b in enumerate(bloques)
)
paneles = "\n".join(
    f'<section id="tab-{b["id"]}" class="panel{" activo" if i == 0 else ""}">{b["html"]}</section>'
    for i, b in enumerate(bloques)
)

plantilla = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Simulador de flota eléctrica — demostración interactiva</title>
<style>
  :root {{
    --fondo: #f8fafc; --tarjeta: #ffffff; --borde: #e2e8f0;
    --texto: #0f172a; --suave: #64748b; --acento: #1d4ed8;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0 0 3rem; background: var(--fondo); color: var(--texto);
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  header {{ background: var(--tarjeta); border-bottom: 1px solid var(--borde);
            padding: 1.75rem 2rem 1.25rem; }}
  h1 {{ margin: 0 0 .35rem; font-size: 1.55rem; font-weight: 650; letter-spacing: -.01em; }}
  .sub {{ color: var(--suave); font-size: .92rem; margin: 0 0 .9rem; }}
  .aviso {{ display: inline-block; background: #eff6ff; color: #1e40af;
            border: 1px solid #bfdbfe; border-radius: 6px; padding: .35rem .7rem;
            font-size: .8rem; }}
  .kpis {{ display: flex; flex-wrap: wrap; gap: .75rem; margin-top: 1.15rem; }}
  .kpi {{ background: var(--fondo); border: 1px solid var(--borde); border-radius: 8px;
          padding: .6rem .9rem; min-width: 128px; }}
  .kpi .valor {{ display: block; font-size: 1.2rem; font-weight: 650; }}
  .kpi .etiqueta {{ display: block; font-size: .74rem; color: var(--suave);
                    text-transform: uppercase; letter-spacing: .04em; margin-top: .15rem; }}
  nav {{ display: flex; flex-wrap: wrap; gap: .4rem; padding: 1rem 2rem .5rem; }}
  .tab {{ border: 1px solid var(--borde); background: var(--tarjeta); color: var(--texto);
          padding: .5rem .95rem; border-radius: 7px; font-size: .86rem; cursor: pointer;
          transition: .12s; }}
  .tab:hover {{ border-color: #cbd5e1; }}
  .tab.activo {{ background: var(--acento); border-color: var(--acento); color: #fff;
                 font-weight: 600; }}
  main {{ padding: 0 2rem; }}
  .panel {{ display: none; background: var(--tarjeta); border: 1px solid var(--borde);
            border-radius: 10px; padding: .5rem; margin-top: .75rem; }}
  .panel.activo {{ display: block; }}
  footer {{ color: var(--suave); font-size: .8rem; padding: 1.5rem 2rem 0; }}
  footer a {{ color: var(--acento); }}
</style>
</head>
<body>
<header>
  <h1>Simulador operacional de flota eléctrica</h1>
  <p class="sub">Demostración interactiva generada desde el motor de simulación del repositorio.
     Datos <strong>sintéticos</strong>: terminales, servicios y programación ficticios.</p>
  <span class="aviso">Día simulado: {TIPO_DIA} · {len(df):,} eventos · {df['bus'].nunique()} buses</span>
  <div class="kpis">{tarjetas}</div>
</header>
<nav>{botones}</nav>
<main>
{paneles}
</main>
<footer>
  Los gráficos se pueden ampliar (arrastrar para hacer zoom), desplazar y explorar pasando el
  cursor. Cada figura tiene controles propios en la esquina superior derecha.
  <br>Proyecto: <a href="https://github.com/luisalbertochavarria/simulador-flota-electrica">
  simulador-flota-electrica</a>.
</footer>
<script>
document.querySelectorAll('.tab').forEach(function (b) {{
  b.addEventListener('click', function () {{
    document.querySelectorAll('.tab').forEach(function (x) {{ x.classList.remove('activo'); }});
    document.querySelectorAll('.panel').forEach(function (p) {{ p.classList.remove('activo'); }});
    b.classList.add('activo');
    var panel = document.getElementById('tab-' + b.dataset.tab);
    if (panel) {{
      panel.classList.add('activo');
      panel.querySelectorAll('.js-plotly-plot').forEach(function (g) {{
        if (window.Plotly) {{ Plotly.Plots.resize(g); }}
      }});
    }}
  }});
}});
</script>
</body>
</html>
"""

destino = SALIDA / "index.html"
destino.write_text(plantilla, encoding="utf-8")
tam = destino.stat().st_size
print(f"Listo: {destino}  ({tam / 1_048_576:.2f} MB)")
print(f"Figuras incluidas: {len(figuras)}")