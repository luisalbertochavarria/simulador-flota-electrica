#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Generador de datos SINTETICOS para el simulador operacional de flota electrica.

Produce un dataset con EXACTAMENTE la misma estructura (columnas, tipos y relaciones)
que consume el simulador, pero cuyos terminales, servicios, codigos de parada,
distancias y programacion son FICTICIOS y generados aleatoriamente.

Objetivo: permitir que cualquiera clone este repositorio y ejecute el simulador
sin que se exponga ningun dato operacional ni contractual real.

Uso:
    python generar_datos_sinteticos.py

Salida (carpeta data_sintetica/):
    modelo_un16.parquet        segmentos por expedicion (tabla principal)
    vacios_un16.parquet        kilometros y tiempos de vacio V01/V02 por servicio
    resumen_sim.json           KPIs agregados por tipo de dia
    terminales.json            coordenadas ficticias de cada terminal
    config_terminales.json     mapa terminal -> servicios (lo usa la app)
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Reproducibilidad
# ----------------------------------------------------------------------------
SEED = 20260101
rng = np.random.default_rng(SEED)

SALIDA = Path(__file__).parent / "data_sintetica"

# ----------------------------------------------------------------------------
# Escenario ficticio: 5 terminales, 30 servicios, 3 tipos de dia
# ----------------------------------------------------------------------------
TERMINALES = {
    "Terminal Norte": (-70.6650, -33.4020),
    "Terminal Poniente": (-70.7410, -33.5120),
    "Terminal Sur": (-70.6520, -33.5900),
    "Terminal Oriente": (-70.5510, -33.4490),
    "Terminal Central": (-70.6480, -33.4610),
}

# servicios ficticios por terminal (codigos S01..S30, sin relacion con rutas reales)
SERVICIOS_POR_TERMINAL = {
    "Terminal Norte": ["S01", "S02", "S03", "S04", "S05", "S06"],
    "Terminal Poniente": ["S07", "S08", "S09", "S10", "S11", "S12"],
    "Terminal Sur": ["S13", "S14", "S15", "S16", "S17", "S18"],
    "Terminal Oriente": ["S19", "S20", "S21", "S22", "S23", "S24"],
    "Terminal Central": ["S25", "S26", "S27", "S28", "S29", "S30"],
}

# tipologias de bus usadas por el motor de autonomia
TIPOS_BUS = ["A2V", "B2V", "C2D"]

# tipo de dia -> (n expediciones base, ventana horaria de operacion en minutos)
PERFIL_DIA = {
    "Laboral": {"base_exp": 90, "h_ini": 5 * 60, "h_fin": 23 * 60 + 30, "peso": 1.00},
    "Sábado": {"base_exp": 68, "h_ini": 5 * 60 + 30, "h_fin": 23 * 60, "peso": 0.72},
    "Domingo": {"base_exp": 58, "h_ini": 6 * 60, "h_fin": 22 * 60 + 30, "peso": 0.55},
}

# prefijos de codigo de parada por terminal (ficticios)
PREFIJO_PARADA = {
    "Terminal Norte": "PN",
    "Terminal Poniente": "PP",
    "Terminal Sur": "PS",
    "Terminal Oriente": "PO",
    "Terminal Central": "PC",
}

SERVICIO_TERMINAL = {s: t for t, ss in SERVICIOS_POR_TERMINAL.items() for s in ss}


