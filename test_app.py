"""Smoke test: ejecuta la app completa y verifica que no haya excepciones."""
from streamlit.testing.v1 import AppTest

at = AppTest.from_file("app.py", default_timeout=900)
at.run()
if at.exception:
    for e in at.exception:
        print("EXCEPCION:", e.value)
        st = getattr(e, "stack_trace", None)
        if st:
            print(st[-2500:])
else:
    print("OK - sin excepciones")
    print("tabs:", len(at.tabs))
    print("metricas:", len(at.metric))
    print("selectbox:", [s.label for s in at.selectbox][:10])
