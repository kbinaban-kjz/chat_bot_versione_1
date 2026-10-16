"""
Logica di prenotazione del BACKEND (gira solo sulla macchina del BE).

Il cliente è anonimo (CHAT/VOICE tolgono i dati personali): la prenotazione si
identifica con un CODICE di 5 cifre, dato alla conferma, necessario per
modificarla o annullarla anche in una conversazione successiva.

Disponibilità: una richiesta viene rifiutata ("il posto non è disponibile") se
  * esiste già una prenotazione con stesso giorno, stessa ora e stesso numero di
    persone (anche fatta da un altro dispositivo), oppure
  * la capienza massima della fascia oraria verrebbe superata.

Dati personali (GDPR): CHAT e VOICE tolgono nome/email/telefono dal testo e ne mandano al BE solo una versione CIFRATA con una chiave che il BE non
ha. Il BE la salva nella colonna 'dati_cifrati' della prenotazione ma non puo' leggerla,
e a chi chiede di rivedere i propri dati sensibili risponde di non poterli fornire (GDPR).

"""
import datetime as dt
import os
import random
import re
import sqlite3
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ristorante.db")
CAPIENZA = 40                 # coperti massimi per fascia oraria
MAX_PERSONE = 12              # oltre: contattare il ristorante
GIORNI_CHIUSURA = {0}         # lunedì
# fasce prenotabili: (primo orario, ultimo orario prenotabile)
FASCE = [(dt.time(12, 0), dt.time(14, 0)), (dt.time(19, 0), dt.time(22, 0))]
ORARI_TESTO = "pranzo 12:00-15:00 e cena 19:00-23:00 (chiuso il lunedì)"

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]
NUMERI = {"un": 1, "uno": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5,
          "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10,
          "undici": 11, "dodici": 12}
_NUM_RE = "|".join(NUMERI)


def nuovo_stato() -> dict:  #history dialog
    return {"attiva": False, "persone": None, "data": None, "ora": None,
            "in_attesa_conferma": False,
            "modifica": None,     # codice della prenotazione che si sta modificando
            "fatte": [],          # codici delle prenotazioni confermate in questa sessione
            "dati_cifrati": None}  # blob cifrato dal client (il BE non puo' decifrarlo)


# PARSING 
def _norm(t: str) -> str:
    t = t.lower()
    for a, b in (("lunedi", "lunedì"), ("martedi", "martedì"), ("mercoledi", "mercoledì"),
                 ("giovedi", "giovedì"), ("venerdi", "venerdì")):
        t = re.sub(rf"\b{a}\b", b, t)
    return t


def parse_data(t: str, oggi: dt.date):
    """Ritorna (data|None, testo_senza_la_data)."""
    d = None
    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(MESI) + r")(?:\s+(\d{4}))?\b", t)
    if m:
        giorno, mese = int(m.group(1)), MESI.index(m.group(2)) + 1
        anno = int(m.group(3)) if m.group(3) else oggi.year
        d = _data_valida(anno, mese, giorno, oggi, m.group(3) is None)
        return d, t[:m.start()] + " " + t[m.end():]
    m = re.search(r"\b(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b", t)
    if m and int(m.group(2)) <= 12 and int(m.group(1)) <= 31:
        anno = oggi.year
        if m.group(3):
            anno = int(m.group(3))
            anno += 2000 if anno < 100 else 0
        d = _data_valida(anno, int(m.group(2)), int(m.group(1)), oggi, m.group(3) is None)
        return d, t[:m.start()] + " " + t[m.end():]
    for parola, delta in (("dopodomani", 2), ("domani", 1), ("oggi", 0), ("stasera", 0)):
        m = re.search(rf"\b{parola}\b", t)
        if m:
            return oggi + dt.timedelta(days=delta), t[:m.start()] + " " + t[m.end():]
    m = re.search(r"\b(" + "|".join(GIORNI) + r")\b", t)
    if m:
        target = GIORNI.index(m.group(1))
        delta = (target - oggi.weekday()) % 7 or 7
        return oggi + dt.timedelta(days=delta), t[:m.start()] + " " + t[m.end():]
    return None, t