def _generar_expedicion(serv, sentido, tipo_dia, exp_ini, terminal, indice):
    """Genera los segmentos de una expedicion (una vuelta del servicio).

    Devuelve la lista de dicts (una fila por segmento) y los agregados de la vuelta.
    """
    perfil = PERFIL_DIA[tipo_dia]
    prefijo = PREFIJO_PARADA[terminal]

    # tipologia del bus asignado (los servicios de terminales con flota electrica
    # usan A2V/B2V; algunos servicios operan con diesel articulado C2D)
    if indice % 11 == 0:
        tipo_bus = "C2D"
    elif indice % 2 == 0:
        tipo_bus = "B2V"
    else:
        tipo_bus = "A2V"

    # largo de la vuelta: 12 a 30 km segun servicio y sentido
    km_total = float(rng.uniform(12.0, 30.0))
    # velocidad media comercial: 16 a 30 km/h
    v_media = float(rng.uniform(16.0, 30.0))
    dur_total = km_total / v_media * 60.0  # minutos

    # cantidad de segmentos (paradas) de la vuelta
    n_seg = int(rng.integers(18, 38))

    # reparto del kilometraje entre segmentos (fracciones aleatorias normalizadas)
    frac = rng.uniform(0.5, 1.5, size=n_seg)
    frac = frac / frac.sum()
    km_seg = km_total * frac

    # velocidad por segmento (varia alrededor de la media)
    v_seg = np.clip(rng.normal(v_media, 4.0, size=n_seg), 8.0, 55.0)
    t_seg = km_seg / v_seg * 60.0
    # reescalar para que la suma calce con la duracion total de la vuelta
    t_seg = t_seg / t_seg.sum() * dur_total
    t_acum = np.cumsum(t_seg)

    sentido_cod = "I" if sentido == "Ida" else "R"
    paradas = [f"{prefijo}{sentido_cod}{n:02d}" for n in range(n_seg + 1)]

    filas = []
    for i in range(n_seg):
        filas.append({
            "servicio": serv,
            "sentido": sentido,
            "tipo_dia": tipo_dia,
            "bus": int(indice),
            "corr": int(indice) + 1,
            "exp_ini": int(exp_ini),
            "tipo_bus": tipo_bus,
            "km_total": round(km_total, 2),
            "dur_total": round(dur_total, 2),
            "seg_i": i,
            "cod_ini": paradas[i],
            "cod_fin": paradas[i + 1],
            "km_seg": round(float(km_seg[i]), 6),
            "v_kmh": round(float(v_seg[i]), 2),
            "t_seg_min": round(float(t_seg[i]), 3),
            "t_acum_min": round(float(t_acum[i]), 3),
        })
    return filas, km_total, dur_total, tipo_bus


def generar_modelo() -> pd.DataFrame:
    """Genera la tabla de segmentos (equivalente a modelo_un16.parquet)."""
    todas = []
    for tipo_dia, perfil in PERFIL_DIA.items():
        for serv, terminal in SERVICIO_TERMINAL.items():
            for sentido in ("Ida", "Ret"):
                n_exp = int(perfil["base_exp"] * rng.uniform(0.85, 1.15))
                h_ini, h_fin = perfil["h_ini"], perfil["h_fin"]
                # Una fraccion de las salidas ocurre de madrugada (00:00-05:00): son las
                # "primeras salidas" del dia siguiente que analiza la iteracion 3.
                n_mad = max(3, int(n_exp * rng.uniform(0.04, 0.10)))
                n_prin = n_exp - n_mad
                mad = rng.choice(np.arange(0, 300), size=n_mad, replace=False)
                prin = rng.choice(np.arange(h_ini, h_fin + 1), size=n_prin, replace=False)
                # minutos de partida DISTINTOS (la clave de expedicion es la hora de inicio)
                inicios = np.sort(np.concatenate([mad, prin]))
                for idx, exp_ini in enumerate(inicios):
                    filas, _, _, _ = _generar_expedicion(
                        serv, sentido, tipo_dia, exp_ini, terminal, idx
                    )
                    todas.extend(filas)
    df = pd.DataFrame(todas)
    return df


