"""
BACKEND (macchina 1) - Sistema AI per la prenotazione di un tavolo.

Come da traccia, il BE:
  * riceve i messaggi di CHAT e VOICE SOLO via rete (HTTP sulla LAN);
  * integra l'LLM locale (Ollama) e il DB SQLite;
  * legge/scrive il DB per disponibilità e prenotazioni (prenotazioni.py).

Casi d'uso coperti: 1 prenotare, 2 modificare, 3 cancellare, 4 menu, 5 informazioni.

Privacy: il BE non riceve mai dati personali in chiaro. Se il client ne ha, manda un
blob CIFRATO ("dati_cifrati") che il BE salva nel DB senza poterlo decifrare. A chi
chiede di rivedere i propri dati sensibili risponde che non puo' per il GDPR.

File: questa versione usa prenotazioni.1.py (il nome con il punto non si puo' importare
con "import", quindi lo carichiamo dal percorso del file).

Avvio:   python backend.py      (prima: python database.py, e Ollama attivo)
"""
import datetime
import importlib.util
import os
import re
import sqlite3
import sys
import threading
import time
from typing import Optional

import requests
from fastapi import FastAPI
from pydantic import BaseModel


def _carica_modulo(nome: str, file: str):
    """import di un file con il punto nel nome (prenotazioni.1.py)."""
    percorso = os.path.join(os.path.dirname(os.path.abspath(__file__)), file)
    spec = importlib.util.spec_from_file_location(nome, percorso)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    spec.loader.exec_module(modulo)
    return modulo


pren = _carica_modulo("prenotazioni_v1", "prenotazioni.1.py")

# ----------------------------------------------------------------- CONFIG ---
DB_PATH = pren.DB_PATH                              # stesso file usato da database.py
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL_NAME = os.getenv("MODEL_NAME", "llama3")      # modello locale (llama3, mistral...)
LLM_TIMEOUT = 120
SESSIONE_TTL = 2 * 3600                             # le sessioni inattive vengono dimenticate

app = FastAPI(title="Backend ristorante")


class UserRequest(BaseModel):
    message: str
    session_id: str = "default"      # CHAT e VOICE mandano un id diverso per ogni conversazione
    dati_cifrati: Optional[str] = None   # dati personali CIFRATI dal client (il BE non ha la chiave)


# --------------------------------------------------------------- SESSIONI ---
_sessioni: dict = {}
_lock = threading.Lock()


def get_stato(session_id: str) -> dict:
    """Stato della prenotazione di una conversazione (l'LLM non ha memoria)."""
    adesso = time.time()
    with _lock:
        for sid in [s for s, v in _sessioni.items() if adesso - v["ts"] > SESSIONE_TTL]:
            del _sessioni[sid]
        voce = _sessioni.setdefault(session_id, {"stato": pren.nuovo_stato(), "ts": adesso})
        voce["ts"] = adesso
        return voce["stato"]


