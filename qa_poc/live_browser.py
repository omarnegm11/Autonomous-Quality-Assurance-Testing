"""Visible presentation of real Playwright interactions; no prerecorded actions."""
import base64
import json
import time

OVERLAY = r"""(() => {
  const install = () => {
    if (document.getElementById('__qa_live_cursor')) return;
    const cursor = document.createElement('div'); cursor.id = '__qa_live_cursor';
    cursor.setAttribute('aria-hidden', 'true');
    cursor.style.cssText = 'position:fixed;left:12px;top:12px;width:18px;height:18px;border:3px solid white;border-radius:50%;background:#f43f5e;box-shadow:0 0 0 2px #be123c,0 2px 10px #0005;pointer-events:none;z-index:2147483647;transform:translate(-50%,-50%);';
    const status = document.createElement('div'); status.id = '__qa_live_status';
    status.setAttribute('aria-hidden', 'true'); status.textContent = 'QA agent: ready';
    status.style.cssText = 'position:fixed;bottom:16px;right:16px;max-width:650px;padding:12px 18px;background:#14213d;color:white;border:1px solid #64748b;border-radius:10px;font:14px system-ui;white-space:pre-line;pointer-events:none;z-index:2147483646;box-shadow:0 3px 15px #0003;';
    document.documentElement.append(cursor, status);
    addEventListener('mousemove', e => {cursor.style.left=e.clientX+'px';cursor.style.top=e.clientY+'px';}, true);
    addEventListener('mousedown', () => {cursor.style.background='#fbbf24';}, true);
    addEventListener('mouseup', () => {cursor.style.background='#f43f5e';}, true);
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', install, {once:true}); else install();
})();"""


class BrowserPresenter:
    def __init__(self, cfg):
        self.stream_dir = getattr(cfg, "live_stream_dir", None)
        self.visible = not cfg.browser_headless or self.stream_dir is not None
        self.typing_delay = cfg.browser_type_delay_ms
        self.session = None
        self.streaming_page = None
        self.last_frame = 0

    def stream(self, page):
        if not self.stream_dir or self.streaming_page is page:
            return
        if self.session:
            try:
                self.session.detach()
            except Exception:
                pass
        self.stream_dir.mkdir(parents=True, exist_ok=True)
        self.session = page.context.new_cdp_session(page)
        self.streaming_page = page
        session = self.session

        def frame(event):
            try:
                session.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
                stamp = time.time()
                if stamp - self.last_frame < 0.12:
                    return
                self.last_frame = stamp
                temp = self.stream_dir / "frame.tmp"
                temp.write_bytes(base64.b64decode(event["data"]))
                temp.replace(self.stream_dir / "frame.jpg")
                (self.stream_dir / "frame.json").write_text(json.dumps({"captured_at": stamp}), encoding="utf-8")
            except Exception:
                pass

        session.on("Page.screencastFrame", frame)
        session.send("Page.startScreencast", {"format": "jpeg", "quality": 70,
                     "maxWidth": 1440, "maxHeight": 900, "everyNthFrame": 2})

    def attach(self, context):
        if self.visible:
            context.add_init_script(OVERLAY)

    def focus(self, page, status=""):
        self.stream(page)
        if self.visible:
            page.bring_to_front()
            if status:
                page.evaluate("text => {const e=document.getElementById('__qa_live_status'); if(e)e.textContent=text}", status)

    def _point(self, page, locator):
        self.focus(page)
        locator.wait_for(state="visible")
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if not box:
            raise ValueError("Browser target is not visible")
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y, steps=24)
        return x, y

    def click(self, page, locator):
        if not self.visible:
            locator.click()
            return
        x, y = self._point(page, locator)
        page.mouse.click(x, y)

    def fill(self, page, locator, value):
        if not self.visible:
            locator.fill(value)
            return
        self.click(page, locator)
        locator.fill("")
        locator.press_sequentially(value, delay=self.typing_delay)
