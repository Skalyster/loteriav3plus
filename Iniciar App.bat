@echo off
cd /d "%~dp0"
echo Iniciando Analizador de Loterias...
echo NO CIERRES ESTA VENTANA. Se abrira el navegador automaticamente.
mi_entorno\Scripts\python.exe -m streamlit run app.py
pause