"""Motor de simulación de autonomía de flota eléctrica (parametrizable).
Los datos de entrada son SINTETICOS (ver generar_datos_sinteticos.py).
Reglas del usuario:
- Eléctrico (A2V/B2V): autonomía 220 km (parametrizable). Diésel articulado (C2D): 500 km.
- Un bus sale del terminal (V01), da vueltas (C01); si tras una vuelta le alcanza
  autonomía para otra, la da (con parada min-max parametrizable); si no, V02 al terminal.
- Tiempo de parada (descanso entre vueltas): min 0 min, max 30 min por defecto.
- RECICLAJE: si ningún bus libre tiene autonomía, el bus libre con más km vuelve al
  terminal (V02), recarga (km_acum=0) y vuelve (V01). El bus conserva su tipo.
Salida (CLI): eventos por bus + KPIs.
Importable: simular(tipo_dia, parada_min, parada_max, aut_dict) para la app Streamlit.
"""
import json
from pathlib import Path
import pandas as pd

OUT = Path(__file__).resolve().parent / "data_sintetica"

# parámetros por defecto
AUTONOMIA = {"A2V": 220.0, "B2V": 220.0, "C2D": 500.0}
PARADA_MIN, PARADA_MAX = 0, 30          # minutos entre vueltas
FACTOR = 1.3                            # red vial para vacíos

# modelo de recarga parametrizable (parámetros de operación)
# - Eléctricos (A2V/B2V): tiempo mínimo en terminal 30 min; cargador 90 kW/h;
#   rendimiento 1,275 kWh/km  →  t = max(t_min_elec, km_acum * rendimiento / potencia * 60).
# - Diésel (C2D): mínimo 15 min en terminal (fijo, no depende de energía).
# - Cargadores por terminal (o "default"): cantidad=None → sin restricción (todos cargan
#   igual); cantidad=N → cola FIFO de N cargadores. Puede haber más de un tipo de
#   cargador por terminal (cada uno con su potencia y tipos_bus compatibles).
RECARGA = {
    "t_min_elec": 30.0,
    "t_min_diesel": 15.0,
    "potencia_kw": 90.0,
    "rendimiento_kwh_km": 1.275,        #: 1,275 supersede al 1,1 (30-ago)
    "cargadores": {
        "default": [{"tipo": "lenta", "potencia_kw": 90.0, "cantidad": None,
                     "tipos_bus": ["A2V", "B2V"]}],
    },
}

# ITERACIÓN 2 — programa de recarga nocturna (, anotación usuario 30-ago).
# Todos los buses deben quedar al 100% (soc_objetivo_fin_dia) al inicio de operación
# del día siguiente; la ventana nocturna × n_cargadores por terminal puede no alcanzar
# → los buses con mayor demanda nocturna adelantan carga al día (última recarga diurna
# extendida). Parámetros todos parametrizables (sidebar).
ITER2 = {
    "soc_objetivo_fin_dia": 385.0,      # kWh — 100% exigido al día siguiente
    "soc_piso_kwh": 80.0,               # margen de seguridad (no bajar en llegada)
    "rendimiento_kwh_km": 1.275,        #
    "potencia_nocturna_kw": 90.0,       # kW por cargador nocturno
    "t_min_nocturno_min": 0.0,          # recarga mínima nocturna por bus (0 = solo energía faltante,
                                        #   consistente con el análisis validado)
    "ventana_nocturna_inicio_min": 23 * 60,   # 23:00 (parametrizable)
    "ventana_nocturna_fin_min": 5 * 60,       # 05:00 (parametrizable; usar horario real de operación)
    "n_cargadores_por_terminal": 20,    # int global, o dict {"Terminal Norte": N, ...}; 0 → sin carga nocturna en ese terminal
    "buses_sin_soc": ["C2D"],           # tipos que NO aplican SOC (diésel): ni consumen ni demandan noche
    "incluir_vacios_en_soc": False,     # V01/V02 NO descuentan SOC (consistente con km_acum)
}

# datos (carga perezosa)
_df = None
_vac = None
_exp = None


def cargar_datos():
    """Carga modelo_un16 (segmentos) y vacios_un16; deriva expediciones únicas."""
    global _df, _vac, _exp
    if _exp is None:
        _df = pd.read_parquet(OUT / "modelo_un16.parquet")
        _vac = pd.read_parquet(OUT / "vacios_un16.parquet")
        _exp = (_df.groupby(["servicio", "sentido", "tipo_dia", "exp_ini", "dur_total", "km_total", "tipo_bus"])
                  .size().reset_index(name="n_seg")
                  .rename(columns={"exp_ini": "h_ini", "dur_total": "dur", "km_total": "km"}))
        _exp = _exp.sort_values(["tipo_dia", "h_ini"]).reset_index(drop=True)
    return _exp, _vac


