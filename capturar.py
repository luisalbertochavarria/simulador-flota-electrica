"""Captura de pantalla del simulador con Playwright (para el README)."""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8556/"
DEST = Path(__file__).parent / "capturas"
DEST.mkdir(exist_ok=True)

PESTANAS = [
    ("02_por_linea", "Por línea"),
    ("03_gantt", "Gantt"),
    ("04_tablas", "Tablas"),
    ("05_jornada_siguiente", "Jornada siguiente"),
]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1680, "height": 1050})
    print("navegando...")
    page.goto(URL, wait_until="domcontentloaded", timeout=90000)

    # esperar a que termine el calculo: aparece la metrica "Km comercial"
    print("esperando render completo...")
    page.wait_for_selector("text=Km comercial", timeout=420000)
    print("render completo")
    time.sleep(12)
    page.screenshot(path=str(DEST / "01_simulacion.png"))
    print("guardada 01_simulacion.png")

    for archivo, etiqueta in PESTANAS:
        try:
            page.get_by_role("tab", name=etiqueta).click(timeout=20000)
        except Exception:
            try:
                page.click(f"button:has-text('{etiqueta}')", timeout=20000)
            except Exception as e:
                print(f"  no se pudo abrir '{etiqueta}': {type(e).__name__}")
                continue
        time.sleep(14)
        page.screenshot(path=str(DEST / f"{archivo}.png"))
        print(f"guardada {archivo}.png")

    browser.close()
print("listo")