def _data_valida(anno, mese, giorno, oggi, anno_implicito):
    try:
        d = dt.date(anno, mese, giorno)
    except ValueError:
        return None
    if anno_implicito and d < oggi:
        try:
            d = dt.date(anno + 1, mese, giorno)
        except ValueError:
            return None
    return d


def parse_ora(t: str) -> Optional[dt.time]:
    m = re.search(r"(?<![\d./:])(\d{1,2})[:.](\d{2})(?![\d])", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"\b(?:alle|ore|verso le)\s+(?:ore\s+)?(\d{1,2})\b(?![:./]\d)", t)
        if not m:
            return None
        h, mi = int(m.group(1)), 0
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return dt.time(h, mi)
    return None


def parse_persone(t: str) -> Optional[int]:
    m = re.search(r"\b(\d{1,2})\s*(?:persone|pers\b|coperti|ospiti|posti)", t)
    if m:
        return int(m.group(1))
    m = re.search(rf"\b({_NUM_RE})\s+(?:persone|coperti|ospiti|posti)", t)
    if m:
        return NUMERI[m.group(1)]
    m = re.search(r"\bper\s+(\d{1,2})\b(?![:.]\d)", t)
    if m:
        return int(m.group(1))
    m = re.search(rf"\bper\s+({_NUM_RE})\b", t)
    if m:
        return NUMERI[m.group(1)]
    m = re.search(r"\b(?:siamo|saremo|in)\s+(\d{1,2})\b(?![:.]\d)", t)
    if m:
        return int(m.group(1))
    m = re.search(rf"\b(?:siamo|saremo|in)\s+({_NUM_RE})\b", t)
    if m:
        return NUMERI[m.group(1)]
    return None

# UTILITA' 
def fmt_data(d: dt.date) -> str:
    return f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]} {d.year}"


def _ora_valida(o: dt.time) -> bool:
    return any(a <= o <= b for a, b in FASCE)


#  DB --
def _nuovo_codice(conn) -> str:
    while True:
        codice = str(random.randint(10000, 99999))
        if not conn.execute("SELECT 1 FROM prenotazioni WHERE codice=?", (codice,)).fetchone():
            return codice


def _conn():
    """Connessione in autocommit (le transazioni si aprono con BEGIN IMMEDIATE)."""
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.execute("""CREATE TABLE IF NOT EXISTS prenotazioni (
        id INTEGER PRIMARY KEY AUTOINCREMENT, codice TEXT, cliente_anonimo TEXT,
        data_prenotazione TEXT, ora TEXT, numero_persone INTEGER, dati_cifrati TEXT)""")
    # migrazione: vecchi DB senza le colonne 'codice' e 'dati_cifrati'
    colonne = [r[1] for r in conn.execute("PRAGMA table_info(prenotazioni)")]
    if "codice" not in colonne:
        conn.execute("ALTER TABLE prenotazioni ADD COLUMN codice TEXT")
    if "dati_cifrati" not in colonne:
        conn.execute("ALTER TABLE prenotazioni ADD COLUMN dati_cifrati TEXT")
    for (pid,) in conn.execute("SELECT id FROM prenotazioni WHERE codice IS NULL").fetchall():
        conn.execute("UPDATE prenotazioni SET codice=? WHERE id=?", (_nuovo_codice(conn), pid))
    return conn


def _posti_occupati(conn, d: dt.date, o: dt.time, escludi: Optional[str] = None) -> int:
    """Coperti già prenotati nella stessa fascia (±1 ora e mezza)."""
    righe = conn.execute(
        "SELECT ora, numero_persone, codice FROM prenotazioni WHERE data_prenotazione=?",
        (d.isoformat(),)).fetchall()
    centro = o.hour * 60 + o.minute
    tot = 0
    for ora, n, codice in righe:
        if codice == escludi:
            continue
        h, m = map(int, ora.split(":"))
        if abs(h * 60 + m - centro) < 90:
            tot += n
    return tot