# ---------------------------------------------------------------- DATABASE ---
def leggi_db() -> dict:
    """Menu e informazioni del ristorante, letti dal DB a ogni richiesta (sempre aggiornati)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        menu = conn.execute("SELECT categoria, piatto, prezzo FROM menu ORDER BY id").fetchall()
        info = dict(conn.execute("SELECT chiave, valore FROM ristorante_info").fetchall())
        conn.close()
        return {"menu": menu, "info": info}
    except Exception as e:
        print(f"[DB] errore lettura: {e}")
        return {"menu": [], "info": {}}


def _prezzo(p: float) -> str:
    return f"{p:g}".replace(".", ",") + " euro"


def testo_menu(menu) -> str:
    if not menu:
        return "Il menu non è al momento disponibile."
    per_categoria = {}
    for cat, piatto, prezzo in menu:
        per_categoria.setdefault(cat, []).append(f"{piatto} ({_prezzo(prezzo)})")
    return "\n".join(f"{cat}: " + ", ".join(piatti) + "." for cat, piatti in per_categoria.items())


def testo_info(info: dict) -> str:
    return "\n".join(f"{k.capitalize()}: {v}" for k, v in info.items() if k != "nome")


# --------------------------------------------------------------------- LLM ---
def chiedi_llm(messaggio: str, stato: dict):
    """Risposta dell'LLM locale per menu, informazioni e dialogo. None se non risponde."""
    db = leggi_db()
    nome = db["info"].get("nome", "il ristorante")
    oggi = datetime.date.today()
    sessione = pren.prenotazioni_sessione(stato)
    contesto = (
        f"Data di oggi: {pren.fmt_data(oggi)}.\n\n"
        f"MENU:\n{testo_menu(db['menu'])}\n\n"
        f"INFORMAZIONI:\n{testo_info(db['info'])}\n\n"
        f"PRENOTAZIONI DI QUESTO CLIENTE: {' | '.join(sessione) if sessione else 'nessuna'}"
    )
    system = f"""Sei l'assistente virtuale di "{nome}". Rispondi SEMPRE in italiano.

REGOLE:
1. Usa SOLO le informazioni del CONTESTO. Se un dato non c'è, di' che non lo sai e consiglia di contattare il ristorante. Non inventare piatti, prezzi, orari o prenotazioni.
2. Non confermare, non modificare e non annullare prenotazioni e non dire se un tavolo è libero: se il cliente vuole prenotare, modificare o annullare, invitalo a scrivere ad esempio "Vorrei prenotare un tavolo per 2 persone domani alle 20".
3. Rispondi in massimo 3 frasi, senza elenchi puntati, senza markdown e senza emoji (la risposta viene letta ad alta voce).
4. Parla solo come assistente: non scrivere mai le battute del cliente.
5. Non fornire mai dati personali o sensibili dei clienti (nome, email, telefono): rispondi che non puoi per via del GDPR.

CONTESTO:
{contesto}"""
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": messaggio}],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 220},
    }
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=LLM_TIMEOUT)
        r.raise_for_status()
        testo = r.json().get("message", {}).get("content", "")
        testo = re.sub(r"[*_`#]+", "", testo)
        testo = re.split(r"\n\s*(?:Cliente|Utente)\s*:", testo)[0].strip()
        return testo or None
    except Exception as e:
        print(f"[LLM] errore: {e}")
        return None


def risposta_di_riserva(messaggio: str) -> str:
    """Se l'LLM non è raggiungibile, menu e informazioni si leggono comunque dal DB."""
    t = messaggio.lower()
    db = leggi_db()
    if re.search(r"\b(menu|menù|piatt|mangiar|antipast|prim[oi]|second[oi]|dolc|bevand|prezz|cost)", t):
        return "Ecco il nostro menu.\n" + testo_menu(db["menu"])
    if re.search(r"\b(orari|aperto|apert|chius|indirizzo|dov|posizione|raggiung|mappa|contatt|telefon|email|sito)", t):
        return testo_info(db["info"]) or "Al momento non ho queste informazioni."
    return ("Scusa, in questo momento non riesco a rispondere. Posso aiutarti a prenotare un tavolo, "
            "oppure chiedimi menu, orari e indirizzo.")


# ---------------------------------------------------------------------- API ---
@app.post("/api/chat")
def chat_endpoint(req: UserRequest):
    """Endpoint unico usato da CHAT e VOICE (def sincrona: FastAPI la esegue in un thread)."""
    stato = get_stato(req.session_id)
    if req.dati_cifrati and re.fullmatch(r"[A-Za-z0-9_=\-]{1,2000}", req.dati_cifrati):
        stato["dati_cifrati"] = req.dati_cifrati         # solo testo cifrato: mai loggato, mai letto
    fonte = "prenotazioni"
    risposta = pren.gestisci(req.message, stato)         # prenotare / modificare / annullare
    if risposta == pren.MSG_GDPR:
        fonte = "gdpr"
    if risposta is None:
        fonte = "llm"
        risposta = chiedi_llm(req.message, stato)        # menu, informazioni, dialogo
        if risposta is None:
            fonte = "riserva"
            risposta = risposta_di_riserva(req.message)
    return {"reply": risposta, "fonte": fonte}


@app.get("/api/health")
def health():
    return {"status": "ok", "db": os.path.exists(DB_PATH), "modello": MODEL_NAME}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)       # 0.0.0.0: raggiungibile dalla LAN