def tiempo_recarga(bus, rec_map, cola, reservar=False):
    """Minutos que el bus permanece en terminal para recargar .
    Eléctricos: max(t_min_elec, km_acum*rendimiento_kwh_km/potencia_kw*60).
    Diésel: t_min_diesel fijo. Si hay cargadores con cantidad>0 definida para el
    terminal/tipo, se aplica cola FIFO (espera hasta que un slot se libere).
    bus['libre_en'] debe ser el momento de LLEGADA al terminal."""
    tb = bus["tipo_bus"]
    if tb == "C2D":
        return rec_map["t_min_diesel"]
    energia = bus["km_acum"] * rec_map["rendimiento_kwh_km"]
    t_carga = max(rec_map["t_min_elec"], energia / rec_map["potencia_kw"] * 60.0)
    # configuración de cargadores para este terminal (o default)
    cfg = (rec_map.get("cargadores") or {}).get(bus.get("terminal", "")) \
          or (rec_map.get("cargadores") or {}).get("default") or []
    for c in cfg:
        if tb in c.get("tipos_bus", []) and c.get("cantidad"):
            n = int(c["cantidad"])
            pot = c.get("potencia_kw") or rec_map["potencia_kw"]
            t_carga = max(rec_map["t_min_elec"], energia / pot * 60.0)
            key = (bus.get("terminal", ""), c.get("tipo", "default"))
            slots = cola.setdefault(key, [0.0] * n)
            i = min(range(n), key=lambda k: slots[k])
            inicio = max(bus["libre_en"], slots[i])
            fin = inicio + t_carga
            if reservar:
                slots[i] = fin
            return fin - bus["libre_en"]   # espera en cola + carga
    return t_carga


