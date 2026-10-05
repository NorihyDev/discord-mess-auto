from pathlib import Path
from time import perf_counter
import tempfile
import unittest

from playwright.async_api import async_playwright

from main import send_checkin, wait_for_composer, wait_for_confirmation
from schedule import DailyState, load_config

ROOT = Path(__file__).resolve().parents[1]

HTML = '''<!doctype html><meta charset="utf-8">
<style>[id^="message-content-"] {white-space: pre-wrap;}</style><main>
<input role="textbox" placeholder="Recherche (ne pas utiliser)">
<ul id="messages"><li id="chat-messages-old"><div id="message-content-old">ancien message</div></li></ul>
<div role="textbox" contenteditable="false" data-slate-editor="true" aria-disabled="true"></div>
</main><script>
window.enterCount = 0;
const editor = document.querySelector('div[role=textbox]');
editor.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault(); window.enterCount++; window.sentAt = performance.now();
    const row = document.createElement('li'); row.id = 'chat-messages-new-' + window.enterCount;
    const content = document.createElement('div'); content.id = 'message-content-new-' + window.enterCount;
    content.textContent = editor.innerText;
    row.append(content); document.querySelector('#messages').append(row); editor.textContent = '';
  }
});
window.openChannel = () => {editor.contentEditable = 'true'; editor.setAttribute('aria-disabled', 'false');};
</script>'''


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = DailyState(Path(self.folder.name) / "state.json")
        self.config = load_config(ROOT / "config.example.json")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        # Toutes les requêtes sont interceptées : aucun trafic vers Discord.
        await self.page.route("**/*", lambda route: route.fulfill(body=HTML, content_type="text/html"))
        await self.page.goto(self.config["channel_url"])

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.folder.cleanup()

    async def test_closed_then_open_sends_exact_emoji_once(self):
        result = await send_checkin(self.page, self.config, self.state, "2026-10-05")
        self.assertEqual(result, "waiting")
        self.assertFalse(self.state.path.exists())
        await self.page.evaluate("openChannel()")
        # Slate représente parfois un éditeur vide par un caractère invisible.
        await self.page.locator('div[role=textbox]').evaluate("e => e.textContent = '\\uFEFF'")
        result = await send_checkin(self.page, self.config, self.state, "2026-10-05")
        self.assertEqual(result, "confirmed")
        self.assertEqual(await self.page.locator('#message-content-new-1').inner_text(), self.config["message"])
        restored = DailyState(self.state.path)
        self.assertEqual(await send_checkin(self.page, self.config, restored, "2026-10-05"), "already_attempted")
        self.assertEqual(await self.page.evaluate("enterCount"), 1)

    async def test_preview_does_not_type_or_send(self):
        await self.page.evaluate("openChannel()")
        result = await send_checkin(self.page, self.config, self.state, "2026-10-05", preview=True)
        self.assertEqual(result, "preview")
        self.assertEqual(await self.page.evaluate("enterCount"), 0)
        self.assertFalse(self.state.path.exists())
        self.assertEqual(await self.page.locator('div[role=textbox]').inner_text(), "")

    async def test_draft_preserved(self):
        await self.page.evaluate("openChannel()")
        await self.page.locator('div[role=textbox]').fill("Mon brouillon")
        result = await send_checkin(self.page, self.config, self.state, "2026-10-05")
        self.assertEqual(result, "draft")
        self.assertEqual(await self.page.locator('div[role=textbox]').inner_text(), "Mon brouillon")
        self.assertEqual(await self.page.evaluate("enterCount"), 0)

    async def test_wrong_channel_does_not_send(self):
        await self.page.goto("https://discord.com/channels/1/2")
        await self.page.evaluate("openChannel()")
        self.assertEqual(await send_checkin(self.page, self.config, self.state, "2026-10-05"), "waiting")
        self.assertEqual(await self.page.evaluate("enterCount"), 0)

    async def test_opening_wakes_before_fallback_interval(self):
        await self.page.evaluate("setTimeout(() => {openChannel(); window.openedAt = performance.now();}, 100)")
        self.assertTrue(await wait_for_composer(self.page, self.config["channel_url"], 2))
        detection_ms = await self.page.evaluate("performance.now() - openedAt")
        # Une attente fixe de 2 secondes échouerait ; marge pour les machines lentes.
        self.assertLess(detection_ms, 1000)
        started = perf_counter()
        self.assertEqual(await send_checkin(self.page, self.config, self.state, "2026-10-05"), "confirmed")
        preparation_ms = (perf_counter() - started) * 1000
        opening_to_send_ms = await self.page.evaluate("sentAt - openedAt")
        self.assertLess(opening_to_send_ms, 1000)
        print(f"\nFaux salon local : réveil {detection_ms:.1f} ms ; préparation + confirmation {preparation_ms:.1f} ms ; ouverture -> Entrée {opening_to_send_ms:.1f} ms.")

    async def test_opening_snapshot_fast_path(self):
        await self.page.locator('div[role=textbox]').count()
        await self.page.evaluate("setTimeout(() => {openChannel(); window.openedAt = performance.now();}, 100)")
        snapshot = await wait_for_composer(self.page, self.config["channel_url"], 2, with_snapshot=True)
        self.assertEqual(snapshot["status"], "ready")
        self.assertIn("chat-messages-old", snapshot["ids"])
        self.assertEqual(await send_checkin(self.page, self.config, self.state, "2026-10-05", snapshot=snapshot), "confirmed")
        delay = await self.page.evaluate("sentAt - openedAt")
        self.assertLess(delay, 1000)
        print(f"\nOptimized local fixture: opening -> Enter {delay:.1f} ms.")

    async def test_confirmation_observes_delayed_message(self):
        await self.page.evaluate("""setTimeout(() => {
            const row = document.createElement('li'); row.id = 'chat-messages-delayed';
            row.innerHTML = '<div id="message-content-delayed">Check-in ✅</div>';
            document.querySelector('#messages').append(row);
            window.confirmedAt = performance.now();
        }, 100)""")
        self.assertTrue(await wait_for_confirmation(self.page, self.config, {"chat-messages-old"}, 2))
        self.assertLess(await self.page.evaluate("performance.now() - confirmedAt"), 400)

    async def test_confirmation_ignores_existing_matching_message(self):
        await self.page.locator('#message-content-old').evaluate("(e, text) => e.textContent = text", self.config["message"])
        self.assertFalse(await wait_for_confirmation(self.page, self.config, {"chat-messages-old"}, 0.05))

    async def test_focus_change_during_preparation_does_not_send(self):
        await self.page.evaluate("""() => {
            openChannel();
            editor.addEventListener('input', () => document.querySelector('input').focus(), {once: true});
        }""")
        self.assertEqual(await send_checkin(self.page, self.config, self.state, "2026-10-05"), "prepare_failed")
        self.assertEqual(await self.page.evaluate("enterCount"), 0)
        self.assertFalse(self.state.path.exists())

    async def test_reclosing_during_preparation_does_not_send(self):
        await self.page.evaluate("""() => {
            openChannel();
            editor.addEventListener('input', () => {
                editor.contentEditable = 'false';
                editor.setAttribute('aria-disabled', 'true');
            }, {once: true});
        }""")
        self.assertEqual(await send_checkin(self.page, self.config, self.state, "2026-10-05"), "prepare_failed")
        self.assertEqual(await self.page.evaluate("enterCount"), 0)
        self.assertFalse(self.state.path.exists())

    async def test_channel_change_during_preparation_does_not_send(self):
        await self.page.evaluate("""() => {
            openChannel();
            editor.addEventListener('input', () => {
                history.pushState({}, '', '/channels/1/2');
            }, {once: true});
        }""")
        self.assertEqual(await send_checkin(self.page, self.config, self.state, "2026-10-05"), "wrong_channel")
        self.assertEqual(await self.page.evaluate("enterCount"), 0)
        self.assertFalse(self.state.path.exists())

    async def test_closed_observer_times_out_without_sending(self):
        self.assertFalse(await wait_for_composer(self.page, self.config["channel_url"], 0.05))
        self.assertEqual(await self.page.evaluate("enterCount"), 0)
        self.assertFalse(self.state.path.exists())

    async def test_multiline_insert_preserves_text(self):
        await self.page.evaluate("openChannel()")
        self.config["message"] = "FZN ✅️\nDeuxième ligne"
        self.assertEqual(await send_checkin(self.page, self.config, self.state, "2026-10-05"), "confirmed")
        self.assertEqual(await self.page.locator('#message-content-new-1').inner_text(), self.config["message"])


if __name__ == "__main__":
    unittest.main()