def _gia_prenotato(conn, d: dt.date, o: dt.time, persone: int,
                   escludi: Optional[str] = None) -> bool:
    """True se esiste già una prenotazione con stesso giorno, stessa ora e stesso
    numero di persone (fatta da qualunque dispositivo). 'escludi' = codice della
    prenotazione che si sta modificando, da non contare."""
    q = ("SELECT 1 FROM prenotazioni "
         "WHERE data_prenotazione=? AND ora=? AND numero_persone=?")
    par = [d.isoformat(), o.strftime("%H:%M"), persone]
    if escludi:
        q += " AND codice<>?"
        par.append(escludi)
    return conn.execute(q + " LIMIT 1", par).fetchone() is not None


def _slot_occupato(stato: dict, escludi: Optional[str] = None) -> bool:
    """Controllo anticipato (al riepilogo), prima di chiedere la conferma."""
    conn = _conn()
    try:
        return _gia_prenotato(conn, stato["data"], stato["ora"], stato["persone"], escludi)
    finally:
        conn.close()


def _prenota(stato: dict) -> Optional[str]:
    """Scrive la prenotazione in modo atomico. Ritorna il codice, None se non c'è posto."""
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")          # blocca altre scritture: niente overbooking
        if _gia_prenotato(conn, stato["data"], stato["ora"], stato["persone"]) \
                or _posti_occupati(conn, stato["data"], stato["ora"]) + stato["persone"] > CAPIENZA:
            conn.execute("ROLLBACK")
            return None
        codice = _nuovo_codice(conn)
        conn.execute(
            "INSERT INTO prenotazioni (codice, cliente_anonimo, data_prenotazione, ora, numero_persone,"
            " dati_cifrati) VALUES (?,?,?,?,?,?)",
            (codice, "anonimo", stato["data"].isoformat(),
             stato["ora"].strftime("%H:%M"), stato["persone"], stato.get("dati_cifrati")))
        conn.execute("COMMIT")
        return codice
    finally:
        conn.close()


def _aggiorna(codice: str, stato: dict) -> bool:
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE") #gestire l'overbooking tra 2 utenti contemporanei , si blocca
        if _gia_prenotato(conn, stato["data"], stato["ora"], stato["persone"], escludi=codice) \
                or _posti_occupati(conn, stato["data"], stato["ora"], escludi=codice) + stato["persone"] > CAPIENZA:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "UPDATE prenotazioni SET data_prenotazione=?, ora=?, numero_persone=? WHERE codice=?",
            (stato["data"].isoformat(), stato["ora"].strftime("%H:%M"), stato["persone"], codice))
        if stato.get("dati_cifrati"):             # il cliente ha dato nuovi dati personali (cifrati)
            conn.execute("UPDATE prenotazioni SET dati_cifrati=? WHERE codice=?",
                         (stato["dati_cifrati"], codice))
        conn.execute("COMMIT")
        return True
    finally:
        conn.close()


def _leggi(codici: list):
    if not codici:
        return []
    conn = _conn()
    try:
        return conn.execute(
            f"SELECT codice, data_prenotazione, ora, numero_persone FROM prenotazioni "
            f"WHERE codice IN ({','.join('?' * len(codici))}) ORDER BY data_prenotazione, ora",
            codici).fetchall()
    finally:
        conn.close()


def _elimina(codice: str) -> bool:
    conn = _conn()
    try:
        return conn.execute("DELETE FROM prenotazioni WHERE codice=?", (codice,)).rowcount > 0
    finally:
        conn.close()


