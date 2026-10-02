"""
Módulo compartilhado — ActiveCampaign.
Centraliza configuração, cliente HTTP e utilitários de console.
"""

import os
import time
import threading
import requests
from dotenv import load_dotenv

load_dotenv()

AC_API_KEY  = os.getenv("AC_API_KEY", "")
AC_BASE_URL = os.getenv("AC_BASE_URL", "https://hashtagtreinamentos.api-us1.com").rstrip("/")
API_BASE    = f"{AC_BASE_URL}/api/3"

MAX_WORKERS = 20

print_lock = threading.Lock()


def tprint(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


def get_headers():
    return {"Api-Token": AC_API_KEY, "Content-Type": "application/json"}


def _norm(path: str) -> str:
    """Garante que o path começa com '/'."""
    return path if path.startswith("/") else f"/{path}"


def ac_get(path, params=None, retries=3, timeout=30):
    url = f"{API_BASE}{_norm(path)}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=get_headers(), params=params, timeout=timeout)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                tprint(f"  ⚠ Rate limit. Aguardando {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            tprint(f"  ⚠ Timeout em {path}, tentativa {attempt + 1}/{retries}...")
            time.sleep(2)
    raise Exception(f"Falha após {retries} tentativas: {path}")


def ac_post(path, payload, retries=3):
    url = f"{API_BASE}{_norm(path)}"
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=get_headers(), json=payload, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            time.sleep(2)
    raise Exception(f"Falha após {retries} tentativas: {path}")


def ac_put(path, payload, retries=3):
    url = f"{API_BASE}{_norm(path)}"
    for attempt in range(retries):
        try:
            r = requests.put(url, headers=get_headers(), json=payload, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            time.sleep(2)
    raise Exception(f"Falha após {retries} tentativas: {path}")


def ac_delete(path, retries=3):
    url = f"{API_BASE}{_norm(path)}"
    for attempt in range(retries):
        try:
            r = requests.delete(url, headers=get_headers(), timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                time.sleep(wait)
                continue
            r.raise_for_status()
            return True
        except requests.exceptions.Timeout:
            time.sleep(2)
    raise Exception(f"Falha após {retries} tentativas: {path}")


def print_separator(char="─", width=65):
    print(char * width)


def confirm(prompt):
    resp = input(f"{prompt} [s/N]: ").strip().lower()
    return resp in ("s", "sim", "y", "yes")
