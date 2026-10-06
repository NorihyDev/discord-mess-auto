"""Check-in quotidien via la fenêtre Discord du navigateur."""

import argparse
import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
import logging
from pathlib import Path
import sys
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from playwright.async_api import async_playwright, Error as BrowserError

from schedule import DailyState, load_config, window, write_json, project_lock

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("check-in")
COMPOSER = 'main [role="textbox"][contenteditable="true"][data-slate-editor="true"]'
MESSAGE_ROWS = 'li[id^="chat-messages-"]'
SNAPSHOT_SCRIPT = """function snapshot({selector, url, rows, prepare = false}) {
    if (location.href.replace(/\\/$/, '') !== url) return {status: 'wrong_channel'};
    const editors = document.querySelectorAll(selector);
    if (editors.length !== 1) return {status: 'waiting', recoverable: !document.querySelector('main')};
    const editor = editors[0];
    const style = getComputedStyle(editor);
    if (!editor.isContentEditable || editor.closest('[aria-disabled="true"], [inert]') ||
        editor.matches(':disabled') || style.visibility === 'hidden' ||
        style.visibility === 'collapse' || editor.getClientRects().length === 0)
        return {status: 'waiting', recoverable: false};
    const result = {status: 'ready', text: editor.innerText,
        focused: document.activeElement === editor,
        ids: rows ? Array.from(document.querySelectorAll(rows), row => row.id) : []};
    if (!prepare || result.text.replace(/[\\uFEFF\\u200B]/g, '').trim()) return result;
    // Prepare native input in the same browser turn as opening detection.
    // Never select a real draft; focus handlers may also change the page.
    editor.focus({preventScroll: true});
    const focused = snapshot({selector, url, rows: null});
    if (focused.status !== 'ready') return focused;
    if (focused.text.replace(/[\\uFEFF\\u200B]/g, '').trim()) return focused;
    if (!focused.focused || document.querySelector(selector) !== editor)
        return {...focused, preparation_failed: true};
    const selection = getSelection();
    if (!selection) return {...focused, preparation_failed: true};
    selection.selectAllChildren(editor);
    // Native keyboard input targets the focused element. Cancel insertion if
    // focus, channel or the empty draft changed during the Python round trip.
    const guard = event => {
        const current = snapshot({selector, url, rows: null});
        if (event.target !== editor || current.status !== 'ready' || !current.focused ||
            current.text.replace(/[\\uFEFF\\u200B]/g, '').trim() ||
            !editor.contains(selection.anchorNode) || !editor.contains(selection.focusNode))
            event.preventDefault();
    };
    document.addEventListener('beforeinput', guard, {capture: true, once: true});
    result.focused = true;
    result.input_prepared = true;
    return result;
}"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def configure() -> None:
    print("Check-in depuis ton compte, dans une fenêtre Discord dédiée.")
    print("Discord interdit cette automatisation et peut sanctionner le compte.")
    print("Clic droit sur le salon Discord → Copier le lien.")
    config = {
        "channel_url": input("Lien du salon : ").strip(),
        "message": input("Message exact du check-in : "),
        "opening_time": input("Heure d'ouverture à Madagascar [20:00] : ").strip() or "20:00",
        "timezone": "Indian/Antananarivo",
        "minutes_before": float(input("Commencer X minutes avant [5] : ") or 5),
        "minutes_after": float(input("Attendre X minutes après (retard du owner) [15] : ") or 15),
        "check_interval_seconds": float(input("Contrôle de secours toutes les X secondes [2] : ") or 2),
    }
    temporary = ROOT / "config.setup.json"
    try:
        write_json(temporary, config)
        load_config(temporary)
    finally:
        temporary.unlink(missing_ok=True)
    write_json(ROOT / "config.json", config)
    print("Enregistré. Lance connexion.cmd, puis demarrer.cmd.")


async def open_browser(playwright, headless: bool = False):
    return await playwright.chromium.launch_persistent_context(
        str(ROOT / "browser-profile"), headless=headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-background-timer-throttling", "--disable-renderer-backgrounding",
              "--disable-backgrounding-occluded-windows"],
    )


async def composer_snapshot(page, channel_url: str, include_messages=False, *, prepare_input=False) -> dict:
    """Vérifie le salon, l'éditeur et le brouillon en un seul aller-retour."""
    return await page.evaluate(
        SNAPSHOT_SCRIPT,
        {"selector": COMPOSER, "url": channel_url, "rows": MESSAGE_ROWS if include_messages else None,
         "prepare": prepare_input},
    )


