import streamlit as st
import sqlite3
import pandas as pd
from collections import Counter
import random
import altair as alt
import os
import base64
import requests
import time
import re
import threading 
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from bs4 import BeautifulSoup

# ==========================================
# Configuración de la página
# ==========================================
st.set_page_config(page_title="Analizador de Loterías", page_icon="🎲", layout="wide", initial_sidebar_state="collapsed")

@st.cache_resource
def get_connection():
    return sqlite3.connect('loteria_db.sqlite', check_same_thread=False)
conn = get_connection()

def init_db():
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sorteos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            juego TEXT NOT NULL,
            fecha TEXT NOT NULL,
            numeros TEXT NOT NULL,
            reintegro INTEGER DEFAULT 0,
            UNIQUE(juego, fecha)
        )
    """)
    conn.commit()
init_db()

# ==========================================
# SISTEMA DE REGISTRO (LOG) SEGURO PARA HILOS
# ==========================================
class LogHandler:
    def __init__(self):
        self.entries = []
        self.lock = threading.Lock()
    
    def add(self, nivel, mensaje):
        marca = datetime.now().strftime("%H:%M:%S")
        entrada = f"[{marca}] [{nivel}] {mensaje}"
        with self.lock:
            self.entries.append(entrada)
            if len(self.entries) > 60:
                self.entries = self.entries[-60:]
    
    def info(self, m): self.add("INFO", m)
    def ok(self, m): self.add("OK ✅", m)
    def warn(self, m): self.add("WARN ⚠️", m)
    def error(self, m): self.add("ERROR ❌", m)
    
    def obtener(self):
        with self.lock:
            return "\n".join(self.entries)

@st.cache_resource
def get_log_handler():
    return LogHandler()
log = get_log_handler()

# ==========================================
# FUNCIÓN DE INSERCIÓN MANUAL
# ==========================================
def insertar_sorteo_manual(juego, fecha, numeros_str, reintegro):
    try:
        if juego == "Loteria Nacional":
            num_str = ''.join(filter(str.isdigit, str(numeros_str)))[:5].zfill(5)
            if len(num_str) < 5: return False
            nums_sql = num_str
        elif juego == "Euromillones":
            # 5 números + 2 estrellas (7 valores separados por coma)
            nums = [int(n) for n in numeros_str.replace(' ', '').split(',')]
            if len(nums) != 7 or not all(1 <= n <= 50 for n in nums[:5]) or not all(1 <= n <= 12 for n in nums[5:]):
                st.error("Euromillones: 5 números (1-50) y 2 estrellas (1-12), 7 valores en total.")
                return False
            nums_sql = ",".join(map(str, nums))
            reintegro = 0
        else:
            nums = [int(n) for n in numeros_str.replace(' ', '').split(',')]
            if len(nums) < 6: return False
            nums_sql = ",".join(map(str, nums))
            
        fecha_sql = pd.to_datetime(fecha).strftime('%Y-%m-%d')
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM sorteos WHERE juego=? AND fecha=?", (juego, fecha_sql))
        if cursor.fetchone():
            st.warning("Este sorteo ya existe.")
            return False
        cursor.execute('INSERT INTO sorteos (juego, fecha, numeros, reintegro) VALUES (?, ?, ?, ?)', (juego, fecha_sql, nums_sql, int(reintegro)))
        conn.commit()
        st.success("¡Insertado correctamente!")
        return True
    except Exception as e:
        st.error(f"Error: {e}")
        return False

# ==========================================
# SCRAPERS (Versión mejorada con fallback múltiple)
# ==========================================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
}
DELAY = 1.5

@dataclass
class Resultado:
    juego: str
    fecha: str = ""
    numeros: list = field(default_factory=list)
    complementario: Optional[int] = None
    reintegro: Optional[int] = None
    clave: Optional[int] = None
    primer_premio: Optional[str] = None
    estrellas: list = field(default_factory=list)
    fuente: str = ""

# Configuración visual y de reglas de cada juego con bolas
# (Euromillones guarda en 'numeros' 7 valores: 5 números + 2 estrellas)
CONFIG_JUEGOS = {
    "Bonoloto":     dict(max=49, extra=(0, 9), color_p="#1f77b4", color_e="#d62728", txt_e="R: ", scheme="blues",   cmap="Blues",   estrellas=False),
    "Eurodreams":   dict(max=40, extra=(1, 5), color_p="#2ca02c", color_e="#ff7f0e", txt_e="E: ", scheme="greens",  cmap="Greens",  estrellas=False),
    "La Primitiva": dict(max=49, extra=(0, 9), color_p="#9467bd", color_e="#d62728", txt_e="R: ", scheme="purples", cmap="Purples", estrellas=False),
    "Euromillones": dict(max=50, extra=(1, 12), color_p="#1a4fa3", color_e="#f2b705", txt_e="★",  scheme="blues",   cmap="Blues",   estrellas=True),
}

# Utilidades compartidas por los scrapers
MESES = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
         "julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12}

def extraer_fecha_texto(texto):
    m = re.search(r"(\d{1,2})/(\d{2})/(\d{4})", texto)
    if m: return f"{m.group(3)}-{m.group(2)}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", texto, re.IGNORECASE)
    if m and m.group(2).lower() in MESES:
        return f"{m.group(3)}-{MESES[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return ""

def parse_primitiva_generico(soup, texto, fuente):
    """Primitiva: 6 números + complementario + reintegro (mismos formatos que Bonoloto)."""
    r = Resultado(juego="la-primitiva", fuente=fuente)
    m = re.search(
        r"(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*\|\s*C:(\d{1,2})\s*\|\s*R:(\d{1,2})",
        texto)
    if m:
        r.numeros = [int(m.group(i)) for i in range(1, 7)]
        r.complementario = int(m.group(7)); r.reintegro = int(m.group(8))
        r.fecha = extraer_fecha_texto(texto)
        return r
    for sec in soup.find_all(string=re.compile("Combinación", re.IGNORECASE)):
        cont = sec.parent.parent if sec.parent else None
        if not cont: continue
        nums = [int(li.get_text(strip=True)) for li in cont.find_all("li")
                if re.fullmatch(r"\d{1,2}", li.get_text(strip=True))]
        if len(nums) >= 6:
            r.numeros = nums[:6]
            texto_sec = cont.get_text(separator=" ", strip=True)
            m_c = re.search(r"Comp\.?\s*(\d{1,2})", texto_sec)
            if m_c: r.complementario = int(m_c.group(1))
            m_r = re.search(r"Reint\.?\s*(\d{1,2})", texto_sec)
            if m_r: r.reintegro = int(m_r.group(1))
            r.fecha = extraer_fecha_texto(texto)
            return r
    m = re.search(
        r"números\s+(\d{1,2}),\s*(\d{1,2}),\s*(\d{1,2}),\s*(\d{1,2}),\s*(\d{1,2})\s*y\s*(\d{1,2})", texto)
    if m:
        r.numeros = [int(m.group(i)) for i in range(1, 7)]
        r.fecha = extraer_fecha_texto(texto)
        return r
    return None

def parse_euromillones_generico(soup, texto, fuente):
    """Euromillones: 5 números + 2 estrellas."""
    r = Resultado(juego="euromillones", fuente=fuente)
    # Patrón 1: "N - N - N - N - N | E:N - N" (o "Estrellas N - N")
    m = re.search(
        r"(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*\|?\s*(?:E|Estrellas?|\*|★)\s*:?\s*(\d{1,2})\s*[-y,]\s*(\d{1,2})",
        texto, re.IGNORECASE)
    if m:
        r.numeros = [int(m.group(i)) for i in range(1, 6)]
        r.estrellas = [int(m.group(6)), int(m.group(7))]
        r.fecha = extraer_fecha_texto(texto)
        return r
    # Patrón 2: "N N N N N" + "Estrellas N N" en texto plano / combinacionganadora
    m = re.search(
        r"(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(?:E|Estrellas?|\*|★)\s*:?\s*(\d{1,2})\s+(?:E|\*|★)?\s*:?\s*(\d{1,2})",
        texto, re.IGNORECASE)
    if m:
        r.numeros = [int(m.group(i)) for i in range(1, 6)]
        r.estrellas = [int(m.group(6)), int(m.group(7))]
        r.fecha = extraer_fecha_texto(texto)
        return r
    # Patrón 3: artículo "números 5, 12, 23, 34 y 45 ... estrellas 3 y 8"
    m = re.search(
        r"números\s+(\d{1,2}),\s*(\d{1,2}),\s*(\d{1,2}),\s*(\d{1,2})\s*y\s*(\d{1,2}).{0,80}?estrellas?\s+(?:número\s+)?(\d{1,2})\s*y\s*(\d{1,2})",
        texto, re.IGNORECASE | re.DOTALL)
    if m:
        r.numeros = [int(m.group(i)) for i in range(1, 6)]
        r.estrellas = [int(m.group(6)), int(m.group(7))]
        r.fecha = extraer_fecha_texto(texto)
        return r
    # Patrón 4: <li> numéricos (5 números + 2 estrellas)
    nums = [int(li.get_text(strip=True)) for li in soup.find_all("li")
            if re.fullmatch(r"\d{1,2}", li.get_text(strip=True))]
    if len(nums) >= 7 and all(1 <= n <= 50 for n in nums[:5]) and all(1 <= n <= 12 for n in nums[5:7]):
        r.numeros = nums[:5]; r.estrellas = nums[5:7]
        r.fecha = extraer_fecha_texto(texto)
        return r
    return None

class ScraperGenerico:
    """Primitiva y Euromillones: laverdad.es y combinacionganadora.com."""
    FUENTES = {
        "la-primitiva": [("laverdad.es", "https://resultados-loteria.laverdad.es/primitiva"),
                         ("combinacionganadora.com", "https://www.combinacionganadora.com/primitiva")],
        "euromillones": [("laverdad.es", "https://resultados-loteria.laverdad.es/euromillones"),
                         ("combinacionganadora.com", "https://www.combinacionganadora.com/euromillones")],
    }

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def obtener(self, juego_key):
        for nombre, url in self.FUENTES.get(juego_key, []):
            try:
                resp = self.s.get(url, timeout=20)
                resp.raise_for_status()
            except Exception as e:
                log.error(f"{nombre} ({juego_key}): {str(e)[:50]}")
                time.sleep(DELAY)
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            texto = soup.get_text(separator="\n", strip=True)
            if juego_key == "la-primitiva":
                r = parse_primitiva_generico(soup, texto, nombre)
            else:
                r = parse_euromillones_generico(soup, texto, nombre)
            if r:
                return r
            log.warn(f"{nombre} ({juego_key}): sin datos")
            time.sleep(DELAY)
        return None

# ──────────────────────────────────────────────
# Scraper 1: La Verdad (Bonoloto y Lotería Nacional)
# ──────────────────────────────────────────────
class ScraperLaVerdad:
    """Periódico regional con resultados de SELAE en HTML simple."""
    BASE = "https://resultados-loteria.laverdad.es"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def obtener(self, juego_key: str) -> Optional[Resultado]:
        url = f"{self.BASE}/{juego_key}"
        try:
            r = self.s.get(url, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log.error(f"La Verdad ({juego_key}): {str(e)[:50]}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        texto = soup.get_text(separator="\n", strip=True)

        if juego_key == "bonoloto":
            return self._parse_bonoloto(soup, texto)
        elif juego_key == "loteria-nacional":
            return self._parse_loteria_nacional(soup, texto)
        return None

    def _parse_bonoloto(self, soup, texto):
        r = Resultado(juego="bonoloto", fuente="laverdad.es")

        # Patrón 1: formato portada "N - N - N - N - N - N | C:N | R:N"
        m = re.search(
            r"(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*\|\s*C:(\d{1,2})\s*\|\s*R:(\d{1,2})",
            texto,
        )
        if m:
            r.numeros = [int(m.group(i)) for i in range(1, 7)]
            r.complementario = int(m.group(7))
            r.reintegro = int(m.group(8))
            r.fecha = self._extraer_fecha(texto)
            return r

        # Patrón 2: sección "Combinación" con <li> + "Comp." + "Reint."
        for sec in soup.find_all(string=re.compile("Combinación", re.IGNORECASE)):
            cont = sec.parent.parent if sec.parent else None
            if not cont:
                continue
            lis = cont.find_all("li")
            nums = [int(li.get_text(strip=True)) for li in lis
                    if re.fullmatch(r"\d{1,2}", li.get_text(strip=True))]
            if len(nums) >= 6:
                r.numeros = nums[:6]
                texto_sec = cont.get_text(separator=" ", strip=True)
                m_c = re.search(r"Comp\.?\s*(\d{1,2})", texto_sec)
                if m_c: r.complementario = int(m_c.group(1))
                m_r = re.search(r"Reint\.?\s*(\d{1,2})", texto_sec)
                if m_r: r.reintegro = int(m_r.group(1))
                r.fecha = self._extraer_fecha(texto)
                return r

        # Patrón 3: artículo "números 7, 12, 14, 37, 42 y 48"
        m = re.search(
            r"números\s+(\d{1,2}),\s*(\d{1,2}),\s*(\d{1,2}),\s*(\d{1,2}),\s*(\d{1,2})\s*y\s*(\d{1,2})",
            texto,
        )
        if m:
            r.numeros = [int(m.group(i)) for i in range(1, 7)]
            r.fecha = self._extraer_fecha(texto)
            return r

        return None

    def _parse_loteria_nacional(self, soup, texto):
        r = Resultado(juego="loteria-nacional", fuente="laverdad.es")

        # Patrón 1: "El primer premio ha sido para el número 07360"
        m = re.search(r"primer\s*premio\s*ha\s*sido\s*para\s*el\s*número\s*(\d{5})", texto, re.IGNORECASE)
        if m:
            r.primer_premio = m.group(1)
            r.fecha = self._extraer_fecha(texto)
            return r

        # Patrón 2: "Primer premio 37872"
        m = re.search(r"[Pp]rimer\s*premio\s*(\d{5})", texto)
        if m:
            r.primer_premio = m.group(1)
            r.fecha = self._extraer_fecha(texto)
            return r

        # Patrón 3: "número 07360"
        m = re.search(r"número\s*(\d{5})", texto, re.IGNORECASE)
        if m:
            r.primer_premio = m.group(1)
            r.fecha = self._extraer_fecha(texto)
            return r

        return None

    def _extraer_fecha(self, texto):
        m = re.search(r"(\d{1,2})/(\d{2})/(\d{4})", texto)
        if m: return f"{m.group(3)}-{m.group(2)}-{int(m.group(1)):02d}"
        meses = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
                 "julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12}
        m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", texto, re.IGNORECASE)
        if m and m.group(2).lower() in meses:
            return f"{m.group(3)}-{meses[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
        return ""

# ──────────────────────────────────────────────
# Scraper 2: EuroDreams (eu-dreams.com + combinacionganadora.com)
# ──────────────────────────────────────────────
class ScraperEuroDreams:
    """Scraper específico para Eurodreams con múltiples patrones."""
    FUENTES = [
        ("eu-dreams.com", "https://eu-dreams.com/es/resultados"),
        ("combinacionganadora.com", "https://www.combinacionganadora.com/eurodreams"),
    ]

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def obtener(self) -> Optional[Resultado]:
        for nombre, url in self.FUENTES:
            try:
                r = self.s.get(url, timeout=20)
                r.raise_for_status()
            except Exception as e:
                log.error(f"{nombre}: {str(e)[:50]}")
                time.sleep(DELAY)
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            texto = soup.get_text(separator="\n", strip=True)
            resultado = self._parse(soup, texto, nombre)
            if resultado:
                return resultado
            log.warn(f"{nombre}: sin datos")
            time.sleep(DELAY)
        return None

    def _parse(self, soup, texto, fuente):
        r = Resultado(juego="eurodreams", fuente=fuente)

        # Patrón 1: eu-dreams.com "Números ganadores * N * N * N * N * N * N * N"
        m = re.search(
            r"Números\s+ganadores\s*\*?\s*(\d{1,2})\s*\*\s*(\d{1,2})\s*\*\s*(\d{1,2})\s*\*\s*(\d{1,2})\s*\*\s*(\d{1,2})\s*\*\s*(\d{1,2})\s*\*\s*(\d{1,2})",
            texto,
        )
        if m:
            r.numeros = [int(m.group(i)) for i in range(1, 7)]
            r.clave = int(m.group(7))
            r.fecha = self._extraer_fecha(texto)
            return r

        # Patrón 2: combinacionganadora.com "N N N N N N S N"
        m = re.search(
            r"(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+S\s*(\d{1,2})",
            texto,
        )
        if m:
            r.numeros = [int(m.group(i)) for i in range(1, 7)]
            r.clave = int(m.group(7))
            r.fecha = self._extraer_fecha(texto)
            return r

        # Patrón 3: <li> con 7 números (6 + Dream)
        nums = []
        for li in soup.find_all("li"):
            val = li.get_text(strip=True)
            if re.fullmatch(r"\d{1,2}", val):
                nums.append(int(val))
        if len(nums) >= 7:
            r.numeros = nums[:6]
            r.clave = nums[6]
            r.fecha = self._extraer_fecha(texto)
            return r

        # Patrón 4: "* N * N * N * N * N * N * N" en texto plano
        m = re.search(
            r"\*\s*(\d{1,2})\s*\*\s*(\d{1,2})\s*\*\s*(\d{1,2})\s*\*\s*(\d{1,2})\s*\*\s*(\d{1,2})\s*\*\s*(\d{1,2})\s*\*\s*(\d{1,2})",
            texto,
        )
        if m:
            r.numeros = [int(m.group(i)) for i in range(1, 7)]
            r.clave = int(m.group(7))
            r.fecha = self._extraer_fecha(texto)
            return r

        return None

    def _extraer_fecha(self, texto):
        m = re.search(r"(\d{1,2})/(\d{2})/(\d{4})", texto)
        if m: return f"{m.group(3)}-{m.group(2)}-{int(m.group(1)):02d}"
        meses = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,
                 "julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12}
        m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", texto, re.IGNORECASE)
        if m and m.group(2).lower() in meses:
            return f"{m.group(3)}-{meses[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
        return ""

# ──────────────────────────────────────────────
# Scraper 3: Benidorm (fallback para los 3 juegos)
# ──────────────────────────────────────────────
class ScraperBenidorm:
    """Administración de lotería. Fallback si las fuentes principales fallan."""
    URL = "https://www.loteria1benidorm.com/resultados?nIdioma=ing&gMobile=0"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def obtener_todos(self) -> list:
        try:
            r = self.s.get(self.URL, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log.error(f"Benidorm: {str(e)[:50]}")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        texto = soup.get_text(separator="\n", strip=True)
        resultados = []

        # Eurodreams
        r_ed = self._parse_eurodreams(soup, texto)
        if r_ed: resultados.append(r_ed)
        # Bonoloto
        r_b = self._parse_bonoloto(soup, texto)
        if r_b: resultados.append(r_b)
        # Lotería Nacional
        r_ln = self._parse_loteria_nacional(soup, texto)
        if r_ln: resultados.append(r_ln)
        # La Primitiva
        r_p = self._parse_primitiva(soup, texto)
        if r_p: resultados.append(r_p)
        # Euromillones
        r_em = self._parse_euromillones(soup, texto)
        if r_em: resultados.append(r_em)

        return resultados

    def _parse_primitiva(self, soup, texto):
        r = Resultado(juego="la-primitiva", fuente="loteria1benidorm.com")
        sec = self._buscar_seccion(soup, "Primitiva")
        if not sec: return None
        texto_sec = sec.get_text(separator=" ", strip=True)
        m = re.search(r"(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})", texto_sec)
        if m:
            r.numeros = [int(m.group(i)) for i in range(1, 7)]
            m_c = re.search(r"(?:Comp|Complem)[.:]?\s*(\d{1,2})", texto_sec, re.IGNORECASE)
            if m_c: r.complementario = int(m_c.group(1))
            m_r = re.search(r"(?:Reint|Reintegro)[.:]?\s*(\d{1,2})", texto_sec, re.IGNORECASE)
            if m_r: r.reintegro = int(m_r.group(1))
            return r
        return None

    def _parse_euromillones(self, soup, texto):
        sec = self._buscar_seccion(soup, "Euromillones")
        if not sec: return None
        texto_sec = sec.get_text(separator=" ", strip=True)
        r = parse_euromillones_generico(soup, texto_sec, "loteria1benidorm.com")
        if r: return r
        r = Resultado(juego="euromillones", fuente="loteria1benidorm.com")
        m = re.search(r"(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s*S:\s*(\d{1,2})\s+(\d{1,2})", texto_sec)
        if m:
            r.numeros = [int(m.group(i)) for i in range(1, 6)]
            r.estrellas = [int(m.group(6)), int(m.group(7))]
            return r
        return None

    def _parse_eurodreams(self, soup, texto):
        r = Resultado(juego="eurodreams", fuente="loteria1benidorm.com")
        sec = self._buscar_seccion(soup, "Eurodreams")
        if not sec: return None
        texto_sec = sec.get_text(separator=" ", strip=True)
        m = re.search(r"(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s*S:\s*(\d{1,2})", texto_sec)
        if m:
            r.numeros = [int(m.group(i)) for i in range(1, 7)]
            r.clave = int(m.group(7))
            return r
        return None

    def _parse_bonoloto(self, soup, texto):
        r = Resultado(juego="bonoloto", fuente="loteria1benidorm.com")
        sec = self._buscar_seccion(soup, "Bonoloto")
        if not sec: return None
        texto_sec = sec.get_text(separator=" ", strip=True)
        m = re.search(r"(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})", texto_sec)
        if m:
            r.numeros = [int(m.group(i)) for i in range(1, 7)]
            m_c = re.search(r"(?:Comp|Complem)[.:]?\s*(\d{1,2})", texto_sec, re.IGNORECASE)
            if m_c: r.complementario = int(m_c.group(1))
            m_r = re.search(r"(?:Reint|Reintegro)[.:]?\s*(\d{1,2})", texto_sec, re.IGNORECASE)
            if m_r: r.reintegro = int(m_r.group(1))
            return r
        return None

    def _parse_loteria_nacional(self, soup, texto):
        r = Resultado(juego="loteria-nacional", fuente="loteria1benidorm.com")
        sec = self._buscar_seccion(soup, "Lotería Nacional")
        if not sec: return None
        texto_sec = sec.get_text(separator=" ", strip=True)
        m = re.search(r"1º\s*(\d{5})", texto_sec)
        if m:
            r.primer_premio = m.group(1)
            return r
        m = re.search(r"1st\.?\s*Prize\s*(\d{5})", texto_sec, re.IGNORECASE)
        if m:
            r.primer_premio = m.group(1)
            return r
        return None

    def _buscar_seccion(self, soup, nombre):
        for elem in soup.find_all(string=re.compile(nombre, re.IGNORECASE)):
            padre = elem.parent
            for _ in range(6):
                if not padre: break
                texto = padre.get_text(separator=" ", strip=True)
                if len(re.findall(r"\b\d{1,2}\b", texto)) >= 4:
                    return padre
                padre = padre.parent
        return None

# ==========================================
# FUNCIÓN DE ACTUALIZACIÓN (con fallback automático)
# ==========================================
def obtener_ultimo(juego_key: str, juego_nombre: str) -> Optional[Resultado]:
    """Prueba múltiples fuentes en orden hasta que una funcione."""

    # ── Eurodreams: fuentes dedicadas ──
    if juego_key == "eurodreams":
        log.info(f"  Probando eu-dreams.com / combinacionganadora.com...")
        r = ScraperEuroDreams().obtener()
        if r: return r
        log.info(f"  → Fallback: loteria1benidorm.com...")
        for rb in ScraperBenidorm().obtener_todos():
            if rb.juego == "eurodreams": return rb
        return None

    # ── Bonoloto y Lotería Nacional ──
    if juego_key in ("bonoloto", "loteria-nacional"):
        log.info(f"  Probando laverdad.es...")
        r = ScraperLaVerdad().obtener(juego_key)
        if r: return r
        log.info(f"  → Fallback: loteria1benidorm.com...")
        for rb in ScraperBenidorm().obtener_todos():
            if rb.juego == juego_key: return rb
        return None

    # ── La Primitiva y Euromillones ──
    if juego_key in ("la-primitiva", "euromillones"):
        log.info(f"  Probando laverdad.es / combinacionganadora.com...")
        r = ScraperGenerico().obtener(juego_key)
        if r: return r
        log.info(f"  → Fallback: loteria1benidorm.com...")
        for rb in ScraperBenidorm().obtener_todos():
            if rb.juego == juego_key: return rb
        return None

    return None

def validar_resultado(juego, fecha, numeros_str, reintegro, db_conn):
    """Devuelve None si el sorteo es creíble o el motivo del rechazo.
    Evita guardar datos de otro juego, combinaciones repetidas o fuera de rango
    cuando una web cambia su estructura."""
    try:
        d = datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        return f"fecha no válida ({fecha})"
    if d > datetime.now() + timedelta(days=1):
        return f"fecha futura ({fecha})"
    if juego == "Loteria Nacional":
        return None
    try:
        vals = [int(n) for n in numeros_str.split(',')]
    except ValueError:
        return "números no numéricos"
    cfg = CONFIG_JUEGOS[juego]
    if cfg['estrellas']:
        nums, ests = vals[:5], vals[5:]
        if (len(vals) != 7 or len(set(nums)) != 5 or len(set(ests)) != 2
                or not all(1 <= n <= cfg['max'] for n in nums)
                or not all(cfg['extra'][0] <= e <= cfg['extra'][1] for e in ests)):
            return f"combinación fuera de rango ({numeros_str})"
    else:
        if (len(vals) != 6 or len(set(vals)) != 6
                or not all(1 <= n <= cfg['max'] for n in vals)):
            return f"combinación fuera de rango ({numeros_str})"
        if not cfg['extra'][0] <= int(reintegro) <= cfg['extra'][1]:
            return f"reintegro/clave fuera de rango ({reintegro})"
    # La misma combinación no se repite en sorteos cercanos (ni en el mismo juego ni en otro):
    # si pasa, la web devolvió datos viejos o de otro juego.
    fila = db_conn.cursor().execute(
        "SELECT juego, fecha FROM sorteos WHERE numeros=? AND ABS(julianday(fecha) - julianday(?)) <= 7",
        (numeros_str, fecha)).fetchone()
    if fila:
        return f"combinación idéntica a un sorteo de {fila[0]} del {fila[1]}"
    return None

def actualizar_base_datos(juego, db_conn):
    log.info(f"Actualizando {juego}...")
    try:
        juego_key = juego.lower().replace(" ", "-")
        resultado = obtener_ultimo(juego_key, juego)

        if resultado and resultado.fecha:
            log.ok(f"  Conexión exitosa vía {resultado.fuente}. Fecha: {resultado.fecha}")

            if juego == "Loteria Nacional":
                if resultado.primer_premio:
                    numeros_str = str(resultado.primer_premio).zfill(5)
                    reintegro = 0
                else:
                    log.warn(f"  {juego}: sin número de 5 cifras.")
                    return
            elif juego == "Euromillones":
                if len(resultado.numeros) < 5 or len(resultado.estrellas) < 2:
                    log.warn(f"  {juego}: datos incompletos.")
                    return
                numeros_str = ",".join(map(str, sorted(resultado.numeros[:5]) + sorted(resultado.estrellas[:2])))
                reintegro = 0
            else:
                if not resultado.numeros or len(resultado.numeros) < 6:
                    log.warn(f"  {juego}: datos incompletos.")
                    return
                numeros_str = ",".join(map(str, sorted(resultado.numeros[:6])))
                reintegro = resultado.reintegro if resultado.reintegro is not None else (resultado.clave if resultado.clave is not None else 0)

            try:
                cursor = db_conn.cursor()
                cursor.execute("SELECT id FROM sorteos WHERE juego=? AND fecha=?", (juego, resultado.fecha))
                if not cursor.fetchone():
                    motivo = validar_resultado(juego, resultado.fecha, numeros_str, reintegro, db_conn)
                    if motivo:
                        log.warn(f"  {juego}: RECHAZADO, {motivo}. Fuente: {resultado.fuente}")
                        return
                    cursor.execute('INSERT INTO sorteos (juego, fecha, numeros, reintegro) VALUES (?, ?, ?, ?)', (juego, resultado.fecha, numeros_str, int(reintegro)))
                    db_conn.commit()
                    log.ok(f"  NUEVO SORTEO GUARDADO: {juego} - {resultado.fecha}")
                else:
                    log.info(f"  {juego}: {resultado.fecha} ya estaba registrado.")
            except Exception as e_db:
                log.error(f"  Error BD: {e_db}")
        else:
            log.warn(f"  No se pudieron obtener datos de {juego}.")

    except Exception as e:
        log.error(f"  Error fatal en scraper de {juego}: {e}")

def tarea_segundo_plano():
    try:
        bg_conn = sqlite3.connect('loteria_db.sqlite', timeout=10)
        log.info("=== Inicio sincronización ===")
        for j in ["Bonoloto", "Eurodreams", "Loteria Nacional", "La Primitiva", "Euromillones"]:
            actualizar_base_datos(j, bg_conn)
            time.sleep(1)
        log.info("=== Sincronización finalizada ===")
        bg_conn.close()
    except Exception as e:
        log.error(f"Error fatal en el hilo: {e}")

if 'bg_sync_started' not in st.session_state:
    st.session_state['bg_sync_started'] = True
    hilo = threading.Thread(target=tarea_segundo_plano, daemon=True)
    hilo.start()

# ==========================================
# FUNCIONES VISUALES
# ==========================================
def mostrar_bolas(apuestas, color_principal, color_extra, texto_extra="R"):
    st.markdown(f"""
    <style>
        /* Un único tamaño de bola para toda la app (apuestas y últimos resultados) */
        :root {{ --ball: clamp(30px, 9.4vw, 40px); --ball-gap: clamp(4px, 1.3vw, 7px); }}
        .lottery-container {{ display: flex; flex-direction: column; gap: 14px; margin-top: 15px; }}
        .lottery-block {{ display: flex; flex-direction: column; gap: 4px; }}
        .label {{ font-weight: bold; opacity: 0.7; font-size: 13px; }}
        .lottery-row {{ display: flex; align-items: center; justify-content: flex-start; gap: var(--ball-gap); flex-wrap: nowrap; }}
        .ball {{
            flex: 0 0 var(--ball); width: var(--ball); height: var(--ball);
            display: flex; justify-content: center; align-items: center;
            border-radius: 50%; box-sizing: border-box;
            color: white; font-weight: bold; font-size: calc(var(--ball) * 0.42);
            box-shadow: 2px 2px 5px rgba(0,0,0,0.2); background-color: {color_principal};
            line-height: 1;
        }}
        .ball-extra {{ background-color: {color_extra}; margin-left: var(--ball-gap); font-size: calc(var(--ball) * 0.36); }}
        .ball-extra + .ball-extra {{ margin-left: 0; }}
    </style>
    """, unsafe_allow_html=True)
    html = "<div class='lottery-container'>"
    for i, apuesta in enumerate(apuestas):
        html += f"<div class='lottery-block'><span class='label'>Apuesta {i+1}</span><div class='lottery-row'>"
        for num in apuesta['numeros']:
            html += f"<span class='ball'>{num}</span>"
        extras = apuesta['extra'] if isinstance(apuesta['extra'], (list, tuple)) else [apuesta['extra']]
        for ex in extras:
            html += f"<span class='ball ball-extra'>{texto_extra.strip().rstrip(':')}{ex}</span>"
        html += "</div></div>"
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)

def mostrar_nacional(apuestas):
    st.markdown("""
    <style>
        .cards-container { display: flex; flex-wrap: wrap; gap: 15px; margin-top: 15px; }
        .card { background: rgba(128,128,128,0.12); border-left: 5px solid #ff7f0e; padding: 12px 15px; border-radius: 6px; box-shadow: 0 3px 6px rgba(0,0,0,0.1); display: flex; flex-direction: column; gap: 8px; margin-bottom: 10px; flex: 1 1 0; min-width: 140px; }
        .card-label { font-weight: bold; opacity: 0.7; font-size: 13px; }
        .card-num { font-size: 26px; font-weight: bold; letter-spacing: 2px; }
        .card-extra { font-size: 12px; opacity: 0.7; font-weight: bold; }
    </style>
    """, unsafe_allow_html=True)
    html = "<div class='cards-container'>"
    for i, ap in enumerate(apuestas):
        html += f"<div class='card'><div class='card-label'>Apuesta {i+1}</div><div class='card-num'>{ap['numero']}</div><div class='card-extra'>Terminación: {ap['terminacion']}</div></div>"
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)

def mostrar_ultimos_resultados(juego, alinear_con_boton=False):
    df_ult = pd.read_sql_query("SELECT fecha, numeros, reintegro FROM sorteos WHERE juego = ? ORDER BY fecha DESC LIMIT 5", conn, params=(juego,))
    if df_ult.empty: return
    margin_top = "22px" if alinear_con_boton else "0px"
    st.markdown(f"<h3 style='margin-top: {margin_top}; margin-bottom: 15px; font-size: 16px;'>🔎 Últimos Resultados</h3>", unsafe_allow_html=True)
    df_ult['fecha'] = pd.to_datetime(df_ult['fecha']).dt.strftime('%d/%m/%Y')
    
    if juego in CONFIG_JUEGOS:
        cfg_j = CONFIG_JUEGOS[juego]
        color_principal = cfg_j['color_p']
        color_extra = cfg_j['color_e']
        texto_extra = cfg_j['txt_e']
        st.markdown(f"""
        <style>
            .res-container {{ display: flex; flex-direction: column; gap: 15px; margin-top: 0px; }}
            .res-row {{ display: flex; flex-direction: column; gap: 4px; background: rgba(128,128,128,0.12); padding: 8px 10px; border-radius: 6px; border: 1px solid rgba(128,128,128,0.25); }}
            .res-date {{ font-weight: bold; opacity: 0.75; font-size: 12px; }}
            :root {{ --ball: clamp(30px, 9.4vw, 40px); --ball-gap: clamp(4px, 1.3vw, 7px); }}
            .res-balls {{ display: flex; align-items: center; justify-content: flex-start; flex-wrap: nowrap; gap: var(--ball-gap); }}
            .res-ball {{ flex: 0 0 var(--ball); width: var(--ball); height: var(--ball); box-sizing: border-box; display: flex; justify-content: center; align-items: center; border-radius: 50%; color: white; font-weight: bold; font-size: calc(var(--ball) * 0.42); background-color: {color_principal}; box-shadow: 2px 2px 5px rgba(0,0,0,0.2); line-height: 1; }}
            .res-extra {{ background-color: {color_extra}; margin-left: var(--ball-gap); font-size: calc(var(--ball) * 0.36); }}
            .res-extra + .res-extra {{ margin-left: 0; }}
        </style>
        """, unsafe_allow_html=True)
        html = "<div class='res-container'>"
        for _, row in df_ult.iterrows():
            html += f"<div class='res-row'><span class='res-date'>{row['fecha']}</span><div class='res-balls'>"
            nums = str(row['numeros']).split(',')
            if cfg_j['estrellas']:
                principales, extras = nums[:5], nums[5:7]
            else:
                principales, extras = nums, [int(row['reintegro'])]
            for n in principales:
                html += f"<span class='res-ball'>{n}</span>"
            for ex in extras:
                html += f"<span class='res-ball res-extra'>{texto_extra.strip().rstrip(':')}{ex}</span>"
            html += "</div></div>"
        html += "</div>"
        st.markdown(html, unsafe_allow_html=True)
        
    elif juego == "Loteria Nacional":
        st.markdown("""
        <style>
            .res-container-nac { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
            .res-row-nac { display: flex; align-items: center; justify-content: space-between; gap: 10px; background: rgba(128,128,128,0.12); padding: 10px 15px; border-radius: 6px; border: 1px solid rgba(128,128,128,0.25); border-left: 4px solid #ff7f0e; }
            .res-date-nac { font-weight: bold; opacity: 0.75; font-size: 13px; }
            .res-num-nac { font-size: 24px; font-weight: bold; letter-spacing: 2px; }
        </style>
        """, unsafe_allow_html=True)
        html = "<div class='res-container-nac'>"
        for _, row in df_ult.iterrows():
            num = str(row['numeros']).zfill(5)
            html += f"<div class='res-row-nac'><span class='res-date-nac'>{row['fecha']}</span><span class='res-num-nac'>{num}</span></div>"
        html += "</div>"
        st.markdown(html, unsafe_allow_html=True)

# ==========================================
# TÍTULO Y MENÚ LATERAL
# ==========================================
st.markdown("""
<style>
    .main-title { font-size: 22px !important; font-weight: bold; margin-bottom: 0px !important; padding-top: 5px !important; }
    .sub-title { font-size: 12px !important; color: #666; margin-top: 2px !important; margin-bottom: 15px !important; }
    h2 { font-size: 18px !important; padding-top: 5px !important; margin-bottom: 5px !important; }
    h3 { font-size: 14px !important; margin-top: 5px !important; margin-bottom: 5px !important; }
    .last-update { font-size: 11px; opacity: 0.65; margin-top: 10px; text-align: center; border-top: 1px solid rgba(128,128,128,0.3); padding-top: 10px; }
    .log-panel { background: rgba(128,128,128,0.12); border: 1px solid rgba(128,128,128,0.3); border-radius: 6px; padding: 10px; margin-top: 10px; font-family: monospace; font-size: 11px; max-height: 200px; overflow-y: auto; white-space: pre-wrap; }
    /* El menú lateral ya no se usa: la navegación va arriba */
    [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"] { display: none !important; }
    /* Barra superior: los 3 botones siempre en una sola fila, también en móvil */
    .st-key-topnav [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 0.4rem !important; }
    .st-key-topnav [data-testid="stColumn"] { min-width: 0 !important; flex: 1 1 0 !important; width: auto !important; }
    .st-key-topnav [data-testid="stColumn"] > div { gap: 0.35rem !important; }
    /* Caja de logo de tamaño fijo: todos centrados y a la misma altura, sea cual sea su proporción */
    .nav-logo { height: 96px; box-sizing: border-box; padding: 6px; margin-bottom: 4px; display: flex; align-items: center; justify-content: center; overflow: hidden; background: rgba(128,128,128,0.12); border: 1px solid rgba(128,128,128,0.25); border-radius: 8px; }
    .nav-logo img { width: 100%; height: 100%; object-fit: contain; display: block; }
    .st-key-topnav [data-testid="stMarkdownContainer"] p { margin: 0 !important; }
    .st-key-topnav button { padding: 0.35rem 0.2rem !important; min-height: 46px; }
    .st-key-topnav button p { font-size: clamp(12px, 3.4vw, 15px) !important; line-height: 1.15 !important; white-space: normal !important; overflow: visible !important; text-overflow: clip !important; text-align: center; }
</style>
""", unsafe_allow_html=True)

st.markdown("<h1 class='main-title'>🎲 Analizador Inteligente de Loterías</h1>", unsafe_allow_html=True)
st.markdown("<p class='sub-title'>Genera tus apuestas basadas en el análisis estadístico real de miles de sorteos históricos.</p>", unsafe_allow_html=True)

if 'juego' not in st.session_state:
    st.session_state.juego = 'Bonoloto'

JUEGOS_NAV = [
    ("Bonoloto", "Bonoloto", "bonoloto.png", "nav_bono"),
    ("Eurodreams", "Eurodreams", "eurodreams.png", "nav_euro"),
    ("Loteria Nacional", "Lotería Nacional", "nacional.png", "nav_nac"),
    ("La Primitiva", "La Primitiva", "laprimitiva.png", "nav_prim"),
    ("Euromillones", "Euromillones", "euromillones.png", "nav_euromil"),
]

# ─── BARRA SUPERIOR DE NAVEGACIÓN (logo opcional + texto) ───
# Logos (nombre de archivo sin extensión) que se muestran sobre fondo negro
LOGOS_FONDO_NEGRO = set()  # p. ej. {"bonoloto"} para convertir blancos a negro
# Logos oscuros que se pierden en el tema oscuro: se muestran sobre recuadro blanco
LOGOS_FONDO_BLANCO = {"euromillones"}

def _blanco_a_negro(svg_texto):
    """Cambia los blancos del SVG (fondo incluido) por negro."""
    svg_texto = re.sub(r'#f{3}(?:f{3})?(?![0-9a-fA-F])', '#000000', svg_texto, flags=re.IGNORECASE)
    svg_texto = re.sub(r'rgb\(\s*255\s*,\s*255\s*,\s*255\s*\)', '#000000', svg_texto, flags=re.IGNORECASE)
    svg_texto = re.sub(r'(fill|stop-color|background(?:-color)?)\s*([=:])\s*(["\']?)white\b', r'\1\2\3#000000', svg_texto, flags=re.IGNORECASE)
    return svg_texto

def logo_html(ruta):
    """Devuelve el logo como <img> en base64 dentro de una caja de tamaño fijo (o '' si no existe)."""
    # Si existe la versión SVG del logo (mismo nombre), tiene prioridad sobre el PNG
    svg = os.path.splitext(ruta)[0] + ".svg"
    if os.path.exists(svg):
        with open(svg, "r", encoding="utf-8", errors="ignore") as f:
            contenido = f.read()
        estilo = ""
        if os.path.splitext(os.path.basename(svg))[0] in LOGOS_FONDO_NEGRO:
            contenido = _blanco_a_negro(contenido)
            estilo = " style='background:#000;'"
        elif os.path.splitext(os.path.basename(svg))[0] in LOGOS_FONDO_BLANCO:
            estilo = " style='background:#fff;'"
        b64 = base64.b64encode(contenido.encode("utf-8")).decode()
        return f"<div class='nav-logo'{estilo}><img src='data:image/svg+xml;base64,{b64}'></div>"
    if not os.path.exists(ruta):
        return ""
    return _logo_b64(ruta)

@st.cache_data(show_spinner=False)
def _logo_b64(ruta):
    """Recorta los márgenes vacíos (transparentes o blancos) del logo para que ocupe todo el recuadro."""
    import io
    try:
        from PIL import Image, ImageChops
        img = Image.open(ruta).convert("RGBA")
        # Margen transparente
        bbox = img.getchannel("A").getbbox()
        # Margen de color liso (el color de la esquina)
        fondo = Image.new("RGBA", img.size, img.getpixel((0, 0)))
        bbox2 = ImageChops.difference(img, fondo).getbbox()
        if bbox2 and (not bbox or (bbox2[2]-bbox2[0])*(bbox2[3]-bbox2[1]) < (bbox[2]-bbox[0])*(bbox[3]-bbox[1])):
            bbox = bbox2
        if bbox:
            img = img.crop(bbox)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        datos = buf.getvalue()
    except Exception:
        with open(ruta, "rb") as f:
            datos = f.read()
    b64 = base64.b64encode(datos).decode()
    return f"<div class='nav-logo'><img src='data:image/png;base64,{b64}'></div>"

with st.container(key="topnav"):
    # Dos filas de 3 columnas (3 + 2) para que los botones quepan bien en móvil
    for fila_nav in (JUEGOS_NAV[:3], JUEGOS_NAV[3:]):
        cols_nav = st.columns(3)
        for col, (clave, etiqueta, logo, key_btn) in zip(cols_nav, fila_nav):
            with col:
                html_logo = logo_html(logo)
                if html_logo:
                    st.markdown(html_logo, unsafe_allow_html=True)
                activo = st.session_state.juego == clave
                if st.button(etiqueta, key=key_btn, use_container_width=True, type="primary" if activo else "secondary"):
                    st.session_state.juego = clave
                    st.rerun()

# ─── HERRAMIENTAS (antes en el menú lateral): se muestran al final de la página ───
def panel_herramientas():
    st.divider()
    with st.expander("⚙️ Herramientas y estado de la base de datos"):
        df_max_fecha = pd.read_sql_query("SELECT MAX(fecha) as max_fecha FROM sorteos", conn)
        ultima_fecha = df_max_fecha['max_fecha'].iloc[0] if not df_max_fecha.empty else None
        if ultima_fecha:
            ultima_fecha_str = pd.to_datetime(ultima_fecha).strftime('%d/%m/%Y')
            st.markdown(f"Base de datos actualizada hasta: **{ultima_fecha_str}**")

        st.markdown("📋 **Registro de actualización**")
        st.markdown(f"<div class='log-panel'>{log.obtener() or 'Esperando...'}</div>", unsafe_allow_html=True)
        if st.button("🔄 Refrescar", key="btn_refrescar", use_container_width=True):
            st.rerun()

        st.markdown("---")
        st.markdown("✍️ **Inserción rápida** (por si Internet falla)")
        juego_manual = st.selectbox("Juego", ["Bonoloto", "Eurodreams", "Loteria Nacional", "La Primitiva", "Euromillones"], key="manual_juego")
        fecha_manual = st.date_input("Fecha del sorteo", datetime.today(), key="manual_fecha")

        if juego_manual == "Loteria Nacional":
            num_manual = st.text_input("Número (5 cifras)", key="manual_num_nac", placeholder="Ej: 12345")
            reint_manual = 0
        elif juego_manual == "Euromillones":
            num_manual = st.text_input("5 números + 2 estrellas (separados por coma)", key="manual_num_euro", placeholder="Ej: 3,15,22,31,48,2,9")
            reint_manual = 0
        else:
            num_manual = st.text_input("Números (separados por coma)", key="manual_num", placeholder="Ej: 1,2,3,4,5,6")
            reint_manual = st.number_input("Reintegro/Euronúmero", min_value=0, max_value=9, key="manual_reint")

        if st.button("Guardar en BD", key="btn_manual", use_container_width=True):
            if num_manual:
                if insertar_sorteo_manual(juego_manual, fecha_manual, num_manual, reint_manual):
                    time.sleep(1)
                    st.rerun()

st.divider()

if 'apuestas_cache' not in st.session_state:
    st.session_state['apuestas_cache'] = {}

# ==========================================
# LÓGICA BONOLOTO Y EURODREAMS
# ==========================================
if st.session_state.juego in ['Bonoloto', 'Eurodreams', 'La Primitiva']:
    juego_actual = st.session_state.juego
    cfg = CONFIG_JUEGOS[juego_actual]
    df = pd.read_sql_query(f"SELECT numeros, reintegro FROM sorteos WHERE juego = '{juego_actual}'", conn)
    
    if df.empty:
        st.warning(f"No hay datos cargados para {juego_actual}.")
    else:
        col_izq, col_der = st.columns([2.5, 1.5])
        with col_izq:
            col_strat, col_btn, _ = st.columns([1.2, 1.0, 1.8])
            with col_strat:
                st.markdown("<p style='font-size: 14px; margin-bottom: 0px; font-weight: bold;'>Elige tu estrategia:</p>", unsafe_allow_html=True)
                estrategia = st.selectbox(
                    "Estrategia", ["Caliente Ponderada", "Mixta (3C / 3F)", "Top 10 Calientes", "Aleatoria Pura"],
                    label_visibility="collapsed", key=f"estrategia_{juego_actual}"
                )
            with col_btn:
                st.markdown("<div style='margin-top: 21px;'></div>", unsafe_allow_html=True)
                if st.button("Generar 5 apuestas", type="primary", key=f"btn_{juego_actual}", use_container_width=True):
                    limite_max = cfg['max']
                    todos_los_numeros = []
                    for fila in df['numeros']:
                        if pd.notna(fila): todos_los_numeros.extend([int(n) for n in fila.split(',')])
                    conteo = Counter(todos_los_numeros)
                    numeros_disponibles = list(range(1, limite_max + 1))
                    pesos = [conteo.get(num, 0) + 1 for num in numeros_disponibles]
                    calientes_25 = [num for num, _ in conteo.most_common(25)]
                    frios_25 = [num for num, _ in conteo.most_common()[-25:]]
                    top_10 = [num for num, _ in conteo.most_common(10)]
                    apuestas_generadas = []
                    for i in range(5):
                        if estrategia == "Caliente Ponderada":
                            combinacion = list(set(random.choices(numeros_disponibles, weights=pesos, k=6)))
                            while len(combinacion) < 6:
                                nuevo = random.choices(numeros_disponibles, weights=pesos, k=1)[0]
                                if nuevo not in combinacion: combinacion.append(nuevo)
                        elif estrategia == "Mixta (3C / 3F)":
                            combinacion = random.sample(calientes_25, 3) + random.sample(frios_25, 3)
                        elif estrategia == "Top 10 Calientes":
                            combinacion = list(set(random.choices(top_10, k=6)))
                            while len(combinacion) < 6:
                                nuevo = random.choice(top_10)
                                if nuevo not in combinacion: combinacion.append(nuevo)
                        elif estrategia == "Aleatoria Pura":
                            combinacion = random.sample(numeros_disponibles, 6)
                        combinacion.sort()
                        reintegro = random.randint(*cfg['extra'])
                        apuestas_generadas.append({'numeros': combinacion, 'extra': reintegro})
                    st.session_state['apuestas_cache'][juego_actual] = apuestas_generadas
            
            if juego_actual in st.session_state['apuestas_cache']:
                color_p, color_e, txt_e = cfg['color_p'], cfg['color_e'], cfg['txt_e']
                mostrar_bolas(st.session_state['apuestas_cache'][juego_actual], color_principal=color_p, color_extra=color_e, texto_extra=txt_e)
        
        with col_der:
            mostrar_ultimos_resultados(juego_actual, alinear_con_boton=True)
        
        st.divider()
        st.header("📊 Estadísticas Detalladas")
        st.write(f"Total de sorteos históricos analizados: **{len(df)}**")
        
        todos_los_numeros = []
        for fila in df['numeros']:
            if pd.notna(fila): todos_los_numeros.extend([int(n) for n in fila.split(',')])
        conteo = Counter(todos_los_numeros)
        df_conteo = pd.DataFrame(conteo.most_common(), columns=['Número', 'Frecuencia'])
        
        st.subheader("🔥 Mapa de Calor de Frecuencias (Todos los números)")
        df_calor = df_conteo.copy()
        df_calor['Número'] = df_calor['Número'].astype(str)
        scheme = cfg['scheme']
        chart_calor = alt.Chart(df_calor).mark_bar().encode(
            x='Frecuencia:Q', y=alt.Y('Número:O', sort=df_calor['Número'].tolist()),
            color=alt.Color('Frecuencia:Q', scale=alt.Scale(scheme=scheme), legend=None)
        ).properties(height=500)
        st.altair_chart(chart_calor, use_container_width=True)
        
        with st.expander("Ver tabla detallada de datos"):
            st.dataframe(df_conteo.style.background_gradient(cmap=cfg['cmap'], subset=['Frecuencia']), hide_index=True, use_container_width=True)
        
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Top 10 Más Premiados")
            df_mas = df_conteo.head(10).copy()
            df_mas['Número'] = df_mas['Número'].astype(str)
            chart_mas = alt.Chart(df_mas).mark_bar(color=cfg['color_p']).encode(
                x=alt.X('Número:O', sort=df_mas['Número'].tolist(), axis=alt.Axis(labelAngle=0)), y='Frecuencia:Q'
            )
            st.altair_chart(chart_mas, use_container_width=True)
        with col2:
            st.subheader("Top 10 Menos Premiados")
            df_menos = df_conteo.tail(10).copy()
            df_menos['Número'] = df_menos['Número'].astype(str)
            chart_menos = alt.Chart(df_menos).mark_bar(color='#d62728').encode(
                x=alt.X('Número:O', sort=df_menos['Número'].tolist(), axis=alt.Axis(labelAngle=0)), y='Frecuencia:Q'
            )
            st.altair_chart(chart_menos, use_container_width=True)

# ==========================================
# LÓGICA EUROMILLONES (5 números + 2 estrellas)
# ==========================================
elif st.session_state.juego == 'Euromillones':
    cfg = CONFIG_JUEGOS['Euromillones']

    def generar_combinacion(estrategia, disponibles, conteo, k):
        ordenados = sorted(disponibles, key=lambda n: (-conteo.get(n, 0), n))
        mitad = len(disponibles) // 2
        calientes, frios = ordenados[:mitad], ordenados[-mitad:]
        top = ordenados[:10 if len(disponibles) > 20 else 5]
        if estrategia == "Caliente Ponderada":
            pool, elegidos = list(disponibles), []
            for _ in range(k):
                pesos_pool = [conteo.get(n, 0) + 1 for n in pool]
                n = random.choices(pool, weights=pesos_pool, k=1)[0]
                elegidos.append(n)
                pool.remove(n)
            return sorted(elegidos)
        if estrategia == "Mixta (calientes / frías)":
            c = (k + 1) // 2
            return sorted(random.sample(calientes, c) + random.sample(frios, k - c))
        if estrategia == "Top Calientes":
            return sorted(random.sample(top, k))
        return sorted(random.sample(disponibles, k))

    df = pd.read_sql_query("SELECT numeros FROM sorteos WHERE juego = 'Euromillones'", conn)
    if df.empty:
        st.warning("No hay datos cargados para Euromillones.")
    else:
        conteo_n, conteo_e = Counter(), Counter()
        for fila in df['numeros']:
            if pd.notna(fila):
                vals = [int(n) for n in str(fila).split(',')]
                if len(vals) == 7:
                    conteo_n.update(vals[:5])
                    conteo_e.update(vals[5:])
        disp_n, disp_e = list(range(1, 51)), list(range(1, 13))

        col_izq, col_der = st.columns([2.5, 1.5])
        with col_izq:
            col_strat, col_btn, _ = st.columns([1.2, 1.0, 1.8])
            with col_strat:
                st.markdown("<p style='font-size: 14px; margin-bottom: 0px; font-weight: bold;'>Elige tu estrategia:</p>", unsafe_allow_html=True)
                estrategia = st.selectbox(
                    "Estrategia", ["Caliente Ponderada", "Mixta (calientes / frías)", "Top Calientes", "Aleatoria Pura"],
                    label_visibility="collapsed", key="estrategia_Euromillones"
                )
            with col_btn:
                st.markdown("<div style='margin-top: 21px;'></div>", unsafe_allow_html=True)
                if st.button("Generar 5 apuestas", type="primary", key="btn_Euromillones", use_container_width=True):
                    st.session_state['apuestas_cache']['Euromillones'] = [
                        {'numeros': generar_combinacion(estrategia, disp_n, conteo_n, 5),
                         'extra': generar_combinacion(estrategia, disp_e, conteo_e, 2)}
                        for _ in range(5)
                    ]
            if 'Euromillones' in st.session_state['apuestas_cache']:
                mostrar_bolas(st.session_state['apuestas_cache']['Euromillones'],
                              color_principal=cfg['color_p'], color_extra=cfg['color_e'], texto_extra=cfg['txt_e'])

        with col_der:
            mostrar_ultimos_resultados('Euromillones', alinear_con_boton=True)

        st.divider()
        st.header("📊 Estadísticas Detalladas")
        st.write(f"Total de sorteos históricos analizados: **{len(df)}**")

        df_n = pd.DataFrame([(n, conteo_n.get(n, 0)) for n in disp_n], columns=['Número', 'Frecuencia']).sort_values('Frecuencia', ascending=False)
        st.subheader("🔥 Mapa de Calor de Frecuencias (Números 1-50)")
        df_calor = df_n.copy()
        df_calor['Número'] = df_calor['Número'].astype(str)
        chart_calor = alt.Chart(df_calor).mark_bar().encode(
            x='Frecuencia:Q', y=alt.Y('Número:O', sort=df_calor['Número'].tolist()),
            color=alt.Color('Frecuencia:Q', scale=alt.Scale(scheme=cfg['scheme']), legend=None)
        ).properties(height=600)
        st.altair_chart(chart_calor, use_container_width=True)

        with st.expander("Ver tabla detallada de datos"):
            st.dataframe(df_n.style.background_gradient(cmap=cfg['cmap'], subset=['Frecuencia']), hide_index=True, use_container_width=True)

        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Top 10 Más Premiados")
            df_mas = df_n.head(10).copy()
            df_mas['Número'] = df_mas['Número'].astype(str)
            st.altair_chart(alt.Chart(df_mas).mark_bar(color=cfg['color_p']).encode(
                x=alt.X('Número:O', sort=df_mas['Número'].tolist(), axis=alt.Axis(labelAngle=0)), y='Frecuencia:Q'
            ), use_container_width=True)
        with col2:
            st.subheader("Top 10 Menos Premiados")
            df_menos = df_n.tail(10).copy()
            df_menos['Número'] = df_menos['Número'].astype(str)
            st.altair_chart(alt.Chart(df_menos).mark_bar(color='#d62728').encode(
                x=alt.X('Número:O', sort=df_menos['Número'].tolist(), axis=alt.Axis(labelAngle=0)), y='Frecuencia:Q'
            ), use_container_width=True)

        st.subheader("⭐ Frecuencia de Estrellas (1-12)")
        df_e = pd.DataFrame([(e, conteo_e.get(e, 0)) for e in disp_e], columns=['Estrella', 'Frecuencia'])
        df_e['Estrella'] = df_e['Estrella'].astype(str)
        st.altair_chart(alt.Chart(df_e).mark_bar(color=cfg['color_e']).encode(
            x=alt.X('Estrella:O', sort=df_e['Estrella'].tolist(), axis=alt.Axis(labelAngle=0)), y='Frecuencia:Q'
        ), use_container_width=True)

# ==========================================
# LÓGICA LOTERÍA NACIONAL
# ==========================================
elif st.session_state.juego == 'Loteria Nacional':
    df = pd.read_sql_query("SELECT numeros, reintegro FROM sorteos WHERE juego = 'Loteria Nacional'", conn)
    if df.empty:
        st.warning("No hay datos cargados para Lotería Nacional.")
    else:
        col_izq, col_der = st.columns([2, 1.2])
        with col_izq:
            st.header("🎰 Generador Inteligente")
            if st.button("Generar 5 números para Lotería Nacional", type="primary", key="btn_nacional", use_container_width=True):
                terminaciones_1 = []
                terminaciones_2 = []
                for fila in df['numeros']:
                    if pd.notna(fila):
                        numero = str(int(float(fila))).zfill(5)
                        if len(numero) == 5:
                            terminaciones_1.append(int(numero[-1]))
                            terminaciones_2.append(int(numero[-2:]))
                conteo_1 = Counter(terminaciones_1)
                conteo_2 = Counter(terminaciones_2)
                terminaciones_disponibles = list(range(100))
                pesos = [conteo_2.get(term, 0) + 1 for term in terminaciones_disponibles]
                apuestas_generadas = []
                for i in range(5):
                    terminacion_elegida = random.choices(terminaciones_disponibles, weights=pesos, k=1)[0]
                    inicio = random.randint(0, 999)
                    numero_final_str = f"{inicio:03d}{terminacion_elegida:02d}"
                    apuestas_generadas.append({'numero': numero_final_str, 'terminacion': f"{terminacion_elegida:02d}"})
                st.session_state['apuestas_cache']['Loteria Nacional'] = apuestas_generadas
            
            if 'Loteria Nacional' in st.session_state['apuestas_cache']:
                mostrar_nacional(st.session_state['apuestas_cache']['Loteria Nacional'])
        
        with col_der:
            mostrar_ultimos_resultados("Loteria Nacional")
        
        st.divider()
        st.header("📊 Estadísticas de Terminaciones")
        st.write(f"Total de sorteos históricos analizados: **{len(df)}**")
        
        terminaciones_1, terminaciones_2 = [], []
        for fila in df['numeros']:
            if pd.notna(fila):
                numero = str(int(float(fila))).zfill(5)
                if len(numero) == 5:
                    terminaciones_1.append(int(numero[-1]))
                    terminaciones_2.append(int(numero[-2:]))
        conteo_1 = Counter(terminaciones_1)
        conteo_2 = Counter(terminaciones_2)
        
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("🔥 Terminaciones 1 cifra (0-9)")
            df_t1 = pd.DataFrame(conteo_1.most_common(), columns=['Terminación', 'Frecuencia'])
            if not df_t1.empty:
                df_t1['Terminación'] = df_t1['Terminación'].astype(str)
                st.dataframe(df_t1.style.background_gradient(cmap='Oranges', subset=['Frecuencia']), hide_index=True)
                chart_t1 = alt.Chart(df_t1).mark_bar(color='#ff7f0e').encode(
                    x=alt.X('Terminación:O', sort=df_t1['Terminación'].tolist(), axis=alt.Axis(labelAngle=0)), y='Frecuencia:Q'
                )
                st.altair_chart(chart_t1, use_container_width=True)
        with col2:
            st.subheader("🔥 Terminaciones 2 cifras (Top 15)")
            df_t2 = pd.DataFrame(conteo_2.most_common(15), columns=['Terminación', 'Frecuencia'])
            if not df_t2.empty:
                df_t2['Terminación'] = df_t2['Terminación'].apply(lambda x: str(x).zfill(2))
                st.dataframe(df_t2.style.background_gradient(cmap='Reds', subset=['Frecuencia']), hide_index=True)
                chart_t2 = alt.Chart(df_t2).mark_bar(color='#d62728').encode(
                    x=alt.X('Terminación:O', sort=df_t2['Terminación'].tolist(), axis=alt.Axis(labelAngle=0)), y='Frecuencia:Q'
                )
                st.altair_chart(chart_t2, use_container_width=True)

# ==========================================
# HERRAMIENTAS (al final de la página)
# ==========================================
panel_herramientas()