def simular(tipo_dia, parada_min=PARADA_MIN, parada_max=PARADA_MAX, aut=None, recarga=None):
    """Greedy: asignar expediciones a buses respetando autonomía y parada.
    aut: dict {A2V/B2V/C2D: km}; None → AUTONOMIA por defecto.
    recarga: dict con los parámetros RECARGA (t_min_elec, t_min_diesel, potencia_kw,
    rendimiento_kwh_km, cargadores); None → RECARGA por defecto.
    La espera por recarga (eléctricos: max(t_min_elec, km*rend/potencia*60); diésel:
    t_min_diesel) se suma al V02; si el bus no alcanza a recargar y volver para h_ini,
    NO se recicla (se crea bus nuevo). Cola FIFO por (terminal, tipo de cargador)
    solo cuando cantidad > 0 está definida."""
    exp, vac = cargar_datos()
    aut_map = dict(AUTONOMIA if aut is None else aut)
    rec_map = dict(RECARGA if recarga is None else recarga)
    exps = exp[exp["tipo_dia"] == tipo_dia].to_dict("records")
    if not exps:
        return [], {}
    # mapa vacío por (servicio, sentido)
    vac_map = {(v["servicio"], v["sentido"]): v for v in vac[vac["tipo_dia"] == tipo_dia].to_dict("records")}
    buses = []          # buses activos: {id, km_acum, libre_en, servicio, sentido, terminal, tipo_bus}
    eventos = []        # eventos: tipo (V01/C01/V02/R01), bus, inicio, fin, km, terminal
    cola = {}           # cola FIFO de cargadores: (terminal, tipo_cargador) -> [fin de cada slot]
    n_buses_creados = 0
    n_recargas = 0
    t_recarga_total = 0.0
    kwh_recarga_total = 0.0
    for e in exps:
        serv, sent, tb = e["servicio"], e["sentido"], e["tipo_bus"]
        aut_bus = aut_map.get(tb, 220.0)
        km_vuelta = e["km"] or 0
        v = vac_map.get((serv, sent))
        # buscar bus disponible con autonomía y MISMO tipo de bus
        bus = None
        for b in buses:
            if b["tipo_bus"] == tb and b["libre_en"] <= e["h_ini"] and b["km_acum"] + km_vuelta <= aut_bus:
                bus = b
                break
        reciclado = False
        if bus is None:
            # RECICLAJE: solo si alcanza a recargar y volver para h_ini (factibilidad)
            libres = [b for b in buses if b["tipo_bus"] == tb and b["libre_en"] <= e["h_ini"]]
            v01_nuevo = int(v["v01_min"]) if v is not None else 0
            for b in sorted(libres, key=lambda x: x["km_acum"], reverse=True):
                v2b = vac_map.get((b["servicio"], "Ida")) or vac_map.get((b["servicio"], "Ret"))
                v02b = int(v2b["v02_min"]) if v2b is not None else 0
                llegada_term = b["libre_en"] + v02b          # llega al terminal tras V02
                b_sim = dict(b); b_sim["libre_en"] = llegada_term
                t_rec = tiempo_recarga(b_sim, rec_map, cola) # sin reservar slot
                if llegada_term + t_rec <= e["h_ini"] - v01_nuevo:
                    bus, v2, v02_min = b, v2b, v02b
                    break
            if bus is not None:
                # V02: vuelve al terminal
                if v2 is not None:
                    eventos.append({"tipo_dia": tipo_dia, "bus": f"B{bus['id']:03d}", "evento": "V02",
                                    "servicio": bus["servicio"], "sentido": bus["sentido"],
                                    "inicio": bus["libre_en"], "fin": bus["libre_en"] + v02_min,
                                    "km": v2["v02_km"], "tipo_bus": bus["tipo_bus"],
                                    "terminal": bus["terminal"]})
                bus["libre_en"] += v02_min                   # llegada al terminal
                t_rec = tiempo_recarga(bus, rec_map, cola, reservar=True)
                bus["libre_en"] += t_rec                     # + espera/carga en terminal
                n_recargas += 1
                t_recarga_total += t_rec
                kwh_recarga_total += bus["km_acum"] * rec_map["rendimiento_kwh_km"]
                bus["km_acum"] = 0.0                         # recarga completa
                reciclado = True
            else:
                bus = None
        if bus is None:
            # crear bus nuevo: V01 desde terminal
            n_buses_creados += 1
            term = v["terminal"] if v is not None else ""
            if v is not None:
                eventos.append({"tipo_dia": tipo_dia, "bus": f"B{n_buses_creados:03d}", "evento": "V01",
                                "servicio": serv, "sentido": sent, "inicio": max(0, e["h_ini"] - int(v["v01_min"])),
                                "fin": e["h_ini"], "km": v["v01_km"], "tipo_bus": tb, "terminal": term})
            bus = {"id": n_buses_creados, "km_acum": 0.0, "libre_en": e["h_ini"],
                   "servicio": serv, "sentido": sent, "terminal": term, "tipo_bus": tb}
            buses.append(bus)
        elif reciclado:
            # bus reciclado: V01 desde el terminal hacia el cabezal de este servicio
            if v is not None:
                eventos.append({"tipo_dia": tipo_dia, "bus": f"B{bus['id']:03d}", "evento": "V01",
                                "servicio": serv, "sentido": sent, "inicio": max(0, e["h_ini"] - int(v["v01_min"])),
                                "fin": e["h_ini"], "km": v["v01_km"], "tipo_bus": tb,
                                "terminal": bus["terminal"]})
            bus["servicio"] = serv
            bus["sentido"] = sent
        # parada (regulación) si llegó antes
        if bus["libre_en"] < e["h_ini"]:
            if reciclado:
                # espera en terminal del bus reciclado: termina cuando parte el V01
                fin_r01 = e["h_ini"] - (int(v["v01_min"]) if v is not None else 0)
                parada = min(parada_max, max(parada_min, fin_r01 - bus["libre_en"]))
                fin_r01 = bus["libre_en"] + parada
            else:
                # parada regulada entre vueltas (mismo servicio)
                parada = min(parada_max, max(parada_min, e["h_ini"] - bus["libre_en"]))
                fin_r01 = bus["libre_en"] + parada
            if parada > 0 and fin_r01 > bus["libre_en"]:
                eventos.append({"tipo_dia": tipo_dia, "bus": f"B{bus['id']:03d}", "evento": "R01",
                                "servicio": serv, "sentido": sent, "inicio": bus["libre_en"],
                                "fin": fin_r01, "km": 0, "tipo_bus": tb,
                                "terminal": bus["terminal"]})
        # expedición C01
        eventos.append({"tipo_dia": tipo_dia, "bus": f"B{bus['id']:03d}", "evento": "C01",
                        "servicio": serv, "sentido": sent, "inicio": e["h_ini"],
                        "fin": e["h_ini"] + e["dur"], "km": km_vuelta, "tipo_bus": tb,
                        "terminal": bus["terminal"]})
        bus["km_acum"] += km_vuelta
        bus["libre_en"] = e["h_ini"] + e["dur"]
    # cierre: V02 para cada bus
    for b in buses:
        serv, sent = b["servicio"], "Ida"
        v = vac_map.get((serv, sent)) or vac_map.get((serv, "Ret"))
        if v is not None:
            eventos.append({"tipo_dia": tipo_dia, "bus": f"B{b['id']:03d}", "evento": "V02",
                            "servicio": serv, "sentido": sent, "inicio": b["libre_en"],
                            "fin": b["libre_en"] + int(v["v02_min"]), "km": v["v02_km"],
                            "tipo_bus": b["tipo_bus"], "terminal": b["terminal"]})
    return eventos, {"n_buses": n_buses_creados, "n_exp": len(exps),
                     "n_recargas": n_recargas, "t_recarga_min": round(t_recarga_total, 1),
                     "kwh_recarga": round(kwh_recarga_total, 1)}