async def wait_for_composer(page, channel_url: str, timeout_seconds: float, *, with_snapshot=False,
                            prepare_input=False):
    """Réveille la surveillance dès qu'une mutation rend l'éditeur utilisable."""
    result = await page.evaluate(
        """({selector, url, timeout, rows, prepare}) => new Promise(resolve => {
            let finished = false;
            let observer;
            let timer;
            let fallback;
            const finish = value => {
                if (finished) return;
                finished = true;
                observer?.disconnect();
                clearTimeout(timer);
                clearInterval(fallback);
                resolve(value);
            };
            const check = () => {
                const snapshot = (__SNAPSHOT__)({selector, url, rows, prepare});
                if (snapshot.status === 'ready') finish(snapshot);
            };
            observer = new MutationObserver(check);
            observer.observe(document, {subtree: true, childList: true,
                attributes: true, characterData: true});
            timer = setTimeout(() => finish(false), timeout);
            // CSS transitions may make an editor visible without a DOM mutation.
            fallback = setInterval(check, 16);
            check();
        })""".replace("__SNAPSHOT__", SNAPSHOT_SCRIPT),
        {"selector": COMPOSER, "url": channel_url, "timeout": max(1, timeout_seconds * 1000),
         "rows": MESSAGE_ROWS if with_snapshot else None, "prepare": prepare_input},
    )
    return result if with_snapshot else bool(result)


def normalized(text: str) -> str:
    return " ".join(text.replace("\ufeff", "").replace("\u200b", "").split())


async def send_checkin(page, config: dict, state: DailyState, day: str, preview=False, *, snapshot=None) -> str:
    preparation_started = perf_counter()
    if snapshot is None:
        snapshot = await composer_snapshot(
            page, config["channel_url"], include_messages=not preview,
            prepare_input=not preview and not state.blocked(config["channel_url"], day)
            and "\n" not in config["message"],
        )
    if snapshot["status"] != "ready":
        return "waiting"
    if preview:
        LOG.info("Le salon semble ouvert. Mode vérification : aucun message envoyé.")
        return "preview"
    if state.blocked(config["channel_url"], day):
        return "already_attempted"
    if normalized(snapshot["text"]):
        LOG.error("Un brouillon est déjà présent. Envoi arrêté pour ne pas l'écraser.")
        return "draft"
    if snapshot.get("preparation_failed"):
        LOG.error("Impossible de préparer le focus. Aucun envoi.")
        return "prepare_failed"
    previous_ids = set(snapshot["ids"])
    editor = page.locator(COMPOSER)
    try:
        lines = config["message"].split("\n")
        if snapshot.get("input_prepared"):
            # Native input, without another locator/focus/select round trip.
            # The observer selected only the empty editor (including Slate markers).
            await page.keyboard.insert_text(lines[0])
        else:
            await editor.fill(lines[0], timeout=3000)
        for line in lines[1:]:
            # Conserve les sauts de ligne natifs de l'éditeur Discord.
            await editor.press("Shift+Enter", timeout=3000)
            if line:
                await page.keyboard.insert_text(line)
    except BrowserError:
        LOG.error("Impossible de préparer le message. Envoi arrêté ; vérifie le brouillon.")
        return "prepare_failed"
    prepared = await composer_snapshot(page, config["channel_url"])
    if prepared["status"] == "wrong_channel":
        LOG.error("Le salon a changé pendant la préparation. Aucun appui sur Entrée.")
        return "wrong_channel"
    if (prepared["status"] != "ready" or not prepared["focused"] or
            normalized(prepared["text"]) != normalized(config["message"])):
        LOG.error("Le brouillon ne correspond pas au message prévu. Aucun envoi.")
        return "prepare_failed"
    # Réserve avant Entrée : même après un crash, pas de nouvel envoi aujourd'hui.
    state.record(config["channel_url"], day, "reserved")
    try:
        # fill has focused the editor, and the snapshot above verifies focus.
        await page.keyboard.press("Enter")
        enter_completed = perf_counter()
        preparation_ms = (enter_completed - preparation_started) * 1000
        LOG.info("Envoi déclenché : %.1f ms (%.3f s) de préparation et appui sur Entrée.", preparation_ms, preparation_ms / 1000)
        confirmed = await wait_for_confirmation(page, config, previous_ids)
        if confirmed:
            confirmation_observed = perf_counter()
            total_ms = (confirmation_observed - preparation_started) * 1000
            confirmation_ms = (confirmation_observed - enter_completed) * 1000
            state.record(config["channel_url"], day, "confirmed")
            LOG.info(
                "Envoi confirmé dans le salon : %.1f ms (%.3f s) au total ; "
                "préparation + Entrée : %.1f ms ; confirmation observée après Entrée : %.1f ms.",
                total_ms, total_ms / 1000, preparation_ms, confirmation_ms,
            )
            return "confirmed"
    except BrowserError:
        pass
    LOG.error("Envoi non confirmé. Vérifie Discord ; aucun nouvel essai automatique aujourd'hui.")
    return "uncertain"