def generar_vacios() -> pd.DataFrame:
    """Genera la tabla de vacios V01/V02 por servicio (equivalente a vacios_un16.parquet)."""
    filas = []
    for tipo_dia, perfil in PERFIL_DIA.items():
        for serv, terminal in SERVICIO_TERMINAL.items():
            for sentido in ("Ida", "Ret"):
                idx = int(rng.integers(0, 11))
                tipo_bus = "C2D" if idx % 11 == 0 else ("B2V" if idx % 2 == 0 else "A2V")
                n_exp = int(perfil["base_exp"] * rng.uniform(0.85, 1.15))
                # distancia de vacio terminal -> cabezal (V01) y cabezal -> terminal (V02)
                v01_km = float(rng.uniform(4.0, 20.0))
                v02_km = float(rng.uniform(4.0, 20.0))
                v_media = float(rng.uniform(20.0, 34.0))
                v01_min = v01_km / v_media * 60.0
                v02_min = v02_km / v_media * 60.0
                filas.append({
                    "tipo_dia": tipo_dia,
                    "servicio": serv,
                    "sentido": sentido,
                    "terminal": terminal,
                    "tipo_bus": tipo_bus,
                    "n_expediciones": n_exp,
                    "v01_km": round(v01_km, 2),
                    "v01_min": round(v01_min, 1),
                    "v01_h": int(rng.integers(0, 3)),
                    "v02_km": round(v02_km, 2),
                    "v02_min": round(v02_min, 1),
                    "v02_h": int(rng.integers(0, 3)),
                })
    return pd.DataFrame(filas)


def generar_resumen(modelo: pd.DataFrame, vacios: pd.DataFrame) -> dict:
    """KPIs agregados por tipo de dia (equivalente a resumen_sim.json)."""
    resumen = {}
    for tipo_dia in PERFIL_DIA:
        m = modelo[modelo["tipo_dia"] == tipo_dia]
        v = vacios[vacios["tipo_dia"] == tipo_dia]
        exp = m.drop_duplicates(subset=["servicio", "sentido", "exp_ini"])
        km_com = float(exp["km_total"].sum())
        # los vacios son una fraccion acotada del km comercial (apertura y cierre de vueltas)
        km_vac = km_com * float(rng.uniform(0.07, 0.12))
        n_buses = int(round(km_com / rng.uniform(180.0, 240.0)))
        n_rec = int(rng.integers(15, 60))
        resumen[tipo_dia] = {
            "n_buses": n_buses,
            "n_exp": int(len(exp)),
            "km_comercial": round(km_com, 1),
            "km_vacio": round(km_vac, 1),
            "n_recargas": n_rec,
            "t_recarga_min": round(n_rec * float(rng.uniform(25.0, 45.0)), 1),
            "kwh_recarga": round(n_rec * float(rng.uniform(180.0, 320.0)), 1),
        }
    return resumen


def main() -> None:
    SALIDA.mkdir(parents=True, exist_ok=True)
    print("Generando modelo de segmentos...")
    modelo = generar_modelo()
    print(f"  {len(modelo):,} segmentos, "
          f"{modelo.drop_duplicates(subset=['servicio','sentido','tipo_dia','exp_ini']).shape[0]:,} expediciones")

    print("Generando vacios...")
    vacios = generar_vacios()
    print(f"  {len(vacios)} filas")

    print("Generando resumen...")
    resumen = generar_resumen(modelo, vacios)

    modelo.to_parquet(SALIDA / "modelo_un16.parquet", index=False)
    vacios.to_parquet(SALIDA / "vacios_un16.parquet", index=False)

    with open(SALIDA / "resumen_sim.json", "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=2)

    with open(SALIDA / "terminales.json", "w", encoding="utf-8") as f:
        json.dump(TERMINALES, f, ensure_ascii=False, indent=2)

    # el mapa terminal -> servicios es un dato de configuracion del escenario
    config = {"terminales": SERVICIOS_POR_TERMINAL,
              "tipos_bus": TIPOS_BUS,
              "nota": "Datos sinteticos generados para demostracion. Sin informacion real."}
    with open(SALIDA / "config_terminales.json", "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(f"\nListo. Archivos escritos en: {SALIDA}")


if __name__ == "__main__":
    main()
