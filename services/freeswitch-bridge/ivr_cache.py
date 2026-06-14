"""
IVR Response Cache — Phase 3 of the sub-500ms latency plan.

Pre-generates TTS WAVs for all deterministic Symphony IVR phrases at startup.
The conversation has 7 predictable steps; by fuzzy-matching the previous LLM
response against the phrase list, we can predict (and pre-serve) the next
response without calling LLM or TTS — saving ~1200ms per cache-hit turn.

Cache hit rate: ~80% (steps 1-6 are fully deterministic).
Cache miss: fallback to LLM+TTS as usual.
"""
import difflib
import logging
import os

log = logging.getLogger("ivr_cache")

# ── IVR phrase definitions ────────────────────────────────────────────────────
# Keys map to lang_key used in bridge.py LANGUAGES dict ("1"=hi, "2"=en, "3"=gu)

IVR_PHRASES: dict[str, dict[str, str]] = {
    "mobile": {
        "1": "Aapka 10 digit mobile number bataiye.",
        "2": "Please tell me your 10 digit mobile number.",
        "3": "Aapno 10 digit mobile number aapvo.",
    },
    "name": {
        "1": "Aapka naam kya hai?",
        "2": "May I know your name please?",
        "3": "Aapnu naam shu chhe?",
    },
    "pincode": {
        "1": "Aapka pincode kya hai?",
        "2": "What is your pin code?",
        "3": "Aapno pincode shu chhe?",
    },
    "address": {
        "1": "Aapka poora pata bataiye.",
        "2": "Please tell me your full address.",
        "3": "Aapnu pooru address aapvo.",
    },
    "model": {
        "1": "Apne product ka model number bataiye.",
        "2": "Please tell me your product model number.",
        "3": "Aapna product nu model number aapvo.",
    },
    "purchase_date": {
        "1": "Product kab liya tha? Kharid ki tarikh bataiye.",
        "2": "When did you purchase the product?",
        "3": "Product kyare kharideyu? Purchase date aapvo.",
    },
    "issue": {
        "1": "Kya samasya aa rahi hai? Apni problem detail mein bataiye.",
        "2": "What is the issue you are facing? Please describe the problem.",
        "3": "Shu problem aavi rahi chhe? Aapni samasya batavo.",
    },
    "warranty_paid": {
        "1": "Aapki warranty khatam ho gayi hai. Technician visit ke liye 472 rupaye lagenge. Kya complaint darj karun?",
        "2": "Your warranty has expired. Technician visit charges 472 rupees. Shall I raise a complaint?",
        "3": "Aapni warranty puri thayi chhe. Technician visit nu 472 rupiya lagse. Complaint darj karu?",
    },
    "complaint_done": {
        "1": "Aapki complaint darj ho gayi hai. Jald hi SMS aayega aur technician aapko call karega.",
        "2": "Your complaint has been registered. You will receive an SMS shortly and a technician will call you.",
        "3": "Aapni complaint darj thayi chhe. Tame SMS melasho ane technician call karse.",
    },
    "not_heard": {
        "1": "Kripya dobara bataiye, mujhe sunai nahi diya.",
        "2": "I'm sorry, I could not hear you. Please repeat.",
        "3": "Mane sambhayayu nahi. Pharthi bolo.",
    },
}

# Deterministic step sequence (greeting not included — handled by bridge warm cache)
STEP_SEQUENCE = [
    "mobile", "name", "pincode", "address",
    "model", "purchase_date", "issue",
]

# ── In-memory WAV path cache ──────────────────────────────────────────────────

_wav_cache: dict[tuple[str, str], str] = {}   # (phrase_key, lang_key) → WAV path
_tts_fn = None   # injected by bridge at startup


def init(tts_function, tmp_dir: str):
    """Called by bridge.serve() to wire in the TTS function and temp dir."""
    global _tts_fn, _TMP_DIR
    _tts_fn = tts_function
    _TMP_DIR = tmp_dir


def warm_cache(lang_keys: list[str], lang_configs: dict) -> int:
    """
    Pre-TTS all phrases for given lang_keys.
    lang_configs: {lang_key: {"lang_code": ..., "sarvam_spk": ...}}
    Returns number of successfully generated WAVs.
    """
    if _tts_fn is None:
        log.error("ivr_cache not initialised — call init() first")
        return 0

    count = 0
    for phrase_key, texts in IVR_PHRASES.items():
        for lang_key, text in texts.items():
            if lang_key not in lang_keys:
                continue
            cfg = lang_configs.get(lang_key)
            if not cfg:
                continue
            try:
                wav_bytes = _tts_fn(text, cfg["lang_code"], cfg["sarvam_spk"])
                if wav_bytes:
                    path = os.path.join(_TMP_DIR, f"cache_{phrase_key}_{lang_key}.wav")
                    with open(path, "wb") as f:
                        f.write(wav_bytes)
                    _wav_cache[(phrase_key, lang_key)] = path
                    count += 1
                    log.debug(f"  cached {phrase_key}/{lang_key}")
            except Exception as e:
                log.warning(f"Cache warm failed {phrase_key}/{lang_key}: {e}")

    log.info(f"IVR cache: {count} WAVs ready ({len(IVR_PHRASES)} phrases × {len(lang_keys)} langs)")
    return count


def get_wav(phrase_key: str, lang_key: str) -> str | None:
    """Return cached WAV path, or None if not in cache."""
    return _wav_cache.get((phrase_key, lang_key))


def lookup(response_text: str, lang_key: str, threshold: float = 0.70) -> tuple[str, str] | tuple[None, None]:
    """
    Fuzzy-match response_text against IVR phrases for lang_key.
    Returns (phrase_key, wav_path) on hit, (None, None) on miss.
    Threshold 0.70: allows paraphrasing while avoiding false positives.
    """
    if not response_text:
        return None, None

    best_key = None
    best_score = 0.0
    text_lower = response_text.lower()

    for phrase_key, texts in IVR_PHRASES.items():
        phrase = texts.get(lang_key, "")
        if not phrase:
            continue
        score = difflib.SequenceMatcher(None, text_lower, phrase.lower()).ratio()
        if score > best_score:
            best_score = score
            best_key = phrase_key

    if best_score >= threshold and best_key:
        wav = _wav_cache.get((best_key, lang_key))
        if wav and os.path.exists(wav):
            return best_key, wav

    return None, None


def next_step(phrase_key: str) -> str | None:
    """Return the next deterministic phrase key after phrase_key, or None."""
    try:
        idx = STEP_SEQUENCE.index(phrase_key)
        return STEP_SEQUENCE[idx + 1] if idx + 1 < len(STEP_SEQUENCE) else None
    except ValueError:
        return None