def descrivi(riga) -> str:
    codice, data_iso, ora, n = riga
    return (f"Codice {codice}: {n} {'persona' if n == 1 else 'persone'}, "
            f"{fmt_data(dt.date.fromisoformat(data_iso))} alle {ora}")


def prenotazioni_sessione(stato: dict) -> list:
    """Per il contesto dell'LLM: prenotazioni di questa sessione, come testo."""
    return [descrivi(r) for r in _leggi(stato.get("fatte", []))]


def _reset(stato: dict):
    fatte = stato.get("fatte", [])
    stato.update(nuovo_stato())
    stato["fatte"] = fatte


def _in_corso(stato: dict) -> bool:
    return stato["in_attesa_conferma"] or stato["modifica"] is not None or \
        any(stato[k] is not None for k in ("persone", "data", "ora"))


# ------------------------------------------------------- GDPR: DATI SENSIBILI --
MSG_GDPR = ("Mi dispiace, non posso fornire dati sensibili (nome, email, numero di telefono) "
            "a causa del GDPR. Posso invece aiutarti con la prenotazione: dimmi il codice di "
            "5 cifre per consultarla, modificarla o annullarla.")

# parole che indicano un dato personale
_RE_BERSAGLIO = re.compile(
    r"\b(dati|informazion\w*|e-?mail|mail|posta elettronica|telefono|telefonic\w*|cellulare|"
    r"numero di telefono|recapit\w*|contatt\w*|nome|cognome|anagrafic\w*)\b")
# riferimento al cliente stesso ("il mio", "ti ho dato", "avete salvato"...)
_RE_PERSONALE = re.compile(
    r"\b(mi[oa]|miei|mie|su di me|mi riguard\w*|mi chiam\w*|ti ho|vi ho|con cui (?:ho|abbiamo)|"
    r"ho (?:dato|lasciato|inserito|fornito|detto|comunicato|registrato|scritto)|"
    r"(?:avete|hai) (?:salvat\w*|registrat\w*|memorizzat\w*|conservat\w*))\b")
# il cliente CHIEDE di vedere/sapere qualcosa
_RE_RICHIESTA = re.compile(
    r"\b(qual\w*|dimm\w*|dammi|dai|mostr\w*|ripet\w*|ricord\w*|riepilog\w*|ricapitol\w*|elenc\w*|"
    r"fornisc\w*|leggi\w*|visualizz\w*|vedere|sapere|controll\w*|verific\w*|cosa)\b")
# richieste che non lasciano dubbi
_RE_ESPLICITA = re.compile(
    r"\b(a che nome|a nome di chi|come mi chiamo|chi ha prenotato|intestat\w*)\b"
    r"|\b(?:che|quale|qual è il)\s+(?:nome|cognome|e-?mail|mail|telefono|numero di telefono)\s+(?:ho|hai|avete)\b")


def _chiede_dati_sensibili(t: str) -> bool:
    """True se il cliente chiede di rivedere nome, email, telefono o altri dati personali suoi.
    (Chiedere il telefono o i contatti DEL RISTORANTE, o il riepilogo della prenotazione,
    resta consentito.)"""
    t = re.sub(r"\[[a-z_]+\]", " ", t)           # via i segnaposto [EMAIL], [TELEFONO], [NOME_CLIENTE]
    if _RE_ESPLICITA.search(t):
        return True
    if not _RE_RICHIESTA.search(t):
        return False
    if re.search(r"\bdati\s+(personal\w*|sensibil\w*|privat\w*|anagrafic\w*)\b", t):
        return True
    if re.search(r"\briepilog\w*", t) and re.search(
            r"\b(nome|cognome|e-?mail|mail|telefono|cellulare|numero di telefono)\b", t):
        return True
    return bool(_RE_BERSAGLIO.search(t) and _RE_PERSONALE.search(t))