async def wait_for_confirmation(page, config, previous_ids, timeout_seconds=10) -> bool:
    """Observe new matching message rows without adding a 500 ms polling delay."""
    return await page.evaluate(
        """({rows, url, message, previous, timeout}) => new Promise(resolve => {
            const ids = new Set(previous);
            const normalize = text => text.replace(/[\\uFEFF\\u200B]/g, '').trim().replace(/\\s+/g, ' ');
            let observer, timer;
            const finish = result => {observer.disconnect(); clearTimeout(timer); resolve(result);};
            const check = () => {
                if (location.href.replace(/\\/$/, '') !== url) return finish(false);
                for (const row of document.querySelectorAll(rows)) {
                    if (ids.has(row.id)) continue;
                    const content = row.querySelectorAll('[id^="message-content-"]');
                    if (content.length === 1 && normalize(content[0].innerText) === message)
                        return finish(true);
                }
            };
            observer = new MutationObserver(check);
            observer.observe(document, {subtree: true, childList: true, characterData: true});
            timer = setTimeout(() => finish(false), timeout);
            check();
        })""",
        {"rows": MESSAGE_ROWS, "url": config["channel_url"], "message": normalized(config["message"]),
         "previous": list(previous_ids), "timeout": max(1, timeout_seconds * 1000)},
    )


async def login() -> None:
    async with async_playwright() as playwright:
        context = await open_browser(playwright)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://discord.com/login", wait_until="domcontentloaded")
            print("Connecte-toi dans le navigateur (QR code ou identifiants + 2FA).")
            print("Le programme ne te demande aucun mot de passe ni token.")
            await asyncio.to_thread(input, "Une fois connecté, appuie sur Entrée ici pour fermer le navigateur : ")
        finally:
            await context.close()


async def watch_window(playwright, config, state, day, end, preview=False) -> str:
    context = await open_browser(playwright)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(3000)
        try:
            await page.goto(config["channel_url"], wait_until="domcontentloaded", timeout=30000)
        except BrowserError:
            LOG.warning("Premier chargement impossible. La surveillance continue dans la fenêtre prévue.")
        last_status = None
        last_reload = now()
        # Load Playwright's selector helpers before the channel opens.
        await page.locator(COMPOSER).count()
        ready_snapshot = None
        while now() < end:
            if page.is_closed():
                LOG.warning("Navigateur fermé. Surveillance interrompue pour cette fenêtre.")
                return "closed"
            try:
                if page.url.rstrip("/") != config["channel_url"]:
                    if "/login" in page.url:
                        status = "login"
                    else:
                        await page.goto(config["channel_url"], wait_until="domcontentloaded", timeout=30000)
                        status = "navigation"
                else:
                    status = await send_checkin(page, config, state, day, preview, snapshot=ready_snapshot)
                    ready_snapshot = None
                    # Récupère aussi une page laissée vide par une coupure réseau.
                    if status == "waiting" and (now() - last_reload).total_seconds() >= 60:
                        last_reload = now()
                        health = await composer_snapshot(page, config["channel_url"])
                        if health.get("recoverable"):
                            await page.reload(wait_until="domcontentloaded", timeout=15000)
                if status not in ("waiting", "login", "navigation"):
                    return status
                if status != last_status:
                    LOG.info("Connexion nécessaire dans le navigateur." if status == "login" else "En attente de l'ouverture du salon…")
                    last_status = status
                if status == "waiting":
                    remaining = (end - now()).total_seconds()
                    if remaining > 0:
                        # Délai de secours, sans pause imposée à l'ouverture de l'éditeur.
                        ready_snapshot = await wait_for_composer(
                            page, config["channel_url"], min(config["check_interval_seconds"], remaining),
                            with_snapshot=True,
                            prepare_input=not preview and "\n" not in config["message"],
                        ) or None
                    continue
            except BrowserError as exc:
                if state.blocked(config["channel_url"], day):
                    LOG.error("Navigateur indisponible après réservation. Vérifie l'envoi manuellement.")
                    return "uncertain"
                LOG.warning("Discord indisponible (%s). Nouvelle vérification prochainement.", type(exc).__name__)
            await asyncio.sleep(min(config["check_interval_seconds"], max(0, (end - now()).total_seconds())))
        LOG.warning("Fin de la fenêtre. Aucun envoi effectué ; prochain essai demain.")
        return "expired"
    finally:
        await context.close()