def kpi_por_bus(eventos):
    """km por bus y vueltas para el resumen CLI."""
    por_bus = {}
    for e in eventos:
        if e["evento"] == "C01":
            por_bus.setdefault(e["bus"], [0, 0.0])
            por_bus[e["bus"]][0] += 1
            por_bus[e["bus"]][1] += e["km"]
    return por_bus


# ============================================================
# ITERACIÓN 2 — PROGRAMA DE RECARGA NOCTURNA
# ============================================================
# Lógica de 2 iteraciones (aprobada 30-ago):
#   ITER 1 (día,): recarga parcial con objetivo dinámico
#     SOC_objetivo = min(CAP, PISO + km_vueltas_hasta_prox_R01 × REND)
#     → cada bus termina el día con SOC_fin distinto.
#   ITER 2 (noche,): Σ t_noct por terminal vs n_cargadores × ventana.
#     SÍ cabe → programa nocturno factible → FIN.
#     NO cabe → los buses con mayor demanda nocturna de ese terminal
#     adelantan su carga al día (se les extiende la última recarga diurna),
#     quedando con SOC_fin = CAP; se itera (en la práctica una pasada basta:
#     los adelantos solo reducen demanda nocturna y no crean restricciones
#     nuevas, porque la recarga diurna usa la parada existente sin límite
#     de cargadores).

def _n_cargadores(nc, terminal):
    """Resuelve n_cargadores_por_terminal (int global o dict por terminal)."""
    if isinstance(nc, dict):
        return nc.get(terminal, nc.get("default", 0))
    return int(nc or 0)


def _ventana(p):
    """Ventana nocturna en minutos, manejando cruce de medianoche (23:00→05:00).
    Devuelve (win_ini, win_fin_abs, win_min): si fin <= inicio, fin se
    normaliza a +1440 min (mismo día siguiente) y la duración es positiva."""
    ini, fin = p["ventana_nocturna_inicio_min"], p["ventana_nocturna_fin_min"]
    if fin <= ini:
        fin += 1440
    return ini, fin, fin - ini


def simular_soc_iter1(eventos, it2=None):
    """ITERACIÓN 1: asigna SOC a cada evento, bus por bus.

    SOC inicial = CAP (100%). Cada C01 descuenta km × REND; en cada R01 el
    objetivo es dinámico (min(CAP, PISO + km_C01_hasta_prox_R01 × REND));
    recarga = max(0, objetivo − SOC) → SOC = objetivo (0 kWh si no hace falta).
    V01/V02 no descuentan SOC salvo `incluir_vacios_en_soc`.

    Devuelve (eventos_con_soc, fin_por_bus):
      eventos_con_soc: copia con soc_inicio/soc_fin por evento.
      fin_por_bus[b] = {soc_fin, terminal, tipo_bus, ultimo_fin, kwh_diurno}.
    """
    p = {**ITER2, **(it2 or {})}
    cap, piso, rend = (p["soc_objetivo_fin_dia"], p["soc_piso_kwh"],
                       p["rendimiento_kwh_km"])
    incl_vac = p["incluir_vacios_en_soc"]
    sin_soc = set(p["buses_sin_soc"])

    agrupado = {}
    for e in eventos:
        agrupado.setdefault(e["bus"], []).append(e)
    salida, fin = [], {}
    for b, evs in agrupado.items():
        evs.sort(key=lambda x: (x["inicio"], x["fin"]))
        idx_r01 = [i for i, e in enumerate(evs) if e["evento"] == "R01"]
        es_diesel = evs[0].get("tipo_bus") in sin_soc
        soc = cap
        kwh_dia = 0.0
        terminal = evs[0].get("terminal")
        for i, e in enumerate(evs):
            e = dict(e)
            e["soc_inicio"] = round(soc, 1)
            if es_diesel:
                pass  # diésel (C2D): sin SOC — no consume ni recarga
            elif e["evento"] == "C01":
                soc -= e["km"] * rend
            elif e["evento"] in ("V01", "V02") and incl_vac:
                soc -= e["km"] * rend
            elif e["evento"] == "R01":
                # km de C01 hasta el próximo R01 (o fin de jornada)
                prox = next((j for j in idx_r01 if j > i), len(evs))
                km_hasta = sum(ev["km"] for ev in evs[i + 1:prox]
                               if ev["evento"] == "C01")
                objetivo = min(cap, piso + km_hasta * rend)
                kwh = max(0.0, objetivo - soc)
                if kwh > 0:
                    soc = objetivo
                    kwh_dia += kwh
            e["soc_fin"] = round(soc, 1)
            salida.append(e)
            if e.get("terminal"):
                terminal = e["terminal"]
        fin[b] = {"soc_fin": soc, "terminal": terminal,
                  "tipo_bus": evs[-1].get("tipo_bus"),
                  "ultimo_fin": evs[-1]["fin"], "kwh_diurno": kwh_dia,
                  "sin_soc": es_diesel}
    return salida, fin