def _trova_codice(t: str) -> Optional[str]:
    m = re.search(r"\b(\d{5})\b", t)
    if m:
        return m.group(1)
    if re.search(r"\bcodice\b", t):              # dettato a voce: "4 8 2 1 3"
        m = re.search(r"\b(\d)[\s-]?(\d)[\s-]?(\d)[\s-]?(\d)[\s-]?(\d)\b", t)
        if m:
            return "".join(m.groups())
    return None


# --------------------------- PRENOTAZIONI GIA' FATTE: elenco, modifica, annulla -
def _gestisci_esistenti(t: str, stato: dict, oggi: dt.date) -> Optional[str]:
    """Ritorna None se il messaggio non riguarda prenotazioni già confermate."""
    fatte = stato["fatte"]
    nome = re.search(r"\bprenotazion[ei]\b", t) is not None
    codice = _trova_codice(t)
    vuole_annullare = re.search(r"\b(annull|cancell|elimin|disdic)\w*", t)
    vuole_modificare = re.search(r"\b(modific|cambi|sposta|anticip|posticip)\w*", t)

    # ---- ANNULLARE
    if vuole_annullare and (nome or codice):
        if codice:
            if _elimina(codice):
                if codice in fatte:
                    fatte.remove(codice)
                if stato["modifica"] == codice:
                    _reset(stato)
                return f"Ho annullato la prenotazione con codice {codice}."
            return (f"Non trovo nessuna prenotazione con codice {codice}. "
                    "Controlla il codice che ti ho dato alla conferma.")
        if _in_corso(stato):
            return None                          # sta annullando la richiesta in corso
        if not fatte:
            return "Per annullare una prenotazione mi serve il codice di 5 cifre che ti ho dato alla conferma."
        if len(fatte) == 1:
            cod = fatte.pop()
            _elimina(cod)
            return f"Ho annullato la prenotazione con codice {cod}."
        return "Quale vuoi annullare? Dimmi il codice: " + ", ".join(fatte) + "."

    # ---- MODIFICARE
    if vuole_modificare and (nome or codice):
        if _in_corso(stato) and not codice:
            return None                          # sta cambiando la richiesta in corso
        cod = codice or (fatte[0] if len(fatte) == 1 else None)
        if not cod:
            if len(fatte) > 1:
                return "Quale vuoi modificare? Dimmi il codice: " + ", ".join(fatte) + "."
            return "Per modificare una prenotazione mi serve il codice di 5 cifre che ti ho dato alla conferma."
        righe = _leggi([cod])
        if not righe:
            return f"Non trovo nessuna prenotazione con codice {cod}."
        _, data_iso, ora, n = righe[0]
        stato.update(attiva=True, modifica=cod, in_attesa_conferma=False,
                     persone=n, data=dt.date.fromisoformat(data_iso),
                     ora=dt.time(*map(int, ora.split(":"))))
        senza_codice = re.sub(r"\b\d{5}\b", " ", t)
        d, resto = parse_data(senza_codice, oggi)
        if d is None and parse_ora(resto) is None and parse_persone(resto) is None:
            return (f"{descrivi(righe[0])}. "
                    "Cosa vuoi cambiare: data, orario o numero di persone?")
        return None                              # ci sono già nuovi dati: li applica gestisci()

    # ---- CONSULTARE (per codice, oppure l'elenco di questa sessione)
    chiede_elenco = (nome and re.search(
        r"\b(mie|miei|fatt\w*|già|gia|riepilog\w*|elenc\w*|list\w*|quali|quante|dimmi|"
        r"mostra\w*|vedere|visualizza\w*|controlla\w*|verifica\w*|effettuat\w*)\b", t)) \
        or re.search(r"\briepilog", t)
    if not chiede_elenco:
        return None
    d, resto = parse_data(t, oggi)
    if d or parse_ora(resto) or parse_persone(resto):
        return None                              # dati nuovi: è una richiesta di prenotazione

    if codice:
        righe = _leggi([codice])
        return (descrivi(righe[0]) + "." if righe
                else f"Non trovo nessuna prenotazione con codice {codice}.")
    righe = _leggi(fatte)
    if righe:
        msg = ("Ecco le prenotazioni fatte in questa conversazione:\n\n"
               + "\n\n".join(descrivi(r) + "." for r in righe))
    else:
        msg = ("In questa conversazione non hai ancora confermato nessuna prenotazione. "
               "Per privacy non conservo i nomi dei clienti: se hai un codice di prenotazione, dimmelo.")
    if stato["persone"] or stato["data"] or stato["ora"]:
        parti = []
        if stato["persone"]:
            parti.append(f"{stato['persone']} persone")
        if stato["data"]:
            parti.append(fmt_data(stato["data"]))
        if stato["ora"]:
            parti.append(f"alle {stato['ora'].strftime('%H:%M')}")
        msg += "\n\nHai anche una richiesta in corso, non ancora confermata: " + ", ".join(parti) + "."
    return msg


