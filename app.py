"""Simulador operacional de flota eléctrica (Streamlit + Plotly + pandas).

Tabs: Simulación (timeline tipo metro, flota y colas), Por línea (rejilla Marey
"Todos" y diagrama por línea), Gantt (bloques por bus), Tablas (detalle de bus y
programación exportable).

Parámetros en vivo: parada en cabezal y autonomía por tipología; tabla de buses por
tipo y terminal; parámetros eléctricos (SOC máximo, piso de seguridad, rendimiento);
y programa de recarga nocturna con restricción de cargadores por terminal.

IMPORTANTE: este repositorio trabaja con DATOS SINTETICOS generados por
generar_datos_sinteticos.py. No contiene información operacional ni contractual real.
"""
import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import motor_autonomia as motor

DATA = Path(__file__).parent / "data_sintetica"

COLOR_EVENTO = {"C01": "#2563eb", "V01": "#dc2626", "V02": "#dc2626", "R01": "#16a34a"}
LABEL_EVENTO = {"C01": "Comercial", "V01": "Vacío → cabezal", "V02": "Vacío → terminal", "R01": "Regulación"}
# Terminales y servicios del escenario sintetico (ver generar_datos_sinteticos.py)
_cfg = json.load(open(DATA / "config_terminales.json", encoding="utf-8"))
TERMINAL_SERVICIOS = _cfg["terminales"]
SERV_TERMINAL = {s: t for t, ss in TERMINAL_SERVICIOS.items() for s in ss}

# Convención de colores Marey / Gantt (aprobada en mockups)
COL_IDA, COL_RET = "#111827", "#b91c1c"
COL_VACIO, COL_REC, COL_ESP = "#06b6d4", "#16a34a", "#9ca3af"
PALETA = px.colors.qualitative.T10 + px.colors.qualitative.Set3


@st.cache_data
def cargar_datos():
    """Modelo (velocidades/segmentos) y resumen del parquet para KPIs rápidos."""
    modelo = pd.read_parquet(DATA / "modelo_un16.parquet")
    resumen = json.load(open(DATA / "resumen_sim.json"))
    return modelo, resumen


def fmt_min(m):
    m = int(m) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def fmt_dur(m):
    m = int(round(m))
    return f"{m // 60}:{m % 60:02d}"


@st.cache_data
def simular(tipo_dia, p_min, p_max, aut, rec, it2):
    """Motor + SOC (iteración 1) + programa nocturno (iteración 2). Devuelve
    (df_eventos, resumen) donde resumen trae fin_por_bus, demanda, kpis, programa."""
    evs, kpi = motor.simular(tipo_dia, p_min, p_max, aut, rec)
    df = pd.DataFrame(evs)
    df["terminal"] = df["terminal"].fillna(df["servicio"].map(SERV_TERMINAL).fillna(""))
    r = motor.iteracion2(df.to_dict("records"), it2)
    df_soc = pd.DataFrame(r["eventos_soc"])
    resumen = {"fin_por_bus": r["fin_por_bus"], "demanda": r["demanda"],
               "programa_nocturno": r["programa_nocturno"],
               "ajustes_diurnos": r["ajustes_diurnos"], "kpis": r["kpis"]}
    return df_soc, resumen


def recargas_de(evs_b, gap_min=30):
    """Huecos V02 → siguiente evento > gap_min (recarga en terminal)."""
    out = []
    v02 = evs_b[evs_b["evento"] == "V02"]
    for _, r in v02.iterrows():
        sig = evs_b[evs_b["inicio"] > r["fin"]]
        if not sig.empty and sig.iloc[0]["inicio"] - r["fin"] > gap_min:
            out.append((r["fin"], sig.iloc[0]["inicio"]))
    return out