def demanda_nocturna(fin_por_bus, it2=None):
    """ITERACIÓN 2 (parte 1): energía y tiempo nocturno por bus y por terminal.

    E_faltante = max(0, CAP − SOC_fin); t_noct = E_faltante / POT × 60.
    Devuelve (por_bus, por_terminal):
      por_bus[b] = fin_por_bus[b] + {e_faltante_kwh, t_noct_min}
      por_terminal[t] = {t_noct, kwh, n_buses}
    """
    p = {**ITER2, **(it2 or {})}
    cap, pot = p["soc_objetivo_fin_dia"], p["potencia_nocturna_kw"]
    por_bus, por_term = {}, {}
    for b, st in fin_por_bus.items():
        if st.get("sin_soc"):   # diésel: sin demanda nocturna
            por_bus[b] = {**st, "e_faltante_kwh": 0.0, "t_noct_min": 0.0}
            continue
        ef = max(0.0, cap - st["soc_fin"])
        t = 0.0 if ef <= 0 else ef / pot * 60.0
        por_bus[b] = {**st, "e_faltante_kwh": round(ef, 1),
                      "t_noct_min": round(t, 1)}
        d = por_term.setdefault(st["terminal"] or "Sin terminal",
                                {"t_noct": 0.0, "kwh": 0.0, "n_buses": 0})
        d["t_noct"] += t
        d["kwh"] += ef
        d["n_buses"] += 1
    for d in por_term.values():
        d["t_noct"] = round(d["t_noct"], 1)
        d["kwh"] = round(d["kwh"], 1)
    return por_bus, por_term


def _resolver_deficit(por_bus, por_terminal, it2=None):
    """ITERACIÓN 2 (parte 2): recorta demanda nocturna que no cabe.

    Para cada terminal: capacidad_min = n_cargadores × ventana. Si Σ t_noct
    excede la capacidad, se sacan de la noche (adelanto al día) los buses con
    mayor t_noct hasta cubrir el sobrante; su SOC_fin pasa a CAP y su energía
    se registra en `ajustes_diurnos` (kWh que se cargan de día en la última
    pasada por terminal).

    Devuelve (ajustes_diurnos, sobrante_h) con sobrante_h por terminal.
    """
    p = {**ITER2, **(it2 or {})}
    win_ini, win_fin_abs, win = _ventana(p)
    ajustes, sobrante_h = {}, {}
    for term, d in por_terminal.items():
        cap_min = _n_cargadores(p["n_cargadores_por_terminal"], term) * win
        if cap_min <= 0:
            # sin cargadores: toda la demanda del terminal se adelanta al día
            for b, st in por_bus.items():
                if (st["terminal"] or "Sin terminal") == term and st["t_noct_min"] > 0:
                    ajustes[b] = st["e_faltante_kwh"]
                    st["soc_fin"] = p["soc_objetivo_fin_dia"]
                    st["t_noct_min"] = 0.0
            sobrante_h[term] = round(d["t_noct"] / 60.0, 1)
            continue
        if d["t_noct"] <= cap_min:
            continue
        buses = sorted([(b, st) for b, st in por_bus.items()
                        if (st["terminal"] or "Sin terminal") == term],
                       key=lambda x: -x[1]["t_noct_min"])
        sobrante = d["t_noct"] - cap_min
        for b, st in buses:
            if sobrante <= 0:
                break
            if st["t_noct_min"] > 0:
                ajustes[b] = st["e_faltante_kwh"]
                sobrante -= st["t_noct_min"]
                st["soc_fin"] = p["soc_objetivo_fin_dia"]
                st["t_noct_min"] = 0.0
        sobrante_h[term] = round(max(0.0, sobrante) / 60.0, 1)
    return ajustes, sobrante_h


