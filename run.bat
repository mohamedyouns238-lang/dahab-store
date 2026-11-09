@echo off
chcp 65001 >nul
echo تثبيت المتطلبات...
python -m pip install -r requirements.txt
echo.
echo افتح المتصفح على:  http://localhost:5000
python app.py
pause