# ============================================================
# FIGURAS
# ============================================================
def fig_rejilla(datos, servicios, t0, t1, color_por_bus, etiquetas, grosor):
    """Rejilla Marey 'Todos' (mockup v3 aprobado): un servicio por fila.
    Diagonal C01 (hora en vértices, bus·servicio al centro si etiquetas),
    V01/V02 cian punteado, espera gris punteada, recarga terminal azul punteada,
    V.C. por fila. FIG A color por sentido / FIG B color por bus."""
    n_filas = len(servicios)
    fig = make_subplots(rows=n_filas, cols=1, shared_xaxes=True,
                        vertical_spacing=0.06, row_heights=[1] * n_filas)
    for nombre, color, dash in [("C01 Ida", COL_IDA, None), ("C01 Retorno", COL_RET, None),
                                ("V01/V02 vacío", COL_VACIO, "dash"), ("Espera cabezal", COL_ESP, "dot"),
                                ("Recarga terminal", COL_REC, "dashdot")]:
        fig.add_trace(go.Scatter(x=[], y=[], mode="lines", line=dict(width=2.4, color=color, dash=dash),
                                 name=nombre, hoverinfo="skip"), row=1, col=1)
    for i, serv in enumerate(servicios, start=1):
        ss = datos[datos["servicio"] == serv]
        c01s = ss[ss["evento"] == "C01"]
        km_max = float(c01s["km"].max()) if not c01s.empty else 22.0
        y_ida, y_ret, y_term = km_max, 0.0, -4.0
        fig.update_yaxes(range=[y_term - 0.6, km_max + 0.6], autorange=False,
                         tickvals=[km_max, 0.0, y_term],
                         ticktext=[f"Cab. Ida · {km_max:.1f} km", "Cab. Retorno · 0,0 km", "Terminal (COCH)"],
                         tickfont=dict(size=9), row=i, col=1)

        def c_bus(b):
            return PALETA[int(b[1:]) % len(PALETA)] if color_por_bus else None

        c01 = c01s[c01s["inicio"].between(t0, t1)].sort_values(["bus", "inicio"])
        vcs = []
        for _, e in c01.iterrows():
            y0, y1 = (y_ida, y_ret) if e["sentido"] == "Ida" else (y_ret, y_ida)
            xm, ym = (e["inicio"] + e["fin"]) / 2, (y0 + y1) / 2
            dur = e["fin"] - e["inicio"]
            vc = e["km"] * 60 / dur if dur > 0 else 0
            vcs.append(vc)
            color = c_bus(e["bus"]) or (COL_RET if e["sentido"] == "Ret" else COL_IDA)
            etq = f"{e['bus']}·{serv}" if etiquetas else ""
            fig.add_trace(go.Scatter(
                x=[e["inicio"], xm, e["fin"]], y=[y0, ym, y1], mode="lines+text",
                text=[fmt_min(e["inicio"]), etq, fmt_min(e["fin"])],
                textposition=["top center", "middle center", "bottom center"],
                textfont=dict(size=8, color="#374151"),
                line=dict(width=grosor, color=color),
                hovertemplate=(f"<b>{e['bus']}</b> · {serv} {e['sentido']}<br>"
                               f"{fmt_min(e['inicio'])} → {fmt_min(e['fin'])} · {int(round(dur))} min<br>"
                               f"km: {e['km']:.1f} · V.C. {vc:.0f} km/h<extra></extra>"),
                showlegend=False), row=i, col=1)
        vc_media = sum(vcs) / len(vcs) if vcs else 0.0
        fig.add_annotation(xref="paper", x=1.0, yref=f"y{i}", y=(y_ida + y_ret) / 2,
                           text=f"V.C. {vc_media:.0f} km/h", showarrow=False,
                           font=dict(size=9, color="#6b7280"), textangle=-90)
        # vacíos (cian punteado)
        for _, e in ss[ss["evento"].isin(["V01", "V02"]) & ss["inicio"].between(t0, t1)].iterrows():
            cab = y_ida if e["sentido"] == "Ida" else y_ret
            y0, y1 = (y_term, cab) if e["evento"] == "V01" else (cab, y_term)
            color = c_bus(e["bus"]) or COL_VACIO
            fig.add_trace(go.Scatter(x=[e["inicio"], e["fin"]], y=[y0, y1], mode="lines",
                                     line=dict(width=2.0, color=color, dash="dash"),
                                     hovertemplate=(f"<b>{e['bus']}</b> · {e['evento']} · {serv} {e['sentido']}<br>"
                                                    f"{fmt_min(e['inicio'])} → {fmt_min(e['fin'])}<br>"
                                                    f"km: {e['km']:.1f}<extra></extra>"),
                                     showlegend=False), row=i, col=1)
        # espera cabezal (gris punteada)
        for b in c01["bus"].unique():
            evs = c01[c01["bus"] == b]
            prev_fin, prev_y = None, None
            for _, e in evs.iterrows():
                if prev_fin is not None and e["inicio"] > prev_fin:
                    color = c_bus(b) or COL_ESP
                    fig.add_trace(go.Scatter(x=[prev_fin, e["inicio"]], y=[prev_y, prev_y], mode="lines",
                                             line=dict(width=1.4, color=color, dash="dot"),
                                             hoverinfo="skip", showlegend=False), row=i, col=1)
                prev_fin, prev_y = e["fin"], y_ida if e["sentido"] == "Ret" else y_ret
        # recarga terminal (azul punteada)
        v02 = ss[(ss["evento"] == "V02") & ss["inicio"].between(t0, t1)]
        v01 = ss[(ss["evento"] == "V01") & ss["inicio"].between(t0, t1)]
        for _, a in v02.iterrows():
            sig = v01[(v01["bus"] == a["bus"]) & (v01["inicio"] > a["fin"])]
            if len(sig):
                b1 = sig.sort_values("inicio").iloc[0]
                color = c_bus(a["bus"]) or COL_REC
                fig.add_trace(go.Scatter(x=[a["fin"], b1["inicio"]], y=[y_term, y_term], mode="lines",
                                         line=dict(width=2.4, color=color, dash="dashdot"),
                                         hovertemplate=(f"<b>{a['bus']}</b> recarga terminal<br>"
                                                        f"{fmt_min(a['fin'])} → {fmt_min(b1['inicio'])} "
                                                        f"({int(round(b1['inicio'] - a['fin']))} min)<extra></extra>"),
                                         showlegend=False), row=i, col=1)
                if etiquetas:
                    fig.add_annotation(x=(a["fin"] + b1["inicio"]) / 2, y=y_term,
                                       text=f"<span style='color:#16a34a'>{fmt_min(a['fin'])} → {fmt_min(b1['inicio'])}</span>",
                                       showarrow=False, xanchor="middle", font=dict(size=7))
    fig.update_layout(height=min(300 * n_filas, 2400), margin=dict(l=130, r=80, t=40, b=50),
                      hovermode="closest", plot_bgcolor="#fafafa",
                      legend=dict(orientation="h", y=1.02, x=0, font=dict(size=11)))
    fig.update_xaxes(gridcolor="#e5e7eb", title="Hora del día",
                     tickvals=list(range(t0, t1 + 1, max(30, (t1 - t0) // 8))),
                     ticktext=[fmt_min(t) for t in range(t0, t1 + 1, max(30, (t1 - t0) // 8))])
    for i in range(1, n_filas + 1):
        fig.update_xaxes(gridcolor="#e5e7eb", row=i, col=1)
        fig.update_yaxes(gridcolor="#e5e7eb", row=i, col=1)
    return fig


def fig_marey_linea(c01m, col_ida, col_ret, col_esp, grosor, sep_y, inv, etiquetas):
    """Marey clásico por línea: diagonales por bus + esperas + etiquetas de hora."""
    fig = go.Figure()
    buses_m = sorted(c01m["bus"].unique())
    km_max = max(c01m["km"].max(), 22.0)
    for bus_id in buses_m:
        evs_b = c01m[c01m["bus"] == bus_id].sort_values("inicio")
        for i, (_, e) in enumerate(evs_b.iterrows()):
            km_v = e["km"] or 22.0
            if e["sentido"] == "Ida":
                y0, y1 = 0.0, km_v
            else:
                y0, y1 = km_v, 0.0
            color = col_ida if e["sentido"] == "Ida" else col_ret
            dur_h = max((e["fin"] - e["inicio"]) / 60, 0.1)
            vel = (e["km"] or 22.0) / dur_h
            etq = f"{bus_id}" if etiquetas else ""
            xm, ym = (e["inicio"] + e["fin"]) / 2, (y0 + y1) / 2 * sep_y
            fig.add_trace(go.Scatter(
                x=[e["inicio"], xm, e["fin"]], y=[y0 * sep_y, ym, y1 * sep_y],
                mode="lines+text", text=[fmt_min(e["inicio"]), etq, fmt_min(e["fin"])],
                textposition=["top center", "middle center", "bottom center"],
                textfont=dict(size=8, color="#374151"),
                line=dict(color=color, width=grosor),
                name="Ida" if e["sentido"] == "Ida" else "Retorno",
                legendgroup="Ida" if e["sentido"] == "Ida" else "Retorno",
                showlegend=(i == 0 and bus_id == buses_m[0]),
                customdata=[[e["bus"], e["servicio"], e["sentido"], e["km"],
                             fmt_min(e["inicio"]), fmt_min(e["fin"]), round(vel, 1)]],
                hovertemplate=("<b>%{customdata[0]}</b> · %{customdata[1]} %{customdata[2]}<br>"
                               "%{customdata[4]} → %{customdata[5]} · %{customdata[3]:.1f} km<br>"
                               "Vel. media: %{customdata[6]} km/h<extra></extra>"),
            ))
            if i < len(evs_b) - 1:
                nxt = evs_b.iloc[i + 1]
                y_esp = y1 * sep_y
                es_rec = (nxt["inicio"] - e["fin"]) >= 30
                fig.add_trace(go.Scatter(
                    x=[e["fin"], nxt["inicio"]], y=[y_esp, y_esp], mode="lines",
                    line=dict(color=(COL_REC if es_rec else col_esp),
                              width=(2.2 if es_rec else 1),
                              dash=("dashdot" if es_rec else "dot")),
                    name="Recarga" if es_rec else "Espera",
                    legendgroup="Recarga" if es_rec else "Espera", showlegend=False,
                    customdata=[[bus_id, fmt_min(e["fin"]), fmt_min(nxt["inicio"])]],
                    hovertemplate=(("<b>%{customdata[0]}</b> recarga en terminal<br>"
                                    "%{customdata[1]} → %{customdata[2]}" if es_rec else
                                    "<b>%{customdata[0]}</b> espera en cabezal<br>"
                                    "%{customdata[1]} → %{customdata[2]}") + "<extra></extra>"),
                ))
    for label, yv in [("Cabezal Ida", 0.0), ("Cabezal Retorno", km_max * sep_y)]:
        fig.add_hline(y=yv, line=dict(color="#d1d5db", width=1, dash="dash"),
                      annotation_text=label, annotation_position="left")
    y_range = [km_max * sep_y * 1.05, -km_max * 0.05]
    if inv:
        y_range = y_range[::-1]
    fig.update_layout(height=560,
                      xaxis=dict(title="Hora del día"),
                      yaxis=dict(title="", range=y_range, showgrid=False),
                      legend=dict(orientation="h", y=1.08), margin=dict(l=10, r=10, t=30, b=10),
                      hovermode="closest", dragmode="pan",
                      modebar=dict(remove=["select2d", "lasso2d", "autoScale2d"]))
    return fig


def fig_gantt(evs, t0, t1):
    """Gantt v2 (mockup aprobado): bloques por bus con etiquetas 3 líneas
    (hora roja, servicio·sentido·km negro, duración azul); V01/V02 cian,
    R01 gris, recarga punteada azul."""
    fig = go.Figure()
    buses = sorted(evs["bus"].unique())
    for i, b in enumerate(buses):
        y = i
        g = evs[evs["bus"] == b].sort_values("inicio")
        term_b = str(g["terminal"].iloc[0]) if ("terminal" in g.columns and len(g)) else ""
        gv = g[(g["fin"] >= t0) & (g["inicio"] <= t1)]
        recs = [(max(r0, t0), min(r1, t1)) for r0, r1 in recargas_de(g) if r1 >= t0 and r0 <= t1]
        for _, e in gv.iterrows():
            ini, fin = max(e["inicio"], t0), min(e["fin"], t1)
            if fin <= ini:
                continue
            if e["evento"] == "C01":
                col = COL_IDA if e["sentido"] == "Ida" else COL_RET
                fig.add_trace(go.Bar(x=[fin - ini], y=[y], base=[ini], orientation="h",
                                     marker_color=col, marker_line_width=0, width=0.42,
                                     name="C01", legendgroup="C01", showlegend=(i == 0)))
                dur = fin - ini
                fig.add_annotation(x=ini, y=y + 0.42,
                                   text=f"<span style='color:#dc2626'>{fmt_min(ini)} → {fmt_min(fin)}</span>",
                                   showarrow=False, xanchor="left", font=dict(size=9))
                fig.add_annotation(x=ini, y=y,
                                   text=f"<span style='color:#111827'><b>{e['servicio']} {e['sentido']}</b> · {e['km']:.1f} km</span>",
                                   showarrow=False, xanchor="left", font=dict(size=9))
                fig.add_annotation(x=ini, y=y - 0.42,
                                   text=f"<span style='color:#2563eb'>{dur:.0f} min</span>",
                                   showarrow=False, xanchor="left", font=dict(size=9))
            elif e["evento"] in ("V01", "V02"):
                fig.add_trace(go.Bar(x=[fin - ini], y=[y], base=[ini], orientation="h",
                                     marker_color=COL_VACIO, marker_line_width=0, width=0.22,
                                     name="Vacío", legendgroup="V", showlegend=(i == 0)))
            elif e["evento"] == "R01":
                fig.add_trace(go.Bar(x=[fin - ini], y=[y], base=[ini], orientation="h",
                                     marker_color=COL_REC, marker_line_width=0, width=0.42,
                                     marker_pattern_shape="/", name="Recarga", legendgroup="REC",
                                     showlegend=(i == 0)))
                fig.add_annotation(x=ini, y=y,
                                   text=f"<span style='color:#16a34a'><b>RECARGA {fin - ini:.0f} min · {term_b}</b></span>",
                                   showarrow=False, xanchor="left", font=dict(size=9))
            else:
                fig.add_trace(go.Bar(x=[fin - ini], y=[y], base=[ini], orientation="h",
                                     marker_color=COL_ESP, marker_line_width=0, width=0.22,
                                     name="Espera", legendgroup="R", showlegend=(i == 0)))
        for r0, r1 in recs:
            if r1 <= r0:
                continue
            fig.add_trace(go.Bar(x=[r1 - r0], y=[y], base=[r0], orientation="h",
                                 marker_color=COL_REC, marker_line_width=0, width=0.42,
                                 marker_pattern_shape="/", name="Recarga", legendgroup="REC",
                                 showlegend=(i == 0)))
            fig.add_annotation(x=r0, y=y,
                               text=f"<span style='color:#16a34a'><b>RECARGA {r1 - r0:.0f} min · {term_b}</b></span>",
                               showarrow=False, xanchor="left", font=dict(size=9))
    fig.update_layout(
        barmode="overlay", height=max(240, 90 * len(buses)),
        xaxis=dict(title="Hora", range=[t0, t1], dtick=60 * 60000, tickformat="%H:%M",
                   gridcolor="#e5e7eb", gridwidth=1),
        yaxis=dict(title="", tickvals=list(range(len(buses))), ticktext=buses,
                   autorange="reversed", showgrid=False, tickfont=dict(size=11)),
        legend=dict(orientation="h", y=1.06, font=dict(size=10)),
        margin=dict(l=10, r=10, t=10, b=10),
    )
    fig.update_xaxes(tickmode="array", tickvals=list(range(t0, t1 + 1, max(60, (t1 - t0) // 8))),
                     ticktext=[fmt_min(t) for t in range(t0, t1 + 1, max(60, (t1 - t0) // 8))])
    return fig


def filas_detalle_bus(b):
    """Jornada completa de un bus estilo hoja de programación (mockup Tablas v1):
    InB, Exp, Vac, Par, Rec, Lin + SOC (soc_inicio/soc_fin de la iteración 1)."""
    b = b.sort_values("inicio").reset_index(drop=True)
    filas = []
    inicio_jornada, fin_jornada = b["inicio"].min(), b["fin"].max()
    filas.append({"Tipo": "InB", "Inicio": inicio_jornada, "De": "COCH", "Fin": inicio_jornada,
                  "A": "COCH", "Dur.": 0, "Servicio": "-", "Sentido": "-", "Kms": 0.0,
                  "Frec.": "-", "SOC ini": b.iloc[0].get("soc_inicio", None), "SOC fin": b.iloc[0].get("soc_inicio", None)})
    prev_fin, prev_serv, prev_tipo = inicio_jornada, None, "InB"
    cambios_linea = 0
    for _, e in b.iterrows():
        ini, fin = e["inicio"], e["fin"]
        serv, sent, km, term = e["servicio"], e["sentido"], e["km"], e["terminal"]
        if prev_tipo == "C01" and prev_serv is not None and serv != prev_serv and abs(ini - prev_fin) < 1:
            cambios_linea += 1
            filas.append({"Tipo": "Lin", "Inicio": ini, "De": "cab. actual", "Fin": ini, "A": "cab. actual",
                          "Dur.": 0, "Servicio": serv, "Sentido": sent, "Kms": 0.0, "Frec.": "-",
                          "SOC ini": e.get("soc_inicio"), "SOC fin": e.get("soc_inicio")})
        if ini - prev_fin > 5:
            filas.append({"Tipo": "Par", "Inicio": prev_fin, "De": term, "Fin": ini, "A": term,
                          "Dur.": ini - prev_fin, "Servicio": prev_serv or "-", "Sentido": "-", "Kms": 0.0,
                          "Frec.": "-", "SOC ini": None, "SOC fin": None})
        if e["evento"] == "C01":
            tipo, de, a = ("Exp", term, f"Cab. {serv}") if sent == "Ida" else ("Exp", f"Cab. {serv}", term)
        elif e["evento"] in ("V01", "V02"):
            tipo, de, a = ("Vac", term, f"Cab. {serv}") if sent == "Ida" else ("Vac", f"Cab. {serv}", term)
        else:
            tipo, de, a = ("Par", term, term)
        frec = fmt_dur(ini - prev_fin) if (tipo == "Exp" and ini - prev_fin > 0 and prev_tipo != "Lin") else "-"
        filas.append({"Tipo": tipo, "Inicio": ini, "De": de, "Fin": fin, "A": a, "Dur.": fin - ini,
                      "Servicio": serv, "Sentido": sent, "Kms": km, "Frec.": frec,
                      "SOC ini": e.get("soc_inicio"), "SOC fin": e.get("soc_fin")})
        prev_fin, prev_serv, prev_tipo = fin, serv, tipo
        if e["evento"] == "V02":
            sig = b[b["inicio"] > fin]
            if not sig.empty and sig.iloc[0]["inicio"] - fin > 30:
                gap = sig.iloc[0]["inicio"] - fin
                filas.append({"Tipo": "Rec", "Inicio": fin, "De": term, "Fin": sig.iloc[0]["inicio"], "A": term,
                              "Dur.": gap, "Servicio": serv, "Sentido": "-", "Kms": 0.0, "Frec.": "-",
                              "SOC ini": e.get("soc_fin"), "SOC fin": sig.iloc[0].get("soc_inicio")})
                prev_fin = sig.iloc[0]["inicio"]
    total = fin_jornada - inicio_jornada
    en_cabecera = sum(f["Dur."] for f in filas if f["Tipo"] in ("Par", "Rec"))
    exp_min = sum(f["Dur."] for f in filas if f["Tipo"] == "Exp")
    kpis = {"Total jornada": fmt_dur(total), "En cabecera": fmt_dur(en_cabecera),
            "Expediciones": fmt_dur(exp_min), "Camb. línea": cambios_linea,
            "Inicio": fmt_min(inicio_jornada), "Fin": fmt_min(fin_jornada)}
    return pd.DataFrame(filas), kpis


# ============================================================
# APP
# ============================================================
st.set_page_config(page_title="Simulador de flota electrica", layout="wide")
st.title("Simulador operacional de flota electrica")
st.caption("Datos sinteticos de demostracion · parada y autonomia parametrizables · flota por terminal · rejilla Marey · SOC y recarga nocturna")

modelo, resumen_parquet = cargar_datos()

# ---------- SIDEBAR ----------
st.sidebar.header("Filtros")
tipo_dia = st.sidebar.selectbox("Tipo de día", ["Laboral", "Sábado", "Domingo"])
terminal = st.sidebar.selectbox("Terminal", ["Todos"] + list(TERMINAL_SERVICIOS.keys()))
servicios_term = sorted(TERMINAL_SERVICIOS[terminal]) if terminal != "Todos" else sorted(
    set().union(*TERMINAL_SERVICIOS.values()))
servicio = st.sidebar.selectbox("Servicio", ["Todos"] + servicios_term)
sentido = st.sidebar.selectbox("Sentido", ["Todos", "Ida", "Ret"])
tipo_bus = st.sidebar.selectbox("Tipo de bus", ["Todos", "A2V", "B2V", "C2D"])
h_ini, h_fin = st.sidebar.slider("Rango horario", 0, 1440, (0, 1440), step=30,
                                 help="Ventana visible del timeline (minutos desde 00:00)")
zoom = st.sidebar.selectbox("Nivel de zoom", ["1h", "30m", "15m", "5m", "1m"], index=0)

st.sidebar.header(" Parada en cabezal y autonomía")
with st.sidebar.expander("Parámetros de operación", expanded=False):
    p_min = st.slider("Parada mínima en cabezal (min)", 0, 15, 0)
    p_max = st.slider("Parada máxima en cabezal (min)", 5, 60, 30)
    c_a2v = st.number_input("Autonomía A2V (km)", 50, 600, 220, step=10)
    c_b2v = st.number_input("Autonomía B2V (km)", 50, 600, 220, step=10)
    c_c2d = st.number_input("Autonomía C2D (km)", 100, 900, 500, step=10)
    if p_min > p_max:
        st.warning("La parada mínima no puede superar la máxima — se usa la máxima.")
        p_min = min(p_min, p_max)

st.sidebar.header(" Buses eléctricos y recarga nocturna")
with st.sidebar.expander("Parámetros eléctricos", expanded=False):
    cap_kwh = st.number_input("Capacidad (kWh)", 200, 600, 385, step=5,
                              help="Carga máxima de los buses eléctricos")
    piso_kwh = st.number_input("Piso de seguridad (kWh)", 40, 150, 80, step=5,
                               help="No pueden bajar de este valor al llegar al terminal")
    rend = st.number_input("Rendimiento (kWh/km)", 0.8, 2.0, 1.275, step=0.005,
                           help="1,275 kWh/km (30-ago) — alcance útil ≈ 239 km")
    pot_kw = st.number_input("Potencia cargador (kW)", 30, 200, 90, step=5)
    t_min_elec = st.number_input("Recarga mínima eléctricos (min)", 10, 120, 30, step=5)
    soc_dinamico = st.toggle("Objetivo de recarga dinámico", value=True,
                             help="Cargar solo lo necesario para la próxima vuelta (mínimo 80 + km×rend)")
    soc_pct = st.number_input("Objetivo fijo (%) — legado", 50, 100, 100, step=5,
                              disabled=soc_dinamico)
    incl_vacios = st.toggle("Descontar vacíos V01/V02 del SOC", value=False)

st.sidebar.header(" Iteración 2 (programa nocturno)")
with st.sidebar.expander("Recarga nocturna", expanded=False):
    soc_fin_dia = st.number_input("Objetivo fin de día (kWh)", 200, 600, cap_kwh, step=5,
                                  help="100% para operar al día siguiente")
    n_carg = st.slider("Cargadores por terminal", 0, 100, 20,
                       help="0 = sin carga nocturna en ningún terminal (todo se adelanta al día)")
    col_w1, col_w2 = st.columns(2)
    wn_ini_h = col_w1.selectbox("Inicio ventana", [f"{h:02d}:00" for h in range(24)], index=23)
    wn_fin_h = col_w2.selectbox("Fin ventana", [f"{h:02d}:00" for h in range(24)], index=5)
    wn_ini, wn_fin = int(wn_ini_h[:2]) * 60, int(wn_fin_h[:2]) * 60

aut = {"A2V": c_a2v, "B2V": c_b2v, "C2D": c_c2d}
rec = {"t_min_elec": float(t_min_elec), "t_min_diesel": 15.0, "potencia_kw": float(pot_kw),
       "rendimiento_kwh_km": float(rend),
       "cargadores": {"default": [{"tipo": "lenta", "potencia_kw": float(pot_kw),
                                   "cantidad": None, "tipos_bus": ["A2V", "B2V"]}]}}
it2 = {"soc_objetivo_fin_dia": float(soc_fin_dia), "soc_piso_kwh": float(piso_kwh),
       "rendimiento_kwh_km": float(rend), "potencia_nocturna_kw": float(pot_kw),
       "ventana_nocturna_inicio_min": wn_ini, "ventana_nocturna_fin_min": wn_fin,
       "n_cargadores_por_terminal": int(n_carg), "incluir_vacios_en_soc": incl_vacios}

# ---------- SIMULACIÓN (en vivo con parámetros) ----------
df, res_iter2 = simular(tipo_dia, p_min, p_max, aut, rec, it2)
k = res_iter2["kpis"]
st.caption(f"Simulación: parada {p_min}–{p_max} min · A2V {c_a2v} km · B2V {c_b2v} km · C2D {c_c2d} km"
           f" · SOC cap {cap_kwh:.0f} kWh, piso {piso_kwh:.0f}, rend {rend:.3f} kWh/km"
           f" · recarga nocturna {n_carg} carg/terminal {wn_ini_h}–{wn_fin_h}")

# ---------- FILTRO ----------
f = df.copy()
if terminal != "Todos":
    f = f[f["terminal"] == terminal]
if servicio != "Todos":
    f = f[f["servicio"] == servicio]
if sentido != "Todos":
    f = f[f["sentido"] == sentido]
if tipo_bus != "Todos":
    f = f[f["tipo_bus"] == tipo_bus]
f_ventana = f[((f["inicio"] < h_fin) & (f["fin"] > h_ini)) | (f["evento"] == "R01")]

tab1, tab2, tab3, tab4, tab5 = st.tabs([" Simulación", " Por línea (Marey)", " Gantt", " Tablas", " Jornada siguiente (iter 3)"])

# ================= TAB 1: SIMULACIÓN =================
with tab1:
    buses_vis = f_ventana["bus"].unique()
    km_com = f[f["evento"] == "C01"]["km"].sum()
    km_vac = f[f["evento"].isin(("V01", "V02"))]["km"].sum()
    tot = km_com + km_vac
    n_buses_tot = df["bus"].nunique()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Buses (selección)", len(buses_vis), help="Buses en la selección + ventana horaria")
    c2.metric("Buses (día completo)", n_buses_tot, help="Flota total del día con los parámetros actuales")
    c3.metric("Expediciones", int(f[f["evento"] == "C01"].shape[0]), help="Comerciales en la selección")
    c4.metric("Km comercial", f"{km_com:,.0f}")
    c5.metric("Km vacío", f"{km_vac:,.0f} ({km_vac / tot * 100:.1f}%)" if tot else "—")

    # KPIs iteración 2 (recarga nocturna)
    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Energía cargada de noche", f"{k['energia_noche_kwh']:,.0f} kWh",
              help="Programa nocturno (iteración 2)")
    g2.metric("Energía cargada de día", f"{k['energia_dia_kwh']:,.0f} kWh",
              help="Recargas diurnas (iteración 1, objetivo dinámico)")
    g3.metric("% cargado de noche", f"{k['pct_noche_pct']:.1f}%",
              help="Energía nocturna / energía total cargada")
    g4.metric("Buses adelantados al día", int(k["n_adelantados"]),
              help="Déficit nocturno → última recarga diurna extendida")
    if k["deficit_original_h"] > 0:
        st.info(f"Déficit nocturno original: **{k['deficit_original_h']:.1f} h** — resuelto adelantando "
                f"{int(k['n_adelantados'])} buses al día. Factible: {'SÍ' if k['factible'] else 'NO'}.")

    st.subheader("Timeline tipo metro")
    n_buses = len(buses_vis)
    if n_buses == 0:
        st.info("Sin eventos en la selección. Ajusta los filtros.")
    else:
        fig = go.Figure()
        for ev in ["C01", "V01", "V02", "R01"]:
            sub = f_ventana[f_ventana["evento"] == ev]
            if sub.empty:
                continue
            fig.add_trace(go.Bar(
                x=sub["fin"] - sub["inicio"], y=sub["bus"],
                base=sub["inicio"], orientation="h", name=LABEL_EVENTO[ev],
                marker_color=COLOR_EVENTO[ev], marker_line_width=0,
                customdata=pd.DataFrame({"servicio": sub["servicio"], "sentido": sub["sentido"],
                                         "km": sub["km"], "tipo": sub["tipo_bus"],
                                         "terminal": sub["terminal"]}),
                hovertemplate=("<b>%{y}</b> · " + LABEL_EVENTO[ev] + "<br>"
                               "%{customdata[4]} · %{customdata[0]} %{customdata[1]}<br>"
                               "%{customdata[2]:.1f} km · %{customdata[3]}<br>"
                               "%{base|%H:%M} → %{x|%H:%M} (%{x} min)<extra></extra>"),
            ))
        tick_min = {"1h": 60, "30m": 30, "15m": 15, "5m": 5, "1m": 1}[zoom]
        fig.update_layout(
            barmode="overlay", height=max(320, min(40 * n_buses, 1400)),
            xaxis=dict(title="Hora", range=[h_ini, h_fin], dtick=tick_min * 60000,
                       tickformat="%H:%M", ticklabelmode="period"),
            yaxis=dict(title="", autorange="reversed", showgrid=False),
            legend=dict(orientation="h", y=1.02), margin=dict(l=10, r=10, t=30, b=10),
            hovermode="closest",
        )
        fig.update_xaxes(tickmode="array", tickvals=list(range(h_ini, h_fin + 1, max(60, tick_min))),
                         ticktext=[fmt_min(t) for t in range(h_ini, h_fin + 1, max(60, tick_min))])
        st.plotly_chart(fig, width='stretch')

    st.subheader("Flota por terminal × tipo de bus ")
    flota = (f.groupby(["terminal", "tipo_bus"])["bus"].nunique().unstack(fill_value=0)
             .reindex(columns=["A2V", "B2V", "C2D"], fill_value=0))
    flota["Total"] = flota.sum(axis=1)
    flota = flota.sort_values("Total", ascending=False)
    st.dataframe(flota, width='stretch')

    st.subheader("Panel de flota — autonomía y SOC por bus")
    if n_buses == 0:
        st.info("Sin datos.")
    else:
        try:
            c01 = f_ventana[f_ventana["evento"] == "C01"]
            agr = (c01.groupby("bus")
                      .agg(tipo=("tipo_bus", "first"), terminal=("terminal", "first"),
                           km=("km", "sum"), vueltas=("bus", "count")))
            n_rec = f_ventana[f_ventana["evento"] == "R01"].groupby("bus").size()
            fin_soc = res_iter2.get("fin_por_bus", {})
            filas = []
            for b in sorted(buses_vis):
                if b not in agr.index:
                    continue
                r = agr.loc[b]
                soc_fin = fin_soc.get(b, {}).get("soc_fin")
                filas.append({"Bus": b, "Terminal": r["terminal"], "Tipo": r["tipo"],
                              "Autonomía (km)": aut.get(r["tipo"], 220.0),
                              "Vueltas": int(r["vueltas"]),
                              "Recargas": int(n_rec.get(b, 0)),
                              "Km/día": round(r["km"], 0),
                              "SOC fin jornada (kWh)": soc_fin if soc_fin is not None else None})
            tabla = pd.DataFrame(filas).sort_values(["Terminal", "Tipo", "Bus"])
            st.dataframe(tabla, width='stretch', height=min(40 * len(tabla) + 40, 480))
        except Exception as ex:
            import traceback
            traceback.print_exc()
            st.error(f"Panel de flota: {type(ex).__name__}: {ex}")

        st.subheader("Buses en ruta por hora (colas / cuellos de botella)")
        c01v = f[f["evento"] == "C01"]
        if not c01v.empty:
            horas = list(range(h_ini // 60, h_fin // 60))
            por_hora = pd.DataFrame({
                "hora": horas,
                "buses": [int(c01v.loc[((c01v["inicio"] <= h * 60 + 59) & (c01v["fin"] >= h * 60)), "bus"].nunique())
                          for h in horas],   # fix: buses únicos, no expediciones (doble conteo)
            })
            fig2 = px.bar(por_hora, x="hora", y="buses", labels={"hora": "Hora", "buses": "Buses en ruta"})
            fig2.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig2, width='stretch')

# ================= TAB 2: POR LÍNEA (MAREY / REJILLA) =================
with tab2:
    st.subheader("Diagrama de Marey — operación espacio-tiempo")
    st.caption("X = hora del día · Y = cabezal ida ↔ cabezal retorno ↔ terminal (COCH). "
               "Negras = ida, rojas = retorno, cian punteado = vacío, azul punteado = recarga, "
               "gris punteado = espera cabezal. Pendiente = velocidad.")

    mc1, mc2, mc3 = st.columns(3)
    with mc1:
        m_serv = st.selectbox("Línea (Marey)", ["Todos"] + servicios_term,
                              index=servicios_term.index("207") + 1 if "207" in servicios_term else 0,
                              help="«Todos» muestra la rejilla con un servicio por fila (v3)")
    with mc2:
        m_zoom = st.select_slider("Ventana de tiempo", options=["6 h", "4 h", "2 h", "1 h", "30 m"], value="4 h")
    with mc3:
        color_por_bus = st.toggle("Color por bus (FIG B)", value=False,
                                  help="Cada bus con su color de ciclo completo; OFF = por sentido (FIG A)")

    mdf = f.copy()
    if m_serv != "Todos":
        mdf = mdf[mdf["servicio"] == m_serv]
    c01m = mdf[mdf["evento"] == "C01"].sort_values("inicio")
    if c01m.empty:
        st.info("Sin viajes en la selección.")
    else:
        t0, t1 = c01m["inicio"].min(), c01m["fin"].max()
        span = t1 - t0
        vlen = {"30 m": 30, "1 h": 60, "2 h": 120, "4 h": 240, "6 h": 360}[m_zoom]
        if span > vlen:
            centro = (t0 + t1) / 2
            x0, x1 = max(t0, centro - vlen / 2), min(t1, centro + vlen / 2)
        else:
            x0, x1 = t0, t1
        if m_serv == "Todos":
            # ---- REJILLA v3: un servicio por fila (top 8 por flota) ----
            etiquetas = st.toggle("Etiquetas de bus en los viajes", value=False,
                                  help="N° bus·servicio al centro de cada diagonal (puede saturar)")
            orden = (mdf[mdf["evento"] == "C01"].groupby("servicio")["bus"].nunique()
                     .sort_values(ascending=False))
            serv_filas = list(orden.index[:8])
            figR = fig_rejilla(mdf, serv_filas, int(x0), int(x1), color_por_bus, etiquetas, 2.2)
            figR.update_layout(title=f"<b>Rejilla Marey 'Todos'</b> — {len(serv_filas)} servicios · {tipo_dia} "
                                     f"{fmt_min(int(x0))}–{fmt_min(int(x1))} · "
                                     f"{'FIG B · color por bus' if color_por_bus else 'FIG A · color por sentido'}")
            st.plotly_chart(figR, width='stretch')
            st.caption(f"{len(serv_filas)} servicios con mayor flota ({', '.join(serv_filas)}). "
                       "Zoom: rueda del mouse o rangeslider. Fila por servicio con su propio km máximo.")
        else:
            # ---- MAREY CLÁSICO por línea ----
            k1, k2, k3, k4 = st.columns(4)
            with k1:
                sep_y = st.slider("Separación vertical de cabezales", 0.3, 2.0, 1.0, step=0.1)
            with k2:
                inv = st.toggle("Invertir cabezales (ida abajo)", value=False)
            with k3:
                grosor = st.slider("Grosor de líneas", 1, 6, 2)
            with k4:
                etq = st.toggle("Etiquetas de bus", value=False)
            c1m, c2m, c3m = st.columns(3)
            with c1m:
                col_ida = st.color_picker("Color Ida", COL_IDA)
            with c2m:
                col_ret = st.color_picker("Color Retorno", COL_RET)
            with c3m:
                col_esp = st.color_picker("Color Espera", COL_ESP)
            if len(c01m) > 600:
                st.warning(f"{len(c01m):,} viajes en la selección — se limita a los primeros 600 para fluidez.")
                c01m = c01m.iloc[:600]
            figM = fig_marey_linea(c01m, col_ida, col_ret, col_esp, grosor, sep_y, inv, etq)
            figM.update_layout(xaxis=dict(title="Hora del día", range=[x0, x1],
                                          tickvals=list(range(int(x0 // 60) * 60, int(x1) + 61, 60)),
                                          ticktext=[fmt_min(t) for t in range(int(x0 // 60) * 60, int(x1) + 61, 60)]))
            figM.update_xaxes(rangeslider=dict(visible=True, thickness=0.06), range=[x0, x1])
            st.plotly_chart(figM, width='stretch')
            st.caption("Zoom: rueda del mouse o arrastra el rangeslider inferior. Pan: arrastra el gráfico.")

        st.subheader("Buses de la selección")
        filas_m = []
        for b in sorted(c01m["bus"].unique()):
            evs_b = c01m[c01m["bus"] == b]
            tb = evs_b["tipo_bus"].iloc[0]
            aut_b = aut.get(tb, 220.0)
            filas_m.append({"Bus": b, "Tipo": tb, "Viajes": len(evs_b),
                            "Km": round(evs_b["km"].sum(), 0), "Terminal": evs_b["terminal"].iloc[0],
                            "Autonomía (km)": aut_b})
        st.dataframe(pd.DataFrame(filas_m).sort_values("Bus"), width='stretch')

# ================= TAB 3: GANTT =================
with tab3:
    st.subheader("Gantt — programación por bus (v2)")
    st.caption("Bloques: C01 negro (ida) / rojo (retorno) · vacíos cian · espera gris · recarga azul punteada. "
               "Etiquetas: horas inicio→fin (rojo), servicio·sentido·km (negro), duración (azul).")
    buses_dia = sorted(df["bus"].unique())
    default_g = [b for b in ["B001", "B100", "B200"] if b in buses_dia][:3] or buses_dia[:3]
    sel_buses = st.multiselect("Buses", buses_dia, default=default_g)
    gt0, gt1 = st.slider("Ventana horaria (Gantt)", 0, 1440, (0, 1440), step=30)
    if not sel_buses:
        st.info("Selecciona al menos un bus.")
    else:
        gdf = df[df["bus"].isin(sel_buses)]
        figG = fig_gantt(gdf, gt0, gt1)
        st.plotly_chart(figG, width='stretch')
        # tabla resumen SOC de los buses seleccionados
        filas_g = []
        for b in sel_buses:
            st_b = res_iter2["fin_por_bus"].get(b, {})
            evs_b = gdf[gdf["bus"] == b]
            sin_soc = st_b.get("sin_soc", False)
            soc_fin_b = st_b.get("soc_fin")
            ef = max(0.0, float(soc_fin_dia) - (soc_fin_b or 0.0)) if (soc_fin_b is not None and not sin_soc) else 0.0
            filas_g.append({"Bus": b, "Tipo": evs_b["tipo_bus"].iloc[0] if len(evs_b) else "—",
                            "Terminal": st_b.get("terminal", "—"), "Km/día": round(evs_b["km"].sum(), 0),
                            "SOC fin jornada": soc_fin_b if (soc_fin_b is not None and not sin_soc) else None,
                            "Carga nocturna (kWh)": round(ef, 1),
                            "Recarga nocturna (min)": round(ef / pot_kw * 60.0, 0)})
        st.dataframe(pd.DataFrame(filas_g), width='stretch')

    st.subheader("Programa de recarga nocturna (iteración 2)")
    prog = res_iter2["programa_nocturno"]
    if prog:
        pn = pd.DataFrame(prog).sort_values(["terminal", "inicio_min"])
        pn["inicio"] = pn["inicio_min"].apply(fmt_min)
        pn["fin"] = pn["fin_min"].apply(fmt_min)
        st.dataframe(pn[["bus", "terminal", "cargador", "inicio", "fin", "t_min", "kwh"]]
                     .rename(columns={"bus": "Bus", "terminal": "Terminal", "cargador": "Cargador",
                                      "inicio": "Inicio", "fin": "Fin", "t_min": "Min", "kwh": "kWh"}),
                     width='stretch', height=320)
    else:
        st.info("Sin programa nocturno (todos los buses terminan al 100% o no hay cargadores nocturnos).")

# ================= TAB 4: TABLAS =================
with tab4:
    st.subheader("Detalle de bus")
    st.caption("Selecciona el N° de bus en la vista general (Panel de flota) o aquí.")
    bus_sel = st.selectbox("N° de bus", sorted(df["bus"].unique()),
                           help="La vista general (tab Simulación) lista los buses; este detalle se actualiza al presionar el N° de bus")
    if bus_sel:
        b_df = df[df["bus"] == bus_sel]
        det, kpis_bus = filas_detalle_bus(b_df)
        tb = b_df["tipo_bus"].iloc[0]
        term = b_df["terminal"].iloc[0]
        st.markdown(f"**{bus_sel}** · {tb} · {term} · {tipo_dia}")
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        for col, (lab, val) in zip([k1, k2, k3, k4, k5, k6], kpis_bus.items()):
            col.metric(lab, val)
        soc_fin_b = res_iter2["fin_por_bus"].get(bus_sel, {}).get("soc_fin")
        sin_soc_b = res_iter2["fin_por_bus"].get(bus_sel, {}).get("sin_soc", False)
        if soc_fin_b is not None and not sin_soc_b:
            ef_b = max(0.0, float(soc_fin_dia) - soc_fin_b)
            st.caption(f"SOC fin de jornada: **{soc_fin_b:.1f} kWh** "
                       f"(recarga nocturna {ef_b:.0f} kWh = {ef_b / pot_kw * 60.0:.0f} min)")
        det_disp = det.copy()
        for c in ["Inicio", "Fin"]:
            det_disp[c] = det_disp[c].apply(fmt_min)
        det_disp["Dur."] = det_disp["Dur."].apply(fmt_dur)
        det_disp["Kms"] = det_disp["Kms"].round(1)
        st.dataframe(det_disp, width='stretch', height=min(44 * len(det_disp) + 40, 520))

    st.subheader("Programación completa")
    st.caption("Toda la programación del día ordenada por N° de bus — exportable a CSV.")
    prog_cols = ["bus", "servicio", "sentido", "evento", "inicio", "fin", "km", "terminal", "tipo_bus"]
    prog_df = df[prog_cols].copy()
    prog_df["inicio_h"] = prog_df["inicio"].apply(fmt_min)
    prog_df["fin_h"] = prog_df["fin"].apply(fmt_min)
    prog_df["dur_min"] = (prog_df["fin"] - prog_df["inicio"]).round(0)
    prog_df = prog_df.sort_values(["bus", "inicio"])
    prog_df["soc_ini"] = df.loc[prog_df.index, "soc_inicio"]
    prog_df["soc_fin"] = df.loc[prog_df.index, "soc_fin"]
    st.dataframe(prog_df[["bus", "servicio", "sentido", "evento", "inicio_h", "fin_h", "dur_min",
                          "km", "terminal", "tipo_bus", "soc_ini", "soc_fin"]]
                 .rename(columns={"bus": "Bus", "servicio": "Servicio", "sentido": "Sentido",
                                  "evento": "Evento", "inicio_h": "Inicio", "fin_h": "Fin",
                                  "dur_min": "Dur. (min)", "km": "Kms", "terminal": "Terminal",
                                  "tipo_bus": "Tipo bus", "soc_ini": "SOC ini", "soc_fin": "SOC fin"}),
                 width='stretch', height=480)
    csv = prog_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("Descargar CSV", csv, f"programacion_{tipo_dia.lower().replace('á', 'a')}.csv",
                       "text/csv", key="csv_prog")

st.caption("Modelo: motor de autonomía parametrizable (parada min–max en cabezal, km por tipo). "
           "SOC: capacidad 385 kWh, piso 80, rendimiento 1,275 kWh/km. "
           "Iteración 2: recarga nocturna con restricción de cargadores por terminal. "
           "Datos sinteticos generados para demostracion.")

# ================= TAB 5: JORNADA SIGUIENTE (ITERACIÓN 3,) =================
COMBOS = {
    "Laboral → Laboral": ("Laboral", "Laboral"),
    "Laboral → Sábado": ("Laboral", "Sábado"),
    "Sábado → Domingo": ("Sábado", "Domingo"),
    "Domingo → Laboral (crítico)": ("Domingo", "Laboral"),
}

@st.cache_data
def iter3_cached(d1, d2, p_min, p_max, it2_key, madrugada_fin):
    evs1, _ = motor.simular(d1, p_min, p_max, aut, rec)
    evs2, _ = motor.simular(d2, p_min, p_max, aut, rec)
    df1 = pd.DataFrame(evs1)
    df1["terminal"] = df1["terminal"].fillna(df1["servicio"].map(SERV_TERMINAL).fillna(""))
    df2 = pd.DataFrame(evs2)
    df2["terminal"] = df2["terminal"].fillna(df2["servicio"].map(SERV_TERMINAL).fillna(""))
    return motor.iteracion3(df1.to_dict("records"), df2.to_dict("records"), it2,
                            {"madrugada_fin_min": float(madrugada_fin)})

with tab5:
    st.subheader("Jornada siguiente — horizonte 40 h (iteración 3,)")
    st.caption("Programa nocturno garantizado para la primera salida del día siguiente: "
               "cada bus enchufa apenas llega (sin piso de ventana, cola por SOC fin de día) "
               "y los slots de salida madrugada del día 2 consumen buses del inventario por terminal.")

    cT1, cT2, cT3 = st.columns([2, 1, 1])
    combo = cT1.selectbox("Combinación día 1 → día 2", list(COMBOS), index=3)
    d1, d2 = COMBOS[combo]
    terminal_i3 = cT2.selectbox("Terminal", ["Todos"] + list(TERMINAL_SERVICIOS), index=5)
    madrugada_fin = cT3.number_input("Fin franja madrugada (min)", 120, 600, 300, step=30,
                                     help="Slots de primera salida hasta esta hora del día 2")

    r3 = iter3_cached(d1, d2, p_min, p_max, str(sorted(it2.items())), madrugada_fin)
    ki = r3["kpis_asignacion"]
    prog_i3 = pd.DataFrame(r3["programa_nocturno"])
    asig_i3 = pd.DataFrame(r3["asignacion"])
    sin_i3 = pd.DataFrame(r3["sin_cubrir"])

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Slots madrugada día 2", ki["slots_total"])
    m2.metric("Cubiertos a tiempo", f"{ki['slots_cubiertos']} ({ki['pct_cobertura']}%)")
    m3.metric("Recargas nocturnas", ki["recargas_nocturnas"])
    m4.metric("Energía nocturna", f"{ki['kwh_noche']:,.0f} kWh")
    m5.metric("Buses nuevos día 2", ki["buses_no_operados_d1"],
              help="No operaron día 1 → disponibles al 100% desde la noche")
    if ki["slots_cubiertos"] < ki["slots_total"]:
        st.error(f"**{ki['slots_total'] - ki['slots_cubiertos']} slots sin bus cargado a la hora** — "
                 "sube cargadores por terminal o revisa la ventana nocturna.")

    # ---- rejilla 40 h ----
    st.markdown("##### Rejilla 40 h (muestra de 12 buses del terminal)")
    ev1_fig = df if tipo_dia == d1 else None
    if ev1_fig is None:
        # el día 1 del combo puede diferir del tipo_dia simulado arriba: reusar iter3 cache
        evs1r, _ = motor.simular(d1, p_min, p_max, aut, rec)
        ev1_fig = pd.DataFrame(evs1r)
        ev1_fig["terminal"] = ev1_fig["terminal"].fillna(ev1_fig["servicio"].map(SERV_TERMINAL).fillna(""))
    term_f = terminal_i3 if terminal_i3 != "Todos" else None
    if term_f:
        ev1_fig = ev1_fig[ev1_fig["terminal"] == term_f]
    muestra = sorted(ev1_fig["bus"].unique())[:12]
    fig40 = go.Figure()
    COLOR_EV = {"C01": "#1d4ed8", "V01": "#94a3b8", "V02": "#64748b", "R01": "#16a34a"}
    for i, b in enumerate(muestra):
        y = len(muestra) - i
        for _, r in ev1_fig[ev1_fig.bus == b].iterrows():
            t0, t1 = r.inicio / 60, max(r.fin / 60, r.inicio / 60 + 0.02)
            fig40.add_trace(go.Scatter(x=[t0, t1], y=[y, y], mode="lines",
                line=dict(width=6, color=COLOR_EV.get(r.evento, "#94a3b8")), showlegend=False,
                hovertemplate=f"{b} {r.evento} {r.servicio} {r.sentido}<extra></extra>"))
        if len(prog_i3):
            pr = prog_i3[prog_i3.bus == b]
            for _, r in pr.iterrows():
                t0 = r.inicio_min / 60 if r.inicio_min < 1440 else 24 + (r.inicio_min - 1440) / 60
                t1 = r.fin_min / 60 if r.fin_min < 1440 else 24 + (r.fin_min - 1440) / 60
                fig40.add_trace(go.Scatter(x=[t0, t1], y=[y, y], mode="lines",
                    line=dict(width=12, color="#16a34a"), showlegend=False,
                    hovertemplate=f"RECARGA NOCTURNA {b} c{int(r.cargador)} {r.kwh:.0f} kWh<extra></extra>"))
        if len(asig_i3):
            fa = asig_i3[asig_i3.bus_fisico == b]
            if len(fa):
                s = fa.iloc[0]
                fig40.add_trace(go.Scatter(x=[24 + s.salida_min / 60], y=[y], mode="markers",
                    marker=dict(size=14, color="#b91c1c", symbol="line-ns-open", line=dict(width=3)),
                    showlegend=False,
                    hovertemplate=f"1ª salida día 2: {int(s.salida_min//60):02d}:{int(s.salida_min%60):02d} {s.servicio} {s.sentido}<extra></extra>"))
    ticks40 = list(range(0, 41, 2))
    fig40.add_vrect(x0=24, x1=24.5, fillcolor="#334155", opacity=0.25, line_width=0)
    fig40.add_vrect(x0=24, x1=29, fillcolor="#16a34a", opacity=0.07, line_width=0)
    fig40.update_xaxes(tickvals=ticks40, ticktext=[f"{t%24:02d}:00" for t in ticks40],
                       range=[0, 40], title="hora — día 1 → noche → día siguiente (24:00 = 00:00)")
    fig40.update_yaxes(tickvals=list(range(1, len(muestra) + 1)), ticktext=muestra[::-1] if muestra else [])
    fig40.update_layout(height=460, margin=dict(l=60, r=20, t=30, b=50), plot_bgcolor="white",
                        xaxis=dict(gridcolor="#e2e8f0"), yaxis=dict(gridcolor="#f1f5f9"))
    if muestra:
        st.plotly_chart(fig40, width='stretch')
    else:
        st.info("Sin buses en el terminal seleccionado.")

    # ---- tabla programación de recargas por bus y tipo de día ----
    st.markdown("##### Programación de recargas por bus y tipo de día")
    if len(prog_i3):
        pn = prog_i3.copy()
        pn["tipo_dia"] = d1
        pn["inicio_h"] = pn["inicio_min"].apply(fmt_min)
        pn["fin_h"] = pn["fin_min"].apply(fmt_min)
        if terminal_i3 != "Todos":
            pn = pn[pn["terminal"] == terminal_i3]
        pn = pn.sort_values(["terminal", "inicio_min"])
        st.dataframe(pn[["bus", "tipo_dia", "terminal", "cargador", "inicio_h", "fin_h", "t_min", "kwh"]]
                     .rename(columns={"bus": "Bus", "tipo_dia": "Tipo día", "terminal": "Terminal",
                                      "cargador": "Cargador", "inicio_h": "Inicio", "fin_h": "Fin",
                                      "t_min": "Min", "kwh": "kWh"}),
                     width='stretch', height=360)
        csv_rec = pn[["bus", "tipo_dia", "terminal", "cargador", "inicio_min", "fin_min", "t_min", "kwh"]]\
            .rename(columns={"inicio_min": "inicio", "fin_min": "fin", "t_min": "minutos"})\
            .to_csv(index=False).encode("utf-8-sig")
        st.download_button("Descargar recargas CSV", csv_rec,
                           f"recargas_nocturnas_{d1.lower().replace('á','a')}_{terminal_i3.lower().replace(' ','_').replace('á','a')}.csv",
                           "text/csv", key="csv_rec_i3")
    else:
        st.info("Sin recargas nocturnas con los parámetros actuales.")

    # ---- tabla asignación slots ----
    st.markdown("##### Asignación de buses a la primera salida del día siguiente")
    filas_asig = []
    if len(asig_i3):
        for _, r in asig_i3.sort_values("salida_min").iterrows():
            if terminal_i3 != "Todos" and r.terminal != terminal_i3:
                continue
            filas_asig.append({"Salida": fmt_min(r.salida_min), "Bus slot": r.bus_slot,
                               "Servicio": f"{r.servicio} {r.sentido}", "Km 1ª": round(r.km_primera, 1),
                               "SOC req": round(r.soc_req, 1), "Bus asignado": r.bus_fisico,
                               "SOC prog": r.soc_prog, "Vuelve": fmt_min(r.vuelta_min), "Terminal": r.terminal})
    if len(sin_i3):
        for _, r in sin_i3.iterrows():
            if terminal_i3 != "Todos" and r.terminal != terminal_i3:
                continue
            filas_asig.append({"Salida": fmt_min(r.salida_min), "Bus slot": r.bus_slot,
                               "Servicio": f"{r.servicio} {r.sentido}", "Km 1ª": round(r.km_primera, 1),
                               "SOC req": round(r.soc_req, 1), "Bus asignado": "— sin bus —",
                               "SOC prog": None, "Vuelve": fmt_min(r.vuelta_min), "Terminal": r.terminal})
    if filas_asig:
        ta = pd.DataFrame(filas_asig)
        st.dataframe(ta, width='stretch', height=360)
        csv_asig = ta.to_csv(index=False).encode("utf-8-sig")
        st.download_button("Descargar asignación CSV", csv_asig,
                           f"asignacion_jornada_siguiente_{d1.lower().replace('á','a')}.csv",
                           "text/csv", key="csv_asig_i3")
    else:
        st.info("Sin slots de madrugada en la selección.")

    # ---- detalle del programa nocturno por terminal (depuración de déficit) ----
    if len(sin_i3):
        st.markdown("##### Causas probables de slots sin cubrir")
        st.caption("Buses listos tarde: el bus más descargado aún no termina de cargar a la hora del slot. "
                   "SOC insuficiente: no hay bus con carga suficiente disponible. "
                   "Ajusta cargadores por terminal (sidebar) o la ventana nocturna y re-ejecuta.")