def secuenciar_nocturno(por_bus, it2=None):
    """ITERACIÓN 2 (parte 3): asigna cargador y horario a cada bus nocturno.

    Cola FIFO por terminal (prioridad por llegada al terminal; la prioridad
    "por hora de salida del día siguiente" aplica cuando el parquet lo tenga):
    cada bus entra al cargador que quede libre antes, con
    inicio = max(hora_libre_cargador, ultimo_fin, inicio_ventana).
    Devuelve lista de dicts {bus, terminal, cargador, inicio_min, fin_min,
    kwh, t_min}.
    """
    p = {**ITER2, **(it2 or {})}
    win_ini, win_fin, win = _ventana(p)
    por_term = {}
    for b, st in por_bus.items():
        if st["t_noct_min"] <= 0:
            continue
        por_term.setdefault(st["terminal"] or "Sin terminal", []).append((st["ultimo_fin"], b, st))
    programa = []
    for term, fila in por_term.items():
        nc = _n_cargadores(p["n_cargadores_por_terminal"], term)
        if nc <= 0 or win <= 0:
            continue
        libres = [win_ini] * nc
        for ultimo_fin, b, st in sorted(fila, key=lambda x: x[0]):
            t = st["t_noct_min"]
            if t <= 0:
                continue
            c = min(range(nc), key=lambda i: libres[i])
            inicio = max(libres[c], ultimo_fin, win_ini)
            if inicio + t > win_fin:
                continue  # no cabe (no debería pasar tras _resolver_deficit)
            fin = inicio + t
            libres[c] = fin
            programa.append({"bus": b, "terminal": term, "cargador": c + 1,
                             "inicio_min": round(inicio, 1), "fin_min": round(fin, 1),
                             "t_min": round(t, 1),
                             "kwh": round(t / 60.0 * p["potencia_nocturna_kw"], 1)})
    return programa


def iteracion2(eventos, it2=None):
    """ITERACIÓN 2 completa: SOC iter 1 → demanda nocturna → déficit → programa.

    Devuelve dict:
      eventos_soc: eventos con soc_inicio/soc_fin (iter 1).
      fin_por_bus: SOC_fin por bus antes de adelantos.
      demanda: por terminal {t_noct, kwh, n_buses, capacidad_min, deficit_h}.
      programa_nocturno: lista (secuenciada por cargador).
      ajustes_diurnos: {bus: kwh adelantados al día}.
      kpis: {energia_dia_kwh, energia_noche_kwh, pct_noche_pct,
             deficit_original_h, deficit_tras_h, factible, n_adelantados}.
    """
    p = {**ITER2, **(it2 or {})}
    eventos_soc, fin = simular_soc_iter1(eventos, it2)
    por_bus, por_term = demanda_nocturna(fin, it2)
    ajustes, sobrante_h = _resolver_deficit(por_bus, por_term, it2)

    win_ini, win_fin_abs, win = _ventana(p)
    demanda = {}
    for term, d in por_term.items():
        cap_min = _n_cargadores(p["n_cargadores_por_terminal"], term) * win
        demanda[term] = {**d, "capacidad_min": round(cap_min, 1),
                         "deficit_h": round(max(0.0, d["t_noct"] - cap_min) / 60.0, 1)}

    programa = secuenciar_nocturno(por_bus, it2)

    energia_dia = sum(st["kwh_diurno"] for st in fin.values()) + sum(ajustes.values())
    energia_noche = sum(pr["kwh"] for pr in programa)
    total = energia_dia + energia_noche
    kpis = {
        "energia_dia_kwh": round(energia_dia, 1),
        "energia_noche_kwh": round(energia_noche, 1),
        "pct_noche_pct": round(energia_noche / total * 100, 1) if total else 0.0,
        "deficit_original_h": round(sum(por_term[t]["t_noct"] / 60.0 for t in por_term
                                        if por_term[t]["t_noct"]
                                        > _n_cargadores(p["n_cargadores_por_terminal"], t) * win), 1),
        "deficit_tras_h": round(sum(sobrante_h.values()), 1),
        "factible": not any(v > 0 for v in sobrante_h.values()),
        "n_adelantados": len(ajustes),
        "kwh_adelantados": round(sum(ajustes.values()), 1),
    }
    return {"eventos_soc": eventos_soc, "fin_por_bus": fin, "demanda": demanda,
            "programa_nocturno": programa, "ajustes_diurnos": ajustes, "kpis": kpis}