# ------------------------------------------------------------------- LOGICA --
def gestisci(testo: str, stato: dict, oggi: Optional[dt.date] = None,
             adesso: Optional[dt.datetime] = None) -> Optional[str]:
    """Risposta di prenotazione/modifica/annullamento, oppure None se la domanda
    non riguarda le prenotazioni (allora risponde l'LLM)."""
    if adesso is None:
        adesso = dt.datetime.now() if oggi is None else dt.datetime.combine(oggi, dt.time(0, 0))
    oggi = oggi or adesso.date()
    stato.setdefault("fatte", [])
    stato.setdefault("modifica", None)
    t = _norm(testo)

    if _chiede_dati_sensibili(t):                 # GDPR: mai restituire dati personali
        return MSG_GDPR

    risposta = _gestisci_esistenti(t, stato, oggi)
    if risposta is not None:
        return risposta

    intento = bool(re.search(r"\b(prenot|riserv|tavolo)", t))
    if not stato["attiva"] and not intento:
        return None
    stato["attiva"] = True

    if re.search(r"\b(annull|lascia perdere|non voglio più|cancella)", t):
        _reset(stato)
        return "Va bene, ho annullato la richiesta. Posso aiutarti in altro modo?"

    t_dati = re.sub(r"\b\d{5}\b", " ", t)        
    d, resto = parse_data(t_dati, oggi)
    o = parse_ora(resto)
    p = parse_persone(resto)
    nuovo = False
    if d is not None:
        stato["data"], nuovo = d, True
    if o is not None:
        stato["ora"], nuovo = o, True
    if p is not None:
        stato["persone"], nuovo = p, True
    secco = re.fullmatch(r"\s*(\d{1,2}|" + _NUM_RE + r")\s*[.!]?\s*", t)
    if secco and not nuovo:                      # risposta secca: "4" oppure "20"
        v = secco.group(1)
        n = int(v) if v.isdigit() else NUMERI[v]
        if stato["persone"] is None and 1 <= n <= MAX_PERSONE:
            stato["persone"], nuovo = n, True
        elif stato["ora"] is None and 0 <= n <= 23:
            stato["ora"], nuovo = dt.time(n, 0), True

    # ---- conferma (solo se avevo già chiesto e non ci sono modifiche)
    if stato["in_attesa_conferma"] and not nuovo:
        if re.search(r"\b(conferm\w*|sì|si|ok|va bene|esatto|certo|perfetto|procedi|d'accordo)\b", t) \
                and not re.search(r"\bno\b", t):
            if stato["modifica"]:
                cod = stato["modifica"]
                if not _aggiorna(cod, stato):
                    stato["ora"], stato["in_attesa_conferma"] = None, False
                    return ("Mi dispiace, il posto non è disponibile per quel giorno e orario. "
                            "Vuoi provare con un altro orario?")
                msg = (f"Prenotazione modificata! Codice {cod}: {stato['persone']} persone, "
                       f"{fmt_data(stato['data'])} alle {stato['ora'].strftime('%H:%M')}.")
                _reset(stato)
                return msg
            cod = _prenota(stato)
            if cod is None:
                stato["ora"], stato["in_attesa_conferma"] = None, False
                return ("Mi dispiace, il posto non è disponibile per quel giorno e orario. "
                        "Vuoi provare con un altro orario?")
            stato["fatte"].append(cod)
            msg = (f"Prenotazione confermata! Il tuo codice è {cod}: conservalo per modificare o annullare. "
                   f"Tavolo per {stato['persone']} persone, {fmt_data(stato['data'])} "
                   f"alle {stato['ora'].strftime('%H:%M')}. Ti aspettiamo!")
            _reset(stato)
            return msg
        if re.search(r"\bno\b|\bcambi|\bmodific", t):
            stato["in_attesa_conferma"] = False
            return "Nessun problema. Cosa vuoi cambiare: data, orario o numero di persone?"
        return None   # altra domanda (es. menu): risponde l'LLM, lo stato resta
    if not nuovo and not intento:
        return None   # domanda generica durante una prenotazione in corso

    stato["in_attesa_conferma"] = False

    # ---- validazione dei dati raccolti
    if stato["persone"] is not None and not (1 <= stato["persone"] <= MAX_PERSONE):
        n = stato["persone"]
        stato["persone"] = None
        return (f"Per {n} persone ti chiedo di contattare direttamente il ristorante: "
                f"online accettiamo da 1 a {MAX_PERSONE} persone. Per quante persone vuoi prenotare?")
    if stato["data"] is not None:
        if stato["data"] < oggi:
            stato["data"] = None
            return "Quella data è già passata. Per quale giorno vuoi prenotare?"
        if stato["data"].weekday() in GIORNI_CHIUSURA:
            g = GIORNI[stato["data"].weekday()]
            stato["data"] = None
            return f"Il {g} siamo chiusi. Scegli un altro giorno: {ORARI_TESTO}."
    if stato["ora"] is not None and not _ora_valida(stato["ora"]):
        ora = stato["ora"].strftime("%H:%M")
        stato["ora"] = None
        return f"Alle {ora} la cucina è chiusa. Si può prenotare a {ORARI_TESTO}. Che orario preferisci?"
    if stato["data"] == adesso.date() and stato["ora"] is not None:
        if dt.datetime.combine(stato["data"], stato["ora"]) < adesso:
            stato["ora"] = None
            return "Quell'orario è già passato. Che orario preferisci?"

    # ---- dati mancanti
    mancanti = []
    if stato["persone"] is None:
        mancanti.append("per quante persone")
    if stato["data"] is None:
        mancanti.append("per quale giorno")
    if stato["ora"] is None:
        mancanti.append("a che ora")
    if mancanti:
        return "Certo! Mi serve sapere " + ", ".join(mancanti) + "?"

    # ---- disponibilità: stesso giorno, ora e persone già prenotati da un altro cliente
    if _slot_occupato(stato, stato["modifica"]):
        ora = stato["ora"].strftime("%H:%M")
        stato["ora"] = None
        return (f"Mi dispiace, il posto non è disponibile: per {stato['persone']} persone "
                f"il {fmt_data(stato['data'])} alle {ora} risulta già prenotato. "
                "Vuoi scegliere un altro orario o un altro giorno?")

    # ---- riepilogo e richiesta di conferma
    stato["in_attesa_conferma"] = True
    dettaglio = (f"tavolo per {stato['persone']} persone, {fmt_data(stato['data'])} "
                 f"alle {stato['ora'].strftime('%H:%M')}")
    if stato["modifica"]:
        return f"Riepilogo della modifica alla prenotazione {stato['modifica']}: {dettaglio}. Confermi?"
    return f"Riepilogo: {dettaglio}. Confermi la prenotazione?"