async def run(config, immediately=False, preview=False) -> int:
    state = DailyState(ROOT / "state.json")
    last_announcement = None
    processed = set()
    async with async_playwright() as playwright:
        while True:
            current = now()
            start, end, day = window(config, current)
            if immediately:
                day = current.astimezone(ZoneInfo(config["timezone"])).date().isoformat()
                start, end = current, current + timedelta(minutes=config["minutes_after"])
            if not preview and state.blocked(config["channel_url"], day):
                if immediately:
                    LOG.info("Une tentative existe déjà aujourd'hui. Vérifie Discord et state.json.")
                    return 0
                processed.add(day)
            if start <= current < end and day not in processed:
                LOG.info("Surveillance du check-in %s jusqu'à %s Madagascar.", day, end.astimezone(ZoneInfo(config["timezone"])).strftime("%H:%M"))
                try:
                    result = await watch_window(playwright, config, state, day, end, preview)
                except BrowserError as exc:
                    LOG.error("Impossible d'ouvrir Discord (%s). Vérifie connexion.cmd et le navigateur.", type(exc).__name__)
                    return 1
                processed.add(day)
                if immediately or preview:
                    return 0 if result in ("confirmed", "preview", "already_attempted") else 1
            if last_announcement != day:
                swiss = start.astimezone(ZoneInfo("Europe/Zurich"))
                LOG.info("Fenêtre : %s–%s Madagascar ; début %s en Suisse (%s).", start.strftime("%H:%M"), end.strftime("%H:%M"), swiss.strftime("%H:%M"), day)
                last_announcement = day
            # Processus actif quotidiennement, sans navigateur hors fenêtre.
            wait = max(1, min(30, (start - now()).total_seconds())) if day not in processed else 30
            await asyncio.sleep(wait)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="Configurer le salon et le message")
    parser.add_argument("--login", action="store_true", help="Se connecter manuellement dans le navigateur dédié")
    parser.add_argument("--check", action="store_true", help="Valider la configuration sans navigateur ni envoi")
    parser.add_argument("--now", action="store_true", help="Surveiller maintenant plutôt qu'attendre l'horaire")
    parser.add_argument("--preview", action="store_true", help="Détecter l'ouverture sans taper ni envoyer de message")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "check-in.log", encoding="utf-8")],
    )
    try:
        with nullcontext() if args.check else project_lock(ROOT / ".instance.lock"):
            if args.setup:
                configure()
                return 0
            if args.login:
                asyncio.run(login())
                return 0
            config = load_config(ROOT / "config.json")
            if args.check:
                print("Configuration valide. Aucun navigateur ouvert, aucun envoi.")
                return 0
            return asyncio.run(run(config, args.now, args.preview))
    except FileNotFoundError:
        LOG.error("Configuration absente. Lance configurer.cmd en premier.")
        return 1
    except (ValueError, OSError, TypeError, ZoneInfoNotFoundError) as exc:
        LOG.error("Configuration/état invalide : %s", exc)
        return 1
    except BrowserError as exc:
        LOG.error("Erreur navigateur (%s). Lance installer.cmd ; ferme les autres instances de l'outil.", type(exc).__name__)
        return 1
    except KeyboardInterrupt:
        LOG.info("Arrêt demandé.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