def iteracion3(dia1_eventos, dia2_eventos, it2=None, it3=None):
    """ITERACIÓN 3: programa nocturno garantizado para la jornada siguiente.

    Horizonte 40 h: día 1 → noche → primera salida del día 2 (slots madrugada
    00:00–05:00 del día 2). Sobre la iteración 2 agrega:
      1. Recarga SIN piso de ventana: cada bus enchufa apenas llega al terminal
         si hay cargador libre (orden de cola = SOC fin de día ascendente, es
         decir, el más descargado carga primero); la ventana solo impone el
         fin máximo (desconexión a cierre de ventana).
      2. Asignación por inventario: los slots de salida del día 2 consumen un
         bus disponible (SOC ≥ requerido, listo a la hora); el bus vuelve tras
         su vuelta con SOC reducido y reingresa al pool.
      3. Buses que no operan el día 1 pero sí el 2 → disponibles al 100% desde
         la noche. Diésel (C2D) y buses que operan tras las 24:00 tratados
         aparte.

    Parámetros it3 (opcionales):
      madrugada_fin_min: fin de la franja de slots de primera salida (default 300).
      margen_min: exigencia adicional entre fin de recarga y salida (default 0).

    Devuelve dict con: programa_nocturno, slots, asignacion, sin_cubrir,
    inventario_inicial, kpis_asignacion, fin_por_bus.
    """
    p = {**ITER2, **(it2 or {})}
    q = {"madrugada_fin_min": 300.0, "margen_min": 0.0, **(it3 or {})}
    cap, piso, rend = (p["soc_objetivo_fin_dia"], p["soc_piso_kwh"],
                       p["rendimiento_kwh_km"])
    pot = p["potencia_nocturna_kw"]
    win_ini, win_fin_abs, win = _ventana(p)

    base = iteracion2(dia1_eventos, it2)
    fin1 = base["fin_por_bus"]

    df2 = pd.DataFrame(list(dia2_eventos))
    c2 = df2[(df2.evento == "C01") & (df2.inicio > 0)]
    v2 = df2[(df2.evento == "V01") & (df2.inicio > 0)]

    # ---- disponibilidad al cierre del día 1, por terminal ----
    # cola de recarga SIN piso de ventana: el bus enchufa al llegar si hay cargador
    buses_op1 = [b for b, st in fin1.items() if not st.get("sin_soc")]
    orden = sorted(buses_op1, key=lambda b: fin1[b]["soc_fin"])  # SOC bajo primero
    # hora de llegada del bus al terminal (último evento que termina antes del cierre)
    df1 = pd.DataFrame(list(dia1_eventos))
    llegada = df1.sort_values("fin").groupby("bus").fin.max().to_dict()

    libres = {}          # terminal -> [min_libre_por_cargador]
    evento_recarga = []  # filas del programa
    for b in orden:
        st = fin1[b]
        term = st.get("terminal") or "Sin terminal"
        ef = max(0.0, cap - st["soc_fin"])
        if ef <= 0:
            continue
        t = ef / pot * 60.0
        nc = _n_cargadores(p["n_cargadores_por_terminal"], term)
        if nc <= 0:
            continue
        arr = libres.setdefault(term, [win_ini] * nc)
        c = min(range(nc), key=lambda i: arr[i])
        inicio = max(arr[c], min(llegada.get(b, win_ini), win_fin_abs - t))
        fin_r = inicio + t
        if fin_r > win_fin_abs + 1e-9:
            continue  # no cabe en la ventana: queda para recarga diurna
        arr[c] = fin_r
        evento_recarga.append({"bus": b, "terminal": term, "cargador": c + 1,
                               "inicio_min": round(inicio, 1), "fin_min": round(fin_r, 1),
                               "t_min": round(t, 1), "kwh": round(t / 60.0 * pot, 1)})
    programa = pd.DataFrame(evento_recarga)

    # ---- inventario disponible al 00:00 del día 2 ----
    op2 = set(c2.bus)
    term_d2 = c2.sort_values("inicio").groupby("bus").terminal.first().to_dict()
    inventario = []  # [soc, listo_min, bus, terminal]
    for b in op2 - set(fin1):
        inventario.append([cap, 0.0, b, term_d2.get(b, "Sin terminal")])  # no operó d1
    for b in buses_op1:
        st = fin1[b]
        term = st.get("terminal") or "Sin terminal"
        pr = programa[programa.bus == b] if len(programa) else programa
        if st["ultimo_fin"] >= 1440:      # opera tras medianoche: no carga esa noche
            soc, listo = st["soc_fin"], st["ultimo_fin"] - 1440
        elif len(pr):
            soc, listo = st["soc_fin"] + pr.kwh.sum(), float(pr.fin_min.max()) % 1440
        else:
            soc, listo = st["soc_fin"], 0.0
        inventario.append([soc, listo, b, term])

    # ---- slots de primera salida del día 2 (madrugada), tipo del día 2 ----
    dur = c2.groupby("bus").apply(
        lambda g: (g.sort_values("inicio").iloc[0]["fin"]
                   - g.sort_values("inicio").iloc[0]["inicio"]), include_groups=False).to_dict()
    slots = []
    for b, g in c2.sort_values("inicio").groupby("bus"):
        f = g.iloc[0]
        if f.tipo_bus == "C2D" or f.inicio >= q["madrugada_fin_min"]:
            continue
        kv = v2[(v2.bus == b) & (v2.inicio <= f.inicio)]
        kmv = kv.km.sum() if len(kv) else 0.0
        slots.append({"bus_slot": b, "terminal": f.terminal if isinstance(f.terminal, str) else "Sin terminal",
                      "salida_min": float(f.inicio), "vuelta_min": float(f.fin),
                      "servicio": str(f.servicio), "sentido": str(f.sentido),
                      "km_primera": float(f.km + kmv),
                      "soc_req": min(cap, piso + (f.km + kmv) * rend)})

    # ---- asignación por inventario ----
    asignacion, sin_cubrir = [], []
    df_slots = pd.DataFrame(slots)
    if slots:
        grupos = df_slots.groupby("terminal")
    else:
        # no hay primeras salidas en la franja de madrugada: nada que asignar
        grupos = []
    for term, grupo in grupos:
        pool = [r[:] for r in inventario if r[3] == term]
        devoluciones = []  # (t_vuelta, soc, bus)
        for s in sorted(grupo.to_dict("records"), key=lambda x: x["salida_min"]):
            # liberar los buses que ya volvieron al terminal antes de esta salida
            pendientes = []
            for (tv, socv, bv) in devoluciones:
                if tv <= s["salida_min"]:
                    if socv >= piso:
                        pool.append([socv, tv, bv])
                else:
                    pendientes.append((tv, socv, bv))
            devoluciones = pendientes
            margen = q["margen_min"]
            cand = [r for r in pool if r[0] >= s["soc_req"] - 0.05
                    and r[1] <= s["salida_min"] - margen]
            if cand:
                r = min(cand, key=lambda x: (x[0], x[1]))  # menor SOC suficiente
                pool.remove(r)
                asignacion.append({**s, "bus_fisico": r[2], "soc_prog": round(r[0], 1),
                                   "listo_min": r[1], "ok": True})
                devoluciones.append((s["vuelta_min"], r[0] - s["km_primera"] * rend, r[2]))
            else:
                sin_cubrir.append({**s, "bus_fisico": None, "ok": False})

    kpis = {
        "slots_total": len(asignacion) + len(sin_cubrir),
        "slots_cubiertos": len(asignacion),
        "pct_cobertura": round(len(asignacion) / max(1, len(asignacion) + len(sin_cubrir)) * 100, 1),
        "recargas_nocturnas": len(evento_recarga),
        "kwh_noche": round(sum(r["kwh"] for r in evento_recarga), 1),
        "buses_no_operados_d1": len(op2 - set(fin1)),
    }
    return {"programa_nocturno": evento_recarga, "slots": slots,
            "asignacion": asignacion, "sin_cubrir": sin_cubrir,
            "inventario_inicial": inventario, "kpis_asignacion": kpis,
            "fin_por_bus": fin1, "base_iter2": base}


def main():
    """Genera sim_un16.parquet para los 3 días (uso CLI)."""
    resumen = {}
    todas_eventos = []
    for td in ["Laboral", "Sábado", "Domingo"]:
        evs, kpi = simular(td)
        todas_eventos.extend(evs)
        km_com = sum(e["km"] for e in evs if e["evento"] == "C01")
        km_vac = sum(e["km"] for e in evs if e["evento"] in ("V01", "V02"))
        resumen[td] = {"n_buses": kpi["n_buses"], "n_exp": kpi["n_exp"],
                       "km_comercial": round(km_com, 1), "km_vacio": round(km_vac, 1),
                       "n_recargas": kpi.get("n_recargas", 0),
                       "t_recarga_min": kpi.get("t_recarga_min", 0),
                       "kwh_recarga": kpi.get("kwh_recarga", 0)}
        print(f"\n=== {td} ===")
        print(f"  Buses necesarios: {kpi['n_buses']} | Expediciones: {kpi['n_exp']}")
        print(f"  KM comercial: {km_com:,.1f} | KM vacío: {km_vac:,.1f} ({km_vac/(km_com+km_vac)*100:.1f}% del total)")
        if kpi.get("n_recargas"):
            print(f"  Recargas: {kpi['n_recargas']} | Tiempo total: {kpi['t_recarga_min']:,.0f} min "
                  f"({kpi['t_recarga_min']/kpi['n_recargas']:.0f} min c/u) | Energía: {kpi['kwh_recarga']:,.0f} kWh")
        por_bus = kpi_por_bus(evs)
        vueltas = [v[0] for v in por_bus.values()]
        kms = [v[1] for v in por_bus.values()]
        if vueltas:
            print(f"  Vueltas/bus: min {min(vueltas)}, max {max(vueltas)}, media {sum(vueltas)/len(vueltas):.1f}")
            print(f"  KM/bus: min {min(kms):.1f}, max {max(kms):.1f}, media {sum(kms)/len(kms):.1f}")

    df_ev = pd.DataFrame(todas_eventos)
    df_ev.to_parquet(OUT / "sim_un16.parquet")
    df_ev.to_csv(OUT / "sim_un16.csv", index=False)
    with open(OUT / "resumen_sim.json", "w") as f:
        json.dump(resumen, f, indent=2)
    print(f"\nGuardado: {OUT/'sim_un16.parquet'} ({len(df_ev):,} eventos)")
    print("\nResumen:", json.dumps(resumen, indent=2))


if __name__ == "__main__":
    main()
